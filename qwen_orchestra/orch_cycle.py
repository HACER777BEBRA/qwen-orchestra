"""Цикл попыток воркера + selfcheck / retry / restore."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from . import selfcheck
from .calc import ensure_computed
from .llm import DEFAULT_WORKER_TEMPERATURE, GenOptions
from .orchestra import (
    MAX_ATTEMPTS,
    NUM_CTX_MAX,
    SELFCHECK_LLM,
    OrchestraResult,
    StatusCallback,
    TokenCallback,
)
from .orch_ctx import _CODEISH_RE, _snap_step, plan_worker_context
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
from .router import RouteDecision, Tier, tier_floor
from .selfcheck_hints import SAME_MODEL_FIX


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


def _gen_for_same_retry(
    base: GenOptions, current: GenOptions, probs: set[str]
) -> GenOptions:
    """Параметры повтора на той же модели: меньше температуры, больше штраф за повтор."""
    gen = current
    if probs & {"cjk", "language", "repetition"}:
        temp = base.temperature if base.temperature is not None else DEFAULT_WORKER_TEMPERATURE
        penalty = 1.25 if "repetition" in probs else 1.15
        gen = replace(
            gen,
            temperature=max(0.0, temp - 0.2),
            repeat_penalty=max(base.repeat_penalty or 1.0, penalty),
        )
    if "truncated" in probs and gen.num_predict:
        gen = replace(gen, num_predict=int(gen.num_predict) * 2)
    return gen


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

    attempt_base = len(attempts)
    same_used: set[str] = set()  # записи пула, где уже был повтор «на месте»
    ctx_bump = 0  # доп. ступень num_ctx после обрыва по длине
    gen_run = effective_gen
    prev_answer: str | None = None
    code_task = (
        start_tier == "coder"
        or "```" in user_text
        or bool(_CODEISH_RE.search(user_text))
        or tier_floor(user_text) == "coder"
    )

    for attempt in range(1, MAX_ATTEMPTS + 1 - attempt_base):
        model = (current_entry.get("model") or "").strip()
        provider = (current_entry.get("provider") or "ollama").strip() or "ollama"
        tier = str(current_entry.get("tier") or tier)
        attempt_no = attempt_base + attempt

        tool_list = _tools_for(decision)
        use_tools = bool(tool_list) and provider == "ollama"
        local_ctx = _local_context_for(
            decision, user_text, precompute=not use_tools
        )
        ctx_plan = plan_worker_context(
            user_text,
            history,
            tier,
            system_extra=system_extra,
            tools=tool_list if use_tools else None,
            local_context=local_ctx,
            retry_hint=retry_hint,
            prev_answer=prev_answer,
            need_web=decision.need_web,
            need_calc=decision.need_calc and use_tools,
            **_ctx_kwargs(current_entry),
        )
        if ctx_bump:
            ceiling = _ctx_kwargs(current_entry)["max_ctx"] or NUM_CTX_MAX
            ctx_plan = replace(
                ctx_plan,
                num_ctx=min(ceiling, _snap_step(ctx_plan.num_ctx + 1)),
                reason=f"{ctx_plan.reason}+len-retry",
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

        worker_meta: dict[str, Any] = {}
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
                gen=gen_run,
                system_extra=system_extra,
                tools=tool_list,
                meta=worker_meta,
                prev_answer=prev_answer,
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
                gen=gen_run,
                system_extra=system_extra,
                meta=worker_meta,
                prev_answer=prev_answer,
            )
            if decision.need_calc:
                text = ensure_computed(text)
            if stream and not stream_now:
                _emit_token(on_token, text)

        entry_key = str(current_entry.get("id") or model)
        next_entry = _next_entry_by_rank(
            current_entry, available_routed, code_task=code_task
        )
        room = attempt_no < MAX_ATTEMPTS
        can_esc = room and next_entry is not None
        can_same = room and entry_key not in same_used
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
        cut_off = bool(worker_meta.get("truncated"))
        verdict = selfcheck.check(
            user_text,
            text,
            model=review_model,
            expect_detail=tier != "tiny",
            use_llm=SELFCHECK_LLM and (can_esc or can_same) and not cut_off,
        )
        if cut_off and "truncated" not in verdict.problems:
            verdict = replace(
                verdict,
                ok=False,
                problems=[*verdict.problems, "truncated"],
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

        if verdict.ok:
            break

        # Тип проблемы решает, что делать: параметры/подсказка на той же модели
        # или эскалация на старшую (см. SAME_MODEL_FIX).
        probs = set(verdict.problems)
        fixable = bool(probs) and probs <= SAME_MODEL_FIX
        if fixable and can_same:
            same_used.add(entry_key)
            gen_run = _gen_for_same_retry(effective_gen, gen_run, probs)
            if "truncated" in probs:
                ctx_bump += 1
            prev_answer = text[:3000] if probs == {"incomplete"} else None
            _emit(
                on_status,
                "retry",
                attempt=attempt_no + 1,
                problems=verdict.problems,
                reason=f"{verdict.summary()}: повтор на той же модели",
                from_model=model,
                to_model=model,
            )
            retry_hint = verdict.hint
            continue

        if not can_esc:
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
        gen_run, ctx_bump, prev_answer = effective_gen, 0, None

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
