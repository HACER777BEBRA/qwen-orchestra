"""Воркер: tools, сообщения, попытки."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .calc import (
    CALC_LINE_RE,
    CALC_SYSTEM,
    CALC_SYSTEM_NOTOOLS,
    TOOL_IMPL_CALC,
    TOOLS_CALC,
    calc_context_block,
)
from .llm import GenOptions, chat, chat_stream, worker_gen
from .orchestra import (
    CTX_OUT_NORMAL,
    CTX_TEMPLATE_TOKENS,
    CTX_TOOL_BUDGET_RATIO,
    MAX_TOOL_CALLS,
    MAX_TOOL_ROUNDS,
    NUM_CTX_FULL,
    RETRY_SYSTEM,
    TOOL_RESULT_CHARS,
    WORKER_SYSTEM,
    ContextPlan,
    StatusCallback,
    TokenCallback,
)
from .orch_ctx import _estimate_tokens
from .orch_status import _emit
from .router import RouteDecision, Tier
from .selfcheck import Verdict
from .tools_local import TOOL_IMPL_LOCAL, TOOLS_LOCAL, local_context_block
from .tools_web import TOOL_IMPL, TOOLS

_ALL_TOOLS = list(TOOLS) + list(TOOLS_LOCAL) + list(TOOLS_CALC)
_ALL_TOOL_IMPL = {**TOOL_IMPL, **TOOL_IMPL_LOCAL, **TOOL_IMPL_CALC}
_SEARCH_RE = re.compile(r"(?:SEARCH|ПОИСК)\s*:\s*(.+)", re.IGNORECASE)
_FETCH_RE = re.compile(r"(?:FETCH|ОТКРЫТЬ)\s*:\s*(https?://\S+)", re.IGNORECASE)
_CALC_RE = CALC_LINE_RE


def _tool_cache_key(name: str, arguments: dict) -> str:
    return f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"


def _trim_tool_result(result: str, budget_chars: int) -> str:
    text = result or ""
    if len(text) <= budget_chars:
        return text
    return text[: max(0, budget_chars - 20)] + "\n...[обрезано]"


def _run_tool(name: str, arguments) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            if name == "web_search":
                arguments = {"query": arguments}
            elif name == "fetch_url":
                arguments = {"url": arguments}
            elif name == "calculate":
                arguments = {"expression": arguments}
            else:
                arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    impl = _ALL_TOOL_IMPL.get(name)
    if not impl:
        return f"Неизвестный инструмент: {name}"
    return impl(arguments)


def _fallback_tools(content: str) -> list[dict]:
    calls = []
    for m in _SEARCH_RE.finditer(content or ""):
        calls.append({"function": {"name": "web_search", "arguments": {"query": m.group(1).strip()}}})
    for m in _FETCH_RE.finditer(content or ""):
        url = m.group(1).strip().rstrip(".,)")
        calls.append({"function": {"name": "fetch_url", "arguments": {"url": url}}})
    for m in _CALC_RE.finditer(content or ""):
        expr = m.group(1).strip().rstrip(".,;")
        if expr:
            calls.append({"function": {"name": "calculate", "arguments": {"expression": expr}}})
    return calls


def _build_messages(
    user_text: str,
    history: list[dict],
    retry_hint: str | None,
    *,
    local_context: str | None = None,
    system_extra: str | None = None,
) -> list[dict]:
    system = WORKER_SYSTEM
    extra = (system_extra or "").strip()
    if extra:
        system = f"{system}\n\n{extra}"
    if local_context:
        system = f"{system}\n\n{local_context}"
    if retry_hint:
        system = f"{system}\n\n{RETRY_SYSTEM.format(hint=retry_hint)}"
    messages: list[dict] = [{"role": "system", "content": system}]
    for h in history:
        if h.get("role") in {"user", "assistant"}:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})
    return messages


def _tools_for(decision: RouteDecision) -> list:
    """Какие tools отдать воркеру: web / время ПК / калькулятор."""
    out: list = []
    if decision.need_web:
        out.extend(TOOLS)
        out.extend(TOOLS_LOCAL)
    if decision.need_calc:
        out.extend(TOOLS_CALC)
    if decision.need_local_time and not decision.need_web:
        out.extend(TOOLS_LOCAL)
    return out


def _local_context_for(
    decision: RouteDecision,
    user_text: str,
    *,
    precompute: bool = False,
) -> str | None:
    parts: list[str] = []
    if decision.need_local_time or decision.need_web:
        parts.append(local_context_block())
    if decision.need_calc:
        if precompute:
            parts.append(CALC_SYSTEM_NOTOOLS)
            pre = calc_context_block(user_text)
            if pre:
                parts.append(pre)
        else:
            parts.append(CALC_SYSTEM)
    return "\n\n".join(parts) if parts else None


def _worker_with_tools(
    model: str,
    user_text: str,
    history: list[dict],
    *,
    on_status: StatusCallback | None,
    verbose: bool,
    retry_hint: str | None = None,
    num_ctx: int = NUM_CTX_FULL,
    tool_cache: dict[str, str] | None = None,
    provider: str = "ollama",
    local_context: str | None = None,
    gen: GenOptions | None = None,
    system_extra: str | None = None,
    tools: list | None = None,
) -> str:
    g = worker_gen(gen)
    tool_list = tools if tools is not None else _ALL_TOOLS
    if provider != "ollama":
        return _worker_plain(
            model,
            user_text,
            history,
            stream=False,
            on_token=None,
            retry_hint=retry_hint,
            num_ctx=num_ctx,
            provider=provider,
            local_context=local_context,
            gen=g,
            system_extra=system_extra,
        )
    messages = _build_messages(
        user_text,
        history,
        retry_hint,
        local_context=local_context,
        system_extra=system_extra,
    )
    cache = tool_cache if tool_cache is not None else {}
    calls_done = 0
    prompt_tok = sum(_estimate_tokens(str(m.get("content") or "")) for m in messages)
    remain_tok = max(
        256,
        num_ctx - prompt_tok - CTX_TEMPLATE_TOKENS - max(256, CTX_OUT_NORMAL // 2),
    )
    tool_budget = max(
        1200,
        min(TOOL_RESULT_CHARS * 2, int(remain_tok * 3 * CTX_TOOL_BUDGET_RATIO)),
    )

    for _ in range(MAX_TOOL_ROUNDS):
        if calls_done >= MAX_TOOL_CALLS:
            break
        msg = chat(
            model,
            messages,
            tools=tool_list,
            gen=g,
            num_ctx=num_ctx,
            provider=provider,
        )
        content = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            tool_calls = _fallback_tools(content)
        if not tool_calls:
            return content or "(пустой ответ)"

        messages.append(msg)
        seen_round: set[str] = set()
        for call in tool_calls:
            if calls_done >= MAX_TOOL_CALLS:
                break
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    if name == "web_search":
                        args = {"query": args}
                    elif name == "fetch_url":
                        args = {"url": args}
                    elif name == "calculate":
                        args = {"expression": args}
                    else:
                        args = {}
            if not isinstance(args, dict):
                args = {}
            key = _tool_cache_key(name, args)
            if key in seen_round:
                continue
            seen_round.add(key)
            _emit(on_status, "tool", name=name, arguments=args, model=model)
            if verbose:
                cached = " cached" if key in cache else ""
                print(f"  [tool/{model}] {name}({args}){cached}", flush=True)
            if key in cache:
                result = cache[key]
            else:
                result = _run_tool(name, args)
                cache[key] = result
            per_call = max(800, tool_budget // max(1, MAX_TOOL_CALLS - calls_done))
            result = _trim_tool_result(result, min(TOOL_RESULT_CHARS, per_call))
            if verbose:
                preview = result if len(result) < 350 else result[:350] + "..."
                print(f"  <- {preview}\n", flush=True)
            messages.append({"role": "tool", "content": result})
            calls_done += 1
            tool_budget = max(400, tool_budget - len(result))

    msg = chat(model, messages, gen=g, num_ctx=num_ctx, provider=provider)
    return (msg.get("content") or "").strip() or "(пустой ответ)"


def _worker_plain(
    model: str,
    user_text: str,
    history: list[dict],
    *,
    stream: bool,
    on_token: TokenCallback | None,
    retry_hint: str | None = None,
    num_ctx: int = NUM_CTX_FULL,
    provider: str = "ollama",
    local_context: str | None = None,
    gen: GenOptions | None = None,
    system_extra: str | None = None,
) -> str:
    g = worker_gen(gen)
    messages = _build_messages(
        user_text,
        history,
        retry_hint,
        local_context=local_context,
        system_extra=system_extra,
    )

    if stream:
        return chat_stream(
            model,
            messages,
            on_token=on_token,
            gen=g,
            num_ctx=num_ctx,
            provider=provider,
        )

    msg = chat(model, messages, gen=g, num_ctx=num_ctx, provider=provider)
    return (msg.get("content") or "").strip() or "(пустой ответ)"


@dataclass
class _Attempt:
    text: str
    verdict: Verdict
    tier: Tier
    model: str
    ctx: ContextPlan


def _best_attempt(attempts: list[_Attempt]) -> _Attempt:
    """Прошедшая проверку попытка, иначе — с наименее серьёзными проблемами."""
    for a in attempts:
        if a.verdict.ok:
            return a
    return min(attempts, key=lambda a: (a.verdict.severity(), -len(a.text)))
