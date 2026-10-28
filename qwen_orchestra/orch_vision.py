"""Зрение как предобработка: vision-модель (gemma3 и др.) расшифровывает картинки в текст.

Текст уходит в system основной модели (`system_extra`), дальше работает обычный оркестр:
роутер → воркер → selfcheck. Основная модель сама решает, что делать с описанием.
"""

from __future__ import annotations

from typing import Any

from .llm import GenOptions, chat
from .orchestra import (
    CTX_SAFETY_FLAT,
    CTX_TEMPLATE_TOKENS,
    NUM_CTX_MAX,
    StatusCallback,
)
from .orch_ctx import _estimate_tokens, _snap_step
from .orch_status import _emit
from .vision import is_vision_model

DEFAULT_IMAGE_PROMPT = "Опиши, что изображено на картинке."
TOKENS_PER_IMAGE = 300  # gemma3 — 256 токенов на картинку, с запасом
DESCRIBE_MAX_TOKENS = 700
DESCRIBE_MAX_CHARS = 6000

DESCRIBE_SYSTEM = (
    "Ты модуль зрения: тебя читает другая модель, которая САМА картинку не видит. "
    "Опиши изображение так, чтобы по описанию можно было ответить на вопрос пользователя. "
    "Сначала суть (что это: фото, скриншот, схема, документ, график), затем важные детали: "
    "объекты, люди, цвета, расположение, числа, подписи. Весь видимый текст перепиши дословно "
    "(в кавычках, на языке оригинала). Код, таблицы и формулы сохраняй как есть. "
    "Не отвечай на вопрос сам, не додумывай: что не разобрать — так и напиши. "
    "Если картинок несколько — по пунктам «Изображение 1», «Изображение 2»… "
    "Пиши на языке пользователя, без вступлений."
)

CONTEXT_HEADER = (
    "Пользователь приложил изображение(я). Ты их не видишь — ниже описание от модуля зрения "
    "({model}). Опирайся на него как на содержимое картинки; если нужной детали в описании нет, "
    "скажи об этом."
)


def pick_vision_entry(
    *,
    pool: list[dict[str, Any]],
    available_all: list[dict[str, Any]],
    installed: set[str],
) -> dict[str, Any]:
    """Vision-модель: доступная запись пула (локальная раньше облачной) → любая установленная."""
    cands = [e for e in available_all if is_vision_model(str(e.get("model") or ""))]
    if cands:
        cands.sort(
            key=lambda e: (
                (e.get("provider") or "ollama") != "ollama",
                -int(e.get("rank") or 0),
                str(e.get("id") or ""),
            )
        )
        return cands[0]
    for name in sorted(installed):
        if is_vision_model(name):
            return {"id": f"ollama:{name}", "model": name, "provider": "ollama", "tier": ""}
    raise RuntimeError(
        "Нет vision-модели для картинок. Установите, например: ollama pull gemma3:4b"
    )


def describe_images(
    user_text: str,
    images: list[str],
    *,
    entry: dict[str, Any],
    on_status: StatusCallback | None = None,
) -> str:
    """Расшифровать картинки в текст (один неблокирующий вызов vision-модели, temperature=0)."""
    model = (entry.get("model") or "").strip()
    provider = (entry.get("provider") or "ollama").strip() or "ollama"
    question = (user_text or "").strip() or DEFAULT_IMAGE_PROMPT
    prompt = f"Вопрос пользователя: {question}\n\nОпиши картинку(и)."
    _emit(on_status, "vision", model=model, provider=provider, images=len(images))

    fixed = (
        _estimate_tokens(DESCRIBE_SYSTEM + prompt)
        + len(images) * TOKENS_PER_IMAGE
        + DESCRIBE_MAX_TOKENS
        + CTX_TEMPLATE_TOKENS
    )
    num_ctx = min(NUM_CTX_MAX, _snap_step(fixed + max(CTX_SAFETY_FLAT, fixed // 10)))
    msg = chat(
        model,
        [
            {"role": "system", "content": DESCRIBE_SYSTEM},
            {"role": "user", "content": prompt, "images": list(images)},
        ],
        gen=GenOptions(temperature=0.0, num_predict=DESCRIBE_MAX_TOKENS),
        num_ctx=num_ctx,
        provider=provider,
    )
    text = (msg.get("content") or "").strip()
    if not text:
        raise RuntimeError(f"Vision-модель {model} вернула пустое описание картинки")
    return text[:DESCRIBE_MAX_CHARS]


def image_system_extra(description: str, model: str) -> str:
    return f"{CONTEXT_HEADER.format(model=model)}\n\n{description}"
