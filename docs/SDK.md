# Qwen Orchestra SDK — гайд для приложений и AI-агентов

Документ для **встраивания** `qwen_orchestra` в другой Python-проект (бот, CLI, сервис).  
Карта исходников самого репозитория — [AGENTS.md](../AGENTS.md). Установка окружения — [SETUP.md](../SETUP.md).

**Версия пакета:** `0.1.2` · **Python:** ≥ 3.10 · **Публичный вход:** `from qwen_orchestra import Client, GenOptions`

---

## 0. Для AI-агента другого проекта (краткий контракт)

Читай этот раздел первым. Остальное — справочник по полям и примерам.

### Что это

In-process оркестр поверх **Ollama** (локально) и опционально **OpenRouter** (облако):

```
ask() → route → need_plan? → (planner → шаги → compose) | worker (± web / ± счёт ПК / ± время ПК)
              → selfcheck → retry на модель с большим rank (до 3 попыток)
```

Не HTTP-клиент к чужому серверу и не OpenAI-совместимый API. Вызовы идут **в том же процессе**, через `Client`.

### Минимальный сценарий

```python
from qwen_orchestra import Client, GenOptions

client = Client(settings_path="path/to/my_bot/settings.json")  # изолируй конфиг!
if not client.ready():
    raise SystemExit("Ollama не отвечает на localhost:11434")
h = client.health()
if not h["ok"]:
    raise SystemExit(f"Нет доступных моделей пула: {h['missing']}")

result = client.ask("привет", temperature=0.2)
# или: client.ask("…", gen=GenOptions(temperature=0.2, seed=42, num_predict=512))
print(result.text)
print(result.model, result.tier, result.gen)
```

### Обязательные правила интеграции

1. **Импортируй только** `from qwen_orchestra import Client` (и типы из `__init__`). Не дублируй роутинг / selfcheck в своём коде.
2. **Изолируй `settings_path`** на проект бота — иначе приложение подхватит `settings.json` из репо оркестра или общий `%LOCALAPPDATA%/qwen-orchestra/`.
3. **Ollama должна быть запущена** для роутера и selfcheck (они всегда локальные), даже если воркер — OpenRouter.
4. **`force_model`** — это **id записи пула** (`"ollama:qwen3.5:4b"`, `"openrouter:anthropic/claude-sonnet-4"`), не сырое имя модели Ollama/OR.
5. **История** в `ask(history=…)` — **без** текущего user-сообщения. Формат: `[{"role":"user"|"assistant","content":"..."}, ...]`.
6. В ботах всегда `verbose` уже выключен у `Client.ask`; стрим — через `on_token` / `on_status`, не через print.
7. **Tools** (`web_search`, `fetch_url`, `calculate`) работают **только у Ollama-воркера**. У OpenRouter tools нет: счёт — предрасчёт в system или строка `CALCULATE:` после ответа.
8. Ключ OpenRouter: env `OPENROUTER_API_KEY` **или** `client.set_openrouter_api_key(...)` → `secrets.json` рядом с settings. Не коммить ключ.
9. Health: `ok=true`, если ≥1 модель пула доступна (Ollama pull’нута **или** OpenRouter с ключом). `missing` — критичные дыры для Auto.
10. Не вызывай низкоуровневые модули (`orchestra.handle`, `llm.chat`) из чужого проекта без нужды — контракт = `Client`.

### Типичные задачи агента

| Задача | Что делать |
|--------|------------|
| Подключить бота | `pip install -e /path/to/qwen-orchestra` → `Client(settings_path=…)` |
| Только локальные модели | пул `provider="ollama"`, `ollama pull …`, `client.ready()` |
| Локально + OpenRouter | ключ + `add_model(..., provider="openrouter", model="vendor/model", tier=…)` |
| Всегда одна модель | `force_model="openrouter:…"` / `"ollama:…"` (id из пула) |
| Auto по сложности | записи пула **с** `tier` + `rank`; `ask()` без force |
| Стрим в Telegram/Discord | `on_token=lambda t: …` |
| Температура / seed / лимит токенов | `ask(..., temperature=0.2)` или `gen=GenOptions(...)` |
| Отладка выбора тира | `client.route(text)` до `ask` |

