"""Персистентные настройки оркестра: пул моделей + промпты роутера.

Файл: settings.json — в корне репозитория при разработке, иначе в user config
(`%LOCALAPPDATA%/qwen-orchestra` / `~/.config/qwen-orchestra`).

Пул `models[]`: каждая запись — provider/model/label/prompt и опционально
tier+rank. Тир задан → Auto и эскалация; без тира — только ручной выбор.
На один тир можно несколько моделей (приоритет по rank).

Семантика 10 тиров (tiny…frontier) сохраняется для floor/ceiling роутера.
Старый формат `slots[]` мигрирует в `models[]` при загрузке.

Ключ OpenRouter — env `OPENROUTER_API_KEY` или `secrets.json` рядом с settings
(не коммитить).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/=+\-]{0,120}$")
_PROVIDERS = frozenset({"ollama", "openrouter"})
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
_lock = threading.RLock()
# Синхронизация apply_to_runtime ↔ orchestra.handle (снимок пула)
runtime_lock = threading.RLock()
_bootstrapped = False


def _user_config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "qwen-orchestra"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "qwen-orchestra"
    return Path.home() / ".config" / "qwen-orchestra"


def default_settings_path() -> Path:
    """Корень репо (если есть pyproject/server) или user config."""
    repo_json = PROJECT_ROOT / "settings.json"
    if repo_json.is_file():
        return repo_json
    if (PROJECT_ROOT / "pyproject.toml").is_file() or (PROJECT_ROOT / "server.py").is_file():
        return PROJECT_ROOT / "settings.json"
    return _user_config_dir() / "settings.json"


SETTINGS_PATH: Path = default_settings_path()
ROOT = SETTINGS_PATH.parent


def set_settings_path(path: Path | str) -> None:
    """Задать путь к settings.json (до bootstrap / Client)."""
    global SETTINGS_PATH, ROOT, _bootstrapped, _settings
    SETTINGS_PATH = Path(path)
    ROOT = SETTINGS_PATH.parent
    with _lock:
        _bootstrapped = False
        _settings = None


def get_settings_path() -> Path:
    return SETTINGS_PATH


def secrets_path() -> Path:
    """secrets.json рядом с settings.json (API-ключи; не в git)."""
    return SETTINGS_PATH.parent / "secrets.json"


def _read_secrets() -> dict[str, Any]:
    p = secrets_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_secrets(data: dict[str, Any]) -> None:
    p = secrets_path()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=".secrets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def get_openrouter_api_key() -> str | None:
    """Ключ: сначала env OPENROUTER_API_KEY, иначе secrets.json."""
    env = (os.environ.get(OPENROUTER_API_KEY_ENV) or "").strip()
    if env:
        return env
    raw = _read_secrets().get("openrouter_api_key")
    key = str(raw or "").strip()
    return key or None


def openrouter_configured() -> bool:
    return bool(get_openrouter_api_key())


def set_openrouter_api_key(api_key: str | None) -> None:
    """Записать/очистить ключ в secrets.json. Env имеет приоритет при чтении."""
    data = _read_secrets()
    key = (api_key or "").strip()
    if key:
        data["openrouter_api_key"] = key
    else:
        data.pop("openrouter_api_key", None)
    if data:
        _write_secrets(data)
    else:
        p = secrets_path()
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                _write_secrets({})


def openrouter_status() -> dict[str, Any]:
    env_set = bool((os.environ.get(OPENROUTER_API_KEY_ENV) or "").strip())
    file_set = bool(str(_read_secrets().get("openrouter_api_key") or "").strip())
    return {
        "configured": openrouter_configured(),
        "from_env": env_set,
        "from_file": file_set and not env_set,
        "base_url": DEFAULT_OPENROUTER_BASE,
        "api_key_env": OPENROUTER_API_KEY_ENV,
    }


DEFAULT_ROUTER_PROMPTS: dict[str, str] = {
    "tiny": (
        "Приветствия, благодарности, прощания, простая арифметика (2+2), "
        "короткие да/нет. Можно сразу дать короткий ответ в поле reply."
    ),
    "nano": (
        "Чуть сложнее tiny: одно короткое уточнение или факт без рассуждений. "
        "Не выбирай для объяснений и кода — это mid+."
    ),
    "small": (
        "Очень короткие уточнения и простые факты чуть сложнее nano, "
        "но без развёрнутых объяснений и кода. Обычно не выбирай, если хватает mid."
    ),
    "mid": (
        "Объяснения, перевод, советы, how-to, «что такое», небольшой код/скрипт, "
        "обычные вопросы по существу. Если нужен интернет (need_web) — минимум этот уровень."
    ),
    "large": (
        "Развёрнутые ответы и средняя сложность между mid и heavy: длиннее how-to, "
        "несколько связанных вопросов, но ещё не архитектура/доказательства."
    ),
    "heavy": (
        "Сложный анализ, сравнение подходов, архитектура, доказательства, "
        "длинные рассуждения, многошаговые планы, несколько вопросов сразу. "
        "Не ставь сюда тяжёлую отладку кода — для этого coder."
    ),
    "xlarge": (
        "Очень сложные задачи и локальная эскалация, когда mid/heavy уже не справились. "
        "Обычно не выбирай первым — это запасная сила."
    ),
    "coder": (
        "Код и отладка: traceback, стектрейс, ошибки в коде, рефакторинг, "
        "сервис/API/приложение, unit-тесты, JWT и стек, ТЗ с кодом."
    ),
    "ultra": (
        "Максимальная локальная сила перед frontier: тяжёлая эскалация после xlarge, "
        "длинные многочастные задачи. Не выбирай первым."
    ),
    "frontier": (
        "Самые сложные задачи и внешние/топ-модели (часто OpenRouter). "
        "Не выбирай первым — только когда локальных тиров мало или пользователь явно просит сильнейшую."
    ),
}

FIXED_TIER_IDS: tuple[str, ...] = (
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
)
FIXED_TIERS = frozenset(FIXED_TIER_IDS)

# Defaults: одна модель на тир (миграция / reset)
_DEFAULT_POOL_SPEC: list[tuple[str, str, str, int]] = [
    ("tiny", "qwen3.5:0.8b", "tiny · 3.5 0.8b", 0),
    ("nano", "qwen3.5:0.8b", "nano · 3.5 0.8b", 1),
    ("small", "qwen3.5:2b", "small · 3.5 2b", 2),
    ("mid", "qwen3.5:4b", "mid · 3.5 4b", 3),
    ("large", "qwen2.5:7b", "large · 2.5 7b", 4),
    ("heavy", "qwen3.5:9b", "heavy · 3.5 9b", 5),
    ("xlarge", "qwen2.5:14b", "xlarge · 14b", 6),
    ("coder", "qwen2.5-coder:14b", "coder · 14b", 6),
    ("ultra", "qwen2.5:14b", "ultra · 14b+", 7),
    ("frontier", "qwen2.5:14b", "frontier · топ / внешний", 8),
]

# Запас ctx % по тиру (300 → база×4). Тяжёлые — 0 (минимум VRAM).
DEFAULT_CTX_OVERHEAD_PCT: dict[str, int] = {
    "tiny": 300,
    "nano": 200,
    "small": 100,
    "mid": 50,
    "large": 0,
    "heavy": 0,
    "xlarge": 0,
    "coder": 0,
    "ultra": 0,
    "frontier": 0,
}

# Потолок для мелких моделей с большим запасом (None → глобальный 8192).
DEFAULT_MAX_CTX: dict[str, int | None] = {
    "tiny": 4096,
    "nano": 4096,
    "small": 4096,
    "mid": None,
    "large": None,
    "heavy": None,
    "xlarge": None,
    "coder": None,
    "ultra": None,
    "frontier": None,
}

DEFAULT_ROUTER_MODEL = "qwen3.5:0.8b"

ROUTER_SYSTEM_HEADER = """Ты роутер запросов. Отвечай ТОЛЬКО валидным JSON без markdown.

