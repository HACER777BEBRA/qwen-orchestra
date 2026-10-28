"""Воркер: tools, сообщения, попытки."""

from __future__ import annotations

import json
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
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
from .llm import GenOptions, chat, chat_stream, is_truncated, worker_gen
from .llm_stream_tools import chat_stream_tools
from .orchestra import (
    CTX_OUT_NORMAL,
    CTX_TEMPLATE_TOKENS,
    CTX_TOOL_BUDGET_RATIO,
    MAX_TOOL_CALLS,
    MAX_TOOL_ROUNDS,
    NUM_CTX_FULL,
    RETRY_COMPLETE_SYSTEM,
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
from .tools_local import (
    TOOL_IMPL_LOCAL,
    TOOLS_LOCAL,
    local_context_block,
    local_date_line,
)
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


_ARG_BY_TOOL = {"web_search": "query", "fetch_url": "url", "calculate": "expression"}


def _parse_args(name: str, arguments) -> dict:
    """arguments от модели → dict (строка-JSON или «голый» аргумент)."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            key = _ARG_BY_TOOL.get(name)
            arguments = {key: arguments} if key else {}
    return arguments if isinstance(arguments, dict) else {}


def _run_tool(name: str, arguments) -> str:
    impl = _ALL_TOOL_IMPL.get(name)
    if not impl:
        return f"Неизвестный инструмент: {name}"
    try:
        return impl(_parse_args(name, arguments))
    except Exception as exc:  # noqa: BLE001
        return f"Ошибка инструмента {name}: {exc}"


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


_GATE_HOLD_CHARS = 120


class _TokenGate:
    """Держит начало ответа, пока не ясно, что это не преамбула/текстовая команда tool.

    tool_calls приходят в стриме вместе с контентом: «Сейчас поищу…» и строки
    `SEARCH: …` не должны попасть в тело сообщения. После `_GATE_HOLD_CHARS`
    символов без команды — стримим как есть.
    """

    def __init__(self, sink: TokenCallback) -> None:
        self._sink = sink
        self._buf = ""
        self._open = False
        self.emitted = False

    def feed(self, piece: str) -> None:
        if self._open:
            self._sink(piece)
            return
        self._buf += piece
        if len(self._buf) >= _GATE_HOLD_CHARS and not _fallback_tools(self._buf):
            self._release()

    def _release(self) -> None:
        self._open = True
        if self._buf:
            self.emitted = True
            self._sink(self._buf)
            self._buf = ""

    def finish(self, *, has_calls: bool) -> None:
        """Конец ответа: без tool-вызовов — отдать остаток, иначе выбросить удержанное."""
        if not self._open and not has_calls:
            self._release()
        self._buf = ""


def _tools_unsupported(exc: Exception, provider: str) -> bool:
    """Модель не умеет tools (Ollama: HTTP 400 «does not support tools»; OR: ошибка с «tool»)."""
    text = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            text = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
    text = text.lower()
    if provider == "ollama":
        return "does not support tools" in text
    return "tool" in text


def _model_step(
    model: str,
    messages: list[dict],
    *,
    tools: list | None,
    tool_choice: str | None,
    on_token: TokenCallback | None,
    **kw: Any,
) -> tuple[dict, bool]:
    """Один запрос к модели; со стримом, если задан `on_token`. → (message, стримили ли текст)."""
    if on_token is None:
        return chat(model, messages, tools=tools, tool_choice=tool_choice, **kw), False
    gate = _TokenGate(on_token)
    msg = chat_stream_tools(
        model,
        messages,
        tools=tools,
        tool_choice=tool_choice,
        on_token=gate.feed,
        **kw,
    )
    has_calls = bool(msg.get("tool_calls")) or bool(_fallback_tools(msg.get("content") or ""))
    gate.finish(has_calls=has_calls)
    return msg, gate.emitted


def _build_messages(
    user_text: str,
    history: list[dict],
    retry_hint: str | None,
    *,
    local_context: str | None = None,
    system_extra: str | None = None,
    prev_answer: str | None = None,
) -> list[dict]:
    system = WORKER_SYSTEM
    extra = (system_extra or "").strip()
    if extra:
        system = f"{system}\n\n{extra}"
    system = f"{system}\n\n{local_context or local_date_line()}"
    if retry_hint and prev_answer:
        system = f"{system}\n\n" + RETRY_COMPLETE_SYSTEM.format(
            hint=retry_hint, prev=prev_answer
        )
    elif retry_hint:
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
    if (
        decision.need_local_time or decision.time_by_model
    ) and not decision.need_web:
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
    meta: dict[str, Any] | None = None,
    prev_answer: str | None = None,
    plain_context: str | None = None,
    on_token: TokenCallback | None = None,
) -> str:
    """`plain_context` — system без tools: фолбэк, если модель OpenRouter их не поддерживает.

    `on_token` — стрим финального ответа; `meta["streamed"]=True`, если текст уже ушёл в UI.
    """
    g = worker_gen(gen)
    tool_list = tools if tools is not None else _ALL_TOOLS
    messages = _build_messages(
        user_text,
        history,
        retry_hint,
        local_context=local_context,
        system_extra=system_extra,
        prev_answer=prev_answer,
    )
    cache = tool_cache if tool_cache is not None else {}
    calls_done = 0
    prompt_tok = sum(_estimate_tokens(str(m.get("content") or "")) for m in messages)
    tools_tok = _estimate_tokens(json.dumps(tool_list, ensure_ascii=False))
    remain_tok = max(
        256,
        num_ctx
        - prompt_tok
        - tools_tok
        - CTX_TEMPLATE_TOKENS
        - max(256, CTX_OUT_NORMAL // 2),
    )
    tool_budget = max(
        1200,
        min(TOOL_RESULT_CHARS * 2, int(remain_tok * 3 * CTX_TOOL_BUDGET_RATIO)),
    )

    for _ in range(MAX_TOOL_ROUNDS):
        if calls_done >= MAX_TOOL_CALLS:
            break
        try:
            msg, streamed = _model_step(
                model,
                messages,
                tools=tool_list,
                tool_choice=None,
                on_token=on_token,
                gen=g,
                num_ctx=num_ctx,
                provider=provider,
            )
        except (RuntimeError, urllib.error.HTTPError) as exc:
            if calls_done or not _tools_unsupported(exc, provider):
                raise
            return _worker_plain(
                model,
                user_text,
                history,
                stream=False,
                on_token=None,
                retry_hint=retry_hint,
                num_ctx=num_ctx,
                provider=provider,
                local_context=plain_context if plain_context is not None else local_context,
                gen=g,
                system_extra=system_extra,
                meta=meta,
                prev_answer=prev_answer,
            )
        content = (msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            tool_calls = _fallback_tools(content)
        if not tool_calls:
            _set_done_reason(meta, msg)
            if meta is not None and streamed:
                meta["streamed"] = True
            return content or "(пустой ответ)"

        # Вызовы раунда (в пределах лимита). Дубли без id пропускаем; с id — оставляем:
        # OpenAI требует ответ tool на каждый tool_call.
        batch: list[tuple[dict, str, dict, str]] = []
        seen_round: set[str] = set()
        for call in tool_calls:
            if calls_done + len(batch) >= MAX_TOOL_CALLS:
                break
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            args = _parse_args(name, fn.get("arguments") or {})
            key = _tool_cache_key(name, args)
            if key in seen_round and not call.get("id"):
                continue
            batch.append((call, name, args, key))
            seen_round.add(key)
        assistant = {k: v for k, v in msg.items() if k != "done_reason"}
        if assistant.get("tool_calls"):
            assistant["tool_calls"] = [c for c, *_ in batch]
        messages.append(assistant)

        todo: list[tuple[str, dict, str]] = []
        announced: set[str] = set()
        for _call, name, args, key in batch:
            if key in announced:
                continue
            announced.add(key)
            _emit(on_status, "tool", name=name, arguments=args, model=model)
            if verbose:
                cached = " cached" if key in cache else ""
                print(f"  [tool/{model}] {name}({args}){cached}", flush=True)
            if key not in cache:
                todo.append((name, args, key))
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                for (_, _, key), res in zip(
                    todo, pool.map(lambda t: _run_tool(t[0], t[1]), todo)
                ):
                    cache[key] = res
        elif todo:
            cache[todo[0][2]] = _run_tool(todo[0][0], todo[0][1])
        for call, name, _args, key in batch:
            per_call = max(800, tool_budget // max(1, MAX_TOOL_CALLS - calls_done))
            result = _trim_tool_result(cache[key], min(TOOL_RESULT_CHARS, per_call))
            if verbose:
                preview = result if len(result) < 350 else result[:350] + "..."
                print(f"  <- {preview}\n", flush=True)
            reply = {"role": "tool", "tool_name": name, "content": result}
            if call.get("id"):
                reply["tool_call_id"] = call["id"]
            messages.append(reply)
            calls_done += 1
            tool_budget = max(400, tool_budget - len(result))

    msg, streamed = _model_step(
        model,
        messages,
        tools=tool_list if provider != "ollama" else None,
        tool_choice="none" if provider != "ollama" else None,
        on_token=on_token,
        gen=g,
        num_ctx=num_ctx,
        provider=provider,
    )
    _set_done_reason(meta, msg)
    if meta is not None and streamed:
        meta["streamed"] = True
    return (msg.get("content") or "").strip() or "(пустой ответ)"


def _set_done_reason(meta: dict[str, Any] | None, msg: dict) -> None:
    if meta is not None:
        meta["done_reason"] = msg.get("done_reason") or ""
        meta["truncated"] = is_truncated(meta["done_reason"])


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
    meta: dict[str, Any] | None = None,
    prev_answer: str | None = None,
) -> str:
    g = worker_gen(gen)
    messages = _build_messages(
        user_text,
        history,
        retry_hint,
        local_context=local_context,
        system_extra=system_extra,
        prev_answer=prev_answer,
    )

    if stream:
        info: dict[str, Any] = {}
        text = chat_stream(
            model,
            messages,
            on_token=on_token,
            gen=g,
            num_ctx=num_ctx,
            provider=provider,
            info=info,
        )
        _set_done_reason(meta, info)
        return text

    msg = chat(model, messages, gen=g, num_ctx=num_ctx, provider=provider)
    _set_done_reason(meta, msg)
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
