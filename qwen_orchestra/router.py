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

ROUTE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "tier": {"type": "string", "enum": ["tiny", "mid", "heavy"]},
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
- tier: tiny | mid | heavy
- need_web: true если нужны свежие данные из интернета
- reason: коротко почему такой tier
- reply: если ok=false — вежливый отказ; если tier=tiny — полный короткий ответ; иначе пустая строка

Правила tier:
- tiny: приветствие, благодарность, прощание, простая арифметика, да/нет
- mid: объяснения, небольшой код, короткие тексты, советы, перевод
- heavy: сложный анализ, архитектура, сравнение подходов, многошаговые планы,
  доказательства, длинные рассуждения (код/отладку не ставь в heavy — это mid+)
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


_WEB_KEYS = (
    "погода",
    "новост",
    "курс доллар",
    "курс евро",
    "актуальн",
    "weather",
    "news",
    "http://",
    "https://",
    "что происходит",
    "свеж",
    "поищи",
    "погугли",
    "найди в интернет",
    "найди в сети",
    "гугл",
    "google",
    "look up",
    "search the web",
    "search online",
)

# Классификатор tiny: нужно ли подставить часы/дату с ПК (без regex-гейтов)
_LOCAL_TIME_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "need_local_time": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["need_local_time"],
}

_LOCAL_TIME_SYSTEM = """Ты классификатор. Реши, подставить ли в контекст точные
текущие дату и время с компьютера пользователя (локальные часы ОС).

need_local_time=true при ЛЮБОМ упоминании времени, даты или связанных понятий —
даже если для ответа часы могут не понадобиться. Включая:
- час, время, дата, день недели, календарь, срок, дедлайн
- сейчас / сегодня / вчера / завтра / утром / вечером / ночью
- который час, сколько времени, какая дата
- timezone / часовой пояс / UTC
- сленг и опечатки («скок щя», «щас», «what time»)
- любое «когда» в смысле момента времени относительно «сейчас»

need_local_time=false ТОЛЬКО если в запросе нет ни времени, ни даты, ни
временных маркеров (чисто код, перевод, факты без «сейчас/сегодня», приветствие).

Сомневаешься — ставь true.

Ответь ТОЛЬКО JSON: {"need_local_time": bool, "reason": "кратко"}."""

# Короткие EN-приветствия только как целые слова (иначе hi ⊂ this/history)
_TINY_WORD_RE = re.compile(
    r"(?:^|[^\w])(?:hi|hey|hello|bye|thanks|привет|пока)(?:[^\w]|$)",
    re.IGNORECASE,
)

# --- Признаки кода -----------------------------------------------------------

_DEBUG_RE = re.compile(
    r"traceback|стектрейс|stack\s*trace|\bexception\b|"
    r"\berror:\s|\berrno\b|typeerror|valueerror|attributeerror|"
    r"syntaxerror|nullpointer|segfault",
    re.IGNORECASE,
)

# Явный кусок кода / команды стека в сообщении
_CODE_SNIPPET_RE = re.compile(
    r"```|"
    r"\bdef\s+\w+\s*\(|\bclass\s+\w+|"
    r"\bimport\s+\w+|\bfrom\s+\w+\s+import\b|"
    r"\bfunction\s+\w+|\bconst\s+\w+\s*=|\blet\s+\w+\s*=|"
    r"\bSELECT\s+.+\s+FROM\b|\bnpm\s+(?:i|install|run)\b|"
    r"\bpip\s+install\b|\bgit\s+(?:commit|push|rebase|merge|clone)\b|"
    r"=>\s*\{|<\/?[a-zA-Z][^>]*>",
    re.IGNORECASE | re.DOTALL,
)

# Просьба написать/править код (не любое «напиши письмо»)
_CODE_REQUEST_RE = re.compile(
    r"(?:"
    r"напиши|написать|сгенерируй|реализуй|запрограммируй|допиши|почини|"
    r"исправь|отрефактор|рефактор|перепиши|добавь|сделай|write|implement|"
    r"fix|refactor|create|build"
    r")\s+(?:"
    r"код|функци|скрипт|класс|программ|парсер|бот|сервис|приложени|модул|"
    r"эндпоинт|endpoint|api\b|rest\b|crud|компонент|хук|hook|тест(?:ы|ов)?|"
    r"unit.?тест|pytest|dockerfile|docker.?compose|миграц|sql\b|запрос|"
    r"алгоритм|метод|утилиту|утилитар|библиотеку|плагин|расширени|"
    r"code|function|script|class|program|parser|service|app\b|component"
    r")|"
    r"(?:unit.?тест|покрой\s+тестами|напиши\s+тест(?:ы|ов)?\s+(?:на|для|к)\b)|"
    r"(?:на\s+(?:python|javascript|typescript|java|rust|go|c\+\+|c#|php|kotlin|swift)\b)|"
    r"(?:\bfastapi\b|\bflask\b|\bdjango\b|\breact\b|\bvue\b|\bexpress\b|"
    r"\bspring\b|\bnext\.?js\b|\bnest\.?js\b)",
    re.IGNORECASE,
)

