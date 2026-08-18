"""Декомпозиция сложного запроса: классификатор → JSON-план → сжатие итогов шагов.

Нативное think у Qwen3.5 не включаем: JSON-планировщик и классификатор
ходят через chat(..., temperature=0) с дефолтным think=false.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import chat
from .calc import need_calc, pick_formula
from .router import (
    ALL_TIERS,
    ROUTE_MODEL,
    TIER_RANK,
    _BRIEF_RE,
    _extract_json,
    _is_coder,
    _looks_tiny,
    _resolve_local_router_model,
)

DEFAULT_PLANNER_MODEL = "qwen3.5:4b"
DEFAULT_MAX_STEPS = 4
MIN_PLAN_STEPS = 2
MAX_STEPS_CEILING = 6
PLAN_CONFIRM_TIMEOUT = 600
STEP_SUMMARY_CHARS = 1200
COMPOSE_RESULT_CHARS = 2500

# Runtime: settings.apply_to_runtime
ENABLED = True
MAX_STEPS = DEFAULT_MAX_STEPS
PLANNER_MODEL = DEFAULT_PLANNER_MODEL
CLASSIFIER_SYSTEM = ""
PLANNER_SYSTEM = ""
COMPOSER_SYSTEM = ""

NEED_PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "need_plan": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["need_plan"],
}

PLANNER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "prompt": {"type": "string"},
                    "tier_hint": {
                        "type": "string",
                        "enum": [
                            "tiny",
                            "nano",
                            "small",
                            "mid",
                            "large",
                            "heavy",
                            "xlarge",
                            "coder",
                            "ultra",
                            "frontier",
                        ],
                    },
                    "need_web": {"type": "boolean"},
                },
                "required": ["title", "prompt"],
            },
        },
    },
    "required": ["steps"],
}

DEFAULT_CLASSIFIER_SYSTEM = """Ты классификатор. Реши, нужно ли разбить запрос на
отдельные шаги (план), которые разные модели выполнят по очереди, а потом склеят.

need_plan=true если сообщение можно честно разделить на ДВА+ независимых
атомарных вопроса/задачи. Даже если они связаны союзом «и» / «а также» / «плюс»
и нет знака вопроса. Примеры true:
- курс доллара И ботаническое описание одуванчика
- найди погоду в Москве и курс евро
- список из 2+ пунктов, два отдельных вопроса, «по шагам»

need_plan=false если это ОДИН вопрос, даже с «и» внутри одной темы:
- какого цвета яблоки? / какого цвета яблоки и груши? (одно сравнение)
- что такое X, how-to, перевод, приветствие, один кусок кода

Вычисления оркестр сам обернёт в блок «формула + счёт ПК» — классификатору
не нужно решать за него.

Один пункт в плане — это не план. Сомневаешься — false.

Ответь ТОЛЬКО JSON: {"need_plan": bool, "reason": "кратко"}."""

DEFAULT_PLANNER_SYSTEM = """Ты планировщик задач для оркестра LLM. Разбей запрос
пользователя на самостоятельные атомарные шаги. Отвечай ТОЛЬКО валидным JSON
без markdown.

Поля:
- goal: одно предложение — что получить в конце
- steps: массив шагов по порядку
  - id: номер с 1
  - title: короткий заголовок для UI (до 80 символов)
  - prompt: полный самодостаточный запрос к воркеру (без «см. выше»)
  - tier_hint: tiny|nano|small|mid|large|heavy|xlarge|coder|ultra|frontier
  - need_web: true только если этому шагу нужен интернет

Правила:
- Если в запросе ДВА+ независимых вопроса (в т.ч. через «и»: курс валюты и
  описание растения) — по шагу на каждый, потом оркестр сам склеит ответы.
- Один вопрос / одна тема — не дроби: верни steps из одного элемента
  (оркестр тогда ответит без плана). «яблоки и груши какого цвета» — один шаг.
- Не плоди «подумай», «проверь себя», «собери ответ» — сборку сделает другая модель.
- Код/отладка → coder. Свежие факты (курс, новости) → need_web=true и минимум mid.
- Вычисления не дроби: оркестр сам добавит неделимый блок «Формула» + «Счёт ПК».
  Не делай шаги «посчитай» / «проверь расчёт».
- Обычные объяснения → mid. Сложный анализ без кода → heavy.
- Каждый prompt — законченное задание на языке пользователя.
- Не больше указанного лимита шагов."""

DEFAULT_COMPOSER_SYSTEM = """Ниже — цель и результаты шагов плана. Ответь на
исходный вопрос пользователя цельным текстом на его языке.

