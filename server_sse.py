"""SSE: стрим ответа и утверждение плана."""
from __future__ import annotations

import json
import threading
import urllib.error
from queue import Empty, Full, Queue
from typing import Any, Iterator

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from qwen_orchestra.orchestra import handle
from qwen_orchestra.plan import PLAN_CONFIRM_TIMEOUT
from qwen_orchestra.vision import ImageError, prepare_images

from server import (
    MAX_MESSAGE_CHARS,
    SSE_QUEUE_MAX,
    ChatMessage,
    PlanActionBody,
    SendMessageBody,
    _PlanWait,
    _chats,
    _lock,
    _now,
    _plan_waits,
    _result_meta,
    _title_from,
    _worker_lock,
    app,
)

def _history_content(m: ChatMessage) -> str:
    """Текст реплики для модели; у сообщений с картинками — описание от vision-модели."""
    desc = (m.meta or {}).get("vision_text") if m.role == "user" else None
    if not desc:
        return m.content
    return f"{m.content}\n\n[К сообщению приложено изображение. Описание: {desc}]"


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
    if not content and not body.images:
        raise HTTPException(400, "Empty message")
    if len(content) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"Сообщение слишком длинное (>{MAX_MESSAGE_CHARS})")
    try:
        prepared = prepare_images(
            [i.data for i in body.images], names=[i.name for i in body.images]
        )
    except ImageError as exc:
        raise HTTPException(400, str(exc)) from exc
    images_b64 = [p.b64 for p in prepared]
    user_meta = {"images": [p.info() for p in prepared]} if prepared else None
    shown = content or ("[изображение]" if prepared else "")

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
                {"role": m.role, "content": _history_content(m)}
                for m in chat.messages
                if m.role in {"user", "assistant"}
            ]
            if chat.title == "New Chat":
                chat.title = _title_from(shown)
            user_msg = ChatMessage(role="user", content=shown, meta=user_meta)
            chat.messages.append(user_msg)
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
    _put = _q_put
    _timing: dict[str, Any] = {}

    def _q_put_sse(queue: Queue, item: tuple[str, Any] | None) -> None:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], dict):
            kind, data = item
            extra = {k: _timing[k] for k in ("elapsed_ms", "dt_ms") if k in _timing}
            if extra and kind != "token":
                data = {**extra, **data}
            _put(queue, (kind, data))
        else:
            _put(queue, item)

    def on_token(token: str) -> None:
        _q_put_sse(q, ("token", {"text": token}))

    def on_plan_confirm(_payload: dict[str, Any]) -> str:
        cur = _plan_waits.get(chat_id)
        if cur is None or cur.generation != generation:
            return "reject"
        if not cur.event.wait(timeout=PLAN_CONFIRM_TIMEOUT):
            return "timeout"
        return (cur.action or "reject").strip().lower()

    def on_status(event: str, payload: dict[str, Any]) -> None:
        _timing.clear()
        for key in ("elapsed_ms", "dt_ms"):
            if key in payload:
                _timing[key] = payload[key]
        if event == "route":
            _q_put_sse(
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
        elif event == "vision":
            _q_put_sse(
                q,
                (
                    "meta",
                    {
                        "phase": "vision",
                        "vision_model": payload.get("model"),
                        "images": payload.get("images"),
                    },
                ),
            )
        elif event == "tool":
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
                q,
                (
                    "meta",
                    {
                        "phase": "restore",
                        "model": payload.get("model"),
                        "tier": payload.get("tier"),
                        "reason": payload.get("reason") or "",
                        "problems": [],
                        "scope": payload.get("scope") or "",
                        "id": payload.get("id") if payload.get("id") is not None else payload.get("step_id"),
                    },
                ),
            )
        elif event == "plan":
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(
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
            _q_put_sse(q, ("meta", {"phase": "plan_accepted"}))
        elif event == "plan_rejected":
            _q_put_sse(
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
                images=images_b64 or None,
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
                    if result.image_context:
                        user_msg.meta = {**(user_msg.meta or {}), "vision_text": result.image_context}
                    c.messages.append(
                        ChatMessage(
                            role="assistant",
                            content=result.text,
                            meta=meta,
                        )
                    )
                    c.updated_at = _now()
            _q_put_sse(q, ("done", {"text": result.text, **meta}))
        except urllib.error.URLError as exc:
            _q_put_sse(q, ("error", {"message": f"Ollama недоступна: {exc}"}))
        except Exception as exc:  # noqa: BLE001
            _q_put_sse(q, ("error", {"message": str(exc)}))
        finally:
            with _lock:
                cur = _plan_waits.get(chat_id)
                if cur is not None and cur.generation == generation:
                    _plan_waits.pop(chat_id, None)
            wlock.release()
            _q_put_sse(q, None)

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

