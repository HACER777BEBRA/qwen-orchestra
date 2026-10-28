"""Роутер: валидация запроса + выбор tier (10 фиксированных: tiny…frontier).

Решение принимает модель роутера (по умолчанию tiny), но выбор ограничен снизу
детерминированными правилами (`tier_floor`) — модель может поднять тир,
но не опустить ниже того, что требует запрос. При отсутствии тира воркер
подставляет ближайший доступный (`_fit_tier`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .calc import need_calc
from .llm import chat

# Tier — один из 10 фиксированных id (tiny…frontier). Обновляется через settings.apply_to_runtime.
Tier = str
# Лестница эскалации по размеру (coder — боковая ветка для кода)
TIER_ORDER: list[str] = [
    "tiny",
    "nano",
    "small",
    "mid",
    "large",
    "heavy",
    "xlarge",
    "ultra",
    "frontier",
]
TIER_RANK: dict[str, int] = {
    "tiny": 0,
    "nano": 1,
    "small": 2,
    "mid": 3,
    "large": 4,
    "heavy": 5,
    "xlarge": 6,
    "coder": 6,
    "ultra": 7,
    "frontier": 8,
}
ALL_TIERS: frozenset[str] = frozenset(TIER_RANK)
ROUTER_AUTO_TIERS: frozenset[str] = frozenset({"tiny", "mid", "heavy"})

ROUTE_MODEL = "qwen3.5:0.8b"
REVALIDATE_MODEL = "qwen3.5:4b"


def _route_tier_enums() -> list[str]:
    """Порядок тиров для JSON-схемы роутера (coder рядом с xlarge)."""
    out: list[str] = []
    for tid in TIER_ORDER:
        out.append(tid)
        if tid == "xlarge":
            out.append("coder")
    for tid in TIER_RANK:
        if tid not in out:
            out.append(tid)
    return out


ROUTE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "tier": {"type": "string", "enum": _route_tier_enums()},
        "need_web": {"type": "boolean"},
        "reason": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["ok", "tier", "need_web", "reason"],
}

# Перезаписывается settings.apply_to_runtime() из слотов; ниже — безопасный default
SYSTEM = """Ты роутер запросов. Отвечай ТОЛЬКО валидным JSON без markdown.

Поля:
- ok: false ТОЛЬКО если запрос пустой, бессмысленный набор символов или спам.
      Непонятный, странный или очень короткий вопрос — это ok=true.
- tier: tiny | nano | small | mid | large | heavy | xlarge | coder | ultra | frontier
- need_web: true если нужны свежие данные из интернета
- reason: коротко почему такой tier
- reply: если ok=false — вежливый отказ; если tier=tiny — полный короткий ответ; иначе пустая строка

Правила tier:
- tiny: приветствие, благодарность, прощание, простая арифметика, да/нет
- nano/small: чуть сложнее tiny, всё ещё коротко
- mid: объяснения, небольшой код, короткие тексты, советы, перевод
- large: между mid и heavy
- heavy: сложный анализ, архитектура, сравнение подходов, многошаговые планы,
  доказательства, длинные рассуждения (код/отладку не ставь в heavy)
