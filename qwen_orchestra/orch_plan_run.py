"""Выполнение утверждённого плана: шаги + сборка."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import plan as plan_mod
from .calc import ensure_computed, need_calc, pick_formula
from .llm import GenOptions
from .orchestra import (
    STEP_HISTORY_CHARS,
    STEP_HISTORY_MAX,
    OrchestraResult,
    PlanConfirmCallback,
    StatusCallback,
    TokenCallback,
)
from .orch_pool import _fit_tier, _rank
from .orch_status import _emit, _emit_token, _status_scoped, _step_token_cb
from .router import ALL_TIERS, RouteDecision, Tier, need_local_time, need_web


def _step_tier(
    hint: str,
    *,
    force_tier: Tier | None,
    available: set[Tier],
) -> Tier:
    tier: Tier = hint if hint in ALL_TIERS else "mid"
    if force_tier and force_tier in ALL_TIERS and _rank(tier) < _rank(force_tier):
        tier = force_tier
    return _fit_tier(tier, available)


def _compose_tier(
    start_tier: Tier,
    *,
    force_tier: Tier | None,
    available: set[Tier],
) -> Tier:
    tier = start_tier if start_tier in ALL_TIERS else "mid"
    if _rank(tier) < _rank("mid"):
        tier = "mid"
    if force_tier and force_tier in ALL_TIERS and _rank(tier) < _rank(force_tier):
        tier = force_tier
    return _fit_tier(tier, available)


def _clip_history_for_steps(
    history: list[dict] | None,
    *,
    max_messages: int = STEP_HISTORY_MAX,
    max_chars: int = STEP_HISTORY_CHARS,
) -> list[dict]:
    """Короткий хвост чата для шага плана (follow-up без полного контекста)."""
    hist = [
        h
        for h in (history or [])
        if h.get("role") in {"user", "assistant"} and (h.get("content") or "").strip()
    ]
    out: list[dict] = []
    for h in hist[-max_messages:]:
        text = str(h.get("content") or "").strip()
        if len(text) > max_chars:
            text = text[: max(0, max_chars - 20)].rstrip() + "\n...[обрезано]"
        out.append({"role": h.get("role"), "content": text})
    return out


def _forced_decision(
    user_text: str,
    *,
    tier: Tier,
    reason: str,
    force_need_web: bool | None = None,
    force_need_calc: bool | None = None,
    force_need_local_time: bool | None = None,
) -> RouteDecision:
    """Роут без LLM-классификаторов, если флаги уже известны (шаг плана)."""
    web = bool(force_need_web) if force_need_web is not None else need_web(user_text)
    calc = bool(force_need_calc) if force_need_calc is not None else need_calc(user_text)
    if force_need_local_time is not None:
        local = bool(force_need_local_time) or web
    elif web:
        local = True
    else:
        local = need_local_time(user_text)
    return RouteDecision(
        True,
        tier,
        web,
        reason,
        "",
        need_local_time=local,
        need_calc=calc,
    )


def _run_plan_pipeline(
    user_text: str,
    history: list[dict],
    *,
    decision: RouteDecision,
    force_tier: Tier | None,
    available: set[Tier],
    installed: set[str],
    stream: bool,
    on_token: TokenCallback | None,
    on_status: StatusCallback | None,
    effective_gen: GenOptions,
    on_plan_confirm: PlanConfirmCallback | None = None,
) -> OrchestraResult | None:
    """Декомпозиция. None — идём обычным воркером."""
    from .orch_handle import handle

    if not plan_mod.ENABLED:
        return None
    verdict = plan_mod.decide_need_plan(user_text)
    _emit(
        on_status,
        "plan",
        need_plan=verdict.need_plan,
        reason=verdict.reason,
        source=verdict.source,
    )
    if not verdict.need_plan:
        return None
    planner = plan_mod.resolve_planner_model(installed)
    max_steps = plan_mod.clamp_max_steps(plan_mod.MAX_STEPS)
    built = plan_mod.build_plan(user_text, model=planner, max_steps=max_steps)
    if built is None:
        _emit(
            on_status,
            "plan",
            need_plan=False,
            reason="bad-json" if planner else "planner-missing",
            source="fail-no",
        )
        return None
    _emit(
        on_status,
        "plan_ready",
        goal=built.goal,
        planner_model=planner or "calc-block",
        steps=built.to_meta(),
        awaiting=True,
    )
    action = "approve"
    if on_plan_confirm is not None:
        try:
            action = str(
                on_plan_confirm(
                    {
                        "goal": built.goal,
                        "planner_model": planner,
                        "steps": built.to_meta(),
                    }
                )
                or "reject"
            ).strip().lower()
        except Exception:  # noqa: BLE001
            action = "reject"
    if action != "approve":
        _emit(on_status, "plan_rejected", reason=action or "reject")
        return None
    _emit(on_status, "plan_accepted")

    step_results: list[dict[str, Any]] = []
    step_meta: list[dict[str, Any]] = []
    any_web = bool(decision.need_web)
    skip_block: str | None = None
    for i, step in enumerate(built.steps):
        if skip_block and step.block == skip_block:
            continue
        skip_block = None
        tier = _step_tier(step.tier_hint, force_tier=force_tier, available=available)
        prompt = step.prompt
        if step.block_role == "cpu" and step_results:
            prev_text = str(step_results[-1].get("text") or "")
            expr = pick_formula(prev_text)
            if expr:
                prompt = f"{prompt}\n\nГотовое выражение для tool calculate:\n{expr}"
        _emit(
            on_status,
            "plan_step",
            index=i + 1,
            total=len(built.steps),
            id=step.id,
            title=step.title,
            tier=tier,
            block=step.block or None,
            block_title=step.block_title or None,
            block_role=step.block_role or None,
        )
        extra = plan_mod.step_system_extra(built.goal, step_results)
        if step.block_role == "formula":
            extra = (
                extra
                + "\n\nЭтот шаг — только формула. Не считай число, не вызывай calculate."
            )
        step_status = _status_scoped(on_status, "step", step_id=step.id, id=step.id)
        step_hist = [] if step.block else _clip_history_for_steps(history)
        try:
            step_res = handle(
                prompt,
                step_hist,
                force_tier=tier,
                stream=stream,
                verbose=False,
                on_token=_step_token_cb(on_status, step.id) if stream else None,
                on_status=step_status,
                gen=effective_gen,
                allow_plan=False,
                system_extra=extra,
                force_need_calc=bool(step.need_calc),
                force_need_web=bool(step.need_web),
                force_need_local_time=bool(step.need_web),
                installed=installed,
            )
        except Exception as exc:  # noqa: BLE001
            note = str(exc)
            step_results.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "text": "",
                    "failed": True,
                    "note": note,
                }
            )
            step_meta.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "tier": tier,
                    "model": "",
                    "prompt": step.prompt,
                    "text": "",
                    "failed": True,
                    "note": note,
                    "block": step.block,
                    "block_title": step.block_title,
                    "block_role": step.block_role,
                }
            )
            _emit(
                on_status,
                "plan_step_done",
                id=step.id,
                index=i + 1,
                failed=True,
                note=note,
                tier=tier,
                text="",
                block=step.block or None,
                block_title=step.block_title or None,
                block_role=step.block_role or None,
            )
            if step.block_role == "formula":
                skip_block = step.block
            if i == 0:
                break
            continue
        text = step_res.text or ""
        if step.block_role == "cpu":
            prev_text = str(step_results[-1].get("text") or "") if step_results else ""
            text = ensure_computed(text, pick_formula(prev_text))
        failed = (not text.strip()) or any(
            p in {"empty", "error"} for p in (step_res.problems or [])
        )
        if step.block_role == "formula" and pick_formula(text):
            failed = False
        note = ", ".join(step_res.problems) if failed else ""
        step_results.append(
            {
                "id": step.id,
                "title": step.title,
                "text": text,
                "failed": failed,
                "note": note,
            }
        )
        step_meta.append(
            {
                "id": step.id,
                "title": step.title,
                "tier": step_res.tier,
                "model": step_res.model,
                "prompt": step.prompt,
                "text": text,
                "failed": failed,
                "note": note,
                "block": step.block,
                "block_title": step.block_title,
                "block_role": step.block_role,
            }
        )
        _emit(
            on_status,
            "plan_step_done",
            id=step.id,
            index=i + 1,
            failed=failed,
            note=note,
            model=step_res.model,
            tier=step_res.tier,
            text=text,
            block=step.block or None,
            block_title=step.block_title or None,
            block_role=step.block_role or None,
        )
        any_web = any_web or step_res.need_web or step.need_web
        if failed and step.block_role == "formula":
            skip_block = step.block
        if i == 0 and failed:
            break

    compose_tier = _compose_tier(
        decision.tier, force_tier=force_tier, available=available
    )
    _emit(on_status, "compose", tier=compose_tier)
    compose_extra = plan_mod.compose_system_extra(built.goal, step_results)
    composed: OrchestraResult | None = None
    try:
        composed = handle(
            user_text,
            history,
            force_tier=compose_tier,
            stream=stream,
            verbose=False,
            on_token=on_token,
            on_status=on_status,
            gen=effective_gen,
            allow_plan=False,
            force_need_calc=False,
            force_need_web=False,
            force_need_local_time=bool(decision.need_local_time),
            system_extra=compose_extra,
            installed=installed,
        )
    except Exception:  # noqa: BLE001
        composed = None
    if composed is None or not (composed.text or "").strip():
        fallback = plan_mod.fallback_compose_text(step_results)
        _emit_token(on_token, fallback)
        return OrchestraResult(
            text=fallback,
            tier=compose_tier,
            model=planner or "calc-block",
            need_web=any_web,
            route_reason=f"{decision.reason}+plan-fallback",
            checked=False,
            need_local_time=decision.need_local_time,
            need_calc=decision.need_calc,
            gen=effective_gen,
            planned=True,
            plan_goal=built.goal,
            plan_steps=step_meta,
        )
    return replace(
        composed,
        planned=True,
        plan_goal=built.goal,
        plan_steps=step_meta,
        route_reason=f"{decision.reason}+plan",
        need_web=any_web,
        need_local_time=decision.need_local_time,
        need_calc=decision.need_calc,
    )
