"""Производные карты пула и public_settings_payload."""
from __future__ import annotations

from typing import Any

from .settings_const import (
    DEFAULT_ROUTER_MODEL,
    DEFAULT_ROUTER_PROMPTS,
    FIXED_TIER_IDS,
    ROUTER_SYSTEM_FOOTER,
    ROUTER_SYSTEM_HEADER,
    _DEFAULT_POOL_SPEC,
    default_ctx_overhead_for_tier,
    default_max_ctx_for_tier,
    openrouter_status,
)
from .settings_types import (
    DEFAULT_PLANNER_MODEL,
    AppSettings,
    PoolModel,
    default_plan_settings,
    default_selfcheck_settings,
    _default_pool,
)


def get_settings():
    from .settings import get_settings as _get
    return _get()


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
    enums = router_auto_tier_ids(s) or [m.tier for m in tiered_models(s) if m.tier]
    if enums:
        lines.append("")
        lines.append("Допустимые значения поля tier: " + ", ".join(enums) + ".")
    lines.append(ROUTER_SYSTEM_FOOTER.rstrip())
    return "\n".join(lines) + "\n"


def build_route_schema(settings: AppSettings | None = None) -> dict[str, Any]:
    enums = router_auto_tier_ids(settings) or list(FIXED_TIER_IDS)
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