Поля:
- ok: false ТОЛЬКО если запрос пустой, бессмысленный набор символов или спам.
      Непонятный, странный или очень короткий вопрос — это ok=true.
- tier: один из допустимых id тиров (см. список ниже)
- need_web: true если нужны свежие данные из интернета
- reason: коротко почему такой tier
- reply: если ok=false — вежливый отказ; если tier=tiny — полный короткий ответ; иначе пустая строка

Правила выбора tier (когда какую модель использовать):
"""

ROUTER_SYSTEM_FOOTER = """
Дополнительно:
- need_web=true => минимум mid (если mid есть в списке)
- Сомневаешься между двумя тирами — выбирай СТАРШИЙ (больший rank / сильнее).
- Не выбирай xlarge/coder/ultra/frontier без явной необходимости —
  обычно их подключает эскалация, код (coder) или ручной выбор.
"""


CTX_OVERHEAD_PCT_MAX = 900
MAX_CTX_ABS_MIN = 256
MAX_CTX_ABS_MAX = 32768


def _normalize_ctx_overhead_pct(raw: Any) -> int:
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(CTX_OVERHEAD_PCT_MAX, n))


def _normalize_max_ctx(raw: Any) -> int | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        n = int(s)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return max(MAX_CTX_ABS_MIN, min(MAX_CTX_ABS_MAX, n))


def default_ctx_overhead_for_tier(tier: str | None) -> int:
    if not tier:
        return 0
    return int(DEFAULT_CTX_OVERHEAD_PCT.get(str(tier).strip().lower(), 0))


def default_max_ctx_for_tier(tier: str | None) -> int | None:
    if not tier:
        return None
    return DEFAULT_MAX_CTX.get(str(tier).strip().lower())


@dataclass
class PoolModel:
    """Запись пула: модель + опциональный тир для Auto."""

    id: str
    model: str
    provider: str
    label: str
    router_prompt: str
    tier: str | None = None
    rank: int | None = None
    ctx_overhead_pct: int = 0  # 0…900; 300 → база×4
    max_ctx: int | None = None  # None → глобальный NUM_CTX_MAX

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def in_routing(self) -> bool:
        return bool(self.tier) and bool((self.model or "").strip())


# Алиас для совместимости импортов
ModelSlot = PoolModel


@dataclass
class SelfcheckSettings:
    """Настройки самопроверки: отказы / неуверенность / «не по теме».

    По умолчанию бракуем короткие ответы с маркерами «не могу» / «не знаю»
    (как раньше в коде) и LLM-вердикт irrelevant. Можно выключить или задать
    свои фразы в settings.json.
    """

    flag_refusal: bool = True
    flag_uncertain: bool = True
    flag_irrelevant: bool = True
    # None → встроенные паттерны из selfcheck; список → заменить (в т.ч. пустой)
    refusal_patterns: list[str] | None = None
    uncertain_patterns: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "flag_refusal": bool(self.flag_refusal),
            "flag_uncertain": bool(self.flag_uncertain),
            "flag_irrelevant": bool(self.flag_irrelevant),
        }
        if self.refusal_patterns is not None:
            out["refusal_patterns"] = list(self.refusal_patterns)
        if self.uncertain_patterns is not None:
            out["uncertain_patterns"] = list(self.uncertain_patterns)
        return out

    def to_rules(self):
        from .selfcheck import SelfcheckRules

        return SelfcheckRules(
            flag_refusal=self.flag_refusal,
            flag_uncertain=self.flag_uncertain,
            flag_irrelevant=self.flag_irrelevant,
            refusal_patterns=(
                None if self.refusal_patterns is None else list(self.refusal_patterns)
            ),
            uncertain_patterns=(
                None
                if self.uncertain_patterns is None
                else list(self.uncertain_patterns)
            ),
        )


def default_selfcheck_settings() -> SelfcheckSettings:
    return SelfcheckSettings()


DEFAULT_PLANNER_MODEL = "qwen3.5:4b"
DEFAULT_PLAN_MAX_STEPS = 4


@dataclass
class PlanSettings:
    """Декомпозиция сложных запросов: вкл/выкл, лимит шагов, промпты."""

    enabled: bool = True
    max_steps: int = DEFAULT_PLAN_MAX_STEPS
    classifier_prompt: str | None = None
    planner_prompt: str | None = None
    composer_prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enabled": bool(self.enabled),
            "max_steps": int(self.max_steps),
        }
        if self.classifier_prompt is not None:
            out["classifier_prompt"] = self.classifier_prompt
        if self.planner_prompt is not None:
            out["planner_prompt"] = self.planner_prompt
        if self.composer_prompt is not None:
            out["composer_prompt"] = self.composer_prompt
        return out


def default_plan_settings() -> PlanSettings:
    return PlanSettings()


def _optional_prompt_field(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _plan_from_dict(raw: Any) -> PlanSettings:
    if not isinstance(raw, dict):
        return default_plan_settings()
    enabled = raw.get("enabled")
    max_steps = raw.get("max_steps")
    try:
        steps = int(max_steps) if max_steps is not None else DEFAULT_PLAN_MAX_STEPS
    except (TypeError, ValueError):
        steps = DEFAULT_PLAN_MAX_STEPS
    steps = max(2, min(6, steps))
    return PlanSettings(
        enabled=True if enabled is None else bool(enabled),
        max_steps=steps,
        classifier_prompt=_optional_prompt_field(raw.get("classifier_prompt"))
        if "classifier_prompt" in raw
        else None,
        planner_prompt=_optional_prompt_field(raw.get("planner_prompt"))
        if "planner_prompt" in raw
        else None,
        composer_prompt=_optional_prompt_field(raw.get("composer_prompt"))
        if "composer_prompt" in raw
        else None,
    )


def _normalize_pattern_list(raw: Any) -> list[str] | None:
    """None → builtins-сигнал; иначе список строк (можно пустой)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        lines = [
            ln.strip().lower()
            for ln in raw.replace("\r\n", "\n").split("\n")
        ]
        return [ln for ln in lines if ln]
    if not isinstance(raw, (list, tuple)):
        return None
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def _selfcheck_from_dict(raw: Any) -> SelfcheckSettings:
    if not isinstance(raw, dict):
        return default_selfcheck_settings()
    flag_refusal = raw.get("flag_refusal")
    flag_uncertain = raw.get("flag_uncertain")
    flag_irrelevant = raw.get("flag_irrelevant")
    # Явные false/true; отсутствие → True (старое поведение)
    fr = True if flag_refusal is None else bool(flag_refusal)
    fu = True if flag_uncertain is None else bool(flag_uncertain)
    fi = True if flag_irrelevant is None else bool(flag_irrelevant)

    def _patterns_field(key: str) -> list[str] | None:
        if key not in raw:
            return None
        val = raw.get(key)
        if val is None:
            return None  # явный null → builtins
        parsed = _normalize_pattern_list(val)
        return [] if parsed is None else parsed

    return SelfcheckSettings(
        flag_refusal=fr,
        flag_uncertain=fu,
        flag_irrelevant=fi,
        refusal_patterns=_patterns_field("refusal_patterns"),
        uncertain_patterns=_patterns_field("uncertain_patterns"),
    )


