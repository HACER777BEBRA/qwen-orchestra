"""Оркестр: tiny → nano → small → mid → large → heavy → xlarge/coder → ultra → frontier.

Цикл одного запроса:
    route → need_plan? → (planner → шаги → compose) | worker → selfcheck
    → (retry на старшем тире)

Адаптивный num_ctx (см. plan_worker_context):
    ceil_256(tokens(промпт + tools + local + retry) + template + reserve_ответа + safety),
    затем округление до ступени CTX_STEPS (2048/4096/8192)

Самопроверка (`selfcheck.check`) отбраковывает ответ, если он ушёл на другой
язык, оказался пустым/зацикленным, стал отказом или содержит явную ошибку.
Тогда запрос переделывается на модели уровнем выше с инструкцией-правкой.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .llm import GenOptions
from .router import Tier

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

PROVIDERS: dict[str, str] = {tid: "ollama" for tid in MODELS}
POOL: list[dict[str, Any]] = []
REQUIRED_TIERS: tuple[str, ...] = ()
OPTIONAL_TIERS: tuple[str, ...] = ()

MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 8
TOOL_RESULT_CHARS = 4000
MAX_ATTEMPTS = 3
SELFCHECK_LLM = True
SELFCHECK_MODEL = MODELS["mid"]

_COMPLEX_TIERS: frozenset[str] = frozenset(
    {"heavy", "xlarge", "coder", "ultra", "frontier"}
)
NUM_CTX_MIN = 256
NUM_CTX_MAX = 8192
NUM_CTX_FULL = NUM_CTX_MAX
CTX_ALIGN = 256
CTX_TEMPLATE_TOKENS = 128
CTX_SAFETY_FLAT = 128
CTX_OUT_SHORT = 512
CTX_OUT_NORMAL = 768
CTX_OUT_CODE = 1536
HISTORY_LIGHT = 4
HISTORY_FULL = 8
STEP_HISTORY_MAX = 4
STEP_HISTORY_CHARS = 600
CTX_TOOL_BUDGET_RATIO = 0.35
# Ступени num_ctx: окно округляется вверх до ступени, чтобы Ollama не
# перезагружала модель на каждый слегка иной размер.
CTX_STEPS: tuple[int, ...] = (2048, 4096, 8192)
# Резерв под результаты tools (web_search/fetch_url, calculate) сверх схем tools.
CTX_DATE_LINE_TOKENS = 40  # строка «Сейчас по часам ПК…» в system воркера
CTX_TOOL_RESULTS_WEB = 1800
CTX_TOOL_RESULTS_CALC = 300

WORKER_SYSTEM = """Ты полезный ассистент. Отвечай на языке пользователя, кратко и по делу.
Если в контексте есть результаты поиска/страниц — опирайся на них и указывай URL.
Не выдумывай свежие факты без источников."""

RETRY_SYSTEM = """Предыдущая попытка ответа забракована автопроверкой.
Что учесть: {hint}

Ответь на вопрос заново и полностью. Не упоминай предыдущие попытки,
не извиняйся и не благодари за замечание."""

RETRY_COMPLETE_SYSTEM = """Предыдущий ответ неполный.
Что учесть: {hint}

Предыдущий ответ:
<<<
{prev}
>>>

Допиши недостающее и выдай итоговый ответ целиком. Не упоминай попытки,
не извиняйся."""

StatusCallback = Callable[[str, dict[str, Any]], None]
TokenCallback = Callable[[str], None]
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
    gen: GenOptions | None = None
    planned: bool = False
    plan_goal: str = ""
    plan_steps: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0


from .orch_pool import (  # noqa: E402
    _entry_by_id,
    _entry_reachable,
    _escalated,
    _fit_tier,
    _local_review_model,
    _next_entry_by_rank,
    _pick_entry_for_tier,
    _pool_rank,
    _provider_of,
    _rank,
    available_pool_entries,
    available_tiers,
    missing_models,
    missing_optional_models,
)
from .orch_ctx import needs_chat_history, plan_worker_context  # noqa: E402
from .orch_status import (  # noqa: E402
    _cli_status_printer,
    _emit,
    _emit_context,
    _emit_token,
    _status_scoped,
    _step_token_cb,
    _with_elapsed,
)
from .orch_worker import (  # noqa: E402
    _ALL_TOOL_IMPL,
    _ALL_TOOLS,
    _Attempt,
    _best_attempt,
    _build_messages,
    _fallback_tools,
    _local_context_for,
    _run_tool,
    _tools_for,
    _trim_tool_result,
    _worker_plain,
    _worker_with_tools,
)
from .orch_plan_run import (  # noqa: E402
    _clip_history_for_steps,
    _compose_tier,
    _forced_decision,
    _run_plan_pipeline,
    _step_tier,
)
from .orch_handle import handle  # noqa: E402
