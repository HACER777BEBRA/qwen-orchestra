"""Клиент LLM: Ollama (локально) + OpenRouter (OpenAI-compatible).

`num_ctx` передаётся в `options` Ollama; для OpenRouter — эвристика `max_tokens`
(или явный `GenOptions.num_predict`).

Размер окна подбирает оркестр (`plan_worker_context`), не этот модуль.

Для Qwen3/3.5 в Ollama-payload уходит top-level `think: false` — иначе модель
тратит токены на reasoning и ломает JSON-роутер / tool_calls.

OpenRouter (MVP): chat / stream без tools; ключ — env или secrets.json.

Сэмплинг для воркера — публичный `GenOptions` (через `Client.ask` / `handle`).
Роутер и selfcheck по-прежнему вызывают `chat(..., temperature=0.0)` напрямую.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_WORKER_TEMPERATURE = 0.3
DEFAULT_WORKER_KEEP_ALIVE = "10m"
_TRUNC_REASONS = frozenset({"length", "max_tokens"})

_ollama_host = DEFAULT_OLLAMA_HOST
OLLAMA_CHAT_URL = f"{DEFAULT_OLLAMA_HOST}/api/chat"
OLLAMA_TAGS_URL = f"{DEFAULT_OLLAMA_HOST}/api/tags"


@dataclass(frozen=True)
class GenOptions:
    """Параметры генерации воркера (Ollama options / OpenRouter chat).

    ``None`` = не трогать / взять default оркестра.
    Роутер и selfcheck GenOptions не используют (там всегда temperature=0).
    """

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    seed: int | None = None
    num_predict: int | None = None  # max tokens ответа; OR → max_tokens
    repeat_penalty: float | None = None  # Ollama
    presence_penalty: float | None = None  # OpenRouter / OpenAI-совместимые
    frequency_penalty: float | None = None
    stop: tuple[str, ...] | None = None
    keep_alive: str | None = None  # только Ollama, напр. "10m" / "0"

    def to_dict(self, *, skip_none: bool = True) -> dict[str, Any]:
        raw = asdict(self)
        if self.stop is not None:
            raw["stop"] = list(self.stop)
        if skip_none:
            return {k: v for k, v in raw.items() if v is not None}
        return raw


def _as_stop(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return (s,) if s else None
    seq = tuple(str(x) for x in value if str(x))
    return seq or None


def merge_gen(
    base: GenOptions | None = None,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    seed: int | None = None,
    num_predict: int | None = None,
    repeat_penalty: float | None = None,
    presence_penalty: float | None = None,
    frequency_penalty: float | None = None,
    stop: list[str] | tuple[str, ...] | str | None = None,
    keep_alive: str | None = None,
) -> GenOptions:
    """Склеить GenOptions с плоскими override (не-None побеждают)."""
    g = base or GenOptions()
    updates: dict[str, Any] = {}
    for name, val in (
        ("temperature", temperature),
        ("top_p", top_p),
        ("top_k", top_k),
        ("seed", seed),
        ("num_predict", num_predict),
        ("repeat_penalty", repeat_penalty),
        ("presence_penalty", presence_penalty),
        ("frequency_penalty", frequency_penalty),
        ("keep_alive", keep_alive),
    ):
        if val is not None:
            updates[name] = val
    if stop is not None:
        updates["stop"] = _as_stop(stop)
    return replace(g, **updates) if updates else g


def worker_gen(gen: GenOptions | None = None) -> GenOptions:
    """Defaults воркера: temperature=0.3, keep_alive=10m."""
    g = gen or GenOptions()
    if g.temperature is None:
        g = replace(g, temperature=DEFAULT_WORKER_TEMPERATURE)
    if g.keep_alive is None:
        g = replace(g, keep_alive=DEFAULT_WORKER_KEEP_ALIVE)
    return g


def is_truncated(done_reason: str | None) -> bool:
    """Ответ оборван по длине (Ollama `done_reason` / OpenRouter `finish_reason`)."""
    return (done_reason or "").strip().lower() in _TRUNC_REASONS


def get_ollama_host() -> str:
    return _ollama_host


def set_ollama_host(host: str) -> None:
    """Сменить базовый URL Ollama (без завершающего слэша)."""
    global _ollama_host, OLLAMA_CHAT_URL, OLLAMA_TAGS_URL
    base = (host or DEFAULT_OLLAMA_HOST).strip().rstrip("/")
    if not base:
        base = DEFAULT_OLLAMA_HOST
    _ollama_host = base
    OLLAMA_CHAT_URL = f"{base}/api/chat"
    OLLAMA_TAGS_URL = f"{base}/api/tags"


def _request(payload: dict, timeout: int):
    req = urllib.request.Request(
        OLLAMA_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _think_default(model: str) -> bool | None:
    """Qwen3/3.5 по умолчанию думают — для оркестра это ломает JSON/tools/latency."""
    name = (model or "").lower()
    if name.startswith("qwen3") or "qwen3." in name:
        return False
    return None


def _ollama_options(num_ctx: int, gen: GenOptions) -> dict[str, Any]:
    opts: dict[str, Any] = {"num_ctx": num_ctx}
    if gen.temperature is not None:
        opts["temperature"] = float(gen.temperature)
    if gen.top_p is not None:
        opts["top_p"] = float(gen.top_p)
    if gen.top_k is not None:
        opts["top_k"] = int(gen.top_k)
    if gen.seed is not None:
        opts["seed"] = int(gen.seed)
    if gen.num_predict is not None:
        opts["num_predict"] = int(gen.num_predict)
    if gen.repeat_penalty is not None:
        opts["repeat_penalty"] = float(gen.repeat_penalty)
    if gen.stop:
        opts["stop"] = list(gen.stop)
    return opts


def _payload(
    model: str,
    messages: list[dict],
    *,
    tools: list | None,
    fmt: Any,
    stream: bool,
    num_ctx: int,
    gen: GenOptions,
    think: bool | None,
) -> dict:
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "options": _ollama_options(num_ctx, gen),
    }
    if tools:
        payload["tools"] = tools
    if fmt is not None:
        payload["format"] = fmt
    if gen.keep_alive:
        payload["keep_alive"] = gen.keep_alive
    # top-level (не в options): иначе Ollama игнорирует think
    resolved = _think_default(model) if think is None else think
    if resolved is not None:
        payload["think"] = resolved
    return payload


def _normalize_provider(provider: str | None) -> str:
    name = (provider or "ollama").strip().lower() or "ollama"
    if name not in {"ollama", "openrouter"}:
        raise ValueError(f"Неизвестный provider: {name}")
    return name


def _openrouter_key_and_base() -> tuple[str, str]:
    from . import settings as app_settings

    key = app_settings.get_openrouter_api_key()
    if not key:
        raise RuntimeError(
            "OpenRouter не настроен: задайте переменную окружения "
            f"{app_settings.OPENROUTER_API_KEY_ENV} или ключ в «Модели и промпты»."
        )
    base = getattr(app_settings, "DEFAULT_OPENROUTER_BASE", DEFAULT_OPENROUTER_BASE)
    return key, str(base).rstrip("/")


def _openrouter_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:8787",
        "X-Title": "Qwen Orchestra",
    }


def _openai_messages(messages: list[dict]) -> list[dict]:
    """Только roles, которые понимает OpenAI-compatible API."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role") or "user"
        if role not in {"system", "user", "assistant"}:
            continue
        out.append({"role": role, "content": str(m.get("content") or "")})
    return out