---

## 1. Установка в другой проект

### Вариант A — editable из клона (разработка)

```powershell
git clone https://github.com/bebra228hacer/qwen-orchestra.git
cd my-bot
python -m pip install -e "C:\path\to\qwen-orchestra"
# только ядро (без FastAPI): достаточно -e без [web]
```

Зависимости ядра: `ddgs`, `psutil`. Extra `[web]` нужен только для UI/сервера оркестра, **не** для SDK в боте.

### Вариант B — requirements.txt бота

```text
qwen-orchestra @ file:///C:/AI/Qwen2.5-3B
# или после публикации на PyPI: qwen-orchestra>=0.1.0
```

### Системные требования

| Компонент | Зачем |
|-----------|--------|
| [Ollama](https://ollama.com) на `http://localhost:11434` | Роутер, selfcheck, локальные воркеры |
| ≥1 модель в пуле | `health()["ok"]` |
| `OPENROUTER_API_KEY` (опц.) | Внешние модели в пуле |
| Сеть | Только для OpenRouter и web-tools |

Рекомендуемый минимум локальных моделей для Auto:

```powershell
ollama pull qwen3.5:0.8b   # роутер + tiny/nano
ollama pull qwen3.5:4b     # mid + selfcheck
ollama pull qwen3.5:9b     # heavy (по желанию)
```

---

## 2. Изоляция конфигурации

По умолчанию путь к settings:

1. Если рядом с пакетом есть репо с `settings.json` / `pyproject.toml` / `server.py` → файл в **корне того репо**.
2. Иначе → `%LOCALAPPDATA%/qwen-orchestra/settings.json` (Windows) или `~/.config/qwen-orchestra/settings.json`.

**Для бота всегда задавай свой файл:**

```python
from pathlib import Path
from qwen_orchestra import Client

ROOT = Path(__file__).resolve().parent
client = Client(
    ollama_host="http://127.0.0.1:11434",  # опционально
    settings_path=ROOT / "orchestra_settings.json",
)
# secrets.json появится рядом: ROOT / "secrets.json"
print(client.settings_path)
```

`Client(...)` сразу делает `bootstrap()`: создаёт defaults, если файла ещё нет.

---

## 3. API `Client`

```python
from qwen_orchestra import (
    Client,
    GenOptions,       # сэмплинг воркера
    OrchestraResult,  # результат ask
    RouteDecision,    # результат route
    Tier,             # alias str: "tiny"|"mid"|…
    Verdict,          # тип selfcheck (редко нужен снаружи)
    merge_gen,        # склеить GenOptions + overrides
)
```

### Конструктор

| Параметр | Тип | Описание |
|----------|-----|----------|
| `ollama_host` | `str \| None` | База Ollama (default `http://localhost:11434`) |
| `settings_path` | `str \| Path \| None` | Путь к `settings.json` |

Свойства: `client.ollama_host`, `client.settings_path`.

### `ready() -> bool`

`True`, если Ollama отвечает на `/api/tags`. **Не** проверяет пул и OpenRouter.

### `health() -> dict`

Снимок как у `GET /api/health` (без HTTP). Ключи:

| Ключ | Смысл |
|------|--------|
| `ok` | ≥1 доступная модель пула |
| `ollama` | Ollama доступна |
| `models` | список имён из Ollama `/api/tags` |
| `missing` | критичные дыры (ломают `ok`, если пул пуст/недоступен) |
| `missing_optional` | назначены, но недоступны (не ломают `ok` сами по себе в типичной схеме) |
| `router_model` | модель роутера из settings |
| `router_missing` | роутер не найден в Ollama |
| `planner_model` | модель JSON-планировщика |
| `planner_missing` | планировщик не найден (план пропускается, `ok` не ломает) |
| `tiers` | map `tier → лучшая модель` (derived) |
| `pool` / `slots` | записи пула (`to_dict`) |
| `providers.openrouter` | `{configured, from_env, from_file, base_url, api_key_env}` |
| `error` | текст ошибки Ollama или `None` |

### `route(user_text) -> RouteDecision`

Только классификация, без генерации воркера.

```python
@dataclass
class RouteDecision:
    ok: bool
    tier: str              # tiny…frontier / coder
    need_web: bool
    reason: str
    reply: str = ""        # короткий ответ tiny или отказ
    need_local_time: bool = False
    need_calc: bool = False
```

### `ask(user_text, history=None, *, ...) -> OrchestraResult`

Полный цикл. У `Client` всегда `verbose=False`.

| Параметр | Default | Описание |
|----------|---------|----------|
| `user_text` | — | текущий запрос |
| `history` | `None` | прошлые реплики **без** текущего user |
| `force_tier` | `None` | тир вручную (`"mid"`, `"coder"`, …); LLM-роутер не вызывается |
| `force_model` | `None` | **id пула**; LLM-роутер не вызывается |
| `stream` | `True` | стрим токенов в `on_token` |
| `on_token` | `None` | `Callable[[str], None]` — куски текста |
| `on_status` | `None` | `Callable[[str, dict], None]` — фазы оркестра |
| `gen` | `None` | `GenOptions` — пакет сэмплинга воркера |
| `temperature` | `None` | override; default воркера **0.3** |
| `top_p` | `None` | nucleus sampling |
| `top_k` | `None` | top-k (Ollama; на OR может игнорироваться) |
| `seed` | `None` | воспроизводимость (если бэкенд поддерживает) |
| `num_predict` | `None` | макс. токенов ответа (Ollama `num_predict` / OR `max_tokens`) |
| `repeat_penalty` | `None` | Ollama; на OR → `repetition_penalty` |
| `presence_penalty` | `None` | OpenRouter / OpenAI-совместимые |
| `frequency_penalty` | `None` | OpenRouter / OpenAI-совместимые |
| `stop` | `None` | стоп-последовательности (`str` / `list[str]`) |
| `keep_alive` | `None` | Ollama; default **`"10m"`** (`"0"` — выгрузить сразу) |
| `allow_plan` | `True` | декомпозиция сложных запросов; `False` — сразу один воркер |
| `on_plan_confirm` | `None` | колбэк `{goal, steps}` → `"approve"` / `"reject"`; без него план идёт сразу |

Плоские kwargs и поля `gen` **мержатся**: не-`None` плоский параметр побеждает.  
Роутер, планировщик и selfcheck **всегда** `temperature=0` — `GenOptions` на них не влияет.

```python
@dataclass
class OrchestraResult:
    text: str
    tier: str
    model: str                 # имя модели провайдера (не обязательно id пула)
    need_web: bool
    route_reason: str
    escalated: bool = False
    attempts: int = 1
    checked: bool = True
    problems: list[str]        # коды selfcheck, если были
    num_ctx: int
    used_history: bool = True
    context_reason: str = ""
    need_local_time: bool = False
    need_calc: bool = False
    gen: GenOptions | None     # фактически применённые опции воркера
    planned: bool = False
    plan_goal: str = ""
    plan_steps: list[dict]     # [{id, title, tier, model, prompt, text}]
```

Метаданные относятся к **выбранной** (лучшей/успешной) попытке.

---

## 3.1. Сэмплинг: `GenOptions`

```python
from qwen_orchestra import GenOptions

client.ask("Напиши короткий слоган", temperature=0.8)

client.ask(
    "Ответь одним словом: да или нет",
    gen=GenOptions(
        temperature=0.0,
        seed=42,
        num_predict=16,
        stop=["\n"],
    ),
)

# Детерминированный код / JSON-подобный стиль
client.ask(prompt, temperature=0.1, top_p=0.9, seed=7)

# Короткий ответ + сразу освободить VRAM (Ollama)
client.ask(prompt, num_predict=256, keep_alive="0")
```

| Поле | Ollama | OpenRouter | Зачем боту |
|------|--------|------------|------------|
| `temperature` | `options.temperature` | `temperature` | креатив vs стабильность |
| `top_p` | ✓ | ✓ | альтернатива/дополнение к temperature |
| `top_k` | ✓ | best-effort | жёстче ограничить хвост распределения |
| `seed` | ✓ | ✓ (если провайдер умеет) | воспроизводимые тесты / A-B |
| `num_predict` | `num_predict` | `max_tokens` | лимит длины ответа (стоимость/латентность) |
| `repeat_penalty` | ✓ | `repetition_penalty` | меньше зацикливаний |
| `presence_penalty` | — | ✓ | поощрять новые темы |
| `frequency_penalty` | — | ✓ | реже повторять те же токены |
| `stop` | ✓ | ✓ | обрезать на маркере / запретить уход в чат |
| `keep_alive` | top-level | — | держать/снять модель из VRAM |

Defaults воркера, если ничего не задано: `temperature=0.3`, `keep_alive="10m"`.

Рекомендации:

| Задача | Старт |
|--------|--------|
| FAQ / факты / код | `temperature=0.1…0.3`, опционально `seed` |
| Ролевая / креатив | `temperature=0.7…1.0`, `top_p≈0.9` |
| Жёсткий формат (одно слово, JSON-ish) | `temperature=0`, `num_predict` маленький, `stop` |
| Экономия OR | `num_predict` потолок под ваш UX |

`result.gen` — снимок применённых опций (удобно логировать в боте).

### Настройки пула

| Метод | Назначение |
|-------|------------|
| `get_settings()` | публичный payload (models, tiers, providers, selfcheck, plan, defaults…) |
| `update_settings(models=…, router_model=…, planner_model=…, selfcheck=…, plan=…)` | заменить пул (+ опц. роутер/планировщик/selfcheck/plan) |
| `reset_settings()` | defaults |
| `add_model(...)` | добавить/обновить одну запись |
| `delete_model(model_id)` | удалить по id |
| `set_openrouter_api_key(key \| None)` | записать/очистить ключ в `secrets.json` |

Алиасы совместимости: `add_slot` / `delete_slot` (старое имя «слот»).

#### `selfcheck` — отказы и неуверенность

По умолчанию короткие ответы с маркерами отказа («не могу», …) или неуверенности
(«не знаю», …) бракуются → retry. В боте часто нужно выключить:

```python
models = client.get_settings()["models"]
client.update_settings(
    models,
    selfcheck={
        "flag_refusal": False,      # не браковать «не могу»
        "flag_uncertain": False,    # не браковать «не знаю»
        "flag_irrelevant": False,   # не браковать LLM-вердикт «не по теме»
        # опционально свои фразы (null/отсутствие = встроенный список):
        # "refusal_patterns": ["не могу", "не смог", "i cannot"],
        # "uncertain_patterns": ["не знаю"],
    },
)
```

Или в `settings.json` рядом с пулом:

```json
"selfcheck": {
  "flag_refusal": true,
  "flag_uncertain": true,
  "flag_irrelevant": true
}
```

#### `plan` — декомпозиция сложных запросов

По умолчанию два независимых атомарных вопроса в одном сообщении (или список
пунктов) идут через планировщик (`planner_model`, обычно `qwen3.5:4b`).
Расчёт (`need_calc`) всегда план: неделимый блок «Формула» + «Счёт ПК» (синтезируется
кодом, без LLM-планировщика). Шаги выполняются последовательно: в веб-чате каждый
пункт стримится в своё сворачиваемое окно, блок расчёта обведён рамкой,
публичный `on_token` получает только склейку.
`force_model` план отключает.
На один вызов: `client.ask(..., allow_plan=False)`.
Веб-чат показывает план и ждёт кнопку; в SDK без `on_plan_confirm` шаги стартуют сразу.

```python
client.update_settings(
    models,
    planner_model="qwen3.5:4b",
    plan={"enabled": True, "max_steps": 4},
)
```

#### `add_model` — параметры

| Параметр | Default | Описание |
|----------|---------|----------|
| `model` | обязателен | имя Ollama (`qwen3.5:4b`) или OR id (`anthropic/claude-sonnet-4`) |
| `provider` | `"ollama"` | `"ollama"` \| `"openrouter"` |
| `tier` | `None` | один из 10 тиров **или** `None` = только ручной выбор |
| `rank` | auto | приоритет внутри тира / лестница эскалации (больше = сильнее) |
| `label` | auto | подпись |
| `router_prompt` | auto/пусто | текст «когда выбирать этот тир» для LLM-роутера |
| `model_id` | `provider:model` | стабильный id пула |
| `ctx_overhead_pct` | по тиру | `0…900`; `300` → база×4 |
| `max_ctx` | по тиру | потолок окна или глобальный 8192 |

---

## 4. Пул моделей и тиры

### 10 фиксированных тиров

`tiny` · `nano` · `small` · `mid` · `large` · `heavy` · `xlarge` · `coder` · `ultra` · `frontier`

Семантика floor роутера (упрощённо):

| Запрос | Минимум |
|--------|---------|
| приветствие, `2+2` | tiny |
| объяснения, перевод, простой код, web | mid |
| архитектура, длинный анализ, план | heavy |
| traceback, рефакторинг, сервис/API | coder |

- С **`tier`** → участвует в Auto и эскалации (на тир берётся max `rank`).
- **Без `tier`** → только `force_model`.
- Эскалация после selfcheck → следующая available-модель с **большим rank** (не обязательно следующий «логический» тир по имени).
- `xlarge` / `ultra` / `frontier` в Auto чаще появляются через эскалацию или ручной выбор.

### Пример записи пула (JSON)

```json
{
  "id": "ollama:qwen3.5:4b",
  "model": "qwen3.5:4b",
  "provider": "ollama",
  "label": "mid · 3.5 4b",
  "router_prompt": "Объяснения, перевод, небольшой код…",
  "tier": "mid",
  "rank": 3,
  "ctx_overhead_pct": 50,
  "max_ctx": null
}
```

OpenRouter:

```json
{
  "id": "openrouter:anthropic/claude-sonnet-4",
  "model": "anthropic/claude-sonnet-4",
  "provider": "openrouter",
  "label": "claude-sonnet-4 · OR",
  "router_prompt": "Самые сложные задачи…",
  "tier": "frontier",
  "rank": 8,
  "ctx_overhead_pct": 0,
  "max_ctx": null
}
```

Файл целиком:

```json
{
  "router_model": "qwen3.5:0.8b",
  "models": [ /* … */ ]
}
```

---

## 5. OpenRouter

1. Ключ:
   ```python
   import os
   os.environ["OPENROUTER_API_KEY"] = "sk-or-…"
   # или:
   client.set_openrouter_api_key("sk-or-…")  # → secrets.json
   client.set_openrouter_api_key(None)       # очистить файл (env останется)
   ```
2. Добавить модель:
   ```python
   client.add_model(
       provider="openrouter",
       model="anthropic/claude-sonnet-4",
       tier="frontier",
       rank=8,
       label="Claude Sonnet · OR",
   )
   ```
3. Вызов:
   ```python
   # Auto может выбрать frontier при сложном запросе / эскалации
   r = client.ask("Спроектируй схему шардирования Postgres…")

   # или жёстко:
   r = client.ask(
       "…",
       force_model="openrouter:anthropic/claude-sonnet-4",
   )
   ```

Каталог id: https://openrouter.ai/models  

**Важно:** роутер и selfcheck остаются на Ollama. Без локальной mid/tiny качество Auto и проверки падает / ломается.

---

## 6. История диалога

```python
history = [
    {"role": "user", "content": "Меня зовут Алекс"},
    {"role": "assistant", "content": "Приятно познакомиться, Алекс!"},
]
result = client.ask("Как меня зовут?", history=history)
```

- Не клади текущий вопрос в `history` — `handle` сам снимет дубликат хвоста, но контракт API: history = прошлое.
- Для heavy/xlarge/coder история подмешивается **только при отсылках** («выше», «продолжи», «исправь») — иначе экономия VRAM. Не полагайся, что длинный контекст чата всегда уйдёт в большую модель.

---

## 7. Колбэки стрима и статуса

```python
buf: list[str] = []

def on_token(chunk: str) -> None:
    buf.append(chunk)
    # например: bot.send_chat_action / edit_message

def on_status(event: str, payload: dict) -> None:
    # event: route | plan | plan_ready | plan_accepted | plan_rejected | plan_step | plan_token | plan_step_done | compose | context | worker | tool | selfcheck | retry | restore
    print(event, payload)

result = client.ask(
    "напиши функцию factorial",
    on_token=on_token,
    on_status=on_status,
)
assert "".join(buf) == result.text or True  # при retry UI может очищать текст
```

События `on_status` (поля в `payload` зависят от фазы):

| event | Когда |
|-------|--------|
| `route` | выбран тир / причина |
| `plan` | нужен ли план (`need_plan`, `reason`) |
| `plan_ready` | JSON-план: `goal`, `steps`, `planner_model`, `awaiting` |
| `plan_accepted` / `plan_rejected` | пользователь утвердил или отклонил план |
| `plan_step` | старт шага (`index`, `total`, `title`, `tier`) |
| `plan_token` | кусок ответа шага (`id`, `text`) — не путать с `on_token` |
| `plan_step_done` | шаг закончен (`id`, `failed`, `text`) |
| `compose` | сборка финального ответа |
| `context` | `num_ctx`, `used_history`, `context_reason` |
| `worker` | старт воркера на модели |
| `tool` | вызов web-tool (Ollama) |
| `selfcheck` | результат проверки |
| `retry` | ответ забракован, escalate (`scope=step` — внутри шага плана) |
| `restore` | возврат лучшей из неудачных попыток |

При `retry` / `restore` **без** `scope=step` потребитель стрима должен уметь **сбросить** уже показанный текст (как UI чата). Токены шагов плана в `on_token` не приходят — только сборка.

---

## 8. Паттерны для ботов

### A. Простой синхронный ответ

```python
def reply(text: str, history: list[dict] | None = None) -> str:
    return client.ask(text, history=history).text
```

### B. Ручной выбор «локально / облако» + температура из конфига бота

```python
FORCE = {
    "local": "ollama:qwen3.5:9b",
    "cloud": "openrouter:anthropic/claude-sonnet-4",
}

def reply(text: str, mode: str = "auto", *, temperature: float = 0.3) -> OrchestraResult:
    kw = {"temperature": temperature}
    if mode == "auto":
        return client.ask(text, **kw)
    return client.ask(text, force_model=FORCE[mode], **kw)
```

Сначала убедись, что id есть в `client.get_settings()["models"]`.

### C. Только OpenRouter-воркер, локальный роутер

```python
client.add_model(
    provider="openrouter",
    model="google/gemini-2.5-flash",
    tier="mid",
    rank=3,
)
# tiny/mid Ollama всё ещё нужны для route + selfcheck
```

### D. Модель вне Auto (только по кнопке)

```python
client.add_model(
    provider="openrouter",
    model="openai/gpt-4.1",
    tier=None,  # или не передавать
    label="GPT-4.1 · ручной",
)
# ask(..., force_model="openrouter:openai/gpt-4.1")
```

### E. Проверка перед стартом сервиса

```python
def assert_orchestra_ready(client: Client) -> None:
    if not client.ready():
        raise RuntimeError("Запустите Ollama (ollama serve)")
    h = client.health()
    if not h["ok"]:
        raise RuntimeError(f"Пул недоступен: missing={h.get('missing')} err={h.get('error')}")
    if h.get("router_missing"):
        raise RuntimeError(f"Нет модели роутера: {h.get('router_model')}")
```

---

## 9. Ошибки

| Ситуация | Исключение / поведение |
|----------|-------------------------|
| Ollama молчит и нет OR-моделей | `RuntimeError: Ollama недоступна: …` |
| Пул пуст / ничего available | `RuntimeError: Нет установленных моделей оркестра` |
| Auto, но ни у кого нет `tier` | `RuntimeError: Нет моделей с тиром для Auto…` |
| Неизвестный `force_model` | `ValueError: Модель пула не найдена` |
| OR без ключа | `ValueError: OpenRouter-модель … Задайте API-ключ` |
| Ollama-модель не pull’нута | `ValueError: Модель … не установлена. ollama pull …` |
| Неверный `force_tier` | `ValueError: Неизвестный tier` / нет моделей тира |

`Client.ask` не глотает эти ошибки — лови в боте и отвечай пользователю.

---

## 10. Ограничения (не обещай агенту лишнего)

- Нет стабильного HTTP SDK для оркестра: веб-API (`server.py`) — для UI, не публичный контракт для ботов.
- Нет multi-tenant / аккаунтов: один процесс = один набор settings.
- Tools (поиск/URL) — только Ollama.
- Чаты веб-UI in-memory; SDK историю **не** хранит — храни в своём боте.
- Qwen3.5 вызывается с `think: false` внутри llm-слоя.
- Точный tokenizer нет: `num_ctx` по эвристике символов.
- GPU-метрики в пакете есть (`metrics`), но боту обычно не нужны.

---

## 11. Чеклист агента при интеграции

```
[ ] pip install -e путь/к/qwen-orchestra
[ ] Ollama запущена; pull роутера (0.8b) и mid (4b)
[ ] Client(settings_path=изолированный json)
[ ] health()["ok"] == True
[ ] (опц.) OPENROUTER_API_KEY / set_openrouter_api_key
[ ] (опц.) add_model для OR / своих ollama-тегов
[ ] ask() с history без текущего сообщения
[ ] temperature / GenOptions под задачу бота (не патчить orchestra.py)
[ ] force_model = id из pool, не сырое имя (если ручной режим)
[ ] on_token/on_status при стриме; сброс текста на retry
[ ] ключи не в git (secrets.json / env)
```

---

## 12. Связанные файлы в репозитории оркестра

| Путь | Роль |
|------|------|
| `qwen_orchestra/client.py` | публичный `Client` |
| `qwen_orchestra/orchestra.py` | `handle`, `OrchestraResult` |
| `qwen_orchestra/plan.py` | декомпозиция: need_plan, JSON-шаги, сборка |
| `qwen_orchestra/router.py` | `route`, `RouteDecision`, floor/ceiling |
| `qwen_orchestra/settings.py` | пул, secrets, bootstrap |
| `qwen_orchestra/llm.py` | Ollama + OpenRouter HTTP |
| `qwen_orchestra/selfcheck.py` | проверка ответа |
| `qwen_orchestra/tools_web.py` | web_search / fetch_url |
| `qwen_orchestra/tools_local.py` | время ПК |
| `qwen_orchestra/calc.py` | прямой счёт на CPU (`calculate`) |
| `examples/ask_sdk.py` | минимальный пример |
| `examples/ask_sdk_bot.py` | шаблон под бота (локально + OR) |

При сомнении в поведении — смотри `Client.ask` → `orchestra.handle`; не копируй логику в чужой репозиторий.

---

## 13. Drop-in правило для чужого репо

Короткая шпаргалка для `.cursor/rules/` или `AGENTS.md` потребительского проекта:  
**[SDK_FOR_AGENTS.md](SDK_FOR_AGENTS.md)** — скопируй как есть.
