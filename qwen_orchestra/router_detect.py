"""Детерминированный floor/ceiling и признаки запроса."""
from __future__ import annotations

import re

from .calc import need_calc
from . import router as r
from .router import Tier

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
    return int(r.TIER_RANK.get(tier, 1))


def _tier_max(a: Tier, b: Tier) -> Tier:
    return a if _rank(a) >= _rank(b) else b


def _has_extra_auto() -> bool:
    """Есть ли auto-слоты кроме базовых tiny/mid/heavy — тогда LLM-роутер полезен и на mid/heavy."""
    base = {"tiny", "mid", "heavy"}
    return bool(r.ROUTER_AUTO_TIERS - base)


def _looks_web(user_text: str) -> bool:
    t = user_text.strip().lower()
    return any(k in t for k in _WEB_KEYS) or bool(_SOFT_WEB_RE.search(t))


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
    for tid in r.ROUTER_AUTO_TIERS:
        if _rank(tid) > _rank(best):
            best = tid
    return best


def _clamp(tier: Tier, user_text: str) -> Tier:
    floor = tier_floor(user_text)
    ceiling = tier_ceiling(user_text)
    if tier not in r.ALL_TIERS:
        tier = floor if floor in r.ALL_TIERS else "mid"
    tier = _tier_max(tier, floor)
    if _rank(tier) > _rank(ceiling):
        return ceiling
    # При равном ранге сохраняем специализацию floor (coder vs xlarge)
    if _rank(tier) == _rank(floor) and floor in {"coder", "xlarge"}:
        return floor
    return tier