def _max_tokens_from_ctx(num_ctx: int) -> int:
    # Грубый запас под ответ; OpenRouter не принимает num_ctx как Ollama
    return max(256, min(4096, int(num_ctx) // 2 if num_ctx else 1024))


def _openrouter_body(
    model: str,
    messages: list[dict],
    *,
    num_ctx: int,
    gen: GenOptions,
    stream: bool,
) -> dict[str, Any]:
    temp = (
        float(gen.temperature)
        if gen.temperature is not None
        else DEFAULT_WORKER_TEMPERATURE
    )
    max_tokens = (
        int(gen.num_predict)
        if gen.num_predict is not None
        else _max_tokens_from_ctx(num_ctx)
    )
    payload: dict[str, Any] = {
        "model": model,
        "messages": _openai_messages(messages),
        "temperature": temp,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if gen.top_p is not None:
        payload["top_p"] = float(gen.top_p)
    if gen.seed is not None:
        payload["seed"] = int(gen.seed)
    if gen.presence_penalty is not None:
        payload["presence_penalty"] = float(gen.presence_penalty)
    if gen.frequency_penalty is not None:
        payload["frequency_penalty"] = float(gen.frequency_penalty)
    if gen.stop:
        payload["stop"] = list(gen.stop)
    # top_k / repeat_penalty — не стандарт OpenAI; некоторые провайдеры OR игнорят
    if gen.top_k is not None:
        payload["top_k"] = int(gen.top_k)
    if gen.repeat_penalty is not None:
        payload["repetition_penalty"] = float(gen.repeat_penalty)
    return payload


def _openrouter_chat(
    model: str,
    messages: list[dict],
    *,
    num_ctx: int,
    gen: GenOptions,
    timeout: int,
) -> dict:
    api_key, base = _openrouter_key_and_base()
    payload = _openrouter_body(
        model, messages, num_ctx=num_ctx, gen=gen, stream=False
    )
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_openrouter_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"OpenRouter ({model}): HTTP {exc.code} {body}") from exc
    if err := data.get("error"):
        msg = err if isinstance(err, str) else err.get("message") or err
        raise RuntimeError(f"OpenRouter ({model}): {msg}")
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "role": message.get("role") or "assistant",
        "content": message.get("content") or "",
        "done_reason": choice.get("finish_reason") or "",
    }


def _openrouter_chat_stream(
    model: str,
    messages: list[dict],
    *,
    on_token: Callable[[str], None] | None,
    num_ctx: int,
    gen: GenOptions,
    timeout: int,
    info: dict[str, Any] | None = None,
) -> str:
    api_key, base = _openrouter_key_and_base()
    payload = _openrouter_body(
        model, messages, num_ctx=num_ctx, gen=gen, stream=True
    )
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_openrouter_headers(api_key),
        method="POST",
    )
    parts: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if err := chunk.get("error"):
                    msg = err if isinstance(err, str) else err.get("message") or err
                    raise RuntimeError(f"OpenRouter ({model}): {msg}")
                choice = (chunk.get("choices") or [{}])[0]
                if info is not None and choice.get("finish_reason"):
                    info["done_reason"] = choice["finish_reason"]
                delta = choice.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    parts.append(piece)
                    if on_token:
                        on_token(piece)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"OpenRouter ({model}): HTTP {exc.code} {body}") from exc
    return "".join(parts)


