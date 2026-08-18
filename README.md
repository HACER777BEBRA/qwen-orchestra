# Qwen Orchestra

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Ollama](https://img.shields.io/badge/Ollama-local%20LLM-000000)](https://ollama.com)
[![FastAPI](https://img.shields.io/badge/FastAPI-SSE%20чат-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Локальный оркестр LLM: несколько моделей [Qwen](https://ollama.com/library/qwen3.5) работают как одна система. Роутер выбирает тир под задачу, воркер отвечает, selfcheck отбраковывает слабый ответ и поднимает модель сильнее.

Веб-чат в стиле Cursor Chat — тёмная тема, один пользователь, по умолчанию только `127.0.0.1`. Это **не** облачный бот и **не** IDE: чат и composer текста к оркестру.

```
user → route → plan context → worker (± web / ± время ПК) → selfcheck
                 ↑                                    │ не ok
                 └──────── retry (больший rank) ──────┘
```

---

## Возможности

- **Сам выбирает модель.** Запрос идёт в **ваш** пул: Ollama, OpenRouter или смесь. Роутер ставит тир, эскалация идёт по rank — лёгкое не гоняет тяжёлую модель зря.
- **Переделывает слабый ответ.** Правила + LLM-ревью: пусто, отказ, галлюцинация в арифметике, иероглифы, обрыв кода. Retry — на модели с большим rank.
- **Ищет в интернете.** `web_search` и чтение URL по запросу («поищи», «погугли», свежие факты). Кэш между retry, лимит вызовов.
- **Знает время ПК.** Если в вопросе есть дата/часы/«сейчас», в контекст попадает снимок часов и часового пояса с машины.
- **Чат как в IDE.** Markdown, подсветка кода, формулы KaTeX, чипы тира / ctx / selfcheck, сайдбар чатов, селектор Auto или конкретной модели.
- **Монитор железа.** Правая панель: CPU, RAM, GPU/VRAM, температуры NVIDIA, какие модели лежат в Ollama и на GPU или CPU.
- **Локально и в сеть.** По умолчанию localhost. `--share` открывает LAN / проброшенный порт; опциональный пароль `--token`. Один общий сеанс, без аккаунтов.
- **Внешние модели.** OpenRouter в том же пуле (ключ в env или `secrets.json`). Роутер и selfcheck остаются локальными; tools пока только на Ollama.
- **Встраивается в бота.** `from qwen_orchestra import Client, GenOptions` — ask / route / health / settings in-process, без HTTP.

---

## Пул моделей

Состав пула **любой**: локальные теги Ollama, внешние id OpenRouter, одна модель или десять. Оркестр не требует фиксированного набора. Записи — в `settings.json` / UI «Модели и промпты»: provider, id, промпт, опционально **тир + rank**.

- **Тир задан** — участвует в Auto и эскалации. Несколько моделей на один тир: берётся с max rank.
- **Тир пуст** — только ручной выбор в селекторе (`force_model`).
- Health ок, если доступна хотя бы одна модель пула.

Пример (не обязательный набор):

| Тир | Пример модели | Зачем в пуле |
|-----|---------------|--------------|
| **tiny** | Qwen3.5 0.8b | короткие реплики и роутинг |
| **mid** | Qwen3.5 4b | обычные вопросы, перевод, how-to |
| **coder** | любая coder / 14b / внешняя | код, отладка, эскалация |

### Правила Auto (кратко)

| Запрос | Минимум | Потолок |
|--------|---------|---------|
| приветствие, простая арифметика | tiny | — |
| объяснения, перевод, простой код, интернет | mid | — |
| «кратко / коротко / в двух словах» | — | mid |
| сравнение, архитектура, длинный анализ | heavy | — |
| traceback, сервис/API, рефакторинг, тесты | coder | — |

Tiny **не может занизить** сложность: floor в коде, не в промпте. Осмысленный вопрос по умолчанию не ниже mid. `xlarge` / `ultra` / `frontier` в Auto почти всегда через эскалацию, не первым ходом.

---

## Слои продукта

### Ядро — `qwen_orchestra`

Пакет Python 3.10+: `orchestra.handle` → роутер → план контекста → воркер → selfcheck. Публичный вход для чужих приложений — `Client` (+ `GenOptions` для temperature / top_p / seed / `num_predict`). Роутер и selfcheck всегда `temperature=0`; сэмплинг воркера задаётся в `ask`.

Контекст: сначала **не обрезать запрос**, потом минимум VRAM. `num_ctx` = база × запас % модели, clamp 256…8192 (или `max_ctx` записи). История для heavy/xlarge/coder — только если есть отсылки («выше», «продолжи», «исправь»). У Qwen3.5 всегда `think: false`, иначе ломаются JSON-роутер и tools.

### Веб — `server.py` + `web/`

FastAPI, порт **8787**, SSE-стрим токенов. Чаты in-memory (сброс при рестарте). UI без npm/CDN: marked, DOMPurify, highlight.js, KaTeX в `web/vendor/`. Настройки пула и промптов — страница «Модели и промпты». Метрики правой панели — `GET /api/metrics` (psutil, nvidia-smi, Ollama `/api/ps`), не через SSE чата.

### SDK

```python
from qwen_orchestra import Client, GenOptions

client = Client()
print(client.health())
result = client.ask("2+2", temperature=0.1)
# result = client.ask("…", gen=GenOptions(temperature=0.2, seed=42, num_predict=256))
print(result.text, result.tier, result.model)
```

Полный контракт: [docs/SDK.md](docs/SDK.md). Примеры: `examples/ask_sdk.py`, `examples/ask_sdk_bot.py`.

### CLI и лаунчер

`orchestra_chat.py` — интерактивный оркестр (`/tier`, `/auto`, `/clear`). `ask_orchestra.py` — один вопрос. `open_web.py` / `QwenChat.exe` поднимают Ollama при необходимости, ждут `/api/ready` и открывают браузер. Меню режимов: `start.bat`.

---

## Стек

| Слой | Технологии |
|------|------------|
| Ядро | Python 3.10+, пакет `qwen_orchestra` |
| Локальные модели | [Ollama](https://ollama.com) (`localhost:11434`) |
| Внешние модели | [OpenRouter](https://openrouter.ai) (опционально) |
| Веб-сервер | FastAPI, Uvicorn, Pydantic, SSE |
| Метрики | psutil, nvidia-smi, Ollama `/api/ps` |
| Tools | ddgs, fetch URL, часы/TZ ПК |
| UI | vanilla HTML/CSS/JS, без npm/CDN |
| Лаунчер | `open_web.py` → опционально `QwenChat.exe` (PyInstaller) |

---

## Быстрый старт

```powershell
git clone https://github.com/bebra228hacer/qwen-orchestra.git
cd qwen-orchestra
python -m pip install -e ".[web]"
ollama pull qwen3.5:0.8b
ollama pull qwen3.5:4b
python open_web.py
# → http://127.0.0.1:8787
```

Установка, пул моделей, `--share`, CLI и SDK — в **[SETUP.md](SETUP.md)**.

---

## Документация

| Файл | Для кого |
|------|----------|
| [SETUP.md](SETUP.md) | Установка, модели, запуск, CLI, SDK |
| [docs/SDK.md](docs/SDK.md) | Встраивание в другой проект / бот |
| [docs/SDK_FOR_AGENTS.md](docs/SDK_FOR_AGENTS.md) | Шпаргалка в чужой `.cursor/rules/` |
| [AGENTS.md](AGENTS.md) | Карта кода и правила для AI-агентов этого репо |
| [THANKS_NEURAL_NETS.md](THANKS_NEURAL_NETS.md) | Благодарность нейронкам |

---

## Лицензия

Код — MIT. Модели Qwen — по их лицензиям через Ollama / провайдера.
