"""Эвристики: нужен ли план (skip/floor/decide)."""
from __future__ import annotations

import json
import re
from typing import Any

from .calc import need_calc, pick_formula
from .llm import chat
from .plan import (
    CLASSIFIER_SYSTEM,
    DEFAULT_CLASSIFIER_SYSTEM,
    NEED_PLAN_SCHEMA,
    NeedPlan,
    classifier_system,
)
from .router import ROUTE_MODEL, _BRIEF_RE, _extract_json, _is_coder, _looks_tiny, _resolve_local_router_model

_EXPLICIT_PLAN_RE = re.compile(
    r"(?:"
    r"по\s+шагам|по\s+пунктам|пошагов|"
    r"составь\s+план|состави\s+план|разбей\s+на|"
    r"сделай\s+вс[её]\s+из\s+(?:списка|пунктов)|"
    r"step\s+by\s+step|make\s+a\s+plan|break\s+(?:it|this)\s+down"
    r")",
    re.IGNORECASE,
)

_EXPLICIT_COMPOUND_RE = re.compile(
    r"(?:"
    r"два\s+(?:разн(?:ых|ые)\s+)?(?:запрос|вопрос|задач)|"
    r"несколько\s+(?:запрос|вопрос|задач)|"
    r"разн(?:ые|ых)\s+(?:запрос|вопрос)|"
    r"отдельн(?:ые|ых|о)\s+(?:запрос|вопрос|задач)|"
    r"two\s+(?:different\s+)?(?:quer(?:y|ies)|questions|tasks)|"
    r"separate\s+(?:queries|questions|tasks)"
    r")",
    re.IGNORECASE,
)

_COMPARE_RE = re.compile(
    r"(?:"
    r"сравн|"
    r"versus|\bvs\.?\b|"
    r"разниц|"
    r"отлич|"
    r"между\s+\S.{0,60}?\s+и\s+"
    r")",
    re.IGNORECASE,
)

_STRONG_CONJ_RE = re.compile(
    r"\s+(?:а\s+также|а\s+ещё|и\s+ещё|плюс|as\s+well\s+as|and\s+also|and\s+then)\s+",
    re.IGNORECASE,
)

_AND_RE = re.compile(r"\s+и\s+|\s+and\s+", re.IGNORECASE)

_ETC_TAIL_RE = re.compile(
    r"^(?:т\.?\s*д\.?|так\s+далее|пр\.|etc\.?)\b",
    re.IGNORECASE,
)

_LEAD_FILLER_RE = re.compile(
    r"^(?:"
    r"я\s+хочу(?:\s+чтобы(?:\s+ты)?)?|"
    r"хочу(?:\s+чтобы(?:\s+ты)?)?|"
    r"пожалуйста|"
    r"можешь(?:\s+ли)?(?:\s+ты)?|"
    r"прошу(?:\s+тебя)?|"
    r"(?:мне\s+)?(?:нужно|надо)|"
    r"i\s+(?:want|need)\s+(?:you\s+to\s+)?|"
    r"please|"
    r"can\s+you"
    r")\s+",
    re.IGNORECASE,
)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

_LIST_ITEM_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+[\.\)]\s+\S|[-*•]\s+\S)",
    re.MULTILINE,
)

_META_STEP_RE = re.compile(
    r"^(?:подумай|проверь\s+себя|самопровер|собери\s+ответ|итоговый\s+ответ|"
    r"think(?:ing)?|self[- ]?check|compose|summarize)$",
    re.IGNORECASE,
)

_HOW_TO_RE = re.compile(
    r"^(?:как\s+(?:сделать|настроить|установить|пользоваться|работает)|"
    r"что\s+такое|how\s+to|what\s+is)\b",
    re.IGNORECASE,
)


def _list_item_count(text: str) -> int:
    return len(_LIST_ITEM_RE.findall(text or ""))


def _question_count(text: str) -> int:
    return (text or "").count("?")


def _strip_lead(text: str) -> str:
    t = (text or "").strip()
    for _ in range(3):
        nxt = _LEAD_FILLER_RE.sub("", t, count=1).strip()
        if nxt == t:
            break
        t = nxt
    return t


def _first_clause(text: str) -> str:
    parts = _SENT_SPLIT_RE.split((text or "").strip(), maxsplit=1)
    return (parts[0] if parts else text).strip()


def _last_clause(text: str) -> str:
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(text or "") if p.strip()]
    return parts[-1] if parts else (text or "").strip()


def _substantial_sentences(text: str) -> list[str]:
    out: list[str] = []
    for part in _SENT_SPLIT_RE.split(text or ""):
        p = part.strip()
        if len(p) < 12:
            continue
        if _EXPLICIT_COMPOUND_RE.search(p) or _EXPLICIT_PLAN_RE.search(p):
            continue
        out.append(p)
    return out


_MIN_SIDE_CHARS = 8
_MIN_SIDE_WORDS = 2


def _side_norm(text: str) -> str:
    return (text or "").strip().rstrip("?!.,;:").strip()


