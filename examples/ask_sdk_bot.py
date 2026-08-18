"""Шаблон SDK для бота: локальный Ollama + опционально OpenRouter.

Запуск из корня репо оркестра или после `pip install -e .`:

    python examples/ask_sdk_bot.py
    python examples/ask_sdk_bot.py --settings %TEMP%\\my-bot-orchestra.json

В другом проекте скопируй паттерн и укажи свой settings_path.
Полная документация: docs/SDK.md
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from qwen_orchestra import Client

# Полный гайд: docs/SDK.md — GenOptions / temperature / seed / num_predict …


def build_client(settings_path: Path) -> Client:
    client = Client(settings_path=settings_path)

    # Опционально: облачная модель в пуле (нужен OPENROUTER_API_KEY).
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        client.set_openrouter_api_key(key)
        client.add_model(
            provider="openrouter",
            model="google/gemini-2.5-flash",
            tier="frontier",
            rank=8,
            label="Gemini Flash · OR",
        )
    return client


def main() -> None:
    parser = argparse.ArgumentParser(description="Демо Client для бота")
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path(tempfile.gettempdir()) / "qwen-orchestra-bot-demo.json",
        help="Изолированный settings.json (не трогает конфиг репо)",
    )
    parser.add_argument(
        "--force-model",
        default=None,
        help="id пула, напр. ollama:qwen3.5:4b или openrouter:google/gemini-2.5-flash",
    )
    args = parser.parse_args()

    client = build_client(args.settings)
    print("settings:", client.settings_path)
    print("ready:", client.ready())
    health = client.health()
    print("health ok:", health.get("ok"), "missing:", health.get("missing"))
    print("openrouter:", health.get("providers", {}).get("openrouter"))

    history: list[dict] = []
    question = "Кратко: что такое оркестр LLM?"
    print("route:", client.route(question))

    chunks: list[str] = []

    def on_token(t: str) -> None:
        chunks.append(t)
        print(t, end="", flush=True)

    def on_status(event: str, payload: dict) -> None:
        if event in {"route", "worker", "retry", "selfcheck", "plan", "plan_ready", "plan_accepted", "plan_rejected", "plan_step", "compose"}:
            print(f"\n[{event}] {payload}", flush=True)

    result = client.ask(
        question,
        history=history,
        force_model=args.force_model,
        temperature=0.2,
        # gen=GenOptions(temperature=0.2, seed=42, num_predict=512),
        on_token=on_token,
        on_status=on_status,
    )
    print()
    print(
        f"→ model={result.model} tier={result.tier} "
        f"attempts={result.attempts} checked={result.checked} gen={result.gen}"
    )

    # Как хранить историю в боте:
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": result.text})


if __name__ == "__main__":
    main()
