"""Пути, секреты OpenRouter и константы пула."""

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
_settings = None


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