@dataclass
class AppSettings:
    models: list[PoolModel] = field(default_factory=list)
    router_model: str = DEFAULT_ROUTER_MODEL
    planner_model: str = DEFAULT_PLANNER_MODEL
    selfcheck: SelfcheckSettings = field(default_factory=default_selfcheck_settings)
    plan: PlanSettings = field(default_factory=default_plan_settings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "router_model": self.router_model,
            "planner_model": self.planner_model,
            "models": [m.to_dict() for m in self.models],
            "selfcheck": self.selfcheck.to_dict(),
            "plan": self.plan.to_dict(),
        }

    # Совместимость: старый код ждал .slots
    @property
    def slots(self) -> list[PoolModel]:
        return self.models


_settings: AppSettings | None = None


def _normalize_provider(raw: Any) -> str:
    name = str(raw or "ollama").strip().lower() or "ollama"
    if name not in _PROVIDERS:
        raise ValueError(f"Неизвестный provider: {name!r} (ожидается ollama|openrouter)")
    return name


def _normalize_router_model(raw: Any, *, fallback: str = DEFAULT_ROUTER_MODEL) -> str:
    name = str(raw or "").strip()
    return name or fallback


def _sanitize_id_part(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.:/=+\-]+", "_", (text or "").strip())
    return s[:80] or "model"


