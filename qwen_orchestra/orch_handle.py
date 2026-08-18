"""Публичный handle(): route → plan? → worker → selfcheck."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import orchestra as orch
from . import selfcheck
from . import settings as app_settings
from .calc import direct_calc_text
from .llm import GenOptions, installed_models, merge_gen, worker_gen
from .orchestra import (
    NUM_CTX_MIN,
    ContextPlan,
    OrchestraResult,
    PlanConfirmCallback,
    StatusCallback,
    TokenCallback,
)
from .orch_cycle import run_worker_attempts
from .orch_plan_run import _forced_decision, _run_plan_pipeline
from .orch_pool import (
    _entry_by_id,
    _entry_reachable,
    _fit_tier,
    _pick_entry_for_tier,
    _pool_rank,
    available_pool_entries,
)
from .orch_status import _cli_status_printer, _emit, _emit_token, _with_elapsed
from .orch_worker import _Attempt
from .router import ALL_TIERS, RouteDecision, Tier, route


def handle(
    user_text: str,
    history: list[dict] | None = None,
    *,
    force_tier: Tier | None = None,
    force_model: str | None = None,
    stream: bool = True,
    verbose: bool = True,
    on_token: TokenCallback | None = None,
    on_status: StatusCallback | None = None,
    gen: GenOptions | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    seed: int | None = None,
    num_predict: int | None = None,
    repeat_penalty: float | None = None,
    presence_penalty: float | None = None,
    frequency_penalty: float | None = None,
    stop: list[str] | tuple[str, ...] | str | None = None,
    keep_alive: str | None = None,
    allow_plan: bool = True,
    system_extra: str | None = None,
    on_plan_confirm: PlanConfirmCallback | None = None,
    force_need_calc: bool | None = None,
    force_need_web: bool | None = None,
    force_need_local_time: bool | None = None,
    installed: set[str] | None = None,
) -> OrchestraResult:
    history = history or []
    app_settings.ensure_bootstrapped()
    effective_gen = worker_gen(
        merge_gen(
            gen,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            num_predict=num_predict,
            repeat_penalty=repeat_penalty,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            stop=stop,
            keep_alive=keep_alive,
        )
    )
    if (
        history
        and history[-1].get("role") == "user"
        and (history[-1].get("content") or "").strip() == (user_text or "").strip()
    ):
        history = history[:-1]

    if stream and on_token is None and verbose:
        on_token = lambda t: print(t, end="", flush=True)  # noqa: E731
    if on_status is None and verbose:
        on_status = _cli_status_printer(verbose)
    on_status, elapsed_of = _with_elapsed(on_status)

    def _stamp(result: OrchestraResult) -> OrchestraResult:
        return replace(result, elapsed_ms=elapsed_of())

    with app_settings.runtime_lock:
        pool = [dict(e) for e in orch.POOL]
        selfcheck_model = orch.SELFCHECK_MODEL

    ollama_exc: Exception | None = None
    if installed is not None:
        have = set(installed)
        ollama_ok = True
    else:
        try:
            have = set(installed_models())
            ollama_ok = True
        except Exception as exc:  # noqa: BLE001
            have = set()
            ollama_ok = False
            ollama_exc = exc
    installed = have

    available_all = available_pool_entries(installed, pool=pool, routed_only=False)
    available_routed = available_pool_entries(installed, pool=pool, routed_only=True)
    available = {str(e["tier"]) for e in available_routed if e.get("tier")}
    if not available_all:
        if not ollama_ok and ollama_exc is not None:
            raise RuntimeError(f"Ollama недоступна: {ollama_exc}") from ollama_exc
        raise RuntimeError("Нет установленных моделей оркестра")

    forced_entry: dict[str, Any] | None = None
    decision: RouteDecision

    if force_model:
        entry = _entry_by_id(force_model, pool)
        if entry is None:
            raise ValueError(f"Модель пула не найдена: {force_model}")
        if not _entry_reachable(
            entry, installed, or_ok=app_settings.openrouter_configured()
        ):
            name = (entry.get("model") or "").strip()
            if (entry.get("provider") or "ollama") == "openrouter":
                raise ValueError(
                    f"OpenRouter-модель `{name}` недоступна. Задайте API-ключ "
                    f"или {app_settings.OPENROUTER_API_KEY_ENV}."
                )
            raise ValueError(
                f"Модель {name} не установлена. Установите: ollama pull {name}"
            )
        forced_entry = entry
        tier_for_force: Tier = str(entry.get("tier") or "mid")
        decision = _forced_decision(
            user_text,
            tier=tier_for_force,
            reason=f"forced_model:{force_model}",
            force_need_web=force_need_web,
            force_need_calc=force_need_calc,
            force_need_local_time=force_need_local_time,
        )
    elif force_tier:
        if force_tier not in ALL_TIERS:
            raise ValueError(f"Неизвестный tier: {force_tier}")
        if force_tier not in available:
            raise ValueError(
                f"Тир «{force_tier}» без доступных моделей в пуле. "
                f"Назначьте модель с этим тиром или выберите другую."
            )
        forced_entry = _pick_entry_for_tier(force_tier, available_routed)
        decision = _forced_decision(
            user_text,
            tier=force_tier,
            reason=f"forced:{force_tier}",
            force_need_web=force_need_web,
            force_need_calc=force_need_calc,
            force_need_local_time=force_need_local_time,
        )
    else:
        if not available:
            raise RuntimeError(
                "Нет моделей с тиром для Auto. Назначьте tier в пуле "
                "или выберите модель вручную."
            )
        decision = route(user_text)
        if decision.ok:
            fitted = _fit_tier(decision.tier, available)
            if fitted != decision.tier:
                decision = RouteDecision(
                    decision.ok,
                    fitted,
                    decision.need_web,
                    f"{decision.reason}+fit:{fitted}",
                    decision.reply if fitted == "tiny" else "",
                    need_local_time=decision.need_local_time,
                    need_calc=decision.need_calc,
                )

    def _resolve_entry(tier: Tier) -> dict[str, Any]:
        if forced_entry is not None and (
            force_model
            or (forced_entry.get("tier") or "") == tier
        ):
            if force_model:
                return forced_entry
        picked = _pick_entry_for_tier(tier, available_routed)
        if picked:
            return picked
        if available_routed:
            return max(
                available_routed,
                key=lambda e: (_pool_rank(e), str(e.get("id") or "")),
            )
        return available_all[0]

    start_entry = (
        forced_entry
        if force_model and forced_entry is not None
        else _resolve_entry(decision.tier)
    )
    start_model_name = (start_entry.get("model") or "").strip()

    _emit(
        on_status,
        "route",
        ok=decision.ok,
        tier=decision.tier,
        need_web=decision.need_web,
        need_local_time=decision.need_local_time,
        need_calc=decision.need_calc,
        reason=decision.reason,
        model=start_model_name,
        pool_id=start_entry.get("id"),
    )

    if not decision.ok:
        text = decision.reply or "Не понял запрос. Переформулируйте, пожалуйста."
        _emit_token(on_token, text)
        if stream and verbose:
            print()
        fb = _fit_tier("tiny", available) if available else decision.tier
        fb_entry = _resolve_entry(fb)
        return _stamp(OrchestraResult(
            text=text,
            tier=fb,
            model=(fb_entry.get("model") or "").strip(),
            need_web=False,
            route_reason=decision.reason,
            checked=False,
            need_local_time=False,
            gen=effective_gen,
        ))

    start_tier: Tier = decision.tier
    attempts: list[_Attempt] = []
    retry_hint: str | None = None
    lock_forced = bool(force_model)

    if decision.tier == "tiny" and decision.reply and not force_model:
        tiny_entry = _resolve_entry("tiny")
        tiny_name = (tiny_entry.get("model") or "").strip()
        verdict = selfcheck.check(
            user_text, decision.reply, model=None, expect_detail=False, use_llm=False
        )
        tiny_ctx = ContextPlan(
            history=[], num_ctx=NUM_CTX_MIN, use_history=False, reason="tiny-reply"
        )
        attempts.append(
            _Attempt(
                text=decision.reply,
                verdict=verdict,
                tier="tiny",
                model=tiny_name,
                ctx=tiny_ctx,
            )
        )
        _emit(
            on_status,
            "selfcheck",
            ok=verdict.ok,
            problems=verdict.problems,
            note=verdict.note,
            attempt=1,
            model=tiny_name,
            checked=verdict.checked,
        )
        if verdict.ok:
            _emit_token(on_token, decision.reply)
            if stream and verbose:
                print()
            return _stamp(OrchestraResult(
                text=decision.reply,
                tier="tiny",
                model=tiny_name,
                need_web=False,
                route_reason=decision.reason,
                attempts=1,
                checked=verdict.checked,
                problems=list(verdict.problems),
                need_local_time=decision.need_local_time,
                need_calc=False,
                gen=effective_gen,
            ))
        mid = _fit_tier("mid", available)
        mid_entry = _resolve_entry(mid)
        _emit(
            on_status,
            "retry",
            attempt=2,
            problems=verdict.problems,
            from_model=tiny_name,
            to_model=(mid_entry.get("model") or "").strip(),
        )
        retry_hint = verdict.hint
        decision = RouteDecision(
            True,
            mid,
            decision.need_web,
            decision.reason + " +recheck",
            "",
            need_local_time=decision.need_local_time,
            need_calc=decision.need_calc,
        )
        lock_forced = False

    if force_need_calc is not None:
        decision = replace(decision, need_calc=bool(force_need_calc))
    if force_need_web is not None:
        decision = replace(decision, need_web=bool(force_need_web))
    if force_need_local_time is not None:
        decision = replace(
            decision,
            need_local_time=bool(force_need_local_time) or bool(decision.need_web),
        )
    if decision.need_web:
        decision = replace(decision, need_local_time=True)

    if allow_plan and not force_model and not attempts:
        planned = _run_plan_pipeline(
            user_text,
            history,
            decision=decision,
            force_tier=force_tier,
            available=available,
            installed=installed,
            stream=stream,
            on_token=on_token,
            on_status=on_status,
            effective_gen=effective_gen,
            on_plan_confirm=on_plan_confirm,
        )
        if planned is not None:
            if stream and verbose:
                print()
            return _stamp(planned)

    if (
        allow_plan
        and not force_model
        and not force_tier
        and decision.need_calc
        and not decision.need_web
        and not attempts
    ):
        cpu_text = direct_calc_text(user_text)
        if cpu_text and selfcheck.is_math_only_question(user_text):
            _emit_token(on_token, cpu_text)
            if stream and verbose:
                print()
            return _stamp(
                OrchestraResult(
                    text=cpu_text,
                    tier=decision.tier,
                    model="cpu",
                    need_web=False,
                    route_reason=f"{decision.reason}+calc-direct",
                    attempts=1,
                    checked=True,
                    need_local_time=decision.need_local_time,
                    need_calc=True,
                    gen=effective_gen,
                    context_reason="calc-direct",
                )
            )

    current_entry = (
        forced_entry
        if lock_forced and forced_entry is not None
        else _resolve_entry(decision.tier)
    )
    return run_worker_attempts(
        user_text=user_text,
        history=history,
        decision=decision,
        current_entry=current_entry,
        available_routed=available_routed,
        available_all=available_all,
        installed=installed,
        attempts=attempts,
        retry_hint=retry_hint,
        system_extra=system_extra,
        on_status=on_status,
        on_token=on_token,
        stream=stream,
        verbose=verbose,
        effective_gen=effective_gen,
        selfcheck_model=selfcheck_model,
        start_tier=start_tier,
        start_model_name=start_model_name,
        stamp=_stamp,
    )