# Крупная кодовая задача — сразу coder
_HEAVY_CODE_RE = re.compile(
    r"(?:"
    r"напиши\s+(?:сервис|приложени|игру|бот(?:а)?|api\b|rest\b|бэкенд|backend|"
    r"фронтенд|frontend|микросервис)|"
    r"(?:реализуй|создай|сделай|напиши)\s+(?:сервис|приложени|api\b|rest\b|бэкенд|бот)|"
    r"спроектируй\s+(?:api|сервис|схем|базу|код)|"
    r"рефактор|отрефактор|"
    r"(?:архитектур(?:а|у|е)\s+(?:код|сервис|приложени)|кодовая\s+архитектур)|"
    r"(?:напиши|сделай|создай|подними|добавь)\s+(?:docker(?:file|.?compose)?|ci/?cd)|"
    r"github\s*actions|"
    r"(?:реализуй|добавь|сделай|напиши)\s+(?:авторизац|аутентификац|"
    r"jwt|oauth|websocket|graphql|grpc)|"
    r"(?:с\s+|и\s+|plus\s+|with\s+)(?:jwt|oauth|websocket|graphql|grpc)\b|"
    r"многопоточ|async(?:hronous)?\s+(?:код|обработ|сервис)|"
    r"оптимизир(?:уй|овать)\s+(?:код|запрос|sql|производительн)|"
    r"покрой\s+тестами|unit.?тест|интеграционн(?:ые|ый)\s+тест|"
    r"миграци(?:я|ю|и)\s+(?:бд|базы|схем|данных|sql)|"
    r"обработк[ауи]\s+ошибок\s+(?:в\s+)?код|"
    r"(?:fastapi|flask|django|express|spring|next\.?js|nest\.?js).{0,60}"
    r"(?:с\s+|и\s+|,\s*|plus\s+|with\s+).{0,30}"
    r"(?:jwt|redis|docker|auth|oauth|postgres|mongodb|kafka)"
    r")",
    re.IGNORECASE,
)

_DEBUG_INTENT_RE = re.compile(
    r"(?:"
    r"отлад|debugg|"
    r"почини\s+(?:код|баг|ошибк|функци)|"
    r"исправь\s+(?:код|баг|ошибк|функци|баг)|"
    r"ошибк[аиуе]\s+в\s+код|"
    r"не\s+работает\s+(?:код|функци|скрипт|программ|сервис|эндпоинт|api)|"
    r"код\s+не\s+работает|"
    r"падает\s+с\s+ошибк|вылетает\s+с\s+ошибк|"
    r"fix\s+(?:this\s+)?(?:bug|error|code)|why\s+(?:doesn.?t|does\s+not)\s+work"
    r")",
    re.IGNORECASE,
)

# --- Признаки heavy (не код) -------------------------------------------------

_HEAVY_RE = re.compile(
    r"(?:"
    r"архитектур|спроектир|системный\s+дизайн|system\s*design|"
    r"сравни\s+|сравнение\s+|versus|\bvs\.?\b|"
    r"докажи|выведи\s+формул|доказательств|"
    r"подробн(?:о|ый|ая|ые)\s+(?:разбер|анализ|обзор|план|описан)|"
    r"разверн[уи]\s+подроб|детальный\s+разбор|глубокий\s+анализ|"
    r"план\s+внедрения|пошагов(?:ый|ый\s+план|о)|roadmap|"
    r"производительност|бенчмарк|нагрузочн|"
    r"миграци(?:я|ю)\s+(?:монолит|легаси|legacy)|"
    r"trade.?off|плюсы\s+и\s+минусы|"
    r"эссе|отчёт|реферат|обзор\s+литератур|"
    r"многошагов|несколько\s+подход"
    r")",
    re.IGNORECASE,
)

# --- mid ---------------------------------------------------------------------

_MID_RE = re.compile(
    r"(?:"
    r"объясн|расскаж|переведи|перевод|подскаж|помоги|"
    r"как\s+(?:сделать|работает|пользоваться|настроить|выбрать|отличить)|"
    r"почему|зачем|чем\s+отлича|"
    r"что\s+такое|что\s+значит|что\s+означа|"
    r"список|инструкц|пример|кратко|коротко|"
    r"посоветуй|рекоменд|идеи?\s+для|"
    r"\bexplain\b|\bwrite\b|\btranslate\b|\bhow\s+to\b|\bwhat\s+is\b|"
    r"\bwhy\b|\bdifference\b|\bsummary\b|\bsummarize\b"
    r")",
    re.IGNORECASE,
)