- xlarge/ultra/frontier: только если запрос явно тяжелее heavy
- coder: отладка, большой код, сервисы, тесты — не heavy
- need_web=true => минимум mid
- Сомневаешься между двумя тирами — выбирай СТАРШИЙ."""


@dataclass
class RouteDecision:
    ok: bool
    tier: Tier
    need_web: bool
    reason: str
    reply: str = ""
    need_local_time: bool = False
    need_calc: bool = False
    # время решает сама модель воркера (tool get_local_time), без вызова tiny
    time_by_model: bool = False


from .router_detect import (  # noqa: E402
    _ALNUM_RE,
    _BRIEF_RE,
    _LOCAL_TIME_SCHEMA,
    _LOCAL_TIME_SYSTEM,
    _SOFT_WEB_RE,
    _clamp,
    _has_extra_auto,
    _is_coder,
    _looks_tiny,
    _looks_web,
    _rank,
    _tier_max,
    looks_meaningful,
    need_web,
    tier_ceiling,
    tier_floor,
)

def _resolve_local_router_model(*preferred: str) -> str | None:
    """Первая установленная Ollama-модель из preferred, иначе любая локальная из пула."""
    from . import orchestra
    from .llm import installed_models

    try:
        have = set(installed_models())
    except Exception:  # noqa: BLE001
        return None
    for name in preferred:
        if name and name in have:
            return name
    pool = getattr(orchestra, "POOL", None) or []
    ranked = sorted(
        (
            e
            for e in pool
            if (e.get("provider") or "ollama") == "ollama"
            and (e.get("model") or "").strip() in have
        ),
        key=lambda e: (int(e.get("rank") or 0), str(e.get("id") or "")),
    )
    if ranked:
        return (ranked[0].get("model") or "").strip() or None
    for tid in sorted(orchestra.MODELS.keys(), key=lambda t: (_rank(t), t)):
        if orchestra.PROVIDERS.get(tid, "ollama") != "ollama":
            continue
        name = orchestra.MODELS.get(tid) or ""
        if name in have:
            return name
    return None


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

def _ask_local_time(model: str, user_text: str, *, timeout: int = 60) -> bool | None:
    """Tiny/small: нужна ли дата/время с ПК. None — сбой вызова."""
    messages = [
        {"role": "system", "content": _LOCAL_TIME_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    try:
        msg = chat(
            model,
            messages,
            fmt=_LOCAL_TIME_SCHEMA,
            temperature=0.0,
            num_ctx=1024,
            keep_alive="10m",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    data = _extract_json(msg.get("content") or "")
    if not data:
        return None
    return bool(data.get("need_local_time", False))


def need_local_time(user_text: str) -> bool:
    """Нужны ли локальные часы ПК — решает только модель-роутер (tiny), без regex."""
    t = (user_text or "").strip()
    if len(t) < 2 or not _ALNUM_RE.search(t):
        return False
    ask_model = _resolve_local_router_model(ROUTE_MODEL)
    if not ask_model:
        return False
    decided = _ask_local_time(ask_model, t)
    return bool(decided)


def _heuristic(user_text: str) -> RouteDecision:
    """Запасной роут, если роутер-модель вернула мусор."""
    t = user_text.strip()
    if len(t) < 2:
        return RouteDecision(
            False, "tiny", False, "too short", "Напишите вопрос чуть подробнее."
        )

    web = _looks_web(t)
    calc = need_calc(t)
    local = True if web else need_local_time(t)
    if _looks_tiny(t) and not local and not calc:
        return RouteDecision(True, "tiny", False, "heuristic:greeting", "")

    return RouteDecision(
        True,
        tier_floor(t),
        web,
        "heuristic:floor",
        need_local_time=local,
        need_calc=calc,
    )


def _ask_router(model: str, user_text: str, *, timeout: int) -> dict | None:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_text},
    ]
    try:
        msg = chat(
            model,
            messages,
            fmt=ROUTE_SCHEMA,
            temperature=0.0,
            num_ctx=2048,
            keep_alive="10m",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    return _extract_json(msg.get("content") or "")


def _tiny_shortcut_reply(user_text: str) -> str | None:
    t = user_text.strip().lower()
    if any(k in t for k in ("спасибо", "благодар")) or re.search(
        r"(?:^|[^\w])thanks(?:[^\w]|$)", t
    ):
        return "Пожалуйста!"
    if any(k in t for k in ("пока",)) or re.search(r"(?:^|[^\w])bye(?:[^\w]|$)", t):
        return "Пока!"
    if any(k in t for k in ("привет", "здравствуй", "hello")) or re.search(
        r"(?:^|[^\w])(?:hi|hey)(?:[^\w]|$)", t
    ):
        return "Привет! Чем могу помочь?"
    return None


def route(user_text: str) -> RouteDecision:
    from . import settings as app_settings

    app_settings.ensure_bootstrapped()
    t = (user_text or "").strip()
    if len(t) < 2 or not _ALNUM_RE.search(t):
        return RouteDecision(
            False, "tiny", False, "invalid:empty", "Не понял ввод. Напишите вопрос текстом."
        )

    floor = tier_floor(user_text)
    web = need_web(user_text)
    calc = need_calc(user_text)

    # Жёсткие лёгкие кейсы — не спрашиваем модели
    if floor == "tiny" and _looks_tiny(user_text) and not calc:
        reply = _tiny_shortcut_reply(user_text)
        if reply:
            return RouteDecision(True, "tiny", False, "tiny-shortcut", reply)

    # Свежие данные — без LLM-роутера; mid+ с учётом floor.
    # В контекст воркера всегда кладём часы ПК (дата новости / «сейчас»).
    if web:
        return RouteDecision(
            True,
            _tier_max("mid", floor),
            True,
            "web-shortcut",
            "",
            need_local_time=True,
            need_calc=calc,
        )

    # Часы ПК: отдельный вызов tiny только когда floor=tiny (иначе лишняя загрузка
    # 0.8b на 8 ГБ). Для mid+ воркер и так получает короткую строку с датой.
    local = need_local_time(user_text) if floor == "tiny" else False
    if floor == "coder":
        return RouteDecision(
            True, "coder", False, "floor:coder", "", need_local_time=local, need_calc=calc
        )
    if floor == "xlarge":
        return RouteDecision(
            True, "xlarge", False, "floor:xlarge", "", need_local_time=local, need_calc=calc
        )
    # mid/heavy без расширенного auto — детерминированно (экономия VRAM)
    if floor == "heavy" and not _has_extra_auto():
        return RouteDecision(
            True, "heavy", False, "floor:heavy", "", need_local_time=local, need_calc=calc
        )
    if floor == "mid" and not _has_extra_auto():
        return RouteDecision(
            True,
            _clamp("mid", user_text),
            False,
            "floor:mid",
            "",
            need_local_time=local,
            need_calc=calc,
        )
    if local and not _has_extra_auto() and not calc:
        return RouteDecision(
            True,
            _clamp(floor, user_text),
            False,
            "local-time",
            "",
            need_local_time=True,
            need_calc=calc,
        )
    if calc and not _has_extra_auto():
        return RouteDecision(
            True,
            _clamp(floor, user_text),
            False,
            "calc-shortcut",
            "",
            need_local_time=local,
            need_calc=True,
        )

    ask_model = _resolve_local_router_model(ROUTE_MODEL)
    if not ask_model:
        return _heuristic(user_text)

    data = _ask_router(ask_model, user_text, timeout=120)
    if not data:
        return _heuristic(user_text)

    ok = bool(data.get("ok", True))
    tier_raw = str(data.get("tier") or "mid").lower().strip()
    if tier_raw in ALL_TIERS:
        tier: Tier = tier_raw
    else:
        tier = "mid" if "mid" in ALL_TIERS else next(iter(sorted(ALL_TIERS, key=_rank)), "mid")
    need = web or (
        bool(data.get("need_web", False)) and bool(_SOFT_WEB_RE.search(user_text))
    )
    reason = " ".join(str(data.get("reason") or "").split())[:80]
    reply = str(data.get("reply") or "").strip()

    # Валидация tiny часто ложно срабатывает: осмысленный запрос не отклоняем.
    # Её оценке тира в этом случае тоже не верим — берём floor.
    if not ok and looks_meaningful(user_text):
        ok = True
        tier = floor
        reason = "override:valid"
        reply = ""
    elif not ok:
        # ввод похож на мусор — второе мнение у mid (или ближайшей локальной), прежде чем отказывать
        revalidate = _resolve_local_router_model(REVALIDATE_MODEL, ROUTE_MODEL)
        second = _ask_router(revalidate, user_text, timeout=180) if revalidate else None
        if second and bool(second.get("ok", True)):
            ok = True
            tier = floor
            reason = "revalidated"
            reply = str(second.get("reply") or "").strip()

    if not ok:
        return RouteDecision(
            False,
            "tiny",
            False,
            reason or "invalid",
            reply or "Не понял запрос. Переформулируйте, пожалуйста.",
        )

    # Модель могла недооценить сложность — поднимаем до минимума
    tier = _clamp(tier, user_text)
    if need:
        tier = _tier_max(tier, "mid")
    if tier != "tiny":
        reply = ""

    return RouteDecision(
        ok,
        tier,
        need,
        reason or f"floor:{floor}",
        reply,
        need_local_time=local or need,
        need_calc=calc,
    )
