"""Оркестр: tiny → nano → small → mid → large → heavy → xlarge/coder → ultra → frontier.

Цикл одного запроса:
    route → need_plan? → (planner → шаги → compose) | worker → selfcheck
    → (retry на старшем тире)

Адаптивный num_ctx (см. plan_worker_context):
    ceil_256(tokens(промпт) + template + reserve_ответа + safety)

Самопроверка (`selfcheck.check`) отбраковывает ответ, если он ушёл на другой
язык, оказался пустым/зацикленным, стал отказом или содержит явную ошибку.
Тогда запрос переделывается на модели уровнем выше с инструкцией-правкой.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import plan as plan_mod
from . import selfcheck
from . import settings as app_settings
from .calc import (
    CALC_LINE_RE,
    CALC_SYSTEM,
    CALC_SYSTEM_NOTOOLS,
    TOOL_IMPL_CALC,
    TOOLS_CALC,
    calc_context_block,
    ensure_computed,
    need_calc,
    pick_formula,
)
from .llm import GenOptions, chat, chat_stream, installed_models, merge_gen, worker_gen
from .router import ALL_TIERS, TIER_ORDER, TIER_RANK, RouteDecision, Tier, need_local_time, need_web, route
from .selfcheck import Verdict
from .tools_local import TOOL_IMPL_LOCAL, TOOLS_LOCAL, local_context_block
from .tools_web import TOOL_IMPL, TOOLS

# Web + локальные tools в одном пуле для Ollama tool-calling
_ALL_TOOLS = list(TOOLS) + list(TOOLS_LOCAL) + list(TOOLS_CALC)
_ALL_TOOL_IMPL = {**TOOL_IMPL, **TOOL_IMPL_LOCAL, **TOOL_IMPL_CALC}

MODELS: dict[str, str] = {
    "tiny": "qwen3.5:0.8b",
    "nano": "qwen3.5:0.8b",
    "small": "qwen3.5:2b",
    "mid": "qwen3.5:4b",
    "large": "qwen2.5:7b",
    "heavy": "qwen3.5:9b",
    "xlarge": "qwen2.5:14b",
    "coder": "qwen2.5-coder:14b",
    "ultra": "qwen2.5:14b",
    "frontier": "qwen2.5:14b",
}

# id слота/тира → ollama | openrouter (derived; синхронизируется apply_to_runtime)
PROVIDERS: dict[str, str] = {tid: "ollama" for tid in MODELS}

# Снимок пула: list[dict] с полями id/model/provider/tier/rank/...
POOL: list[dict[str, Any]] = []

REQUIRED_TIERS: tuple[str, ...] = ()
OPTIONAL_TIERS: tuple[str, ...] = ()


def _entry_reachable(entry: dict[str, Any], have: set[str], *, or_ok: bool) -> bool:
    name = (entry.get("model") or "").strip()
    if not name:
        return False
    if (entry.get("provider") or "ollama") == "openrouter":
        return or_ok
    return name in have


def available_pool_entries(
    have: set[str] | None = None,
    *,
    pool: list[dict[str, Any]] | None = None,
    routed_only: bool = False,
) -> list[dict[str, Any]]:
    """Доступные записи пула (Ollama installed или OR + ключ)."""
    have = have if have is not None else set(installed_models())
    src = pool if pool is not None else POOL
    or_ok = app_settings.openrouter_configured()
    out: list[dict[str, Any]] = []
    for e in src:
        if routed_only and not (e.get("tier") or "").strip():
            continue
        if _entry_reachable(e, have, or_ok=or_ok):
            out.append(e)
    return out


def available_tiers(
    have: set[str] | None = None,
    *,
    models: dict[str, str] | None = None,
    providers: dict[str, str] | None = None,
    pool: list[dict[str, Any]] | None = None,
) -> set[Tier]:
    """Тиры с ≥1 доступной routed-моделью в пуле."""
    del models, providers  # derived из пула
    entries = available_pool_entries(have, pool=pool, routed_only=True)
    return {str(e["tier"]) for e in entries if e.get("tier")}


def _unavailable_pool_labels(
    have: set[str],
    *,
    pool: list[dict[str, Any]] | None = None,
) -> list[str]:
    src = pool if pool is not None else POOL
    or_ok = app_settings.openrouter_configured()
    out: list[str] = []
    for e in src:
        name = (e.get("model") or "").strip()
        if not name:
            continue
        if _entry_reachable(e, have, or_ok=or_ok):
            continue
        if (e.get("provider") or "ollama") == "openrouter":
            out.append(f"openrouter:{name}")
        else:
            out.append(name)
    return out


def missing_models(have: set[str] | None = None) -> list[str]:
    """Критичные пробелы: нет ни одной доступной модели пула."""
    have = have if have is not None else set(installed_models())
    if available_pool_entries(have):
        return []
    return _unavailable_pool_labels(have) or ["(нет доступных моделей)"]


def missing_optional_models(have: set[str] | None = None) -> list[str]:
    """Назначенные, но недоступные модели при том, что пул частично работает."""
    have = have if have is not None else set(installed_models())
    if not available_pool_entries(have):
        return []
    return _unavailable_pool_labels(have)

MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 8
TOOL_RESULT_CHARS = 4000

# Сколько всего попыток на один запрос (1 основная + повторы после самопроверки)
MAX_ATTEMPTS = 3
# LLM-ревью ответа (ловит ошибки и уход от вопроса); проверяющий — mid
SELFCHECK_LLM = True
SELFCHECK_MODEL = MODELS["mid"]

# num_ctx = ceil_256(prompt + template + reserve_ответа + safety), clamp [MIN…MAX]
_COMPLEX_TIERS: frozenset[str] = frozenset(
    {"heavy", "xlarge", "coder", "ultra", "frontier"}
)
NUM_CTX_MIN = 256
NUM_CTX_MAX = 8192
NUM_CTX_FULL = NUM_CTX_MAX
CTX_ALIGN = 256
CTX_TEMPLATE_TOKENS = 128  # chat-шаблон / спецтокены
CTX_SAFETY_FLAT = 128  # мин. safety поверх суммы
CTX_OUT_SHORT = 512
CTX_OUT_NORMAL = 768
CTX_OUT_CODE = 1536
HISTORY_LIGHT = 4
HISTORY_FULL = 8
# Запас под tool-результаты внутри уже выбранного num_ctx
CTX_TOOL_BUDGET_RATIO = 0.35

WORKER_SYSTEM = """Ты полезный ассистент. Отвечай на языке пользователя, кратко и по делу.
Если в контексте есть результаты поиска/страниц — опирайся на них и указывай URL.
Не выдумывай свежие факты без источников."""

# Правка идёт в system, а не в текст пользователя: иначе модель начинает
# извиняться и «соглашаться с поправкой» вместо нормального ответа
RETRY_SYSTEM = """Предыдущая попытка ответа забракована автопроверкой.
Что учесть: {hint}