_MATH_RE = re.compile(
    r"(?:"
    r"\d{2,}\s*[\+\-\*/^]\s*\d|"
    r"процент|интеграл|производн|уравнени|реш[иь]\s+(?:уравнен|задач)|"
    r"факториал|матриц|предел\s+функц|дифференциал"
    r")",
    re.IGNORECASE,
)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")
_VOWEL_RE = re.compile(r"[аеёиоуыэюяAEIOUYaeiouy]", re.IGNORECASE)
_ALNUM_RE = re.compile(r"[A-Za-zА-Яа-яЁё\d]")
_BRIEF_RE = re.compile(
    r"кратко|коротко|в двух словах|одним словом|briefly|in short|tl;?dr",
    re.IGNORECASE,
)

# 0.5b любит ставить need_web без причины; её «да» принимаем только при этих признаках
# (чистое «сколько сейчас времени» / «сегодня какая дата» — local time, не web)
_SOFT_WEB_RE = re.compile(
    r"(?:"
    r"последн|нов(?:ый|ая|ое|ые|ости)|верси(?:я|и|ю)|релиз|стоимост|"
    r"\bкурс\b|когда\s+(?:будет|выйдет|откро|закры|выходит|релиз)|кто такой|"
    r"рейтинг|статистик|прогноз|расписани|"
    r"поищи|погугли|найди\s+в\s+(?:интернет|сети)|look\s+up|search\s+(?:the\s+)?(?:web|online)|"
    r"цена\b|погода|weather"
    r")",
    re.IGNORECASE,
)

