"""Цикл попыток воркера + selfcheck / retry / restore."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from . import selfcheck
from .calc import ensure_computed
from .llm import GenOptions
from .orchestra import (
    MAX_ATTEMPTS,
    SELFCHECK_LLM,
    OrchestraResult,
    StatusCallback,
    TokenCallback,
)
from .orch_ctx import plan_worker_context
from .orch_pool import _escalated, _local_review_model, _next_entry_by_rank, _rank
from .orch_status import _emit, _emit_context, _emit_token
from .orch_worker import (
    _Attempt,
    _best_attempt,
    _local_context_for,
    _tools_for,
    _worker_plain,
    _worker_with_tools,
)
from .router import RouteDecision, Tier


def _ctx_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    pct = entry.get("ctx_overhead_pct")
    try:
        pct_i = int(pct) if pct is not None else 0
    except (TypeError, ValueError):
        pct_i = 0
    raw_max = entry.get("max_ctx")
    try:
        max_i = int(raw_max) if raw_max not in (None, "") else None
    except (TypeError, ValueError):
        max_i = None
    return {"ctx_overhead_pct": pct_i, "max_ctx": max_i}


def run_worker_attempts(
    *,
    user_text: str,
    history: list[dict],
    decision: RouteDecision,
    current_entry: dict[str, Any],
    available_routed: list[dict[str, Any]],
    available_all: list[dict[str, Any]],
    installed: set[str],
    attempts: list[_Attempt],
    retry_hint: str | None,
    system_extra: str | None,
    on_status: StatusCallback | None,
    on_token: TokenCallback | None,
    stream: bool,
    verbose: bool,
    effective_gen: GenOptions,
    selfcheck_model: str,
    start_tier: Tier,
    start_model_name: str,
    stamp: Callable[[OrchestraResult], OrchestraResult],
) -> OrchestraResult:
    tier: Tier = str(current_entry.get("tier") or decision.tier)
    tool_cache: dict[str, str] = {}

    ctx_plan = plan_worker_context(
        user_text,
        history,
        tier,
        system_extra=system_extra,
        **_ctx_kwargs(current_entry),
    )
    _emit_context(on_status, ctx_plan, tier)
    attempt_base = len(attempts)

    for attempt in range(1, MAX_ATTEMPTS + 1 - attempt_base):
        model = (current_entry.get("model") or "").strip()
        provider = (current_entry.get("provider") or "ollama").strip() or "ollama"
        tier = str(current_entry.get("tier") or tier)
        attempt_no = attempt_base + attempt
        if attempt > 1:
            ctx_plan = plan_worker_context(
                user_text,
                history,
                tier,
                system_extra=system_extra,
                **_ctx_kwargs(current_entry),
            )
            _emit_context(on_status, ctx_plan, tier)
        _emit(
            on_status,
            "worker",
            model=model,
            provider=provider,
            need_web=decision.need_web,
            need_local_time=decision.need_local_time,
            need_calc=decision.need_calc,
            tier=tier,
            pool_id=current_entry.get("id"),
            attempt=attempt_no,
            num_ctx=ctx_plan.num_ctx,
            used_history=ctx_plan.use_history,
        )

        tool_list = _tools_for(decision)
        use_tools = bool(tool_list) and provider == "ollama"
        local_ctx = _local_context_for(
            decision, user_text, precompute=not use_tools
        )
        if use_tools:
            text = _worker_with_tools(
                model,
                user_text,
                ctx_plan.history,
                on_status=on_status,
                verbose=verbose,
                retry_hint=retry_hint,
                num_ctx=ctx_plan.num_ctx,
                tool_cache=tool_cache,
                provider=provider,
                local_context=local_ctx,
                gen=effective_gen,
                system_extra=system_extra,
                tools=tool_list,
            )
            if decision.need_calc:
                text = ensure_computed(text)
            _emit_token(on_token, text)
        else:
            stream_now = bool(stream) and not decision.need_calc
            text = _worker_plain(
                model,
                user_text,
                ctx_plan.history,
                stream=stream_now,
                on_token=on_token if stream_now else None,
                retry_hint=retry_hint,
                num_ctx=ctx_plan.num_ctx,
                provider=provider,
                local_context=local_ctx,
                gen=effective_gen,
                system_extra=system_extra,
            )
            if decision.need_calc:
                text = ensure_computed(text)
            if stream and not stream_now:
                _emit_token(on_token, text)

        next_entry = _next_entry_by_rank(current_entry, available_routed)
        can_retry = attempt_no < MAX_ATTEMPTS and next_entry is not None
        prefer_review = (
            model
            if provider == "ollama" and _rank(tier) >= _rank("mid")
            else None
        )
        review_model = _local_review_model(
            available_all,
            installed=installed,
            preferred_tier="mid",
            fallback=selfcheck_model,
            prefer_model=prefer_review,
        )
        verdict = selfcheck.check(
            user_text,
            text,
            model=review_model,
            expect_detail=tier != "tiny",
            use_llm=SELFCHECK_LLM and can_retry,
        )
        if not verdict.ok and "error" in verdict.problems:
            patched = selfcheck.patch_math_answer(user_text, text)
            if patched:
                text = patched
                _emit(on_status, "restore", model=model, tier=tier, reason="math-patch")
                _emit_token(on_token, text)
                verdict = selfcheck.check(
                    user_text,
                    text,
                    model=None,
                    expect_detail=False,
                    use_llm=False,
                )
                if verdict.ok:
                    verdict = replace(verdict, note="math-patched")
        attempts.append(
            _Attempt(text=text, verdict=verdict, tier=tier, model=model, ctx=ctx_plan)
        )
        _emit(
            on_status,
            "selfcheck",
            ok=verdict.ok,
            problems=verdict.problems,
            note=verdict.note,
            attempt=attempt_no,
            model=model,
            checked=verdict.checked,
        )

        if verdict.ok or not can_retry:
            break

        _emit(
            on_status,
            "retry",
            attempt=attempt_no + 1,
            problems=verdict.problems,
            reason=verdict.summary(),
            from_model=model,
            to_model=(next_entry.get("model") or "").strip(),
        )
        retry_hint = verdict.hint
        current_entry = next_entry

    best = _best_attempt(attempts)
    if best is not attempts[-1]:
        _emit(on_status, "restore", model=best.model, tier=best.tier)
        _emit_token(on_token, best.text)

    if stream and verbose:
        print()

    return stamp(
        OrchestraResult(
            text=best.text,
            tier=best.tier,
            model=best.model,
            need_web=decision.need_web,
            route_reason=decision.reason,
            escalated=_escalated(start_tier, best.tier)
            or (best.model != start_model_name and len(attempts) > 1),
            attempts=len(attempts),
            checked=best.verdict.checked,
            problems=list(best.verdict.problems),
            num_ctx=best.ctx.num_ctx,
            used_history=best.ctx.use_history,
            context_reason=best.ctx.reason,
            need_local_time=decision.need_local_time,
            need_calc=decision.need_calc,
            gen=effective_gen,
        )
    )