Ответь на вопрос заново и полностью. Не упоминай предыдущие попытки,
не извиняйся и не благодари за замечание."""

_SEARCH_RE = re.compile(r"(?:SEARCH|ПОИСК)\s*:\s*(.+)", re.IGNORECASE)
_FETCH_RE = re.compile(r"(?:FETCH|ОТКРЫТЬ)\s*:\s*(https?://\S+)", re.IGNORECASE)
_CALC_RE = CALC_LINE_RE
_CODEISH_RE = re.compile(
    r"```|\bdef\s|\bclass\s|\bfunction\s|напиши\s+(код|функци|скрипт|класс|программ)|"
    r"реализуй|запрограммируй|сгенерируй\s+код|\btraceback\b|\bstack\s+trace\b",
    re.IGNORECASE,
)
# Отсылки к предыдущим репликам чата (без голых «это/этот» — слишком широко)
_HISTORY_REF_RE = re.compile(
    r"(?:"
    r"\b(?:выше|ранее|предыдущ\w*|продолж\w*|вышесказан\w*)\b|"
    r"как\s+ты\s+(?:сказал|написал|предложил|ответил)|"
    r"\b(?:ещё|еще)\s+раз\b|"
    r"(?:исправь|поправь|переделай|доработай)\s+(?:это|этот|код|ответ|функци\w*)|"
    r"\b(?:тот\s+же|ту\s+же|тот\s+код|мой\s+код|из\s+чата|из\s+истории)\b|"
    r"\b(?:previous|above|continue|aforementioned)\b|"
    r"\b(?:fix|change|update)\s+(?:it|that|this|the\s+code)\b"
    r")",
    re.IGNORECASE,
)

StatusCallback = Callable[[str, dict[str, Any]], None]
TokenCallback = Callable[[str], None]
# approve | reject | timeout — иначе как reject
PlanConfirmCallback = Callable[[dict[str, Any]], str]


@dataclass
class ContextPlan:
    """Сколько истории и num_ctx отдать воркеру (экономия VRAM на 14b)."""

    history: list[dict]
    num_ctx: int
    use_history: bool
    reason: str


@dataclass
class OrchestraResult:
    text: str
    tier: Tier
    model: str
    need_web: bool
    route_reason: str
    escalated: bool = False
    attempts: int = 1
    checked: bool = True
    problems: list[str] = field(default_factory=list)
    num_ctx: int = NUM_CTX_FULL
    used_history: bool = True
    context_reason: str = ""
    need_local_time: bool = False
    need_calc: bool = False
    gen: GenOptions | None = None  # фактически применённые опции воркера
    planned: bool = False
    plan_goal: str = ""
    plan_steps: list[dict[str, Any]] = field(default_factory=list)


def _provider_of(tier: str, providers: dict[str, str] | None = None) -> str:
    src = providers if providers is not None else PROVIDERS
    return src.get(tier, "ollama") or "ollama"


def _pool_rank(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("rank") if entry.get("rank") is not None else -1)
    except (TypeError, ValueError):
        return -1


def _pick_entry_for_tier(
    tier: Tier,
    available_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Среди available routed-моделей тира — max rank."""
    cands = [
        e
        for e in available_entries
        if (e.get("tier") or "").strip() == tier and (e.get("model") or "").strip()
    ]
    if not cands:
        return None
    return max(cands, key=lambda e: (_pool_rank(e), str(e.get("id") or "")))