def make_pool_id(provider: str, model: str, *, used: set[str] | None = None) -> str:
    """Стабильный id: provider:model; при коллизии — суффикс."""
    base = f"{provider}:{_sanitize_id_part(model)}"
    if not _ID_RE.match(base):
        base = f"{provider}_{_sanitize_id_part(model)}"
    if used is None:
        return base
    if base not in used:
        used.add(base)
        return base
    n = 2
    while f"{base}#{n}" in used:
        n += 1
    cid = f"{base}#{n}"
    used.add(cid)
    return cid


def _pool_from_dict(raw: dict[str, Any], *, used_ids: set[str] | None = None) -> PoolModel:
    provider = _normalize_provider(raw.get("provider"))
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError("У записи пула должно быть непустое имя модели")
    pid = str(raw.get("id") or "").strip()
    if not pid:
        pid = make_pool_id(provider, model, used=used_ids)
    elif used_ids is not None:
        if pid in used_ids:
            pid = make_pool_id(provider, model, used=used_ids)
        else:
            used_ids.add(pid)
    label = str(raw.get("label") or "").strip() or (
        model if provider == "ollama" else f"{model} · OR"
    )
    tier_raw = raw.get("tier")
    if tier_raw is None and "id" in raw and str(raw.get("id") or "").lower() in FIXED_TIERS:
        # Миграция старого слота: id был тиром
        tier_raw = raw["id"]
    tier: str | None
    if tier_raw is None or str(tier_raw).strip() == "" or str(tier_raw).strip().lower() in {
        "none",
        "null",
        "-",
        "manual",
    }:
        tier = None
    else:
        tier = str(tier_raw).strip().lower()
        if tier not in FIXED_TIERS:
            raise ValueError(
                f"Неизвестный tier: {tier}. Допустимы: {', '.join(FIXED_TIER_IDS)} или пусто"
            )
    rank: int | None
    if tier is None:
        rank = None
    else:
        if raw.get("rank") is None:
            default_rank = next(
                (r for t, _m, _l, r in _DEFAULT_POOL_SPEC if t == tier), 1
            )
            rank = int(default_rank)
        else:
            rank = int(raw["rank"])
    prompt = str(raw.get("router_prompt") or "").strip()
    if not prompt:
        if tier:
            prompt = DEFAULT_ROUTER_PROMPTS.get(
                tier, "Используй для задач, которые подходят этой модели."
            )
        else:
            prompt = f"Модель `{model}` — только ручной выбор (вне Auto)."
    if "ctx_overhead_pct" in raw:
        ctx_oh = _normalize_ctx_overhead_pct(raw.get("ctx_overhead_pct"))
    else:
        ctx_oh = default_ctx_overhead_for_tier(tier)
    if "max_ctx" in raw:
        max_c = _normalize_max_ctx(raw.get("max_ctx"))
    else:
        max_c = default_max_ctx_for_tier(tier)
    return PoolModel(
        id=pid,
        model=model,
        provider=provider,
        label=label,
        router_prompt=prompt,
        tier=tier,
        rank=rank,
        ctx_overhead_pct=ctx_oh,
        max_ctx=max_c,
    )


def _default_pool() -> list[PoolModel]:
    used: set[str] = set()
    out: list[PoolModel] = []
    for tier, model, label, rank in _DEFAULT_POOL_SPEC:
        out.append(
            _pool_from_dict(
                {
                    "id": make_pool_id("ollama", model, used=used),
                    "model": model,
                    "provider": "ollama",
                    "label": label,
                    "tier": tier,
                    "rank": rank,
                    "router_prompt": DEFAULT_ROUTER_PROMPTS[tier],
                    "ctx_overhead_pct": default_ctx_overhead_for_tier(tier),
                    "max_ctx": default_max_ctx_for_tier(tier),
                },
                used_ids=None,
            )
        )
    return out


def default_settings() -> AppSettings:
    return AppSettings(
        models=_default_pool(),
        router_model=DEFAULT_ROUTER_MODEL,
        planner_model=DEFAULT_PLANNER_MODEL,
        selfcheck=default_selfcheck_settings(),
        plan=default_plan_settings(),
    )


