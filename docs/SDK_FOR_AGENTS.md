# Правило для AI-агента: qwen_orchestra SDK

Скопируй этот файл в `.cursor/rules/` или приложи к `AGENTS.md` **потребительского** проекта.  
Полный справочник: в репозитории оркестра — `docs/SDK.md`.

## Назначение

Локальный оркестр LLM: Ollama + опционально OpenRouter. Встраивание **in-process** через `Client`, не через HTTP UI.

```python
from qwen_orchestra import Client, GenOptions

client = Client(settings_path="…/orchestra_settings.json")
result = client.ask(
    "…",
    history=[…],  # БЕЗ текущего user
    temperature=0.2,  # или gen=GenOptions(temperature=0.2, seed=42, num_predict=512)
)
# result.gen — фактически применённые опции воркера
```

## Must

- Установка: `pip install -e <путь-к-клону-qwen-orchestra>` (ядро без `[web]`).
- Всегда задавай **свой** `settings_path` (не общий конфиг оркестра).
- Перед работой: `client.ready()` (Ollama) и `client.health()["ok"]`.
- Роутер и selfcheck — **локальные** Ollama (всегда `temperature=0`); OpenRouter только как воркер.
- Сэмплинг воркера — через `ask(temperature=…)`, `top_p`, `seed`, `num_predict`, `stop`, `keep_alive` или `gen=GenOptions(...)`. **Не** патчить `orchestra.py`.
- `force_model` = **id записи пула** (`ollama:…` / `openrouter:…`), не сырой tag.
- Стрим: `on_token` / `on_status`; на `retry`/`restore` сбрасывай показанный текст (`restore` + `reason=math-patch` — патч числа без эскалации). В payload есть `elapsed_ms`.
- Ключ OR: `OPENROUTER_API_KEY` или `client.set_openrouter_api_key`; не в git.
- Не дублируй роутинг/selfcheck; не импортируй внутренности пакета без нужды.
- Selfcheck по умолчанию бракует короткие «не могу»/«не знаю». В боте часто:
  `update_settings(models, selfcheck={"flag_refusal": false, "flag_uncertain": false, "flag_irrelevant": false})`.

## Пул

- С `tier` + `rank` → Auto и эскалация; без `tier` → только `force_model`.
- Тиры: `tiny` `nano` `small` `mid` `large` `heavy` `xlarge` `coder` `ultra` `frontier`.
- `provider`: `ollama` | `openrouter`. Tools (web) — только Ollama.

## API (кратко)

`ready` · `health` · `route` · `ask` · `get_settings` · `update_settings` · `reset_settings` · `add_model` · `delete_model` · `set_openrouter_api_key`

`ask` сэмплинг: `gen` / `temperature` / `top_p` / `top_k` / `seed` / `num_predict` / `repeat_penalty` / `presence_penalty` / `frequency_penalty` / `stop` / `keep_alive`

`OrchestraResult`: `text`, `tier`, `model`, `gen`, `attempts`, `checked`, `problems`, `need_web`, `need_local_time`, …

Defaults воркера: `temperature=0.3`, `keep_alive="10m"`.

## Не делай

- Не обещай OpenAI-compatible HTTP API.
- Не считай, что история всегда попадает в heavy/coder (там фильтр по отсылкам).
- Не коммить `secrets.json` / API-ключи.
- Не обходи `Client.ask` ради температуры — она уже в SDK.