def _resolve_call_gen(
    *,
    temperature: float | None,
    gen: GenOptions | None,
    default_temperature: float | None,
) -> GenOptions:
    g = merge_gen(gen, temperature=temperature)
    if g.temperature is None and default_temperature is not None:
        g = replace(g, temperature=default_temperature)
    return g


def chat(
    model: str,
    messages: list[dict],
    *,
    tools: list | None = None,
    fmt: Any = None,
    temperature: float | None = None,
    gen: GenOptions | None = None,
    num_ctx: int = 8192,
    keep_alive: str | None = None,
    think: bool | None = None,
    timeout: int = 600,
    provider: str = "ollama",
) -> dict:
    """Один запрос без стриминга. Возвращает message целиком (+ `done_reason`, если есть)."""
    g = _resolve_call_gen(
        temperature=temperature,
        gen=merge_gen(gen, keep_alive=keep_alive),
        default_temperature=DEFAULT_WORKER_TEMPERATURE,
    )
    prov = _normalize_provider(provider)
    if prov == "openrouter":
        if tools:
            raise RuntimeError("OpenRouter: tools пока не поддерживаются")
        return _openrouter_chat(
            model,
            messages,
            num_ctx=num_ctx,
            gen=g,
            timeout=timeout,
        )
    payload = _payload(
        model,
        messages,
        tools=tools,
        fmt=fmt,
        stream=False,
        num_ctx=num_ctx,
        gen=g,
        think=think,
    )
    with _request(payload, timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if err := data.get("error"):
        raise RuntimeError(f"Ollama ({model}): {err}")
    message = dict(data.get("message") or {})
    if data.get("done_reason"):
        message["done_reason"] = data["done_reason"]
    return message


def chat_stream(
    model: str,
    messages: list[dict],
    *,
    on_token: Callable[[str], None] | None = None,
    temperature: float | None = None,
    gen: GenOptions | None = None,
    num_ctx: int = 8192,
    keep_alive: str | None = None,
    think: bool | None = None,
    timeout: int = 600,
    provider: str = "ollama",
    info: dict[str, Any] | None = None,
) -> str:
    """Потоковый ответ. Токены отдаются в on_token, полный текст возвращается.

    `info` (опц.) — out-параметр: заполняется `done_reason`.
    """
    g = _resolve_call_gen(
        temperature=temperature,
        gen=merge_gen(gen, keep_alive=keep_alive),
        default_temperature=DEFAULT_WORKER_TEMPERATURE,
    )
    prov = _normalize_provider(provider)
    if prov == "openrouter":
        return _openrouter_chat_stream(
            model,
            messages,
            on_token=on_token,
            num_ctx=num_ctx,
            gen=g,
            timeout=timeout,
            info=info,
        )
    payload = _payload(
        model,
        messages,
        tools=None,
        fmt=None,
        stream=True,
        num_ctx=num_ctx,
        gen=g,
        think=think,
    )
    parts: list[str] = []
    with _request(payload, timeout) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            chunk = json.loads(line)
            if err := chunk.get("error"):
                raise RuntimeError(f"Ollama ({model}): {err}")
            piece = (chunk.get("message") or {}).get("content", "")
            if piece:
                parts.append(piece)
                if on_token:
                    on_token(piece)
            if chunk.get("done"):
                if info is not None and chunk.get("done_reason"):
                    info["done_reason"] = chunk["done_reason"]
                break
    return "".join(parts)


def loaded_num_ctx(model: str, *, timeout: int = 3) -> int | None:
    """num_ctx, с которым модель сейчас загружена в Ollama (`/api/ps`), иначе None.

    Другой num_ctx заставляет Ollama перезагрузить модель — ревьюеру выгодно
    взять уже загруженное окно.
    """
    try:
        with urllib.request.urlopen(f"{_ollama_host}/api/ps", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    want = (model or "").strip()
    names = {want, want if ":" in want else f"{want}:latest"}
    for m in data.get("models") or []:
        if (m.get("name") in names) or (m.get("model") in names):
            ctx = m.get("context_length")
            try:
                return int(ctx) if ctx else None
            except (TypeError, ValueError):
                return None
    return None


def installed_models() -> list[str]:
    with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [m["name"] for m in data.get("models", [])]
