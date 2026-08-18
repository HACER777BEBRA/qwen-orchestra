"""Локальный веб-сервер: Cursor-like UI + оркестр (по умолчанию 127.0.0.1).

Режим --share: слушает 0.0.0.0 (проброшенный порт / LAN). Опционально --token —
простой HTTP Basic (пароль = токен), без мультипользователей.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Iterator

import urllib.error

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from qwen_orchestra.llm import installed_models
from qwen_orchestra.metrics import collect as collect_metrics
from qwen_orchestra.orchestra import MODELS, handle, missing_models, missing_optional_models
from qwen_orchestra import settings as app_settings
from qwen_orchestra.plan import PLAN_CONFIRM_TIMEOUT

import share_config

app_settings.ensure_bootstrapped()

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DEFAULT_PORT = share_config.DEFAULT_LOCAL_PORT
SSE_QUEUE_MAX = 512
MAX_MESSAGE_CHARS = 100_000


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def share_mode() -> bool:
    return _env_truthy("QWEN_SHARE")


def share_token() -> str:
    return os.environ.get("QWEN_SHARE_TOKEN", "").strip()


def listen_port() -> int:
    raw = os.environ.get("QWEN_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    if share_mode():
        cfg = share_config.configured_share_port()
        if cfg is not None:
            return cfg
    return DEFAULT_PORT


def _localhost_hosts(port: int) -> frozenset[str]:
    return frozenset(
        {
            "127.0.0.1",
            "localhost",
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            "[::1]",
            f"[::1]:{port}",
        }
    )


def _localhost_origins(port: int) -> frozenset[str]:
    return frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
            "null",
        }
    )

app = FastAPI(title="Qwen Orchestra Chat", docs_url=None, redoc_url=None)


class ChatMessage(BaseModel):
    role: str
    content: str
    meta: dict[str, Any] | None = None


class ChatSession(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    messages: list[ChatMessage] = Field(default_factory=list)
    generation: int = 0


class CreateChatBody(BaseModel):
    title: str | None = None


class SendMessageBody(BaseModel):
    content: str
    force_tier: str | None = None
    force_model: str | None = None


class PlanActionBody(BaseModel):
    action: str  # approve | reject


class SettingsPutBody(BaseModel):
    models: list[dict[str, Any]] | None = None
    slots: list[dict[str, Any]] | None = None  # compat
    router_model: str | None = None
    planner_model: str | None = None
    selfcheck: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None


class AddModelBody(BaseModel):
    model: str
    label: str | None = None
    router_prompt: str | None = None
    tier: str | None = None
    id: str | None = None
    rank: int | None = None
    provider: str = "ollama"
    ctx_overhead_pct: int | None = None
    max_ctx: int | None = None


# Совместимость со старым именем
AddSlotBody = AddModelBody


class OpenRouterKeyBody(BaseModel):
    api_key: str | None = None
    clear: bool = False


_chats: dict[str, ChatSession] = {}
_lock = threading.Lock()
# Один активный ответ на чат — иначе гонка истории при параллельных POST
_chat_workers: dict[str, threading.Lock] = {}
# chat_id → ожидание кнопки «утвердить план» (не кладём Event в pydantic-чат)
_plan_waits: dict[str, "_PlanWait"] = {}


class _PlanWait:
    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.event = threading.Event()
        self.action: str | None = None


def _now() -> float:
    return time.time()


def _title_from(text: str) -> str:
    t = " ".join(text.strip().split())
    if not t:
        return "New Chat"
    return t[:48] + ("…" if len(t) > 48 else "")


def _chat_summary(chat: ChatSession) -> dict[str, Any]:
    return {
        "id": chat.id,
        "title": chat.title,
        "created_at": chat.created_at,
        "updated_at": chat.updated_at,
        "message_count": len(chat.messages),
    }


def _result_meta(result: Any) -> dict[str, Any]:
    return {
        "tier": result.tier,
        "model": result.model,
        "need_web": result.need_web,
        "need_local_time": getattr(result, "need_local_time", False),
        "need_calc": getattr(result, "need_calc", False),
        "route_reason": result.route_reason,
        "escalated": result.escalated,
        "attempts": result.attempts,
        "checked": result.checked,
        "problems": result.problems,
        "num_ctx": result.num_ctx,
        "used_history": result.used_history,
        "context_reason": result.context_reason,
        "planned": getattr(result, "planned", False),
        "plan_goal": getattr(result, "plan_goal", "") or "",
        "plan_steps": getattr(result, "plan_steps", None) or [],
    }


def _worker_lock(chat_id: str) -> threading.Lock:
    with _lock:
        lock = _chat_workers.get(chat_id)
        if lock is None:
            lock = threading.Lock()
            _chat_workers[chat_id] = lock
        return lock


def _check_share_basic_auth(request: Request) -> Response | None:
    """Опциональный пароль в --share: HTTP Basic (пароль = QWEN_SHARE_TOKEN)."""
    token = share_token()
    if not token:
        return None
    # Лаунчер ждёт /api/ready без пароля
    if request.url.path == "/api/ready":
        return None
    header = (request.headers.get("authorization") or "").strip()
    if header.lower().startswith("basic "):
        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8", errors="replace")
        except (ValueError, UnicodeError):
            raw = ""
        # user:pass — принимаем, если pass == token (user любой) или user == token
        user, _, password = raw.partition(":")
        if secrets.compare_digest(password, token) or secrets.compare_digest(user, token):
            return None
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Qwen Orchestra", charset="UTF-8"'},
        media_type="text/plain",
    )


@app.middleware("http")
async def access_guard(request: Request, call_next):  # noqa: ANN001
    """Локально — только localhost Host/Origin; в --share — Basic Auth (если задан токен)."""
    if share_mode():
        denied = _check_share_basic_auth(request)
        if denied is not None:
            return denied
        return await call_next(request)

    port = listen_port()
    host = (request.headers.get("host") or "").strip().lower()
    if host and host not in _localhost_hosts(port):
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    origin = (request.headers.get("origin") or "").strip().lower()
    if origin and request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        if origin not in _localhost_origins(port):
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    return await call_next(request)


@app.get("/api/ready")
def ready() -> dict[str, bool]:
    """Быстрый ping для лаунчера (без обращения к Ollama)."""
    return {"ok": True}


@app.get("/api/health")
def health() -> dict[str, Any]:
    ollama_ok = False
    models: list[str] = []
    missing: list[str] = []
    missing_optional: list[str] = []
    error: str | None = None
    router_missing = False
    planner_missing = False
    try:
        models = installed_models()
        have = set(models)
        ollama_ok = True
        missing = missing_models(have)
        missing_optional = missing_optional_models(have)
        router_name = app_settings.get_settings().router_model
        if router_name and router_name not in have:
            # точное имя или префикс тега
            router_missing = not any(
                m == router_name or m.startswith(router_name + ":") for m in have
            )
        planner_name = app_settings.get_settings().planner_model
        if planner_name and planner_name not in have:
            planner_missing = not any(
                m == planner_name or m.startswith(planner_name + ":") for m in have
            )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        have = set()
        missing = missing_models(have)
        missing_optional = missing_optional_models(have)
        router_missing = True
        planner_missing = True
    or_status = app_settings.openrouter_status()
    cfg = app_settings.get_settings()
    return {
        # ok = есть ≥1 доступная модель пула (локальная или OpenRouter)
        "ok": not missing,
        "ollama": ollama_ok,
        "models": models,
        "missing": missing,
        "missing_optional": missing_optional,
        "router_model": cfg.router_model,
        "router_missing": router_missing,
        "planner_model": cfg.planner_model,
        "planner_missing": planner_missing,
        "tiers": dict(MODELS),
        "pool": [m.to_dict() for m in cfg.models],
        "slots": [m.to_dict() for m in cfg.models],  # compat
        "providers": {"openrouter": or_status},
        "error": error,
    }


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    """CPU / RAM / GPU / загруженные в Ollama модели (для правой панели)."""
    return collect_metrics()


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return app_settings.public_settings_payload()


@app.put("/api/settings")
def put_settings(body: SettingsPutBody) -> dict[str, Any]:
    payload = body.models if body.models is not None else body.slots
    if payload is None:
        raise HTTPException(status_code=400, detail="Нужен список models")
    try:
        saved = app_settings.update_settings(
            payload,
            router_model=body.router_model,
            planner_model=body.planner_model,
            selfcheck=body.selfcheck,
            plan=body.plan,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return app_settings.public_settings_payload(saved)


@app.post("/api/settings/reset")
def reset_settings() -> dict[str, Any]:
    saved = app_settings.reset_settings()
    return app_settings.public_settings_payload(saved)


@app.post("/api/settings/models")
@app.post("/api/settings/slots")
def add_settings_model(body: AddModelBody) -> dict[str, Any]:
    try:
        saved = app_settings.add_model(
            model=body.model,
            label=body.label,
            router_prompt=body.router_prompt,
            model_id=body.id,
            tier=body.tier,
            rank=body.rank,
            provider=body.provider,
            ctx_overhead_pct=body.ctx_overhead_pct,
            max_ctx=body.max_ctx,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return app_settings.public_settings_payload(saved)


@app.put("/api/settings/providers/openrouter")
def put_openrouter_key(body: OpenRouterKeyBody) -> dict[str, Any]:
    """Сохранить или очистить OpenRouter API key (secrets.json)."""
    if body.clear or not (body.api_key or "").strip():
        if body.clear or body.api_key is not None:
            app_settings.set_openrouter_api_key(None)
        else:
            raise HTTPException(status_code=400, detail="Укажите api_key или clear=true")
    else:
        app_settings.set_openrouter_api_key(body.api_key)
    return app_settings.public_settings_payload()


@app.delete("/api/settings/models/{model_id:path}")
@app.delete("/api/settings/slots/{model_id:path}")
def delete_settings_model(model_id: str) -> dict[str, Any]:
    try:
        saved = app_settings.delete_model(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Модель не найдена: {model_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return app_settings.public_settings_payload(saved)


@app.get("/api/chats")
def list_chats() -> list[dict[str, Any]]:
    with _lock:
        chats = sorted(_chats.values(), key=lambda c: c.updated_at, reverse=True)
        return [_chat_summary(c) for c in chats]


@app.post("/api/chats")
def create_chat(body: CreateChatBody | None = None) -> dict[str, Any]:
    body = body or CreateChatBody()
    chat_id = uuid.uuid4().hex[:12]
    ts = _now()
    chat = ChatSession(
        id=chat_id,
        title=(body.title or "New Chat").strip() or "New Chat",
        created_at=ts,
        updated_at=ts,
    )
    with _lock:
        _chats[chat_id] = chat
    return _chat_summary(chat)


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str) -> dict[str, Any]:
    with _lock:
        chat = _chats.get(chat_id)
        if not chat:
            raise HTTPException(404, "Chat not found")
        return {
            **_chat_summary(chat),
            "messages": [m.model_dump() for m in chat.messages],
        }


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str) -> dict[str, str]:
    wlock = _worker_lock(chat_id)
    if not wlock.acquire(blocking=False):
        raise HTTPException(409, "Дождитесь ответа на предыдущее сообщение в этом чате")
    try:
        with _lock:
            if chat_id not in _chats:
                raise HTTPException(404, "Chat not found")
            del _chats[chat_id]
            _chat_workers.pop(chat_id, None)
            _plan_waits.pop(chat_id, None)
        return {"status": "ok"}
    finally:
        wlock.release()


@app.post("/api/chats/{chat_id}/clear")
def clear_chat(chat_id: str) -> dict[str, Any]:
    wlock = _worker_lock(chat_id)
    if not wlock.acquire(blocking=False):
        raise HTTPException(409, "Дождитесь ответа на предыдущее сообщение в этом чате")
    try:
        with _lock:
            chat = _chats.get(chat_id)
            if not chat:
                raise HTTPException(404, "Chat not found")
            chat.messages.clear()
            chat.title = "New Chat"
            chat.updated_at = _now()
            chat.generation += 1
            return _chat_summary(chat)
    finally:
        wlock.release()


def _sse(event: str, data: dict[str, Any] | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _q_put(q: Queue, item: tuple[str, Any] | None) -> None:
    """Неблокирующая запись: при переполнении вытесняем старые token-события."""
    while True:
        try:
            q.put(item, block=False)
            return
        except Full:
            try:
                q.get_nowait()
            except Empty:
                return


@app.post("/api/chats/{chat_id}/messages")
def send_message(chat_id: str, body: SendMessageBody) -> StreamingResponse:
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "Empty message")
    if len(content) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"Сообщение слишком длинное (>{MAX_MESSAGE_CHARS})")

    wlock = _worker_lock(chat_id)
    if not wlock.acquire(blocking=False):
        raise HTTPException(409, "Дождитесь ответа на предыдущее сообщение в этом чате")

    try:
        with _lock:
            chat = _chats.get(chat_id)
            if not chat:
                wlock.release()
                raise HTTPException(404, "Chat not found")
            history = [
                {"role": m.role, "content": m.content}
                for m in chat.messages
                if m.role in {"user", "assistant"}
            ]
            if chat.title == "New Chat":
                chat.title = _title_from(content)
            chat.messages.append(ChatMessage(role="user", content=content))
            chat.updated_at = _now()
            generation = chat.generation
            wait = _PlanWait(generation)
            _plan_waits[chat_id] = wait
    except HTTPException:
        raise
    except Exception:
        wlock.release()
        raise

    q: Queue[tuple[str, Any] | None] = Queue(maxsize=SSE_QUEUE_MAX)

    def on_token(token: str) -> None:
        _q_put(q, ("token", {"text": token}))

    def on_plan_confirm(_payload: dict[str, Any]) -> str:
        cur = _plan_waits.get(chat_id)
        if cur is None or cur.generation != generation:
            return "reject"
        if not cur.event.wait(timeout=PLAN_CONFIRM_TIMEOUT):
            return "timeout"
        return (cur.action or "reject").strip().lower()

    def on_status(event: str, payload: dict[str, Any]) -> None:
        if event == "route":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "tier": payload.get("tier"),
                        "model": payload.get("model"),
                        "need_web": payload.get("need_web"),
                        "need_local_time": payload.get("need_local_time"),
                        "need_calc": payload.get("need_calc"),
                        "route_reason": payload.get("reason"),
                        "ok": payload.get("ok"),
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "tool":
            _q_put(
                q,
                (
                    "tool",
                    {
                        "name": payload.get("name"),
                        "arguments": payload.get("arguments"),
                        "model": payload.get("model"),
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "worker":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "tier": payload.get("tier"),
                        "model": payload.get("model"),
                        "need_web": payload.get("need_web"),
                        "need_local_time": payload.get("need_local_time"),
                        "need_calc": payload.get("need_calc"),
                        "phase": "worker",
                        "num_ctx": payload.get("num_ctx"),
                        "used_history": payload.get("used_history"),
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "context":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "context",
                        "num_ctx": payload.get("num_ctx"),
                        "used_history": payload.get("used_history"),
                        "context_reason": payload.get("reason"),
                        "history_messages": payload.get("history_messages"),
                        "tier": payload.get("tier"),
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "selfcheck":
            _q_put(
                q,
                (
                    "check",
                    {
                        "ok": payload.get("ok"),
                        "problems": payload.get("problems") or [],
                        "note": payload.get("note") or "",
                        "attempt": payload.get("attempt"),
                        "model": payload.get("model"),
                        "checked": payload.get("checked"),
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "retry":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "retry",
                        "from_model": payload.get("from_model"),
                        "model": payload.get("to_model"),
                        "attempt": payload.get("attempt"),
                        "problems": payload.get("problems") or [],
                        "escalated": True,
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "restore":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "restore",
                        "model": payload.get("model"),
                        "tier": payload.get("tier"),
                        "problems": [],
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "plan":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "plan",
                        "need_plan": payload.get("need_plan"),
                        "reason": payload.get("reason"),
                        "source": payload.get("source"),
                    },
                ),
            )
        elif event == "plan_ready":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "plan_ready",
                        "goal": payload.get("goal"),
                        "planner_model": payload.get("planner_model"),
                        "steps": payload.get("steps") or [],
                        "awaiting": bool(payload.get("awaiting")),
                    },
                ),
            )
        elif event == "plan_step":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "plan_step",
                        "index": payload.get("index"),
                        "total": payload.get("total"),
                        "id": payload.get("id"),
                        "title": payload.get("title"),
                        "model": payload.get("model"),
                        "tier": payload.get("tier"),
                        "block": payload.get("block"),
                        "block_title": payload.get("block_title"),
                        "block_role": payload.get("block_role"),
                    },
                ),
            )
        elif event == "plan_token":
            _q_put(
                q,
                (
                    "token",
                    {
                        "text": payload.get("text") or "",
                        "scope": "step",
                        "id": payload.get("id"),
                    },
                ),
            )
        elif event == "plan_step_done":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "plan_step_done",
                        "id": payload.get("id"),
                        "index": payload.get("index"),
                        "failed": bool(payload.get("failed")),
                        "note": payload.get("note") or "",
                        "model": payload.get("model"),
                        "tier": payload.get("tier"),
                        "text": payload.get("text") or "",
                        "block": payload.get("block"),
                        "block_title": payload.get("block_title"),
                        "block_role": payload.get("block_role"),
                    },
                ),
            )
        elif event == "compose":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "compose",
                        "model": payload.get("model"),
                        "tier": payload.get("tier"),
                    },
                ),
            )
        elif event == "plan_accepted":
            _q_put(q, ("meta", {"phase": "plan_accepted"}))
        elif event == "plan_rejected":
            _q_put(
                q,
                (
                    "meta",
                    {
                        "phase": "plan_rejected",
                        "reason": payload.get("reason") or "reject",
                    },
                ),
            )

    def worker() -> None:
        try:
            result = handle(
                content,
                history,
                force_tier=body.force_tier,
                force_model=body.force_model,
                stream=True,
                verbose=False,
                on_token=on_token,
                on_status=on_status,
                on_plan_confirm=on_plan_confirm,
            )
            meta = _result_meta(result)
            with _lock:
                c = _chats.get(chat_id)
                if c is not None and c.generation == generation:
                    c.messages.append(
                        ChatMessage(
                            role="assistant",
                            content=result.text,
                            meta=meta,
                        )
                    )
                    c.updated_at = _now()
            _q_put(q, ("done", {"text": result.text, **meta}))
        except urllib.error.URLError as exc:
            _q_put(q, ("error", {"message": f"Ollama недоступна: {exc}"}))
        except Exception as exc:  # noqa: BLE001
            _q_put(q, ("error", {"message": str(exc)}))
        finally:
            with _lock:
                cur = _plan_waits.get(chat_id)
                if cur is not None and cur.generation == generation:
                    _plan_waits.pop(chat_id, None)
            wlock.release()
            _q_put(q, None)

    threading.Thread(target=worker, daemon=True).start()

    def event_stream() -> Iterator[str]:
        while True:
            try:
                item = q.get(timeout=600)
            except Empty:
                yield _sse("error", {"message": "Timeout"})
                break
            if item is None:
                break
            event, data = item
            yield _sse(event, data)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chats/{chat_id}/plan")
def decide_chat_plan(chat_id: str, body: PlanActionBody) -> dict[str, Any]:
    """Утвердить или отклонить план, пока SSE ещё ждёт (без worker-lock)."""
    action = (body.action or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise HTTPException(400, "action: approve или reject")
    with _lock:
        if chat_id not in _chats:
            raise HTTPException(404, "Chat not found")
        wait = _plan_waits.get(chat_id)
        if wait is None or wait.event.is_set():
            raise HTTPException(409, "Нет плана, ожидающего утверждения")
        wait.action = action
        wait.event.set()
    return {"status": "ok", "action": action}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def _lan_ipv4() -> list[str]:
    """Локальные IPv4 (кроме loopback) — подсказка для LAN-доступа."""
    found: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                ip = probe.getsockname()[0]
                if ip and not ip.startswith("127."):
                    found.append(ip)
            finally:
                probe.close()
        except OSError:
            pass
    return found


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Qwen Orchestra web chat")
    parser.add_argument(
        "--share",
        action="store_true",
        help="Слушать 0.0.0.0 (LAN / проброшенный порт). Один общий сеанс без аккаунтов.",
    )
    parser.add_argument(
        "--token",
        default="",
        help="Пароль для --share (HTTP Basic). Пусто = без пароля (не рекомендуется в интернет).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Порт. В --share по умолчанию из share.json (иначе 8787).",
    )
    parser.add_argument(
        "--host",
        default="",
        help="Bind-адрес. По умолчанию: 127.0.0.1 или 0.0.0.0 при --share.",
    )
    args = parser.parse_args()

    if args.share:
        os.environ["QWEN_SHARE"] = "1"
    if args.token.strip():
        os.environ["QWEN_SHARE_TOKEN"] = args.token.strip()

    sharing = share_mode()
    if sharing:
        public_ip = share_config.ensure_public_ip(refresh=True)
        port = share_config.resolve_share_port(args.port)
        share_config.remember_share_endpoint(public_ip, port)
        os.environ["QWEN_PORT"] = str(port)
    else:
        if args.port > 0:
            os.environ["QWEN_PORT"] = str(args.port)
        port = listen_port()
        public_ip = None

    host = (args.host.strip() or ("0.0.0.0" if sharing else "127.0.0.1"))

    if sharing:
        guest = share_config.guest_url(public_ip, port)
        print("Режим SHARE: сервер доступен с сети (один общий чат/настройки).")
        print(f"  bind: {host}:{port}")
        print(f"  локально: http://127.0.0.1:{port}")
        if guest:
            print(f"  гостю: {guest}")
        else:
            print("  гостю: задайте public_ip/port в share.json")
        for ip in _lan_ipv4():
            print(f"  в LAN: http://{ip}:{port}")
        print(f"  конфиг: {share_config.share_config_path()}")
        if share_token():
            print("  пароль: задан (--token / QWEN_SHARE_TOKEN), браузер спросит логин/пароль")
            print("          (логин любой, пароль = ваш токен)")
        else:
            print("  ВНИМАНИЕ: пароль не задан — любой, кто дойдёт до порта, увидит чаты")
            print("            и сможет менять настройки/OpenRouter-ключ.")
            print("            Запуск с паролем: python server.py --share --token СЕКРЕТ")
        print()
    else:
        print(f"Локальный режим: http://127.0.0.1:{port}")

    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
