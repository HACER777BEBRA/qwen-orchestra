"""Датаклассы settings.json и разбор пула."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .settings_const import (
    DEFAULT_ROUTER_MODEL,
    DEFAULT_ROUTER_PROMPTS,
    FIXED_TIER_IDS,
    FIXED_TIERS,
    _DEFAULT_POOL_SPEC,
    _ID_RE,
    _PROVIDERS,
    default_ctx_overhead_for_tier,
    default_max_ctx_for_tier,
    _normalize_ctx_overhead_pct,
    _normalize_max_ctx,
)

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