def _sides_ok(left: str, right: str) -> bool:
    a = _side_norm(_strip_lead(_last_clause(left)))
    b = _side_norm(_strip_lead(_first_clause(right)))
    if _ETC_TAIL_RE.match(b):
        return False
    # «чай и кофе» / «яблоки и груши» — по одному короткому слову, не два запроса.
    wa, wb = a.split(), b.split()
    if len(a) < _MIN_SIDE_CHARS or len(b) < _MIN_SIDE_CHARS:
        return False
    if len(wa) < _MIN_SIDE_WORDS or len(wb) < _MIN_SIDE_WORDS:
        return False
    return True


def _split_sides(pattern: re.Pattern[str], text: str) -> tuple[str, str] | None:
    m = pattern.search(text)
    if not m:
        return None
    left, right = text[: m.start()].strip(), text[m.end() :].strip()
    if _sides_ok(left, right):
        return left, right
    return None


def coordinated_atomics(text: str) -> bool:
    """Два независимых объекта/вопроса через «и» / «а также» / «and»."""
    t = (text or "").strip()
    if not t or _COMPARE_RE.search(t):
        return False
    if _split_sides(_STRONG_CONJ_RE, t):
        return True
    best_min = 0
    for m in _AND_RE.finditer(t):
        left, right = t[: m.start()].strip(), t[m.end() :].strip()
        if not _sides_ok(left, right):
            continue
        a = _side_norm(_strip_lead(_last_clause(left)))
        b = _side_norm(_strip_lead(_first_clause(right)))
        best_min = max(best_min, min(len(a), len(b)))
    return best_min >= _MIN_SIDE_CHARS


def _compound_certain(text: str) -> str | None:
    """Детерминированный пол: точно два+ атомарных вопроса."""
    t = (text or "").strip()
    if not t:
        return None
    if _EXPLICIT_PLAN_RE.search(t):
        return "explicit"
    if _EXPLICIT_COMPOUND_RE.search(t):
        return "compound-explicit"
    if _list_item_count(t) >= 2:
        return "list"
    if _question_count(t) >= 2:
        return "multi-q"
    if coordinated_atomics(t):
        return "compound"
    return None


def _compound_maybe(text: str) -> bool:
    """Слабый сигнал: классификатору есть что решать."""
    t = (text or "").strip()
    if not t:
        return False
    if _compound_certain(t):
        return True
    if _STRONG_CONJ_RE.search(t):
        return True
    if _AND_RE.search(t) and len(t) >= 40:
        return True
    return len(_substantial_sentences(t)) >= 2


def skip_plan(user_text: str) -> str | None:
    """Почему план точно не нужен, иначе None."""
    t = (user_text or "").strip()
    if not t:
        return "empty"
    if need_calc(t):
        # Составной запрос всё равно в план; голое выражение — без плана (CPU).
        if _compound_certain(t) or _compound_maybe(t):
            return None
        if pick_formula(t):
            return "calc-direct"
        return None
    if _looks_tiny(t):
        return "tiny"
    if _BRIEF_RE.search(t):
        return "brief"
    if _compound_certain(t) or _compound_maybe(t):
        return None
    if _is_coder(t):
        return "coder-single"
    if _HOW_TO_RE.search(t):
        return "short-howto"
    # Один вопрос / одна задача — не план («какого цвета яблоки?»).
    return "single-task"


def floor_plan(user_text: str) -> str | None:
    """Почему план обязателен (детерминированный пол), иначе None."""
    t = (user_text or "").strip()
    if skip_plan(t):
        return None
    if need_calc(t):
        if pick_formula(t) and not (_compound_certain(t) or _compound_maybe(t)):
            return None
        return "calc"
    return _compound_certain(t)


def _ask_need_plan(model: str, user_text: str, *, timeout: int = 60) -> bool | None:
    messages = [
        {"role": "system", "content": classifier_system()},
        {"role": "user", "content": user_text},
    ]
    try:
        msg = chat(
            model,
            messages,
            fmt=NEED_PLAN_SCHEMA,
            temperature=0.0,
            num_ctx=2048,
            keep_alive="10m",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    data = _extract_json(msg.get("content") or "")
    if not data:
        return None
    return bool(data.get("need_plan", False))


def decide_need_plan(user_text: str) -> NeedPlan:
    """Нужна ли декомпозиция. Сомнение / сбой tiny → нет."""
    t = (user_text or "").strip()
    skipped = skip_plan(t)
    if skipped:
        return NeedPlan(False, skipped, source="heuristic-no")
    forced = floor_plan(t)
    if forced:
        return NeedPlan(True, forced, source="heuristic-yes")

    ask_model = _resolve_local_router_model(ROUTE_MODEL)
    if not ask_model:
        return NeedPlan(False, "no-classifier", source="fail-no")
    decided = _ask_need_plan(ask_model, t)
    if decided is None:
        return NeedPlan(False, "classifier-fail", source="fail-no")
    if decided:
        return NeedPlan(True, "llm", source="llm")
    return NeedPlan(False, "llm", source="llm")