def _slots_to_pool(raw_slots: list[Any]) -> list[dict[str, Any]]:
    """Миграция старого slots[] → сырые dict для PoolModel."""
    used: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in raw_slots:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        if not model:
            continue  # свободный тир — пропускаем
        sid = str(item.get("id") or "").strip().lower()
        provider = _normalize_provider(item.get("provider"))
        tier = sid if sid in FIXED_TIERS else item.get("tier")
        # router_auto=false на старом слоте → без тира (только вручную)
        if item.get("router_auto") is False and sid in FIXED_TIERS:
            # План: участие = наличие тира. Старый router_auto=false оставляем с тиром
            # (эскалация/ручной тир), но в Auto enum не попадёт через tiered list —
            # фактически все с tier в routing. Оставляем tier.
            pass
        pid = make_pool_id(provider, model, used=used)
        entry: dict[str, Any] = {
            "id": pid,
            "model": model,
            "provider": provider,
            "label": item.get("label") or model,
            "router_prompt": item.get("router_prompt"),
            "tier": tier if tier in FIXED_TIERS else None,
            "rank": item.get("rank"),
        }
        if "ctx_overhead_pct" in item:
            entry["ctx_overhead_pct"] = item.get("ctx_overhead_pct")
        if "max_ctx" in item:
            entry["max_ctx"] = item.get("max_ctx")
        out.append(entry)
    return out


def _merge_with_defaults(raw: dict[str, Any] | None) -> AppSettings:
    raw = raw if isinstance(raw, dict) else {}
    raw_models = list(raw.get("models") or [])
    if not raw_models and raw.get("slots"):
        raw_models = _slots_to_pool(list(raw.get("slots") or []))
    if not raw_models:
        return default_settings()

    used: set[str] = set()
    models: list[PoolModel] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        try:
            models.append(_pool_from_dict(item, used_ids=used))
        except (ValueError, TypeError):
            continue
    if not models:
        return default_settings()

    tiny = next(
        (m.model for m in models if m.tier == "tiny" and m.provider == "ollama"),
        next(
            (m.model for m in models if m.provider == "ollama"),
            DEFAULT_ROUTER_MODEL,
        ),
    )
    router_model = _normalize_router_model(raw.get("router_model"), fallback=tiny)
    planner_model = _normalize_router_model(
        raw.get("planner_model"), fallback=DEFAULT_PLANNER_MODEL
    )
    selfcheck = _selfcheck_from_dict(raw.get("selfcheck"))
    plan = _plan_from_dict(raw.get("plan"))
    return AppSettings(
        models=models,
        router_model=router_model,
        planner_model=planner_model,
        selfcheck=selfcheck,
        plan=plan,
    )


def load_settings(*, path: Path | None = None) -> AppSettings:
    global _settings
    p = path or SETTINGS_PATH
    with _lock:
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            try:
                _settings = _merge_with_defaults(raw if isinstance(raw, dict) else None)
            except (ValueError, TypeError):
                _settings = default_settings()
        else:
            _settings = default_settings()
        return deepcopy_settings(_settings)


def deepcopy_settings(src: AppSettings) -> AppSettings:
    used: set[str] = set()
    return AppSettings(
        models=[_pool_from_dict(m.to_dict(), used_ids=used) for m in src.models],
        router_model=src.router_model,
        planner_model=src.planner_model,
        selfcheck=_selfcheck_from_dict(src.selfcheck.to_dict()),
        plan=_plan_from_dict(src.plan.to_dict()),
    )


def get_settings() -> AppSettings:
    with _lock:
        if _settings is None:
            return load_settings()
        return deepcopy_settings(_settings)


def save_settings(settings: AppSettings, *, path: Path | None = None) -> AppSettings:
    global _settings
    p = path or SETTINGS_PATH
    normalized = _merge_with_defaults(settings.to_dict())
    text = json.dumps(normalized.to_dict(), ensure_ascii=False, indent=2) + "\n"
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(p.parent), prefix=".settings-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(text)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, p)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _settings = normalized
        apply_to_runtime(normalized)
        return deepcopy_settings(normalized)


def reset_settings(*, path: Path | None = None) -> AppSettings:
    return save_settings(default_settings(), path=path)


def models_by_id(settings: AppSettings | None = None) -> dict[str, PoolModel]:
    s = settings or get_settings()
    return {m.id: m for m in s.models}


def tiered_models(settings: AppSettings | None = None) -> list[PoolModel]:
    return [m for m in (settings or get_settings()).models if m.in_routing]


def models_for_tier(tier: str, settings: AppSettings | None = None) -> list[PoolModel]:
    tid = (tier or "").strip().lower()
    return [m for m in tiered_models(settings) if m.tier == tid]


def pick_model_for_tier(
    tier: str,
    available_ids: set[str] | None = None,
    settings: AppSettings | None = None,
) -> PoolModel | None:
    """Лучшая (max rank) модель тира; available_ids — id записей пула."""
    cands = models_for_tier(tier, settings)
    if available_ids is not None:
        cands = [m for m in cands if m.id in available_ids]
    if not cands:
        return None
    return max(cands, key=lambda m: (int(m.rank or 0), m.id))


def derived_tiers_map(settings: AppSettings | None = None) -> dict[str, str]:
    """tier → имя модели (лучшая по rank) — для UI/compat."""
    s = settings or get_settings()
    out: dict[str, str] = {}
    for tid in FIXED_TIER_IDS:
        picked = pick_model_for_tier(tid, settings=s)
        if picked:
            out[tid] = picked.model
    return out


def derived_providers_map(settings: AppSettings | None = None) -> dict[str, str]:
    s = settings or get_settings()
    out: dict[str, str] = {}
    for tid in FIXED_TIER_IDS:
        picked = pick_model_for_tier(tid, settings=s)
        if picked:
            out[tid] = picked.provider
    return out


