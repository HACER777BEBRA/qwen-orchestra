"""Стрим ответа вместе с tools: токены уходят в `on_token`, tool_calls собираются в message."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from .llm import (
    DEFAULT_WORKER_TEMPERATURE,
    GenOptions,
    _normalize_provider,
    _openrouter_body,
    _openrouter_headers,
    _openrouter_key_and_base,
    _payload,
    _request,
    _resolve_call_gen,
    merge_gen,
)


def _ollama_stream_message(
    model: str,
    messages: list[dict],
    *,
    tools: list | None,
    on_token: Callable[[str], None] | None,
    num_ctx: int,
    gen: GenOptions,
    think: bool | None,
    timeout: int,
) -> dict:
    payload = _payload(
        model,
        messages,
        tools=tools,
        fmt=None,
        stream=True,
        num_ctx=num_ctx,
        gen=gen,
        think=think,
    )
    parts: list[str] = []
    calls: list[dict] = []
    done_reason = ""
    with _request(payload, timeout) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            chunk = json.loads(line)
            if err := chunk.get("error"):
                raise RuntimeError(f"Ollama ({model}): {err}")
            msg = chunk.get("message") or {}
            if piece := msg.get("content"):
                parts.append(piece)
                if on_token:
                    on_token(piece)
            calls.extend(msg.get("tool_calls") or [])
            if chunk.get("done"):
                done_reason = chunk.get("done_reason") or ""
                break
    out: dict = {"role": "assistant", "content": "".join(parts), "done_reason": done_reason}
    if calls:
        out["tool_calls"] = calls
    return out


def _merge_tool_delta(acc: dict[int, dict], deltas: list[dict]) -> None:
    """Фрагменты tool_calls OpenAI-стрима (по index) → накопленные вызовы."""
    for d in deltas:
        slot = acc.setdefault(
            int(d.get("index") or 0),
            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
        )
        if d.get("id"):
            slot["id"] = d["id"]
        fn = d.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def _openrouter_stream_message(
    model: str,
    messages: list[dict],
    *,
    tools: list | None,
    tool_choice: str | None,
    on_token: Callable[[str], None] | None,
    num_ctx: int,
    gen: GenOptions,
    timeout: int,
) -> dict:
    api_key, base = _openrouter_key_and_base()
    payload = _openrouter_body(
        model,
        messages,
        num_ctx=num_ctx,
        gen=gen,
        stream=True,
        tools=tools,
        tool_choice=tool_choice,
    )
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=_openrouter_headers(api_key),
        method="POST",
    )
    parts: list[str] = []
    acc: dict[int, dict] = {}
    done_reason = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line.startswith("data:"):
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
                if choice.get("finish_reason"):
                    done_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if piece := delta.get("content"):
                    parts.append(piece)
                    if on_token:
                        on_token(piece)
                if delta.get("tool_calls"):
                    _merge_tool_delta(acc, delta["tool_calls"])
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"OpenRouter ({model}): HTTP {exc.code} {body}") from exc
    out: dict = {"role": "assistant", "content": "".join(parts), "done_reason": done_reason}
    if acc:
        out["tool_calls"] = [acc[i] for i in sorted(acc)]
    return out


def chat_stream_tools(
    model: str,
    messages: list[dict],
    *,
    tools: list | None = None,
    on_token: Callable[[str], None] | None = None,
    temperature: float | None = None,
    gen: GenOptions | None = None,
    num_ctx: int = 8192,
    keep_alive: str | None = None,
    think: bool | None = None,
    timeout: int = 600,
    provider: str = "ollama",
    tool_choice: str | None = None,
) -> dict:
    """Потоковый запрос с tools. Возвращает message как `chat` (content, tool_calls, done_reason).

    Контент уходит в `on_token` по мере генерации — вызывающий сам решает, что из него
    показывать (например, не светить «SEARCH: …» или преамбулу перед вызовом tool).
    """
    g = _resolve_call_gen(
        temperature=temperature,
        gen=merge_gen(gen, keep_alive=keep_alive),
        default_temperature=DEFAULT_WORKER_TEMPERATURE,
    )
    if _normalize_provider(provider) == "openrouter":
        return _openrouter_stream_message(
            model,
            messages,
            tools=tools,
            tool_choice=tool_choice,
            on_token=on_token,
            num_ctx=num_ctx,
            gen=g,
            timeout=timeout,
        )
    return _ollama_stream_message(
        model,
        messages,
        tools=tools,
        on_token=on_token,
        num_ctx=num_ctx,
        gen=g,
        think=think,
        timeout=timeout,
    )
