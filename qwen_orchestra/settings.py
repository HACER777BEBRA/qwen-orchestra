"""Персистентные настройки оркестра: пул моделей + промпты роутера.

Файл: settings.json — в корне репозитория при разработке, иначе в user config.
Реализация: settings_const / settings_types / settings_query.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import settings_const as st
from .settings_const import (
    DEFAULT_ROUTER_MODEL,
    FIXED_TIER_IDS,
    FIXED_TIERS,
    OPENROUTER_API_KEY_ENV,
    ROOT,
    SETTINGS_PATH,
    default_ctx_overhead_for_tier,
    default_max_ctx_for_tier,
    default_settings_path,
    get_openrouter_api_key,
    get_settings_path,
    openrouter_configured,
    openrouter_status,
    runtime_lock,
    secrets_path,
    set_openrouter_api_key,
    set_settings_path,
)
from .settings_query import (
    build_route_schema,
    build_router_system,
    derived_providers_map,
    derived_tiers_map,
    models_by_id,
    models_for_tier,
    pick_model_for_tier,
    public_settings_payload,
    router_auto_tier_ids,
    tier_order,
    tier_rank_map,
    tiered_models,
)
from .settings_types import (
    DEFAULT_PLANNER_MODEL,
    AppSettings,
    PlanSettings,
    PoolModel,
    SelfcheckSettings,
    _merge_with_defaults,
    _normalize_provider,
    _normalize_router_model,
    _plan_from_dict,
    _pool_from_dict,
    _selfcheck_from_dict,
    default_plan_settings,
    default_selfcheck_settings,
    default_settings,
    make_pool_id,
)

ModelSlot = PoolModel


def deepcopy_settings(src: AppSettings) -> AppSettings:
    used: set[str] = set()
    return AppSettings(
        models=[_pool_from_dict(m.to_dict(), used_ids=used) for m in src.models],
        router_model=src.router_model,
        planner_model=src.planner_model,
        selfcheck=_selfcheck_from_dict(src.selfcheck.to_dict()),
        plan=_plan_from_dict(src.plan.to_dict()),
    )


def load_settings(*, path: Path | None = None) -> AppSettings:
    p = path or st.SETTINGS_PATH
    with st._lock:
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            try:
                st._settings = _merge_with_defaults(raw if isinstance(raw, dict) else None)
            except (ValueError, TypeError):
                st._settings = default_settings()
        else:
            st._settings = default_settings()
        return deepcopy_settings(st._settings)


def get_settings() -> AppSettings:
    with st._lock:
        if st._settings is None:
            return load_settings()
        return deepcopy_settings(st._settings)


def save_settings(settings: AppSettings, *, path: Path | None = None) -> AppSettings:
    p = path or st.SETTINGS_PATH
    normalized = _merge_with_defaults(settings.to_dict())
    text = json.dumps(normalized.to_dict(), ensure_ascii=False, indent=2) + "\n"
    with st._lock:
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
        st._settings = normalized
        apply_to_runtime(normalized)
        return deepcopy_settings(normalized)


def reset_settings(*, path: Path | None = None) -> AppSettings:
    return save_settings(default_settings(), path=path)


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
        orchestra.POOL[:] = [m.to_dict() for m in pool]
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
    if path is not None:
        set_settings_path(path)
    s = load_settings()
    apply_to_runtime(s)
    with st._lock:
        st._bootstrapped = True
    return s


def ensure_bootstrapped(*, path: Path | str | None = None) -> AppSettings:
    """Однократная инициализация; повторные вызовы — no-op (если path не задан)."""
    if path is not None:
        return bootstrap(path=path)
    with st._lock:
        if st._bootstrapped and st._settings is not None:
            return deepcopy_settings(st._settings)
    return bootstrap()