def tier_rank_map(settings: AppSettings | None = None) -> dict[str, int]:
    """Тир → max rank среди моделей на тире (для floor/ceiling / TIER_RANK)."""
    s = settings or get_settings()
    out: dict[str, int] = {}
    for tid in FIXED_TIER_IDS:
        cands = models_for_tier(tid, s)
        if cands:
            out[tid] = max(int(m.rank or 0) for m in cands)
        else:
            # дефолтный rank семантики тира (даже без моделей)
            out[tid] = next((r for t, _m, _l, r in _DEFAULT_POOL_SPEC if t == tid), 1)
    return out


def tier_order(settings: AppSettings | None = None) -> list[str]:
    ranks = tier_rank_map(settings)
    order = sorted(
        (t for t in FIXED_TIER_IDS if t != "coder"),
        key=lambda t: (ranks.get(t, 1), t),
    )
    return order


def router_auto_tier_ids(settings: AppSettings | None = None) -> list[str]:
    """Уникальные тиры, у которых есть ≥1 модель в routing."""
    seen: list[str] = []
    for m in sorted(tiered_models(settings), key=lambda x: (int(x.rank or 0), x.id)):
        assert m.tier
        if m.tier not in seen:
            seen.append(m.tier)
    return seen


def build_router_system(settings: AppSettings | None = None) -> str:
    s = settings or get_settings()
    lines = [ROUTER_SYSTEM_HEADER.rstrip(), ""]
    for m in sorted(tiered_models(s), key=lambda x: (int(x.rank or 0), x.tier or "", x.id)):
        prov = "" if m.provider == "ollama" else f", provider={m.provider}"
        lines.append(
            f"- {m.tier} (модель `{m.model}`{prov}, rank={m.rank}, id={m.id}): "
            f"{m.router_prompt}"
        )
    if not tiered_models(s):
        lines.append("- (нет моделей с тиром — назначьте tier в настройках пула)")
    lines.append(ROUTER_SYSTEM_FOOTER.rstrip())
    return "\n".join(lines) + "\n"


def build_route_schema(settings: AppSettings | None = None) -> dict[str, Any]:
    enums = router_auto_tier_ids(settings) or ["tiny", "mid", "heavy"]
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "tier": {"type": "string", "enum": enums},
            "need_web": {"type": "boolean"},
            "reason": {"type": "string"},
            "reply": {"type": "string"},
        },
        "required": ["ok", "tier", "need_web", "reason"],
    }


def public_settings_payload(settings: AppSettings | None = None) -> dict[str, Any]:
    from .selfcheck import DEFAULT_REFUSAL_PATTERNS, DEFAULT_UNCERTAIN_PATTERNS
    from . import plan as plan_mod

    s = settings or get_settings()
    sc = s.selfcheck
    pl = s.plan
    return {
        "router_model": s.router_model,
        "planner_model": s.planner_model,
        "models": [m.to_dict() for m in s.models],
        # compat для старого UI на один релиз
        "slots": [m.to_dict() for m in s.models],
        "selfcheck": {
            **sc.to_dict(),
            # эффективные списки для UI (builtins, если не переопределены)
            "refusal_patterns_effective": list(
                sc.refusal_patterns
                if sc.refusal_patterns is not None
                else DEFAULT_REFUSAL_PATTERNS
            ),
            "uncertain_patterns_effective": list(
                sc.uncertain_patterns
                if sc.uncertain_patterns is not None
                else DEFAULT_UNCERTAIN_PATTERNS
            ),
            "using_default_refusal_patterns": sc.refusal_patterns is None,
            "using_default_uncertain_patterns": sc.uncertain_patterns is None,
        },
        "plan": {
            **pl.to_dict(),
            "classifier_prompt_effective": (
                pl.classifier_prompt
                if pl.classifier_prompt is not None
                else plan_mod.DEFAULT_CLASSIFIER_SYSTEM
            ),
            "planner_prompt_effective": (
                pl.planner_prompt
                if pl.planner_prompt is not None
                else plan_mod.DEFAULT_PLANNER_SYSTEM
            ),
            "composer_prompt_effective": (
                pl.composer_prompt
                if pl.composer_prompt is not None
                else plan_mod.DEFAULT_COMPOSER_SYSTEM
            ),
            "using_default_classifier_prompt": pl.classifier_prompt is None,
            "using_default_planner_prompt": pl.planner_prompt is None,
            "using_default_composer_prompt": pl.composer_prompt is None,
        },
        "fixed_tiers": [
            {
                "id": tid,
                "rank": r,
                "label": lab,
                "ctx_overhead_pct": default_ctx_overhead_for_tier(tid),
                "max_ctx": default_max_ctx_for_tier(tid),
            }
            for tid, _m, lab, r in _DEFAULT_POOL_SPEC
        ],
        "defaults": {
            "router_model": DEFAULT_ROUTER_MODEL,
            "planner_model": DEFAULT_PLANNER_MODEL,
            "models": [m.to_dict() for m in _default_pool()],
            "router_prompts": DEFAULT_ROUTER_PROMPTS,
            "fixed_tiers": list(FIXED_TIER_IDS),
            "selfcheck": {
                **default_selfcheck_settings().to_dict(),
                "refusal_patterns": list(DEFAULT_REFUSAL_PATTERNS),
                "uncertain_patterns": list(DEFAULT_UNCERTAIN_PATTERNS),
            },
            "plan": {
                **default_plan_settings().to_dict(),
                "classifier_prompt": plan_mod.DEFAULT_CLASSIFIER_SYSTEM,
                "planner_prompt": plan_mod.DEFAULT_PLANNER_SYSTEM,
                "composer_prompt": plan_mod.DEFAULT_COMPOSER_SYSTEM,
            },
        },
        "router_system": build_router_system(s),
        "providers": {
            "openrouter": openrouter_status(),
        },
        "tiers": derived_tiers_map(s),
    }