def _entry_by_id(
    pool_id: str, pool: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for e in pool:
        if e.get("id") == pool_id:
            return e
    return None


def _emit(on_status: StatusCallback | None, event: str, **payload: Any) -> None:
    if on_status:
        on_status(event, payload)


def _emit_token(on_token: TokenCallback | None, text: str) -> None:
    if on_token and text:
        on_token(text)


def _rank(tier: str) -> int:
    return int(TIER_RANK.get(tier, 1))


def _fit_tier(tier: Tier, available: set[Tier]) -> Tier:
    """Подобрать тир с моделями: тот же, иначе ближайший ниже/альтернатива."""
    if not available:
        raise RuntimeError("Нет установленных моделей оркестра")
    if tier in available:
        return tier
    if tier == "coder" and "xlarge" in available:
        return "xlarge"
    if tier == "xlarge" and "coder" in available:
        return "coder"
    for t in reversed(TIER_ORDER):
        if _rank(t) <= _rank(tier) and t in available:
            return t
    for t in TIER_ORDER:
        if t in available:
            return t
    return next(iter(available))


def _next_entry_by_rank(
    current: dict[str, Any],
    available_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Следующая routed-модель с rank строго выше текущей."""
    cur_rank = _pool_rank(current)
    routed = [
        e
        for e in available_entries
        if (e.get("tier") or "").strip() and (e.get("model") or "").strip()
    ]
    stronger = [e for e in routed if _pool_rank(e) > cur_rank]
    if not stronger:
        return None
    return min(stronger, key=lambda e: (_pool_rank(e), str(e.get("id") or "")))


def _local_review_model(
    available_entries: list[dict[str, Any]],
    *,
    installed: set[str],
    preferred_tier: str = "mid",
    fallback: str = "",
) -> str | None:
    """Локальная Ollama-модель для selfcheck."""
    local = [
        e
        for e in available_entries
        if (e.get("provider") or "ollama") == "ollama"
        and (e.get("model") or "").strip() in installed
    ]
    if not local:
        return fallback if fallback in installed else None
    preferred = [
        e for e in local if (e.get("tier") or "") == preferred_tier
    ]
    pick_from = preferred or local
    best = max(pick_from, key=lambda e: (_pool_rank(e), str(e.get("id") or "")))
    return (best.get("model") or "").strip() or None


def _escalated(start: Tier, final: Tier) -> bool:
    if _rank(final) > _rank(start):
        return True
    return start != final and {start, final} <= {"coder", "xlarge"}


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


def needs_chat_history(user_text: str, history: list[dict]) -> tuple[bool, str]:
    """Нужны ли предыдущие сообщения чата для ответа.

    Для heavy/xlarge/coder отказ от истории → меньший num_ctx → больше VRAM под веса.
    """
    if not history:
        return False, "no-history"
    t = (user_text or "").strip()
    if not t:
        return False, "empty"
    if _HISTORY_REF_RE.search(t):
        return True, "ref"
    # «исправь/поправь» без кода в сообщении — почти наверняка про прошлый ответ
    if re.search(r"\b(?:исправь|поправь|переделай|доработай)\b", t, re.IGNORECASE) and "```" not in t:
        return True, "edit-prev"
    # Короткий follow-up почти всегда про прошлый ответ
    if len(t) < 48:
        return True, "short-followup"
    # Самодостаточный запрос: длинный текст или код внутри сообщения
    if "```" in t or len(t) >= 160:
        return False, "self-contained"
    return False, "standalone"


def _estimate_tokens(text: str) -> int:
    """Оценка токенов: ~2 символа/токен (завышено для RU/кода, чтобы не обрезать)."""
    n = len(text or "")
    if n == 0:
        return 0
    return max(1, (n + 1) // 2)


def _prompt_tokens(
    user_text: str,
    history: list[dict],
    *,
    system: str = WORKER_SYSTEM,
) -> int:
    """Токены сырого промпта (system + история + user), без шаблона."""
    total = _estimate_tokens(system) + _estimate_tokens(user_text) + 8
    for h in history:
        total += _estimate_tokens(str(h.get("content") or "")) + 4
    return total


def _output_reserve_tokens(user_text: str, tier: Tier) -> int:
    """Запас под ответ: короткий / обычный / код."""
    t = user_text or ""
    if tier == "coder" or _CODEISH_RE.search(t) or "```" in t:
        return CTX_OUT_CODE
    if len(t.strip()) < 48:
        return CTX_OUT_SHORT
    return CTX_OUT_NORMAL


def _ceil_align(n: int, step: int = CTX_ALIGN) -> int:
    if n <= 0:
        return step
    return ((n + step - 1) // step) * step


def _min_num_ctx(prompt_tokens: int, out_reserve: int) -> int:
    """num_ctx = ceil_256(prompt + template + reserve_ответа + safety)."""
    base = prompt_tokens + CTX_TEMPLATE_TOKENS + out_reserve
    safety = max(CTX_SAFETY_FLAT, base // 10)  # ≥128 или ~10% суммы
    need = _ceil_align(base + safety)
    return min(NUM_CTX_MAX, max(NUM_CTX_MIN, need))


def _trim_history_to_fit(
    user_text: str, history: list[dict], *, out_reserve: int
) -> list[dict]:
    """Приоритет — полный текущий запрос; историю урезаем с начала, если не влезает."""
    # Сколько токенов промпта максимум влезает при потолке num_ctx
    # num_ctx >= prompt + template + out + safety(≥10% of base)
    # худший случай safety ≈ 10% → prompt ≈ (0.9*MAX - template - out) / 1.0 приблизительно
    max_prompt = NUM_CTX_MAX - CTX_TEMPLATE_TOKENS - out_reserve - CTX_SAFETY_FLAT
    max_prompt = max(64, (max_prompt * 9) // 10)  # чуть места под 10% safety
    base = _estimate_tokens(WORKER_SYSTEM) + _estimate_tokens(user_text) + 8
    if base >= max_prompt:
        return []
    kept: list[dict] = []
    used = base
    for h in reversed(history):
        cost = _estimate_tokens(str(h.get("content") or "")) + 4
        if used + cost > max_prompt:
            break
        kept.append(h)
        used += cost
    kept.reverse()
    return kept


def _apply_ctx_scale(
    base_ctx: int,
    *,
    ctx_overhead_pct: int = 0,
    max_ctx: int | None = None,
) -> tuple[int, float]:
    """Запас % поверх базы → ceil_256, clamp [MIN…ceiling]. Возвращает (num_ctx, factor)."""
    pct = max(0, int(ctx_overhead_pct or 0))
    factor = 1.0 + pct / 100.0
    ceiling = NUM_CTX_MAX if not max_ctx else int(max_ctx)
    ceiling = min(NUM_CTX_MAX, max(NUM_CTX_MIN, ceiling))
    scaled = _ceil_align(int(base_ctx * factor))
    return min(ceiling, max(NUM_CTX_MIN, scaled)), factor


def plan_worker_context(
    user_text: str,
    history: list[dict],
    tier: Tier,
    *,
    ctx_overhead_pct: int = 0,
    max_ctx: int | None = None,
    system_extra: str | None = None,
) -> ContextPlan:
    """История и num_ctx: 1) не обрезать запрос 2) минимальный ctx / VRAM (+ запас модели)."""
    hist = [
        h
        for h in (history or [])
        if h.get("role") in {"user", "assistant"} and (h.get("content") or "").strip()
    ]
    out_res = _output_reserve_tokens(user_text, tier)
    system = WORKER_SYSTEM
    extra = (system_extra or "").strip()
    if extra:
        system = f"{system}\n\n{extra}"

    if tier not in _COMPLEX_TIERS:
        hist = _trim_history_to_fit(user_text, hist[-HISTORY_FULL:], out_reserve=out_res)
        prompt_tok = _prompt_tokens(user_text, hist, system=system)
        base = _min_num_ctx(prompt_tok, out_res)
        ctx, factor = _apply_ctx_scale(
            base, ctx_overhead_pct=ctx_overhead_pct, max_ctx=max_ctx
        )
        reason = "light-tier"
        if factor != 1.0:
            reason = f"{reason}+ctx×{factor:.2f}"
        return ContextPlan(
            history=hist,
            num_ctx=ctx,
            use_history=bool(hist),
            reason=reason,
        )

    use, why = needs_chat_history(user_text, hist)
    if use:
        hist = _trim_history_to_fit(user_text, hist[-HISTORY_LIGHT:], out_reserve=out_res)
        if not hist:
            why = f"{why}+drop-hist"
            use = False
    else:
        hist = []

    prompt_tok = _prompt_tokens(user_text, hist, system=system)
    base = _min_num_ctx(prompt_tok, out_res)
    ctx, factor = _apply_ctx_scale(
        base, ctx_overhead_pct=ctx_overhead_pct, max_ctx=max_ctx
    )
    ceiling = NUM_CTX_MAX if not max_ctx else min(NUM_CTX_MAX, max(NUM_CTX_MIN, int(max_ctx)))
    if ctx >= ceiling and prompt_tok + CTX_TEMPLATE_TOKENS + out_res + CTX_SAFETY_FLAT > ceiling:
        why = f"{why}+ctx-max"
    if factor != 1.0:
        why = f"{why}+ctx×{factor:.2f}"

    return ContextPlan(
        history=hist,
        num_ctx=ctx,
        use_history=use and bool(hist),
        reason=why,
    )


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
    if decision.need_local_time:
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
        # MVP: tools только через Ollama
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
    # Бюджет в символах из остатка токенов окна (оценка ~2 символа/токен)
    prompt_tok = sum(_estimate_tokens(str(m.get("content") or "")) for m in messages)
    remain_tok = max(
        256,
        num_ctx - prompt_tok - CTX_TEMPLATE_TOKENS - max(256, CTX_OUT_NORMAL // 2),
    )
    tool_budget = max(
        1200,
        min(TOOL_RESULT_CHARS * 2, int(remain_tok * 2 * CTX_TOOL_BUDGET_RATIO)),
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


def _emit_context(on_status: StatusCallback | None, ctx_plan: ContextPlan, tier: Tier) -> None:
    _emit(
        on_status,
        "context",
        used_history=ctx_plan.use_history,
        num_ctx=ctx_plan.num_ctx,
        reason=ctx_plan.reason,
        history_messages=len(ctx_plan.history),
        tier=tier,
    )


def _cli_status_printer(verbose: bool) -> StatusCallback:
    def _print(event: str, payload: dict[str, Any]) -> None:
        if event == "route":
            print(
                f"  [route] ok={payload.get('ok')} tier={payload.get('tier')} "
                f"web={payload.get('need_web')} ({payload.get('reason')})",
                flush=True,
            )
        elif event == "worker":
            web = " + web" if payload.get("need_web") else ""
            local = " + time" if payload.get("need_local_time") else ""
            calc = " + calc" if payload.get("need_calc") else ""
            attempt = payload.get("attempt") or 1
            suffix = f" (попытка {attempt})" if attempt > 1 else ""
            ctx = payload.get("num_ctx")
            hist = "hist" if payload.get("used_history") else "no-hist"
            ctx_s = f" ctx={ctx} {hist}" if ctx else ""
            print(f"  [worker] {payload.get('model')}{web}{local}{calc}{suffix}{ctx_s}", flush=True)
        elif event == "context":
            hist = "история" if payload.get("used_history") else "без истории"
            print(
                f"  [context] {hist}, num_ctx={payload.get('num_ctx')} ({payload.get('reason')})",
                flush=True,
            )
        elif event == "selfcheck":
            state = "ok" if payload.get("ok") else ", ".join(payload.get("problems") or [])
            print(f"\n  [check] {state}", flush=True)
        elif event == "retry":
            print(
                f"  [retry] {payload.get('from_model')} -> {payload.get('to_model')} "
                f"({', '.join(payload.get('problems') or [])})",
                flush=True,
            )
        elif event == "restore":
            print(f"  [restore] лучший ответ: {payload.get('model')}", flush=True)
        elif event == "plan":
            print(
                f"  [plan] need={payload.get('need_plan')} ({payload.get('reason')})",
                flush=True,
            )
        elif event == "plan_ready":
            n = len(payload.get("steps") or [])
            print(
                f"  [plan_ready] {payload.get('planner_model')} · {n} шагов: "
                f"{payload.get('goal') or ''}",
                flush=True,
            )
        elif event == "plan_step":
            print(
                f"  [plan_step] {payload.get('index')}/{payload.get('total')} "
                f"{payload.get('title')} ({payload.get('tier')})",
                flush=True,
            )
        elif event == "plan_step_done":
            state = "fail" if payload.get("failed") else "ok"
            print(
                f"  [plan_step_done] {payload.get('id')} {state}",
                flush=True,
            )
        elif event == "compose":
            print(
                f"  [compose] {payload.get('model')} ({payload.get('tier')})",
                flush=True,
            )
        elif event == "plan_accepted":
            print("  [plan] утверждён, выполняем шаги", flush=True)
        elif event == "plan_rejected":
            print(
                f"  [plan] отклонён ({payload.get('reason')}), обычный ответ",
                flush=True,
            )

    return _print


def _status_scoped(
    on_status: StatusCallback | None,
    scope: str,
    **extra: Any,
) -> StatusCallback | None:
    if on_status is None:
        return None

    def _wrapped(event: str, payload: dict[str, Any]) -> None:
        data = dict(payload)
        data["scope"] = scope
        for key, val in extra.items():
            data.setdefault(key, val)
        on_status(event, data)

    return _wrapped


def _step_token_cb(
    on_status: StatusCallback | None, step_id: int
) -> TokenCallback | None:
    """Токены шага — в on_status(plan_token), не в публичный on_token склейки."""
    if on_status is None:
        return None

    def _cb(text: str) -> None:
        if text:
            _emit(on_status, "plan_token", id=step_id, text=text)

    return _cb


def _step_tier(
    hint: str,
    *,
    force_tier: Tier | None,
    available: set[Tier],
) -> Tier:
    tier: Tier = hint if hint in ALL_TIERS else "mid"
    if force_tier and force_tier in ALL_TIERS and _rank(tier) < _rank(force_tier):
        tier = force_tier
    return _fit_tier(tier, available)


def _compose_tier(
    start_tier: Tier,
    *,
    force_tier: Tier | None,
    available: set[Tier],
) -> Tier:
    tier = start_tier if start_tier in ALL_TIERS else "mid"
    if _rank(tier) < _rank("mid"):
        tier = "mid"
    if force_tier and force_tier in ALL_TIERS and _rank(tier) < _rank(force_tier):
        tier = force_tier
    return _fit_tier(tier, available)


def _run_plan_pipeline(
    user_text: str,
    history: list[dict],
    *,
    decision: RouteDecision,
    force_tier: Tier | None,
    available: set[Tier],
    installed: set[str],
    stream: bool,
    on_token: TokenCallback | None,
    on_status: StatusCallback | None,
    effective_gen: GenOptions,
    on_plan_confirm: PlanConfirmCallback | None = None,
) -> OrchestraResult | None:
    """Декомпозиция. None — идём обычным воркером."""
    if not plan_mod.ENABLED:
        return None
    verdict = plan_mod.decide_need_plan(user_text)
    _emit(
        on_status,
        "plan",
        need_plan=verdict.need_plan,
        reason=verdict.reason,
        source=verdict.source,
    )
    if not verdict.need_plan:
        return None
    planner = plan_mod.resolve_planner_model(installed)
    max_steps = plan_mod.clamp_max_steps(plan_mod.MAX_STEPS)
    built = plan_mod.build_plan(user_text, model=planner, max_steps=max_steps)
    if built is None:
        _emit(
            on_status,
            "plan",
            need_plan=False,
            reason="bad-json" if planner else "planner-missing",
            source="fail-no",
        )
        return None
    _emit(
        on_status,
        "plan_ready",
        goal=built.goal,
        planner_model=planner or "calc-block",
        steps=built.to_meta(),
        awaiting=True,
    )
    action = "approve"
    if on_plan_confirm is not None:
        try:
            action = str(
                on_plan_confirm(
                    {
                        "goal": built.goal,
                        "planner_model": planner,
                        "steps": built.to_meta(),
                    }
                )
                or "reject"
            ).strip().lower()
        except Exception:  # noqa: BLE001
            action = "reject"
    if action != "approve":
        _emit(on_status, "plan_rejected", reason=action or "reject")
        return None
    _emit(on_status, "plan_accepted")

    step_results: list[dict[str, Any]] = []
    step_meta: list[dict[str, Any]] = []
    any_web = bool(decision.need_web)
    skip_block: str | None = None
    for i, step in enumerate(built.steps):
        if skip_block and step.block == skip_block:
            continue
        skip_block = None
        tier = _step_tier(step.tier_hint, force_tier=force_tier, available=available)
        prompt = step.prompt
        if step.block_role == "cpu" and step_results:
            prev_text = str(step_results[-1].get("text") or "")
            expr = pick_formula(prev_text)
            if expr:
                prompt = (
                    f"{prompt}\n\nГотовое выражение для tool calculate:\n{expr}"
                )
        _emit(
            on_status,
            "plan_step",
            index=i + 1,
            total=len(built.steps),
            id=step.id,
            title=step.title,
            tier=tier,
            block=step.block or None,
            block_title=step.block_title or None,
            block_role=step.block_role or None,
        )
        extra = plan_mod.step_system_extra(built.goal, step_results)
        if step.block_role == "formula":
            extra = (
                extra
                + "\n\nЭтот шаг — только формула. Не считай число, не вызывай calculate."
            )
        step_status = _status_scoped(on_status, "step", step_id=step.id, id=step.id)
        try:
            step_res = handle(
                prompt,
                [],
                force_tier=tier,
                stream=stream,
                verbose=False,
                on_token=_step_token_cb(on_status, step.id) if stream else None,
                on_status=step_status,
                gen=effective_gen,
                allow_plan=False,
                system_extra=extra,
                force_need_calc=bool(step.need_calc),
                force_need_web=bool(step.need_web),
            )
        except Exception as exc:  # noqa: BLE001
            note = str(exc)
            step_results.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "text": "",
                    "failed": True,
                    "note": note,
                }
            )
            step_meta.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "tier": tier,
                    "model": "",
                    "prompt": step.prompt,
                    "text": "",
                    "failed": True,
                    "note": note,
                    "block": step.block,
                    "block_title": step.block_title,
                    "block_role": step.block_role,
                }
            )
            _emit(
                on_status,
                "plan_step_done",
                id=step.id,
                index=i + 1,
                failed=True,
                note=note,
                tier=tier,
                text="",
                block=step.block or None,
                block_title=step.block_title or None,
                block_role=step.block_role or None,
            )
            if step.block_role == "formula":
                skip_block = step.block
            if i == 0:
                break
            continue
        text = step_res.text or ""
        if step.block_role == "cpu":
            prev_text = str(step_results[-1].get("text") or "") if step_results else ""
            text = ensure_computed(text, pick_formula(prev_text))
        failed = (not text.strip()) or any(
            p in {"empty", "error"} for p in (step_res.problems or [])
        )
        if step.block_role == "formula" and pick_formula(text):
            failed = False
        note = ", ".join(step_res.problems) if failed else ""
        step_results.append(
            {
                "id": step.id,
                "title": step.title,
                "text": text,
                "failed": failed,
                "note": note,
            }
        )
        step_meta.append(
            {
                "id": step.id,
                "title": step.title,
                "tier": step_res.tier,
                "model": step_res.model,
                "prompt": step.prompt,
                "text": text,
                "failed": failed,
                "note": note,
                "block": step.block,
                "block_title": step.block_title,
                "block_role": step.block_role,
            }
        )
        _emit(
            on_status,
            "plan_step_done",
            id=step.id,
            index=i + 1,
            failed=failed,
            note=note,
            model=step_res.model,
            tier=step_res.tier,
            text=text,
            block=step.block or None,
            block_title=step.block_title or None,
            block_role=step.block_role or None,
        )
        any_web = any_web or step_res.need_web or step.need_web
        if failed and step.block_role == "formula":
            skip_block = step.block
        if i == 0 and failed:
            break

    compose_tier = _compose_tier(
        decision.tier, force_tier=force_tier, available=available
    )
    _emit(on_status, "compose", tier=compose_tier)
    compose_extra = plan_mod.compose_system_extra(built.goal, step_results)
    composed: OrchestraResult | None = None
    try:
        composed = handle(
            user_text,
            history,
            force_tier=compose_tier,
            stream=stream,
            verbose=False,
            on_token=on_token,
            on_status=on_status,
            gen=effective_gen,
            allow_plan=False,
            force_need_calc=False,
            system_extra=compose_extra,
        )
    except Exception:  # noqa: BLE001
        composed = None
    if composed is None or not (composed.text or "").strip():
        fallback = plan_mod.fallback_compose_text(step_results)
        _emit_token(on_token, fallback)
        return OrchestraResult(
            text=fallback,
            tier=compose_tier,
            model=planner or "calc-block",
            need_web=any_web,
            route_reason=f"{decision.reason}+plan-fallback",
            checked=False,
            need_local_time=decision.need_local_time,
            need_calc=decision.need_calc,
            gen=effective_gen,
            planned=True,
            plan_goal=built.goal,
            plan_steps=step_meta,
        )
    return replace(
        composed,
        planned=True,
        plan_goal=built.goal,
        plan_steps=step_meta,
        route_reason=f"{decision.reason}+plan",
        need_web=any_web,
        need_local_time=decision.need_local_time,
        need_calc=decision.need_calc,
    )


def handle(
    user_text: str,
    history: list[dict] | None = None,
    *,
    force_tier: Tier | None = None,
    force_model: str | None = None,
    stream: bool = True,
    verbose: bool = True,
    on_token: TokenCallback | None = None,
    on_status: StatusCallback | None = None,
    gen: GenOptions | None = None,
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
    allow_plan: bool = True,
    system_extra: str | None = None,
    on_plan_confirm: PlanConfirmCallback | None = None,
    force_need_calc: bool | None = None,
    force_need_web: bool | None = None,
) -> OrchestraResult:
    history = history or []
    app_settings.ensure_bootstrapped()
    effective_gen = worker_gen(
        merge_gen(
            gen,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            num_predict=num_predict,
            repeat_penalty=repeat_penalty,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            stop=stop,
            keep_alive=keep_alive,
        )
    )
    if (
        history
        and history[-1].get("role") == "user"
        and (history[-1].get("content") or "").strip() == (user_text or "").strip()
    ):
        history = history[:-1]

    if stream and on_token is None and verbose:
        on_token = lambda t: print(t, end="", flush=True)  # noqa: E731
    if on_status is None and verbose:
        on_status = _cli_status_printer(verbose)

    with app_settings.runtime_lock:
        pool = [dict(e) for e in POOL]
        selfcheck_model = SELFCHECK_MODEL

    ollama_exc: Exception | None = None
    try:
        installed = set(installed_models())
        ollama_ok = True
    except Exception as exc:  # noqa: BLE001
        installed = set()
        ollama_ok = False
        ollama_exc = exc

    available_all = available_pool_entries(installed, pool=pool, routed_only=False)
    available_routed = available_pool_entries(installed, pool=pool, routed_only=True)
    available = {str(e["tier"]) for e in available_routed if e.get("tier")}
    if not available_all:
        if not ollama_ok and ollama_exc is not None:
            raise RuntimeError(f"Ollama недоступна: {ollama_exc}") from ollama_exc
        raise RuntimeError("Нет установленных моделей оркестра")

    forced_entry: dict[str, Any] | None = None
    decision: RouteDecision

    if force_model:
        entry = _entry_by_id(force_model, pool)
        if entry is None:
            raise ValueError(f"Модель пула не найдена: {force_model}")
        if not _entry_reachable(
            entry, installed, or_ok=app_settings.openrouter_configured()
        ):
            name = (entry.get("model") or "").strip()
            if (entry.get("provider") or "ollama") == "openrouter":
                raise ValueError(
                    f"OpenRouter-модель `{name}` недоступна. Задайте API-ключ "
                    f"или {app_settings.OPENROUTER_API_KEY_ENV}."
                )
            raise ValueError(
                f"Модель {name} не установлена. Установите: ollama pull {name}"
            )
        forced_entry = entry
        tier_for_force: Tier = str(entry.get("tier") or "mid")
        decision = RouteDecision(
            ok=True,
            tier=tier_for_force,
            need_web=need_web(user_text),
            reason=f"forced_model:{force_model}",
            reply="",
            need_local_time=need_local_time(user_text),
            need_calc=need_calc(user_text),
        )
    elif force_tier:
        if force_tier not in ALL_TIERS:
            raise ValueError(f"Неизвестный tier: {force_tier}")
        if force_tier not in available:
            raise ValueError(
                f"Тир «{force_tier}» без доступных моделей в пуле. "
                f"Назначьте модель с этим тиром или выберите другую."
            )
        forced_entry = _pick_entry_for_tier(force_tier, available_routed)
        decision = RouteDecision(
            ok=True,
            tier=force_tier,
            need_web=need_web(user_text),
            reason=f"forced:{force_tier}",
            reply="",
            need_local_time=need_local_time(user_text),
            need_calc=need_calc(user_text),
        )
    else:
        if not available:
            # Есть только модели без тира — Auto невозможен
            raise RuntimeError(
                "Нет моделей с тиром для Auto. Назначьте tier в пуле "
                "или выберите модель вручную."
            )
        decision = route(user_text)
        if decision.ok:
            fitted = _fit_tier(decision.tier, available)
            if fitted != decision.tier:
                decision = RouteDecision(
                    decision.ok,
                    fitted,
                    decision.need_web,
                    f"{decision.reason}+fit:{fitted}",
                    decision.reply if fitted == "tiny" else "",
                    need_local_time=decision.need_local_time,
                    need_calc=decision.need_calc,
                )

    def _resolve_entry(tier: Tier) -> dict[str, Any]:
        if forced_entry is not None and (
            force_model
            or (forced_entry.get("tier") or "") == tier
        ):
            # force_model: всегда эта запись; force_tier: пока не эскалировали
            if force_model:
                return forced_entry
        picked = _pick_entry_for_tier(tier, available_routed)
        if picked:
            return picked
        # fallback: любая available
        if available_routed:
            return max(
                available_routed,
                key=lambda e: (_pool_rank(e), str(e.get("id") or "")),
            )
        return available_all[0]

    start_entry = (
        forced_entry
        if force_model and forced_entry is not None
        else _resolve_entry(decision.tier)
    )
    start_model_name = (start_entry.get("model") or "").strip()

    _emit(
        on_status,
        "route",
        ok=decision.ok,
        tier=decision.tier,
        need_web=decision.need_web,
        need_local_time=decision.need_local_time,
        need_calc=decision.need_calc,
        reason=decision.reason,
        model=start_model_name,
        pool_id=start_entry.get("id"),
    )

    if not decision.ok:
        text = decision.reply or "Не понял запрос. Переформулируйте, пожалуйста."
        _emit_token(on_token, text)
        if stream and verbose:
            print()
        fb = _fit_tier("tiny", available) if available else decision.tier
        fb_entry = _resolve_entry(fb)
        return OrchestraResult(
            text=text,
            tier=fb,
            model=(fb_entry.get("model") or "").strip(),
            need_web=False,
            route_reason=decision.reason,
            checked=False,
            need_local_time=False,
            gen=effective_gen,
        )

    start_tier: Tier = decision.tier
    attempts: list[_Attempt] = []
    retry_hint: str | None = None
    # После эскалации force_model больше не держим
    lock_forced = bool(force_model)

    if decision.tier == "tiny" and decision.reply and not force_model:
        tiny_entry = _resolve_entry("tiny")
        tiny_name = (tiny_entry.get("model") or "").strip()
        verdict = selfcheck.check(
            user_text, decision.reply, model=None, expect_detail=False, use_llm=False
        )
        tiny_ctx = ContextPlan(
            history=[], num_ctx=NUM_CTX_MIN, use_history=False, reason="tiny-reply"
        )
        attempts.append(
            _Attempt(
                text=decision.reply,
                verdict=verdict,
                tier="tiny",
                model=tiny_name,
                ctx=tiny_ctx,
            )
        )
        _emit(
            on_status,
            "selfcheck",
            ok=verdict.ok,
            problems=verdict.problems,
            note=verdict.note,
            attempt=1,
            model=tiny_name,
            checked=verdict.checked,
        )
        if verdict.ok:
            _emit_token(on_token, decision.reply)
            if stream and verbose:
                print()
            return OrchestraResult(
                text=decision.reply,
                tier="tiny",
                model=tiny_name,
                need_web=False,
                route_reason=decision.reason,
                attempts=1,
                checked=verdict.checked,
                problems=list(verdict.problems),
                need_local_time=decision.need_local_time,
                need_calc=False,
                gen=effective_gen,
            )
        mid = _fit_tier("mid", available)
        mid_entry = _resolve_entry(mid)
        _emit(
            on_status,
            "retry",
            attempt=2,
            problems=verdict.problems,
            from_model=tiny_name,
            to_model=(mid_entry.get("model") or "").strip(),
        )
        retry_hint = verdict.hint
        decision = RouteDecision(
            True,
            mid,
            decision.need_web,
            decision.reason + " +recheck",
            "",
            need_local_time=decision.need_local_time,
            need_calc=decision.need_calc,
        )
        lock_forced = False

    if force_need_calc is not None:
        decision = replace(decision, need_calc=bool(force_need_calc))
    if force_need_web is not None:
        decision = replace(decision, need_web=bool(force_need_web))

    if allow_plan and not force_model and not attempts:
        planned = _run_plan_pipeline(
            user_text,
            history,
            decision=decision,
            force_tier=force_tier,
            available=available,
            installed=installed,
            stream=stream,
            on_token=on_token,
            on_status=on_status,
            effective_gen=effective_gen,
            on_plan_confirm=on_plan_confirm,
        )
        if planned is not None:
            if stream and verbose:
                print()
            return planned

    current_entry = (
        forced_entry
        if lock_forced and forced_entry is not None
        else _resolve_entry(decision.tier)
    )
    tier: Tier = str(current_entry.get("tier") or decision.tier)
    tool_cache: dict[str, str] = {}

    def _ctx_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
        pct = entry.get("ctx_overhead_pct")
        try:
            pct_i = int(pct) if pct is not None else 0
        except (TypeError, ValueError):
            pct_i = 0
        raw_max = entry.get("max_ctx")
        try:
            max_i = int(raw_max) if raw_max not in (None, "") else None
        except (TypeError, ValueError):
            max_i = None
        return {"ctx_overhead_pct": pct_i, "max_ctx": max_i}

    ctx_plan = plan_worker_context(
        user_text,
        history,
        tier,
        system_extra=system_extra,
        **_ctx_kwargs(current_entry),
    )
    _emit_context(on_status, ctx_plan, tier)
    attempt_base = len(attempts)

    for attempt in range(1, MAX_ATTEMPTS + 1 - attempt_base):
        model = (current_entry.get("model") or "").strip()
        provider = (current_entry.get("provider") or "ollama").strip() or "ollama"
        tier = str(current_entry.get("tier") or tier)
        attempt_no = attempt_base + attempt
        if attempt > 1:
            ctx_plan = plan_worker_context(
                user_text,
                history,
                tier,
                system_extra=system_extra,
                **_ctx_kwargs(current_entry),
            )
            _emit_context(on_status, ctx_plan, tier)
        _emit(
            on_status,
            "worker",
            model=model,
            provider=provider,
            need_web=decision.need_web,
            need_local_time=decision.need_local_time,
            need_calc=decision.need_calc,
            tier=tier,
            pool_id=current_entry.get("id"),
            attempt=attempt_no,
            num_ctx=ctx_plan.num_ctx,
            used_history=ctx_plan.use_history,
        )

        tool_list = _tools_for(decision)
        use_tools = bool(tool_list) and provider == "ollama"
        local_ctx = _local_context_for(
            decision, user_text, precompute=not use_tools
        )
        if use_tools:
            text = _worker_with_tools(
                model,
                user_text,
                ctx_plan.history,
                on_status=on_status,
                verbose=verbose,
                retry_hint=retry_hint,
                num_ctx=ctx_plan.num_ctx,
                tool_cache=tool_cache,
                provider=provider,
                local_context=local_ctx,
                gen=effective_gen,
                system_extra=system_extra,
                tools=tool_list,
            )
            if decision.need_calc:
                text = ensure_computed(text)
            _emit_token(on_token, text)
        else:
            stream_now = bool(stream) and not decision.need_calc
            text = _worker_plain(
                model,
                user_text,
                ctx_plan.history,
                stream=stream_now,
                on_token=on_token if stream_now else None,
                retry_hint=retry_hint,
                num_ctx=ctx_plan.num_ctx,
                provider=provider,
                local_context=local_ctx,
                gen=effective_gen,
                system_extra=system_extra,
            )
            if decision.need_calc:
                text = ensure_computed(text)
            if stream and not stream_now:
                _emit_token(on_token, text)

        review_model = _local_review_model(
            available_all,
            installed=installed,
            preferred_tier="mid",
            fallback=selfcheck_model,
        )
        verdict = selfcheck.check(
            user_text,
            text,
            model=review_model,
            expect_detail=tier != "tiny",
            use_llm=SELFCHECK_LLM,
        )
        attempts.append(
            _Attempt(text=text, verdict=verdict, tier=tier, model=model, ctx=ctx_plan)
        )
        _emit(
            on_status,
            "selfcheck",
            ok=verdict.ok,
            problems=verdict.problems,
            note=verdict.note,
            attempt=attempt_no,
            model=model,
            checked=verdict.checked,
        )

        if verdict.ok or attempt_no >= MAX_ATTEMPTS:
            break

        # force_model без тира — эскалировать некуда по rank-лестнице routed
        next_entry = _next_entry_by_rank(current_entry, available_routed)
        if next_entry is None:
            break

        _emit(
            on_status,
            "retry",
            attempt=attempt_no + 1,
            problems=verdict.problems,
            reason=verdict.summary(),
            from_model=model,
            to_model=(next_entry.get("model") or "").strip(),
        )
        retry_hint = verdict.hint
        current_entry = next_entry
        lock_forced = False

    best = _best_attempt(attempts)
    if best is not attempts[-1]:
        _emit(on_status, "restore", model=best.model, tier=best.tier)
        _emit_token(on_token, best.text)

    if stream and verbose:
        print()

    return OrchestraResult(
        text=best.text,
        tier=best.tier,
        model=best.model,
        need_web=decision.need_web,
        route_reason=decision.reason,
        escalated=_escalated(start_tier, best.tier)
        or (best.model != start_model_name and len(attempts) > 1),
        attempts=len(attempts),
        checked=best.verdict.checked,
        problems=list(best.verdict.problems),
        num_ctx=best.ctx.num_ctx,
        used_history=best.ctx.use_history,
        context_reason=best.ctx.reason,
        need_local_time=decision.need_local_time,
        need_calc=decision.need_calc,
        gen=effective_gen,
    )
