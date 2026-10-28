"""Публичный in-process клиент оркестра для других Python-приложений."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import settings as app_settings
from .llm import GenOptions, get_ollama_host, installed_models, set_ollama_host
from .orchestra import (
    MODELS,
    OrchestraResult,
    handle,
    missing_models,
    missing_optional_models,
)
from .router import RouteDecision, Tier, route as route_fn
from .selfcheck import Verdict
from .vision import prepare_images

StatusCallback = Callable[[str, dict[str, Any]], None]
TokenCallback = Callable[[str], None]


class Client:
    """Локальный SDK: роутинг + оркестр + настройки поверх Ollama / OpenRouter."""

    def __init__(
        self,
        *,
        ollama_host: str | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        if ollama_host:
            set_ollama_host(ollama_host)
        if settings_path is not None:
            app_settings.set_settings_path(settings_path)
        app_settings.bootstrap()

    @property
    def ollama_host(self) -> str:
        return get_ollama_host()

    @property
    def settings_path(self) -> Path:
        return app_settings.get_settings_path()

    def ready(self) -> bool:
        """Ollama отвечает на /api/tags."""
        try:
            installed_models()
            return True
        except Exception:  # noqa: BLE001
            return False

    def health(self) -> dict[str, Any]:
        """Снимок как у GET /api/health (без HTTP)."""
        app_settings.ensure_bootstrapped()
        ollama_ok = False
        models: list[str] = []
        missing: list[str] = []
        missing_optional: list[str] = []
        error: str | None = None
        router_missing = False
        planner_missing = False
        cfg = app_settings.get_settings()
        try:
            models = installed_models()
            have = set(models)
            ollama_ok = True
            missing = missing_models(have)
            missing_optional = missing_optional_models(have)
            router_name = cfg.router_model
            if router_name and router_name not in have:
                router_missing = not any(
                    m == router_name or m.startswith(router_name + ":") for m in have
                )
            planner_name = cfg.planner_model
            if planner_name and planner_name not in have:
                planner_missing = not any(
                    m == planner_name or m.startswith(planner_name + ":") for m in have
                )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            have: set[str] = set()
            missing = missing_models(have)
            missing_optional = missing_optional_models(have)
            router_missing = True
            planner_missing = True
        return {
            "ok": not missing,
            "ollama": ollama_ok,
            "models": models,
            "missing": missing,
            "missing_optional": missing_optional,
            "router_model": cfg.router_model,
            "router_missing": router_missing,
            "planner_model": cfg.planner_model,
            "planner_missing": planner_missing,
            "tiers": dict(MODELS),
            "pool": [m.to_dict() for m in cfg.models],
            "slots": [m.to_dict() for m in cfg.models],
            "providers": {"openrouter": app_settings.openrouter_status()},
            "error": error,
        }

    def route(self, user_text: str) -> RouteDecision:
        """Только роутинг (без генерации воркера)."""
        return route_fn(user_text)

    def ask(
        self,
        user_text: str,
        history: list[dict] | None = None,
        *,
        force_tier: Tier | None = None,
        force_model: str | None = None,
        stream: bool = True,
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
        on_plan_confirm: Callable[[dict[str, Any]], str] | None = None,
        images: list[bytes | str | Path] | None = None,
    ) -> OrchestraResult:
        """Полный цикл оркестра. По умолчанию без print в stdout.

        ``images`` — картинки (путь, байты или base64/data-URL; до ``vision.MAX_IMAGES``):
        слишком крупные сжимаются, vision-модель (gemma3, llava, …) переводит их в текст,
        описание добавляется в контекст основной модели (план при картинках отключён).

        Сэмплинг воркера: ``temperature`` / ``gen=GenOptions(...)`` и соседние
        параметры (``top_p``, ``seed``, ``num_predict``, …). Роутер, планировщик
        и selfcheck всегда с temperature=0 и эти опции не меняют.
        ``allow_plan=False`` — без декомпозиции (один воркер).
        ``on_plan_confirm`` — колбэк утверждения плана (``approve`` / ``reject``);
        без него план выполняется сразу.
        """
        prepared = (
            [
                p.b64
                for p in prepare_images(
                    [Path(i).read_bytes() if isinstance(i, Path) else i for i in images]
                )
            ]
            if images
            else None
        )
        return handle(
            user_text,
            history,
            images=prepared,
            force_tier=force_tier,
            force_model=force_model,
            stream=stream,
            verbose=False,
            on_token=on_token,
            on_status=on_status,
            gen=gen,
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
            allow_plan=allow_plan,
            on_plan_confirm=on_plan_confirm,
        )

    def get_settings(self) -> dict[str, Any]:
        app_settings.ensure_bootstrapped()
        return app_settings.public_settings_payload()

    def update_settings(
        self,
        models: list[dict[str, Any]] | None = None,
        *,
        slots: list[dict[str, Any]] | None = None,
        router_model: str | None = None,
        planner_model: str | None = None,
        selfcheck: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        app_settings.ensure_bootstrapped()
        payload = models if models is not None else slots
        if payload is None:
            raise ValueError("Нужен список models")
        app_settings.update_settings(
            payload,
            router_model=router_model,
            planner_model=planner_model,
            selfcheck=selfcheck,
            plan=plan,
        )
        return app_settings.public_settings_payload()

    def reset_settings(self) -> dict[str, Any]:
        app_settings.reset_settings()
        return app_settings.public_settings_payload()

    def add_model(
        self,
        *,
        model: str,
        label: str | None = None,
        router_prompt: str | None = None,
        model_id: str | None = None,
        tier: str | None = None,
        rank: int | None = None,
        provider: str = "ollama",
        ctx_overhead_pct: int | None = None,
        max_ctx: int | None = None,
    ) -> dict[str, Any]:
        """Добавить или обновить модель в пуле."""
        app_settings.ensure_bootstrapped()
        app_settings.add_model(
            model=model,
            label=label,
            router_prompt=router_prompt,
            model_id=model_id,
            tier=tier,
            rank=rank,
            provider=provider,
            ctx_overhead_pct=ctx_overhead_pct,
            max_ctx=max_ctx,
        )
        return app_settings.public_settings_payload()

    def add_slot(self, **kwargs: Any) -> dict[str, Any]:
        """Совместимость: см. add_model."""
        if "slot_id" in kwargs and "model_id" not in kwargs:
            kwargs["model_id"] = kwargs.pop("slot_id")
        kwargs.pop("router_auto", None)
        return self.add_model(**kwargs)

    def delete_model(self, model_id: str) -> dict[str, Any]:
        """Убрать модель из пула."""
        app_settings.ensure_bootstrapped()
        app_settings.delete_model(model_id)
        return app_settings.public_settings_payload()

    def delete_slot(self, slot_id: str) -> dict[str, Any]:
        return self.delete_model(slot_id)

    def set_openrouter_api_key(self, api_key: str | None) -> dict[str, Any]:
        """Сохранить или очистить (None) ключ OpenRouter в secrets.json."""
        app_settings.ensure_bootstrapped()
        app_settings.set_openrouter_api_key(api_key)
        return app_settings.public_settings_payload()


__all__ = [
    "Client",
    "GenOptions",
    "OrchestraResult",
    "RouteDecision",
    "Tier",
    "Verdict",
]