def add_model(
    *,
    model: str,
    label: str | None = None,
    router_prompt: str | None = None,
    tier: str | None = None,
    rank: int | None = None,
    provider: str = "ollama",
    model_id: str | None = None,
    ctx_overhead_pct: int | None = None,
    max_ctx: int | None = None,
) -> AppSettings:
    """Добавить модель в пул (или обновить по id)."""
    s = get_settings()
    model = (model or "").strip()
    if not model:
        raise ValueError("Укажите имя модели")
    prov = _normalize_provider(provider)
    tid: str | None
    if tier is None or str(tier).strip() == "" or str(tier).strip().lower() in {
        "none",
        "null",
        "-",
        "manual",
    }:
        tid = None
    else:
        tid = str(tier).strip().lower()
        if tid not in FIXED_TIERS:
            raise ValueError(
                f"Неизвестный tier: {tid}. Допустимы: {', '.join(FIXED_TIER_IDS)} или пусто"
            )
    used = {m.id for m in s.models}
    pid = (model_id or "").strip() or make_pool_id(prov, model, used=used)
    existing = next((m for m in s.models if m.id == pid), None)
    raw = {
        "id": pid,
        "model": model,
        "provider": prov,
        "label": (label or "").strip()
        or (model if prov == "ollama" else f"{model} · OR"),
        "router_prompt": (router_prompt or "").strip()
        or (existing.router_prompt if existing else ""),
        "tier": tid,
        "rank": rank if tid is not None else None,
        "ctx_overhead_pct": (
            ctx_overhead_pct
            if ctx_overhead_pct is not None
            else (
                existing.ctx_overhead_pct
                if existing
                else default_ctx_overhead_for_tier(tid)
            )
        ),
        "max_ctx": (
            max_ctx
            if max_ctx is not None
            else (existing.max_ctx if existing else default_max_ctx_for_tier(tid))
        ),
    }
    entry = _pool_from_dict(raw)
    if existing:
        s.models = [entry if m.id == pid else m for m in s.models]
    else:
        s.models.append(entry)
    return save_settings(s)


def update_settings(
    models_payload: list[dict[str, Any]],
    *,
    router_model: str | None = None,
    planner_model: str | None = None,
    selfcheck: dict[str, Any] | SelfcheckSettings | None = None,
    plan: dict[str, Any] | PlanSettings | None = None,
) -> AppSettings:
    """Полная замена пула (+ опционально модель роутера / планировщика / selfcheck / plan)."""
    if not isinstance(models_payload, list) or not models_payload:
        raise ValueError("Нужен непустой список models")
    for item in models_payload:
        if not isinstance(item, dict):
            raise ValueError("Каждая модель должна быть объектом")
    current = get_settings()
    rm = _normalize_router_model(
        router_model if router_model is not None else current.router_model,
        fallback=current.router_model or DEFAULT_ROUTER_MODEL,
    )
    pm = _normalize_router_model(
        planner_model if planner_model is not None else current.planner_model,
        fallback=current.planner_model or DEFAULT_PLANNER_MODEL,
    )
    used: set[str] = set()
    models = [_pool_from_dict(x, used_ids=used) for x in models_payload]
    if selfcheck is None:
        sc = _selfcheck_from_dict(current.selfcheck.to_dict())
    elif isinstance(selfcheck, SelfcheckSettings):
        sc = selfcheck
    else:
        sc = _selfcheck_from_dict(selfcheck)
    if plan is None:
        pl = _plan_from_dict(current.plan.to_dict())
    elif isinstance(plan, PlanSettings):
        pl = plan
    else:
        pl = _plan_from_dict(plan)
    return save_settings(
        AppSettings(
            models=models,
            router_model=rm,
            planner_model=pm,
            selfcheck=sc,
            plan=pl,
        )
    )


def delete_model(model_id: str) -> AppSettings:
    """Убрать модель из пула."""
    mid = (model_id or "").strip()
    s = get_settings()
    before = len(s.models)
    s.models = [m for m in s.models if m.id != mid]
    if len(s.models) == before:
        raise KeyError(model_id)
    if not s.models:
        raise ValueError(
            "Нельзя удалить последнюю модель — в пуле должна остаться хотя бы одна"
        )
    return save_settings(s)


# --- Совместимость со старыми именами ---

def add_slot(
    *,
    model: str,
    label: str | None = None,
    router_prompt: str | None = None,
    slot_id: str | None = None,
    tier: str | None = None,
    rank: int | None = None,
    router_auto: bool | None = None,
    provider: str = "ollama",
) -> AppSettings:
    """Совместимость: назначить/добавить модель (tier из tier или slot_id)."""
    del router_auto
    tid = tier if tier is not None else slot_id
    return add_model(
        model=model,
        label=label,
        router_prompt=router_prompt,
        tier=tid,
        rank=rank,
        provider=provider,
    )


def delete_slot(slot_id: str) -> AppSettings:
    """Совместимость: удалить по id записи пула (не по тиру)."""
    return delete_model(slot_id)