Не перечисляй «Шаг 1 / Шаг 2», если пользователь это не просил.
Не упоминай план, оркестр и самопроверку.
Если среди шагов есть результат счёта на ПК — используй это число, не пересчитывай.
Опирайся только на результаты шагов; пробелы помечай честно, не выдумывай."""


@dataclass
class NeedPlan:
    need_plan: bool
    reason: str
    source: str = "heuristic"


@dataclass
class PlanStep:
    id: int
    title: str
    prompt: str
    tier_hint: str = "mid"
    need_web: bool = False
    need_calc: bool = False
    block: str = ""
    block_title: str = ""
    block_role: str = ""

    def to_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "tier_hint": self.tier_hint,
            "need_web": self.need_web,
            "need_calc": self.need_calc,
        }
        if self.block:
            meta["block"] = self.block
            meta["block_title"] = self.block_title or self.block
            meta["block_role"] = self.block_role
        return meta


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)

    def to_meta(self) -> list[dict[str, Any]]:
        return [s.to_meta() for s in self.steps]


def classifier_system() -> str:
    return (CLASSIFIER_SYSTEM or "").strip() or DEFAULT_CLASSIFIER_SYSTEM


def planner_system(*, max_steps: int) -> str:
    base = (PLANNER_SYSTEM or "").strip() or DEFAULT_PLANNER_SYSTEM
    return f"{base}\n\nЛимит шагов: не больше {max_steps}."


def composer_system() -> str:
    return (COMPOSER_SYSTEM or "").strip() or DEFAULT_COMPOSER_SYSTEM


def clamp_max_steps(raw: Any, default: int = DEFAULT_MAX_STEPS) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(MIN_PLAN_STEPS, min(MAX_STEPS_CEILING, n))


from .plan_detect import (  # noqa: E402
    _META_STEP_RE,
    _compound_certain,
    coordinated_atomics,
    decide_need_plan,
    floor_plan,
    skip_plan,
)


def _fit_hint(raw: Any) -> str:
    name = str(raw or "mid").strip().lower()
    if name in ALL_TIERS:
        return name
    return "mid"


def parse_plan(raw: Any, *, max_steps: int = DEFAULT_MAX_STEPS) -> Plan | None:
    """Разобрать JSON планировщика. None — битый / пустой план."""
    if isinstance(raw, str):
        raw = _extract_json(raw)
    if not isinstance(raw, dict):
        return None
    limit = clamp_max_steps(max_steps)
    goal = str(raw.get("goal") or "").strip()
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        return None

    steps: list[PlanStep] = []
    for i, item in enumerate(steps_raw):
        if len(steps) >= limit:
            break
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        title = str(item.get("title") or "").strip() or f"Шаг {len(steps) + 1}"
        if not prompt:
            continue
        if _META_STEP_RE.match(title.strip()) or _META_STEP_RE.match(prompt[:40].strip()):
            continue
        try:
            sid = int(item.get("id") or (len(steps) + 1))
        except (TypeError, ValueError):
            sid = len(steps) + 1
        if sid <= 0:
            sid = len(steps) + 1
        steps.append(
            PlanStep(
                id=sid,
                title=title[:80],
                prompt=prompt,
                tier_hint=_fit_hint(item.get("tier_hint")),
                need_web=bool(item.get("need_web")),
            )
        )

    if len(steps) < MIN_PLAN_STEPS:
        return None
    for i, step in enumerate(steps, start=1):
        step.id = i
    return Plan(goal=goal or "Ответить на запрос", steps=steps)


def make_plan(
    user_text: str,
    *,
    model: str,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout: int = 120,
) -> Plan | None:
    """JSON-план от выбранной модели. None при сбое / мусоре."""
    name = (model or "").strip()
    if not name:
        return None
    limit = clamp_max_steps(max_steps)
    messages = [
        {"role": "system", "content": planner_system(max_steps=limit)},
        {"role": "user", "content": user_text},
    ]
    try:
        msg = chat(
            name,
            messages,
            fmt=PLANNER_SCHEMA,
            temperature=0.0,
            num_ctx=4096,
            keep_alive="10m",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return None
    return parse_plan(msg.get("content") or "", max_steps=limit)


CALC_BLOCK = "calc"
CALC_BLOCK_TITLE = "Расчёт"

_LLM_CALC_STEP_RE = re.compile(
    r"формул|посчитай|вычисли|calculate|счёт\s*пк|калькулятор|compute",
    re.IGNORECASE,
)


def calc_block_steps(user_text: str, *, start_id: int = 1) -> list[PlanStep]:
    """Неделимый блок: сначала формула, затем счёт на CPU."""
    q = (user_text or "").strip()
    formula = PlanStep(
        id=start_id,
        title="Формула",
        prompt=(
            f"Исходная задача:\n{q}\n\n"
            "Подбери точную математическую формулу и подставь все числа из условия. "
            "Не считай результат и не округляй промежуточно. "
            "В ответе: коротко какая это формула и одна отдельная строка с выражением "
            "из чисел и операций + - * / ** () (степень как ** или ^)."
        ),
        tier_hint="mid",
        need_web=False,
        need_calc=False,
        block=CALC_BLOCK,
        block_title=CALC_BLOCK_TITLE,
        block_role="formula",
    )
    cpu = PlanStep(
        id=start_id + 1,
        title="Счёт ПК",
        prompt=(
            f"Исходная задача:\n{q}\n\n"
            "Посчитай выражение из предыдущего шага «Формула» на процессоре ПК. "
            "Вызови tool calculate. Не считай в уме. "
            "В ответе — число из результата tool и что оно означает для задачи."
        ),
        tier_hint="mid",
        need_web=False,
        need_calc=True,
        block=CALC_BLOCK,
        block_title=CALC_BLOCK_TITLE,
        block_role="cpu",
    )
    return [formula, cpu]


def _renumber(steps: list[PlanStep]) -> list[PlanStep]:
    for i, step in enumerate(steps, start=1):
        step.id = i
    return steps


def attach_calc_block(
    user_text: str,
    plan: Plan | None,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Plan:
    """Вставить блок формулы+счёта; чужие «посчитай»-шаги LLM убрать."""
    limit = clamp_max_steps(max_steps)
    kept: list[PlanStep] = []
    if plan:
        for step in plan.steps:
            blob = f"{step.title} {step.prompt[:80]}"
            if _LLM_CALC_STEP_RE.search(blob):
                continue
            kept.append(step)
    room = max(0, limit - 2)
    kept = kept[:room]
    formula = pick_formula(user_text)
    if formula:
        cpu = calc_block_steps(user_text, start_id=len(kept) + 1)[1]
        cpu.prompt = (
            f"{cpu.prompt}\n\nГотовое выражение для tool calculate:\n{formula}"
        )
        kept.append(cpu)
    else:
        kept.extend(calc_block_steps(user_text, start_id=len(kept) + 1))
    steps = _renumber(kept)
    goal = (plan.goal if plan else "") or "Найти формулу и посчитать на процессоре ПК"
    return Plan(goal=goal, steps=steps)


def build_plan(
    user_text: str,
    *,
    model: str | None,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout: int = 120,
) -> Plan | None:
    """Собрать план: LLM при составном запросе, блок расчёта — всегда из кода."""
    t = (user_text or "").strip()
    calc = need_calc(t)
    compound = bool(_compound_certain(t))
    llm_plan = None
    want_llm = bool(model) and (compound or not calc)
    if want_llm:
        llm_plan = make_plan(t, model=model or "", max_steps=max_steps, timeout=timeout)
    if calc:
        return attach_calc_block(t, llm_plan, max_steps=max_steps)
    return llm_plan


def resolve_planner_model(installed: set[str] | None = None) -> str | None:
    """Выбранная planner_model, если она есть в Ollama."""
    from .llm import installed_models

    name = (PLANNER_MODEL or "").strip()
    if not name:
        return None
    try:
        have = installed if installed is not None else set(installed_models())
    except Exception:  # noqa: BLE001
        return None
    if name in have:
        return name
    if any(m == name or m.startswith(name + ":") for m in have):
        return name
    return None


def _clip(text: str, limit: int) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 20)].rstrip() + "\n...[обрезано]"


def compress_step_results(
    goal: str,
    results: list[dict[str, Any]],
    *,
    per_step: int = STEP_SUMMARY_CHARS,
) -> str:
    """Краткий блок для system следующего шага / сборки."""
    lines = [f"Цель плана: {goal.strip() or 'ответить на запрос'}"]
    if not results:
        return "\n".join(lines)
    lines.append("Результаты предыдущих шагов:")
    for item in results:
        title = str(item.get("title") or f"Шаг {item.get('id') or '?'}")
        text = str(item.get("text") or "").strip()
        failed = bool(item.get("failed"))
        if failed or not text:
            note = str(item.get("note") or "шаг не удался")
            lines.append(f"- {title}: {note}")
            continue
        lines.append(f"- {title}:\n{_clip(text, per_step)}")
    return "\n".join(lines)


def step_system_extra(goal: str, previous: list[dict[str, Any]]) -> str:
    body = compress_step_results(goal, previous, per_step=STEP_SUMMARY_CHARS)
    return (
        "Ты выполняешь один шаг плана. Ответь только на свой подзапрос, "
        "без итогового отчёта по всему заданию.\n\n" + body
    )


def compose_system_extra(goal: str, results: list[dict[str, Any]]) -> str:
    body = compress_step_results(goal, results, per_step=COMPOSE_RESULT_CHARS)
    return composer_system() + "\n\n" + body


def fallback_compose_text(results: list[dict[str, Any]]) -> str:
    """Без сырых ответов шагов: заголовки и пометки о сбоях."""
    lines: list[str] = []
    for item in results:
        title = str(item.get("title") or f"Шаг {item.get('id') or '?'}")
        failed = bool(item.get("failed")) or not str(item.get("text") or "").strip()
        if failed:
            note = str(item.get("note") or "шаг не удался")
            lines.append(f"- **{title}**: {note}")
        else:
            lines.append(f"- **{title}**: готово")
    if not lines:
        return "Не удалось выполнить план."
    return "Не удалось склеить ответ. Кратко по шагам:\n" + "\n".join(lines)


def rank_of(tier: str) -> int:
    return int(TIER_RANK.get(tier, 1))
