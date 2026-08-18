"""Оценка токенов, история чата и plan_worker_context."""

from __future__ import annotations

import re

from .orchestra import (
    CTX_ALIGN,
    CTX_OUT_CODE,
    CTX_OUT_NORMAL,
    CTX_OUT_SHORT,
    CTX_SAFETY_FLAT,
    CTX_TEMPLATE_TOKENS,
    HISTORY_FULL,
    HISTORY_LIGHT,
    NUM_CTX_MAX,
    NUM_CTX_MIN,
    WORKER_SYSTEM,
    ContextPlan,
    _COMPLEX_TIERS,
)
from .router import Tier

_CODEISH_RE = re.compile(
    r"```|\bdef\s|\bclass\s|\bfunction\s|напиши\s+(код|функци|скрипт|класс|программ)|"
    r"реализуй|запрограммируй|сгенерируй\s+код|\btraceback\b|\bstack\s+trace\b",
    re.IGNORECASE,
)
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
    if re.search(r"\b(?:исправь|поправь|переделай|доработай)\b", t, re.IGNORECASE) and "```" not in t:
        return True, "edit-prev"
    if len(t) < 48:
        return True, "short-followup"
    if "```" in t or len(t) >= 160:
        return False, "self-contained"
    return False, "standalone"


def _estimate_tokens(text: str) -> int:
    """Оценка токенов: кириллица ~3 симв., латиница/прочее ~4, CJK ~1."""
    s = text or ""
    if not s:
        return 0
    cyr = lat = cjk = 0
    for ch in s:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF or ch in "Ёё":
            cyr += 1
        elif ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            lat += 1
        elif (
            0x3040 <= o <= 0x30FF
            or 0x3400 <= o <= 0x4DBF
            or 0x4E00 <= o <= 0x9FFF
            or 0xF900 <= o <= 0xFAFF
            or 0xAC00 <= o <= 0xD7AF
        ):
            cjk += 1
    other = max(0, len(s) - cyr - lat - cjk)
    return max(1, (cyr + 2) // 3 + (lat + 3) // 4 + cjk + (other + 3) // 4)


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
    safety = max(CTX_SAFETY_FLAT, base // 10)
    need = _ceil_align(base + safety)
    return min(NUM_CTX_MAX, max(NUM_CTX_MIN, need))


def _trim_history_to_fit(
    user_text: str, history: list[dict], *, out_reserve: int
) -> list[dict]:
    """Приоритет — полный текущий запрос; историю урезаем с начала, если не влезает."""
    max_prompt = NUM_CTX_MAX - CTX_TEMPLATE_TOKENS - out_reserve - CTX_SAFETY_FLAT
    max_prompt = max(64, (max_prompt * 9) // 10)
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