# Независимые требования («A, и B» / «также» / нумерованный список) —
# голая запятая в тексте/коде не считается отдельным требованием
_REQUIREMENT_RE = re.compile(
    r"(?:"
    r",\s*(?:и|а\s+также|также|плюс|ещё)\s+"
    r"|;\s+(?:и\s+|а\s+также\s+|также\s+)?"
    r"|\bтакже\b|\bплюс\b|\badditionally\b|\balso\b|"
    r"(?:^|\n)\s*(?:\d+[\.\)]\s+|[-*•]\s+)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _rank(tier: str) -> int:
    return int(TIER_RANK.get(tier, 1))


def _tier_max(a: Tier, b: Tier) -> Tier:
    return a if _rank(a) >= _rank(b) else b


def _has_extra_auto() -> bool:
    """Есть ли auto-слоты кроме базовых tiny/mid/heavy — тогда LLM-роутер полезен и на mid/heavy."""
    base = {"tiny", "mid", "heavy"}
    return bool(ROUTER_AUTO_TIERS - base)


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


def _looks_web(user_text: str) -> bool:
    t = user_text.strip().lower()
    return any(k in t for k in _WEB_KEYS) or bool(_SOFT_WEB_RE.search(t))


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


def need_web(user_text: str) -> bool:
    """Нужен ли интернет — детерминированно (для force_tier и shortcuts)."""
    return _looks_web(user_text)


def _looks_tiny(user_text: str) -> bool:
    t = user_text.strip().lower()
    if _looks_web(user_text):
        return False
    if len(t) > 28:
        return False
    phrase_keys = (
        "привет",
        "здравствуй",
        "здравствуйте",
        "как дела",
        "спасибо",
        "благодар",
        "пока",
        "доброе утро",
        "добрый день",
        "добрый вечер",
        "hello",
        "thanks",
        "bye",
    )
    if any(k in t for k in phrase_keys):
        return True
    if _TINY_WORD_RE.search(t):
        return True
    if re.fullmatch(r"\d+\s*[\+\-\*/]\s*\d+\s*=?\s*", t):
        return True
    return False


def looks_meaningful(user_text: str) -> bool:
    """Есть ли в запросе хоть что-то осмысленное.

    0.5b часто помечает нормальные вопросы как невалидные, поэтому её `ok=false`
    принимается только для реально пустого/мусорного ввода.
    """
    t = (user_text or "").strip()
    if len(t) < 2:
        return False
    words = [w for w in _WORD_RE.findall(t) if _VOWEL_RE.search(w)]
    if len(words) >= 2:
        return True
    if words and len(t) >= 4:
        return True
    if re.fullmatch(r"[\d\s\+\-\*/^=.,()]+", t) and re.search(r"[\+\-\*/^]", t):
        return True
    if re.fullmatch(r"\d{1,6}", t):
        return True
    return False


def _requirement_count(text: str) -> int:
    """Сколько дополнительных требований (запятые «и/также», маркеры списка)."""
    return len(_REQUIREMENT_RE.findall(text or ""))


def _has_code_context(text: str) -> bool:
    return bool(
        _CODE_SNIPPET_RE.search(text)
        or _CODE_REQUEST_RE.search(text)
        or _HEAVY_CODE_RE.search(text)
        or _DEBUG_RE.search(text)
        or _DEBUG_INTENT_RE.search(text)
    )


def _is_coder(user_text: str) -> bool:
    """Тяжёлый код / отладка — сразу на coder 14b."""
    t = user_text or ""
    n = len(t.strip())
    if _DEBUG_RE.search(t) or _DEBUG_INTENT_RE.search(t):
        return True
    if _HEAVY_CODE_RE.search(t):
        return True
    code_req = bool(_CODE_REQUEST_RE.search(t))
    snippet = bool(_CODE_SNIPPET_RE.search(t))
    reqs = _requirement_count(t)
    # Существенная кодовая задача: длинное ТЗ или несколько требований
    if code_req and (n > 80 or reqs >= 2):
        return True
    # В сообщении уже есть код + просьба что-то сделать с ним
    if snippet and code_req:
        return True
    if snippet and n > 120:
        return True
    return False


def _is_heavy(user_text: str) -> bool:
    """Сложный не-кодовый (или смешанный) анализ — 7b."""
    t = (user_text or "").strip()
    n = len(t)
    if n > 350:
        return True
    if t.count("?") >= 3:
        return True
    if _HEAVY_RE.search(t):
        return True
    # Длинный развёрнутый запрос без явного «кратко»
    if n > 220 and not _BRIEF_RE.search(t):
        return True
    return False


def _is_mid(user_text: str) -> bool:
    t = (user_text or "").strip()
    n = len(t)
    if n > 40:
        return True
    if _looks_web(t):
        return True
    if _has_code_context(t):
        return True
    if _MID_RE.search(t):
        return True
    if _MATH_RE.search(t):
        return True
    # 3+ слова — уже не tiny-приветствие
    words = _WORD_RE.findall(t)
    if len(words) >= 3:
        return True
    return False


def tier_floor(user_text: str) -> Tier:
    """Минимальный тир, ниже которого опускаться нельзя (см. таблицу в докстринге)."""
    t = (user_text or "").strip()

    if _looks_tiny(t):
        return "tiny"

    # coder важнее heavy: «спроектируй API» — код, не общий анализ
    if _is_coder(t):
        return "coder"

    if _is_heavy(t):
        return "heavy"

    if _is_mid(t):
        return "mid"

    # Осмысленный короткий запрос (не мусор) — не отдаём 0.5b «наугад»
    if looks_meaningful(t):
        return "mid"

    return "tiny"


def tier_ceiling(user_text: str) -> Tier:
    """Потолок стартового тира: «кратко» не гоняем через 7b/14b без нужды.

    xlarge в auto появляется только при эскалации после selfcheck, не как старт
    от роутера — кроме явного force_tier. Пользовательские auto-слоты могут
    поднимать потолок до своего rank.
    """
    floor = tier_floor(user_text)
    if _BRIEF_RE.search(user_text or "") and floor not in {"heavy", "coder", "xlarge"}:
        return "mid"
    if floor == "coder":
        return "coder"
    if floor == "xlarge":
        return "xlarge"
    best: Tier = "heavy"
    for tid in ROUTER_AUTO_TIERS:
        if _rank(tid) > _rank(best):
            best = tid
    return best


def _clamp(tier: Tier, user_text: str) -> Tier:
    floor = tier_floor(user_text)
    ceiling = tier_ceiling(user_text)
    if tier not in ALL_TIERS:
        tier = floor if floor in ALL_TIERS else "mid"
    tier = _tier_max(tier, floor)
    if _rank(tier) > _rank(ceiling):
        return ceiling
    # При равном ранге сохраняем специализацию floor (coder vs xlarge)
    if _rank(tier) == _rank(floor) and floor in {"coder", "xlarge"}:
        return floor
    return tier


def _heuristic(user_text: str) -> RouteDecision:
    """Запасной роут, если роутер-модель вернула мусор."""
    t = user_text.strip()
    if len(t) < 2:
        return RouteDecision(
            False, "tiny", False, "too short", "Напишите вопрос чуть подробнее."
        )

    local = need_local_time(t)
    calc = need_calc(t)
    if _looks_tiny(t) and not local and not calc:
        return RouteDecision(True, "tiny", False, "heuristic:greeting", "")

    return RouteDecision(
        True,
        tier_floor(t),
        _looks_web(t),
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

    # Нужны ли часы ПК — только решение tiny (отдельный вызов)
    local = need_local_time(user_text)

    # Свежие данные — без LLM-роутера; mid+ с учётом floor
    if web:
        return RouteDecision(
            True,
            _tier_max("mid", floor),
            True,
            "web-shortcut",
            "",
            need_local_time=local,
            need_calc=calc,
        )
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
        ok, tier, need, reason or f"floor:{floor}", reply, need_local_time=local, need_calc=calc
    )
