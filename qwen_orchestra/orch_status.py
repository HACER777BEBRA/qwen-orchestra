"""Колбэки статуса CLI / SSE: emit, тайминг, принтер."""

from __future__ import annotations

import time
from typing import Any, Callable

from .orchestra import ContextPlan, StatusCallback, TokenCallback
from .router import Tier


def _emit(on_status: StatusCallback | None, event: str, **payload: Any) -> None:
    if on_status:
        on_status(event, payload)


def _with_elapsed(
    on_status: StatusCallback | None, *, t0: float | None = None
) -> tuple[StatusCallback | None, Callable[[], int]]:
    """Добавляет elapsed_ms (от старта handle) и dt_ms (от прошлого события)."""
    start = time.monotonic() if t0 is None else t0
    last = [start]

    def elapsed_ms() -> int:
        return int((time.monotonic() - start) * 1000)

    if on_status is None:
        return None, elapsed_ms

    def _wrapped(event: str, payload: dict[str, Any]) -> None:
        now = time.monotonic()
        data = dict(payload)
        data["elapsed_ms"] = int((now - start) * 1000)
        data["dt_ms"] = int((now - last[0]) * 1000)
        last[0] = now
        on_status(event, data)

    return _wrapped, elapsed_ms


def _emit_token(on_token: TokenCallback | None, text: str) -> None:
    if on_token and text:
        on_token(text)


def _emit_context(on_status: StatusCallback | None, ctx_plan: ContextPlan, tier: Tier) -> None:
    _emit(
        on_status,
        "context",
        used_history=ctx_plan.use_history,
        num_ctx=ctx_plan.num_ctx,
        reason=ctx_plan.reason,
        history_messages=len(ctx_plan.history),
        tier=tier,
    )


def _cli_status_printer(verbose: bool) -> StatusCallback:
    def _print(event: str, payload: dict[str, Any]) -> None:
        elapsed = payload.get("elapsed_ms")
        clock = f" +{elapsed}ms" if isinstance(elapsed, int) else ""
        if event == "route":
            print(
                f"  [route] ok={payload.get('ok')} tier={payload.get('tier')} "
                f"web={payload.get('need_web')} ({payload.get('reason')}){clock}",
                flush=True,
            )
        elif event == "worker":
            web = " + web" if payload.get("need_web") else ""
            local = " + time" if payload.get("need_local_time") else ""
            calc = " + calc" if payload.get("need_calc") else ""
            attempt = payload.get("attempt") or 1
            suffix = f" (попытка {attempt})" if attempt > 1 else ""
            ctx = payload.get("num_ctx")
            hist = "hist" if payload.get("used_history") else "no-hist"
            ctx_s = f" ctx={ctx} {hist}" if ctx else ""
            print(
                f"  [worker] {payload.get('model')}{web}{local}{calc}{suffix}{ctx_s}{clock}",
                flush=True,
            )
        elif event == "context":
            hist = "история" if payload.get("used_history") else "без истории"
            print(
                f"  [context] {hist}, num_ctx={payload.get('num_ctx')} ({payload.get('reason')})",
                flush=True,
            )
        elif event == "selfcheck":
            state = "ok" if payload.get("ok") else ", ".join(payload.get("problems") or [])
            print(f"\n  [check] {state}{clock}", flush=True)
        elif event == "retry":
            print(
                f"  [retry] {payload.get('from_model')} -> {payload.get('to_model')} "
                f"({', '.join(payload.get('problems') or [])})",
                flush=True,
            )
        elif event == "restore":
            print(f"  [restore] лучший ответ: {payload.get('model')}", flush=True)
        elif event == "plan":
            print(
                f"  [plan] need={payload.get('need_plan')} ({payload.get('reason')}){clock}",
                flush=True,
            )
        elif event == "plan_ready":
            n = len(payload.get("steps") or [])
            print(
                f"  [plan_ready] {payload.get('planner_model')} · {n} шагов: "
                f"{payload.get('goal') or ''}",
                flush=True,
            )
        elif event == "plan_step":
            print(
                f"  [plan_step] {payload.get('index')}/{payload.get('total')} "
                f"{payload.get('title')} ({payload.get('tier')})",
                flush=True,
            )
        elif event == "plan_step_done":
            state = "fail" if payload.get("failed") else "ok"
            print(
                f"  [plan_step_done] {payload.get('id')} {state}",
                flush=True,
            )
        elif event == "compose":
            print(
                f"  [compose] {payload.get('model')} ({payload.get('tier')}){clock}",
                flush=True,
            )
        elif event == "plan_accepted":
            print("  [plan] утверждён, выполняем шаги", flush=True)
        elif event == "plan_rejected":
            print(
                f"  [plan] отклонён ({payload.get('reason')}), обычный ответ",
                flush=True,
            )

    del verbose
    return _print


def _status_scoped(
    on_status: StatusCallback | None,
    scope: str,
    **extra: Any,
) -> StatusCallback | None:
    if on_status is None:
        return None

    def _wrapped(event: str, payload: dict[str, Any]) -> None:
        data = dict(payload)
        data["scope"] = scope
        for key, val in extra.items():
            data.setdefault(key, val)
        on_status(event, data)

    return _wrapped


def _step_token_cb(
    on_status: StatusCallback | None, step_id: int
) -> TokenCallback | None:
    """Токены шага — в on_status(plan_token), не в публичный on_token склейки."""
    if on_status is None:
        return None

    def _cb(text: str) -> None:
        if text:
            _emit(on_status, "plan_token", id=step_id, text=text)

    return _cb
