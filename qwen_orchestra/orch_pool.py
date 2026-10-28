"""Пул моделей: доступность, выбор тира, эскалация по rank."""

from __future__ import annotations

from typing import Any

from . import settings as app_settings
from .llm import installed_models
from .orchestra import POOL, PROVIDERS
from .router import TIER_ORDER, TIER_RANK, Tier


def _entry_reachable(entry: dict[str, Any], have: set[str], *, or_ok: bool) -> bool:
    name = (entry.get("model") or "").strip()
    if not name:
        return False
    if (entry.get("provider") or "ollama") == "openrouter":
        return or_ok
    return name in have


def available_pool_entries(
    have: set[str] | None = None,
    *,
    pool: list[dict[str, Any]] | None = None,
    routed_only: bool = False,
) -> list[dict[str, Any]]:
    """Доступные записи пула (Ollama installed или OR + ключ)."""
    have = have if have is not None else set(installed_models())
    src = pool if pool is not None else POOL
    or_ok = app_settings.openrouter_configured()
    out: list[dict[str, Any]] = []
    for e in src:
        if routed_only and not (e.get("tier") or "").strip():
            continue
        if _entry_reachable(e, have, or_ok=or_ok):
            out.append(e)
    return out


def available_tiers(
    have: set[str] | None = None,
    *,
    models: dict[str, str] | None = None,
    providers: dict[str, str] | None = None,
    pool: list[dict[str, Any]] | None = None,
) -> set[Tier]:
    """Тиры с ≥1 доступной routed-моделью в пуле."""
    del models, providers  # derived из пула
    entries = available_pool_entries(have, pool=pool, routed_only=True)
    return {str(e["tier"]) for e in entries if e.get("tier")}


def _unavailable_pool_labels(
    have: set[str],
    *,
    pool: list[dict[str, Any]] | None = None,
) -> list[str]:
    src = pool if pool is not None else POOL
    or_ok = app_settings.openrouter_configured()
    out: list[str] = []
    for e in src:
        name = (e.get("model") or "").strip()
        if not name:
            continue
        if _entry_reachable(e, have, or_ok=or_ok):
            continue
        if (e.get("provider") or "ollama") == "openrouter":
            out.append(f"openrouter:{name}")
        else:
            out.append(name)
    return out


def missing_models(have: set[str] | None = None) -> list[str]:
    """Критичные пробелы: нет ни одной доступной модели пула."""
    have = have if have is not None else set(installed_models())
    if available_pool_entries(have):
        return []
    return _unavailable_pool_labels(have) or ["(нет доступных моделей)"]


def missing_optional_models(have: set[str] | None = None) -> list[str]:
    """Назначенные, но недоступные модели при том, что пул частично работает."""
    have = have if have is not None else set(installed_models())
    if not available_pool_entries(have):
        return []
    return _unavailable_pool_labels(have)


def _provider_of(tier: str, providers: dict[str, str] | None = None) -> str:
    src = providers if providers is not None else PROVIDERS
    return src.get(tier, "ollama") or "ollama"


def _pool_rank(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("rank") if entry.get("rank") is not None else -1)
    except (TypeError, ValueError):
        return -1


def _pick_entry_for_tier(
    tier: Tier,
    available_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Среди available routed-моделей тира — max rank."""
    cands = [
        e
        for e in available_entries
        if (e.get("tier") or "").strip() == tier and (e.get("model") or "").strip()
    ]
    if not cands:
        return None
    return max(cands, key=lambda e: (_pool_rank(e), str(e.get("id") or "")))


def _entry_by_id(
    pool_id: str, pool: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for e in pool:
        if e.get("id") == pool_id:
            return e
    return None


def _rank(tier: str) -> int:
    return int(TIER_RANK.get(tier, 1))


def _fit_tier(tier: Tier, available: set[Tier]) -> Tier:
    """Подобрать тир с моделями: тот же, иначе ближайший ниже/альтернатива."""
    if not available:
        raise RuntimeError("Нет установленных моделей оркестра")
    if tier in available:
        return tier
    if tier == "coder" and "xlarge" in available:
        return "xlarge"
    if tier == "xlarge" and "coder" in available:
        return "coder"
    for t in reversed(TIER_ORDER):
        if _rank(t) <= _rank(tier) and t in available:
            return t
    for t in TIER_ORDER:
        if t in available:
            return t
    return next(iter(available))


def _is_coder_entry(entry: dict[str, Any]) -> bool:
    return (entry.get("tier") or "") == "coder" or "coder" in str(
        entry.get("model") or ""
    ).lower()


def _next_entry_by_rank(
    current: dict[str, Any],
    available_entries: list[dict[str, Any]],
    *,
    code_task: bool = False,
) -> dict[str, Any] | None:
    """Следующая routed-модель с rank строго выше текущей.

    Специализация: не-кодовый запрос на coder не эскалируем; кодовый — сначала
    на coder (если он сильнее текущей модели).
    """
    cur_rank = _pool_rank(current)
    routed = [
        e
        for e in available_entries
        if (e.get("tier") or "").strip() and (e.get("model") or "").strip()
    ]
    stronger = [e for e in routed if _pool_rank(e) > cur_rank]
    if not code_task:
        stronger = [e for e in stronger if not _is_coder_entry(e)]
    else:
        coders = [e for e in stronger if _is_coder_entry(e)]
        stronger = coders or stronger
    if not stronger:
        return None
    return min(stronger, key=lambda e: (_pool_rank(e), str(e.get("id") or "")))


def _local_review_model(
    available_entries: list[dict[str, Any]],
    *,
    installed: set[str],
    preferred_tier: str = "mid",
    fallback: str = "",
    prefer_model: str | None = None,
) -> str | None:
    """Локальная Ollama-модель для selfcheck.

    Если воркер уже mid+ на Ollama — ревьюим им же (без выгрузки 9b/14b ради 4b).
    """
    if prefer_model and prefer_model in installed:
        return prefer_model
    local = [
        e
        for e in available_entries
        if (e.get("provider") or "ollama") == "ollama"
        and (e.get("model") or "").strip() in installed
    ]
    if not local:
        return fallback if fallback in installed else None
    preferred = [
        e for e in local if (e.get("tier") or "") == preferred_tier
    ]
    pick_from = preferred or local
    best = max(pick_from, key=lambda e: (_pool_rank(e), str(e.get("id") or "")))
    return (best.get("model") or "").strip() or None


def _escalated(start: Tier, final: Tier) -> bool:
    if _rank(final) > _rank(start):
        return True
    return start != final and {start, final} <= {"coder", "xlarge"}