def models_map(settings: AppSettings | None = None) -> dict[str, str]:
    return derived_tiers_map(settings)


def providers_map(settings: AppSettings | None = None) -> dict[str, str]:
    return derived_providers_map(settings)


def labels_map(settings: AppSettings | None = None) -> dict[str, str]:
    s = settings or get_settings()
    out: dict[str, str] = {}
    for tid in FIXED_TIER_IDS:
        picked = pick_model_for_tier(tid, settings=s)
        if picked:
            out[tid] = picked.label
    return out


def required_ids(settings: AppSettings | None = None) -> tuple[str, ...]:
    del settings
    return ()


def optional_ids(settings: AppSettings | None = None) -> tuple[str, ...]:
    return tuple(FIXED_TIER_IDS)


def tier_rank(settings: AppSettings | None = None) -> dict[str, int]:
    return tier_rank_map(settings)


def router_auto_ids(settings: AppSettings | None = None) -> list[str]:
    return router_auto_tier_ids(settings)


def slots_by_id(settings: AppSettings | None = None) -> dict[str, PoolModel]:
    return models_by_id(settings)


def apply_to_runtime(settings: AppSettings | None = None) -> None:
    """Прописать пул / MODELS / ранги / промпт роутера в orchestra и router."""
    s = settings or get_settings()
    from . import orchestra
    from . import router

    pool = list(s.models)
    models = derived_tiers_map(s)
    providers = derived_providers_map(s)
    ranks = tier_rank_map(s)
    order = tier_order(s)

    # Selfcheck — локальная Ollama из пула (предпочтительно mid)
    def _local_name(preferred_tiers: tuple[str, ...]) -> str:
        for tid in preferred_tiers:
            for m in models_for_tier(tid, s):
                if m.provider == "ollama" and m.model:
                    return m.model
        for m in s.models:
            if m.provider == "ollama" and m.model:
                return m.model
        return ""

    selfcheck = _local_name(("mid", "large", "small", "heavy", "nano", "tiny"))
    heavy_rank = ranks.get("heavy", 5)
    complex_tiers = frozenset(
        tid
        for tid, r in ranks.items()
        if r >= heavy_rank or tid in {"heavy", "xlarge", "coder", "ultra", "frontier"}
    )
    route_model = s.router_model or _local_name(("tiny", "nano", "small", "mid")) or router.ROUTE_MODEL
    revalidate = _local_name(("mid", "large", "small", "heavy")) or route_model
    system = build_router_system(s)
    schema = build_route_schema(s)
    auto_tiers = frozenset(router_auto_tier_ids(s))

    with runtime_lock:
        orchestra.POOL = [m.to_dict() for m in pool]
        for key, val in models.items():
            orchestra.MODELS[key] = val
        for key in list(orchestra.MODELS):
            if key not in models:
                del orchestra.MODELS[key]
        for key, val in providers.items():
            orchestra.PROVIDERS[key] = val
        for key in list(orchestra.PROVIDERS):
            if key not in providers:
                del orchestra.PROVIDERS[key]
        orchestra.REQUIRED_TIERS = ()
        orchestra.OPTIONAL_TIERS = tuple(FIXED_TIER_IDS)
        orchestra.SELFCHECK_MODEL = selfcheck
        orchestra._COMPLEX_TIERS = complex_tiers  # type: ignore[attr-defined]

        from . import plan as plan_mod
        from . import selfcheck as selfcheck_mod

        selfcheck_mod.set_rules(s.selfcheck.to_rules())
        plan_mod.ENABLED = bool(s.plan.enabled)
        plan_mod.MAX_STEPS = plan_mod.clamp_max_steps(s.plan.max_steps)
        plan_mod.PLANNER_MODEL = s.planner_model or DEFAULT_PLANNER_MODEL
        plan_mod.CLASSIFIER_SYSTEM = (s.plan.classifier_prompt or "").strip()
        plan_mod.PLANNER_SYSTEM = (s.plan.planner_prompt or "").strip()
        plan_mod.COMPOSER_SYSTEM = (s.plan.composer_prompt or "").strip()

        for key, val in ranks.items():
            router.TIER_RANK[key] = val
        for key in list(router.TIER_RANK):
            if key not in ranks:
                del router.TIER_RANK[key]
        router.TIER_ORDER[:] = order
        router.ALL_TIERS = frozenset(FIXED_TIER_IDS)
        router.ROUTE_MODEL = route_model
        router.REVALIDATE_MODEL = revalidate
        router.SYSTEM = system
        router.ROUTE_SCHEMA = schema
        router.ROUTER_AUTO_TIERS = auto_tiers


def bootstrap(*, path: Path | str | None = None) -> AppSettings:
    """Загрузить settings и применить к runtime (явно или через Client)."""
    global _bootstrapped
    if path is not None:
        set_settings_path(path)
    s = load_settings()
    apply_to_runtime(s)
    with _lock:
        _bootstrapped = True
    return s


def ensure_bootstrapped(*, path: Path | str | None = None) -> AppSettings:
    """Однократная инициализация; повторные вызовы — no-op (если path не задан)."""
    if path is not None:
        return bootstrap(path=path)
    with _lock:
        if _bootstrapped and _settings is not None:
            return deepcopy_settings(_settings)
    return bootstrap()
