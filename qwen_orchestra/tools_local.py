"""Локальные инструменты с ПК пользователя (время, ОС и т.п.)."""

from __future__ import annotations

import platform
from datetime import datetime
from zoneinfo import ZoneInfo

# RU названия дней / месяцев для человекочитаемой строки
_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_MONTHS_RU = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _local_now() -> datetime:
    """Текущий момент в локальной зоне ОС (Windows / Linux / macOS)."""
    return datetime.now().astimezone()


def _tz_label(now: datetime) -> str:
    tz = now.tzinfo
    if tz is None:
        return "локальное время ОС"
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    name = tz.tzname(now) or "локальная зона"
    off = now.utcoffset()
    if off is None:
        return name
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh, rem = divmod(total, 3600)
    mm = rem // 60
    return f"{name} (UTC{sign}{hh:02d}:{mm:02d})"


def local_datetime_snapshot() -> dict[str, str | int]:
    """Структурированный снимок локального времени с ПК."""
    now = _local_now()
    iso = now.isoformat(timespec="seconds")
    human = (
        f"{_WEEKDAYS_RU[now.weekday()]}, {now.day} {_MONTHS_RU[now.month]} {now.year}, "
        f"{now.strftime('%H:%M:%S')}"
    )
    return {
        "iso": iso,
        "human_ru": human,
        "timezone": _tz_label(now),
        "weekday": _WEEKDAYS_RU[now.weekday()],
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "utc_offset": now.strftime("%z") or "",
        "os": f"{platform.system()} {platform.release()}".strip(),
    }


def local_datetime_text() -> str:
    """Текст для tool-результата / system-контекста воркера."""
    snap = local_datetime_snapshot()
    return (
        f"Локальное время ПК пользователя: {snap['human_ru']}\n"
        f"ISO: {snap['iso']}\n"
        f"Часовой пояс: {snap['timezone']}\n"
        f"ОС: {snap['os']}"
    )


def local_context_block() -> str:
    """Фрагмент system-промпта: факты с ПК, модель не должна выдумывать время."""
    return (
        "Локальные данные с ПК пользователя (точные, не выдумывай другие):\n"
        f"{local_datetime_text()}\n"
        "Если вопрос про текущее время/дату/день недели — опирайся на эти данные. "
        "Если ищешь в интернете — «сейчас» / свежесть фактов считай от этих часов."
    )


def local_date_line() -> str:
    """Короткая строка с датой/временем для system воркера (~30 токенов)."""
    snap = local_datetime_snapshot()
    return f"Сейчас по часам ПК: {snap['human_ru']} ({snap['timezone']})."


def get_local_time(_args: dict | None = None) -> str:
    """Ollama tool: текущие дата и время с ПК."""
    del _args
    return local_datetime_text()


TOOLS_LOCAL = [
    {
        "type": "function",
        "function": {
            "name": "get_local_time",
            "description": (
                "Получить точные текущие дату и время с компьютера пользователя "
                "(локальная зона ОС). Используй для вопросов «который час», "
                "«какая сегодня дата», день недели."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

TOOL_IMPL_LOCAL = {
    "get_local_time": get_local_time,
}


def resolve_zoneinfo(name: str) -> ZoneInfo | None:
    """Опциональный хелпер для тестов / будущих tools."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None
