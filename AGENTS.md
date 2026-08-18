# AGENTS.md — Qwen Orchestra

Руководство для AI-агентов, работающих с этим репозиторием.

## Что это

Локальный **оркестр LLM** поверх **Ollama** (10 тиров: tiny→frontier, Qwen3.5/2.5 + optional OpenRouter) + **веб-чат** в стиле Cursor Chat (только тёмная тема, один пользователь, `127.0.0.1`).

Это **не** облачный бот и **не** редактор кода. Веб-UI — чат/composer текста к оркестру.

## Обязательные зависимости среды

1. Сервис **Ollama** запущен (`http://localhost:11434`).
2. Хотя бы одна модель в пуле оркестра (`ollama pull …` или OpenRouter-ключ).
3. Рекомендуемый набор: tiny/mid/heavy + по желанию nano…frontier.
4. Опционально: OpenRouter API key (`OPENROUTER_API_KEY` или UI → secrets.json) для внешних моделей.
5. Python: `pip install -e ".[web]"` или `pip install -r requirements.txt` (`ddgs`, `psutil`; для веба — `fastapi`, `uvicorn`, `pydantic`).
6. Публичный SDK: `from qwen_orchestra import Client` (in-process; см. `examples/ask_sdk.py`).
7. Фронтенд (vendored, без npm): см. `web/vendor/` —
   - `marked.min.js` — Markdown (GFM);
   - `purify.min.js` — DOMPurify, санитизация HTML;
   - `highlight.min.js` + `highlight-github-dark.min.css` — подсветка кода;
   - `katex/` — KaTeX + auto-render + fonts (формулы `$…$` / `$$…$$`).
   Подключение в `web/index.html`; CDN не нужен (офлайн-чат).
8. Для GPU-метрик и температур в правой панели — NVIDIA + `nvidia-smi` в PATH (иначе секции GPU/температур пустые). Температура цепей памяти часто N/A на GDDR.

На GPU ~8 ГБ VRAM 14b часто идёт как hybrid CPU/GPU — скорость ниже, чем у 9b на 100% GPU.

## Карта файлов

| Файл / папка | Роль |
|---|---|
| `qwen_orchestra/` | Устанавливаемый пакет (`pip install -e .`): ядро оркестра |
| `qwen_orchestra/client.py` | Публичный `Client`: ask / route / health / settings |
| `qwen_orchestra/orchestra.py` | `handle()`, `plan_worker_context()`, цикл попыток, tools, колбэки |
| `qwen_orchestra/plan.py` | Нужен ли план, JSON-шаги, сжатие итогов, сборка |
| `qwen_orchestra/settings.py` | Слоты + промпты (`settings.json`); OpenRouter key (`secrets.json` / env); lazy bootstrap |
| `qwen_orchestra/router.py` | Роутер + `tier_floor` / `tier_ceiling`; SYSTEM из settings |
| `qwen_orchestra/selfcheck.py` | Самопроверка ответа: правила + LLM-ревью на mid (4b) |
| `qwen_orchestra/llm.py` | Клиент Ollama + OpenRouter (`provider`, `think: false` для Qwen3.5) |
| `qwen_orchestra/metrics.py` | CPU/RAM/GPU/температуры + Ollama `/api/ps` |
| `qwen_orchestra/tools_web.py` | `web_search`, `fetch_url` |
| `qwen_orchestra/tools_local.py` | локальные данные ПК: `get_local_time`, снимок даты/времени |
| `qwen_orchestra/calc.py` | прямой счёт на CPU: `calculate`, `need_calc` |
| `orchestra.py` / `router.py` / … | Shim-модули для старых импортов |
| `orchestra_chat.py` | CLI оркестра |
| `ask_orchestra.py` | Один вопрос через оркестр |
| `chat.py` / `ask_once.py` | Только mid (4b), без оркестра |
| `chat_web.py` / `ask_web.py` | Только mid + web tools |
| `server.py` | FastAPI: статика + API чатов + SSE + `/api/settings` + `/api/metrics` |
| `web/` | UI: `index.html`, `styles.css`, `app.js` (чат + правая панель монитора) |
| `web/vendor/` | Фронт-библиотеки: marked, DOMPurify, highlight.js, KaTeX (+ тема/шрифты) |
| `open_web.py` | Лаунчер: старт сервера + открытие браузера |
| `examples/ask_sdk.py` | Минимальный пример `Client` |
| `examples/ask_sdk_bot.py` | Шаблон бота: изолированный settings + OpenRouter |
| `docs/SDK.md` | Полный SDK-гайд для другого проекта / AI-агента |
| `docs/SDK_FOR_AGENTS.md` | Drop-in правило для `.cursor/rules/` чужого репо |
| `QwenChat.exe` / `QwenChat.bat` | Сборка/обёртка лаунчера |
| `start.bat` | Меню режимов (CP866, `cd /d "%~dp0"`) |
| `README.md` | Обзор продукта: возможности, стек, особенности |
| `SETUP.md` | Установка, модели, запуск, CLI, SDK (для людей) |
| `CODE_AUDIT.md` | Статический аудит: баги / избыточность / оптимизации; не перепроверять заново без нужды |

## SDK (`qwen_orchestra.Client`)

Публичный вход для других Python-приложений (in-process, без HTTP):

```python
from qwen_orchestra import Client
client = Client()                      # или ollama_host=..., settings_path=...
client.ready() / client.health()
client.route(text)                     # RouteDecision
client.ask(text, history=..., on_token=..., temperature=0.2)
# или: client.ask(..., gen=GenOptions(temperature=0.2, seed=42, num_predict=512))
client.get_settings() / update_settings(...) / add_model(...) / delete_model(...)
client.set_openrouter_api_key(...)  # secrets.json; None — очистить
```

`GenOptions` и плоские kwargs `ask` влияют **только на воркер** (default `temperature=0.3`, `keep_alive="10m"`). Роутер, планировщик и selfcheck всегда `temperature=0`.

`settings.json`: в корне репо при разработке; иначе `%LOCALAPPDATA%/qwen-orchestra/` (Windows) или `~/.config/qwen-orchestra/`.
Ключ OpenRouter — env `OPENROUTER_API_KEY` или `secrets.json` рядом (не в git).
Блок `selfcheck` (опц.): `flag_refusal` / `flag_uncertain` / `flag_irrelevant` + списки фраз; по умолчанию все флаги `true`.
Блок `plan` (опц.): `enabled`, `max_steps`, промпты классификатора/планировщика/сборки. Модель планировщика — `planner_model` (как `router_model`).
`client.ask(..., allow_plan=False)` отключает декомпозицию на один вызов.
Bootstrap ленивый (`ensure_bootstrapped` / `Client.__init__`), не при голом импорте модулей.

### Пул моделей и OpenRouter

- В UI «Модели и промпты»: пул записей (provider + model + prompt + опционально tier/rank + запас ctx % / max ctx).
- **`tier` задан** → Auto и эскалация; несколько моделей на один тир — ответ с max rank.
- **`tier` пуст** → только ручной выбор (`force_model`).
- Поле `provider`: `ollama` (default) | `openrouter`.
- Worker ходит в OpenRouter chat/completions (stream); **tools пока только на Ollama**.
- Роутер и selfcheck остаются локальными (tiny/mid).
- Availability: ключ задан → OR-модель доступна; иначе missing (`openrouter:…`).
- API: `PUT /api/settings/providers/openrouter` `{ "api_key": "…" }` / `{ "clear": true }`.

## Оркестр — как работает

```
user → route → need_plan? → (planner → шаги → compose) | worker (± web / ± счёт ПК / ± время ПК) → selfcheck
                 ↑                                      │ не ok
                 └──── retry на модель с большим rank ──┘  (до MAX_ATTEMPTS)
```

- Тиры (семантика роутера): **10 фиксированных** id: `tiny` / `nano` / `small` / `mid` / `large` / `heavy` / `xlarge` / `coder` / `ultra` / `frontier`.
- Пул в `settings.json` → `models[]` (старый `slots[]` мигрирует при загрузке).
- Обязательных тиров нет: health ok при ≥1 доступной модели пула; недоступные — в `missing_optional`.
- Промпты роутера — per-model `router_prompt` у записей с тиром; собираются в `router.SYSTEM`.
- Эскалация: следующая available-модель с **большим rank**.
- UI: пул, тир/rank, OpenRouter-ключ, промпты, селект **модели планировщика**, тумблер плана; «Добавить в пул» / «Удалить» из пула.
- Публичный вход: `orchestra.handle(..., force_tier=…, force_model=…, stream=…, verbose=…, on_token=…, on_status=…)`.
- История в `handle` **без** текущего user-сообщения (сервер так и передаёт; CLI тоже; если хвост дублирует вопрос — он снимается).
- Для веба/SSE **не** печатать в stdout: `verbose=False` + колбэки.
- События `on_status`: `route`, `plan`, `plan_ready`, `plan_accepted`, `plan_rejected`, `plan_step`, `plan_token`, `plan_step_done`, `compose`, `context`, `worker`, `tool`, `selfcheck`, `retry`, `restore`.
- `force_model` — id записи пула; `force_tier` — лучшая available-модель тира (compat). Ручной `force_model` **без плана**.
- `OrchestraResult`: `text`, `tier`, `model`, `need_web`, `need_local_time`, `need_calc`, `route_reason`, `escalated`, `attempts`, `checked`, `problems`, `num_ctx`, `used_history`, `context_reason`, `gen`, `planned`, `plan_goal`, `plan_steps` (метаданные — от **выбранной** попытки / сборки; `gen` — применённый сэмплинг воркера; в `plan_steps` для UI могут быть `prompt`/`text`).

## Auto-режим: когда какая модель

Решение tiny (0.8b) **ограничено** детерминированными правилами `router.tier_floor()` —
модель может поднять тир, но не опустить ниже нужного (tiny систематически
недооценивает сложность). `router.tier_ceiling()` держит потолок.

| Признак запроса | Минимум |
|---|---|
| приветствие, спасибо/пока, простая арифметика `2+2` | tiny |
| объяснения, перевод, советы, how-to, «что такое», web | mid |
| простой код / скрипт / 3+ слова / длина > 40 | mid |
| сравнение, архитектура, доказательство, длинный анализ | heavy |
| длина > 350 (или > 220 без «кратко»), 3+ вопроса, пошаговый план | heavy |
| traceback / стектрейс / «ошибка в коде» / отладка | coder |
| сервис/API/приложение, рефакторинг, unit-тесты, JWT+стек | coder |
| код + ТЗ > 80 символов или ≥2 требований | coder |

Дополнительно:

- По умолчанию осмысленный запрос — не ниже **mid** (tiny не гадает на реальных вопросах).
- Голые слова вроде «тест» / «не работает» **без** кодового контекста больше не поднимают heavy/coder.
- «кратко / коротко / в двух словах» → потолок **mid** (не гоняем 9b/14b зря).
- `xlarge` (14b) в auto — обычно через эскалацию после selfcheck; вручную — селектор.
- Детерминированные shortcuts **без** вызова tiny: приветствия, web-intent,
  floor `mid`/`heavy`/`coder`. tiny остаётся для неоднозначных коротких запросов.
- `need_web` — детерминированно (`need_web()` / hard+soft ключи, в т.ч. «поищи»,
  «погугли»); при `force_tier` / `force_model` LLM-роутер **не** вызывается.
- `need_local_time` — только решение tiny (`need_local_time()` / JSON-классификатор),
  без regex. Критерий широкий: любое упоминание времени/даты/«сейчас» → true
  (даже если часы могут не пригодиться). При true в system воркера — снимок
  часов/даты/TZ с ПК (`tools_local`); чип UI «время ПК».
- `need_calc` — детерминированно (`calc.need_calc`): «посчитай/вычисли», сложный процент /
  аннуитет, выражение с `+ * / **` или минусом **с пробелами**, проценты, два+ числа
  со «сколько». Auto → план с блоком «Формула» + «Счёт ПК»; во втором пункте tool
  `calculate` (безопасный AST на CPU). Чип UI «счёт ПК». Без tools (OpenRouter) —
  предрасчёт или строка `CALCULATE:`. Модель не считает в уме.
- `ok=false` от tiny **игнорируется**, если запрос осмысленный (`looks_meaningful`):
  есть слова с гласными или простая арифметика. Пустой ввод и «??» отклоняются сразу,
  мусор — после второго мнения mid (4b).
- Ручной выбор модели (`force_model` / селектор в UI) обходит модельный роутер **и план**;
  `force_tier` роутер обходит, план можно (шаги не ниже этого тира). Web — правилами.
- Qwen3.5 вызывается с `think: false` (см. `llm.py`), чтобы не ломать JSON/tools.

## План сложных запросов (`plan.py`)

После `route`, если Auto и `plan.enabled`, эвристика (и при неоднозначности tiny)
решают `need_plan`. Один вопрос / одна тема («какого цвета яблоки?») — **не** план.
Два независимых атомарных вопроса в одном сообщении (курс доллара **и** описание
одуванчика, даже без `?`) — да; так же список 2+ пунктов, 2+ вопроса или «по шагам».
**Расчёт** (`need_calc`) всегда идёт в план: неделимый блок «Формула» → «Счёт ПК»
(tool `calculate` только во втором пункте). В UI блок с рамкой `ƒx`. Планировщик
LLM для голого счёта не нужен. `force_model` план отключает.

При готовом плане UI показывает сворачиваемое окно плана и ждёт **утверждения**
(`POST /api/chats/{id}/plan` `{action: approve|reject}`). Reject / таймаут → один воркер.
Утверждённые шаги — вложенный `handle(..., allow_plan=False)` **со стримом** в отдельное
окно пункта (потом сворачивается до заголовка). Сборка стримится в тело сообщения.
`on_token` — только склейка; токены шага — `on_status("plan_token")`.
`on_plan_confirm` в SDK; без колбэка план выполняется сразу.
История для модели — только склейка. `retry`/`restore` шага — `scope=step`.

## Контекст и VRAM

Перед воркером `plan_worker_context()` (все тиры; для heavy/xlarge/coder строже с историей):

1. **Приоритет** — текущий запрос целиком (не обрезать system + user).
2. **Второй** — минимальный `num_ctx` / VRAM (с опциональным запасом per-модель).

База (токены):

```
base = ceil_256(tokens(промпт) + template(128) + reserve_ответа + safety)
num_ctx = min(ceiling, max(256, ceil_256(base * (1 + ctx_overhead_pct/100))))
```

| Кусок | Значение |
|---|---|
| `tokens(промпт)` | system + история + user; оценка ~2 символа/токен (завышена для RU/кода) |
| `template` | 128 (chat-шаблон / спецтокены) |
| `reserve_ответа` | 512 короткий (< 48 символов) / 768 обычный / 1536 код (`coder` или признаки кода) |
| `safety` | `max(128, ~10% от суммы трёх кусков выше)` |
| `ceil_256` | выравнивание как в llama.cpp |
| `ctx_overhead_pct` | поле записи пула `0…900`; `0` = как база; `300` → база×4 |
| `ceiling` | `max_ctx` модели или глобальный `8192` (`NUM_CTX_MAX`); clamp [256…8192] |

Per-model в `models[]` / UI «Модели и промпты»: **Запас ctx %** и опциональный **max ctx**.
Defaults по тиру: tiny `300` / nano `200` / small `100` / mid `50` / large+ `0`;
max ctx tiny/nano/small `4096`, остальные без потолка (8192). Сброс пула подтягивает эти значения.
При эскалации берётся запас **текущей** записи пула.

История:

- tiny/mid — до 8 реплик (урезаются с начала, если не влезают в потолок);
- heavy/xlarge/coder — только если нужна: отсылки («выше», «продолжи», «исправь это»),
  короткий follow-up, правка без кода в сообщении; иначе `[]`;
- самодостаточный длинный текст / блок кода → без истории.

Константы в `orchestra.py`: `CTX_TEMPLATE_TOKENS`, `CTX_OUT_*`, `CTX_SAFETY_FLAT`, `NUM_CTX_MAX`.
В `context_reason` при ненулевом запасе — `+ctx×{factor}`.

Событие `context` / SSE `meta.phase=context`: `used_history`, `num_ctx`, `context_reason`, `history_messages`.  
В `done` и чипах UI: `ctx N`, `без истории` / `история`.

## Самопроверка (`selfcheck.py`)

После каждого ответа запускается `selfcheck.check()`:

1. **Быстрые правила** (без модели):
   `cjk` (ушёл в иероглифы), `language` (не язык вопроса), `empty`, `too_short`,
   `refusal`, `uncertain`, `repetition` (зацикливание), `truncated` (незакрытый ```).
   `refusal` / `uncertain` **по умолчанию включены** (короткий ответ с «не могу» /
   «не знаю» → retry). Настройка в `settings.json` → `selfcheck` или UI «Модели и промпты»:
   `flag_refusal` / `flag_uncertain` / `flag_irrelevant` (bool) и опционально свои
   `refusal_patterns` / `uncertain_patterns` (список фраз; `null`/отсутствие = встроенные).
   При `flag_refusal=false` LLM-ревью тоже не бракует problem=`refusal`.
   При `flag_irrelevant=false` — не бракует problem=`irrelevant`.
2. **Арифметика** (`arithmetic_problems`): если в вопросе есть «сколько / посчитай /
   вычисли» и выражение, оно считается через `ast` и сверяется с ответом.
   Расхождение → `error`, а в `hint` уходит правильное значение. Совпало →
   ответ считается проверенным, LLM-ревью пропускается (оно тут только шумит).
3. **LLM-ревью** на 3b (`SELFCHECK_LLM`), если предыдущее прошло:
   `irrelevant`, `error`, `incomplete`. Ревьюер оценивает только существо, не стиль.
   `num_ctx` ревьюера — 4096. При сбое ревьюера `ok=true`, но `checked=false`
   (`note=review-error`). Ревью запускается и на последней попытке (без дальнейшего retry).

Не прошло → запрос **переделывается** на модели уровнем выше, до `MAX_ATTEMPTS`
(по умолчанию 3). Правка (`Verdict.hint`) идёт в **system** (`RETRY_SYSTEM`), а не
в текст пользователя — иначе модель извиняется вместо ответа.
Web-tool результаты **кэшируются** между retry (повторный поиск не делается).

Если ни одна попытка не прошла, берётся ответ с минимальным весом проблем
(`Verdict.severity()` по таблице `SEVERITY`; `empty`/`error` тяжелее, чем
`too_short`/`incomplete`). Если это не последняя попытка — летит `restore`.

В UI: событие `check` (строка self-check), `meta.phase="retry"` очищает
забракованный текст, `meta.phase="restore"` — восстановление лучшей попытки,
чипы `попыток: N` / `проверено` / коды проблем.

Регулировка в `orchestra.py`: `MAX_ATTEMPTS`, `SELFCHECK_LLM`, `SELFCHECK_MODEL`.

## Веб-API (`server.py`, порт **8787**, host по умолчанию **127.0.0.1**)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/ready` | Быстрый ping (лаунчер; **без** Ollama) |
| GET | `/api/health` | Ollama + `missing` + `missing_optional` + `tiers` + `pool` + `planner_missing` |
| GET | `/api/metrics` | CPU / RAM / GPU / температуры / загруженные модели Ollama (`/api/ps`) |
| GET/PUT | `/api/settings` | Пул `models` + `router_model` + `planner_model` + `selfcheck` + `plan` (`settings.json`) |
| PUT | `/api/settings/providers/openrouter` | API-ключ OpenRouter (`secrets.json`; `{api_key}` / `{clear:true}`) |
| POST | `/api/settings/reset` | Сброс к defaults |
| POST | `/api/settings/models` | Добавить/обновить модель в пуле (`tier` опционален) |
| DELETE | `/api/settings/models/{id}` | Убрать модель из пула |
| GET/POST | `/api/chats` | Список / создать |
| GET/DELETE | `/api/chats/{id}` | История / удалить (409 если идёт генерация) |
| POST | `/api/chats/{id}/clear` | Очистить (409 если идёт генерация) |
| POST | `/api/chats/{id}/messages` | SSE ответ (409 при параллельном POST) |
| POST | `/api/chats/{id}/plan` | `approve` / `reject` плана, пока SSE ждёт |

Тело сообщения: `{ "content": "...", "force_model": null|"ollama:…", "force_tier": null|"tiny"|… }` (`force_model` — id пула; `force_tier` — compat).

SSE events: `meta`, `token`, `tool`, `check`, `done`, `error`.

- `meta.phase`: `context` | `worker` | `retry` | `restore` | `plan` | `plan_ready` | `plan_accepted` | `plan_rejected` | `plan_step` | `plan_step_done` | `compose` (`retry`/`restore` без `scope=step` очищают текст в UI).
- `token`: `{text}` — склейка; `{text, scope:"step", id}` — стрим пункта плана.
- `meta` также может нести `num_ctx`, `used_history`, `context_reason`, `planned`, `plan_goal`, `plan_steps`.
- `check`: `{ok, problems, note, attempt, model, checked}` — результат самопроверки.
- `done`: `attempts`, `checked`, `problems`, `num_ctx`, `used_history`, `context_reason`, `planned`, `plan_goal`, `plan_steps`.

Middleware (локальный режим): только Host `127.0.0.1`/`localhost` (+порт); для мутаций — Origin localhost (DNS rebinding).
Режим **`--share`** / `QWEN_SHARE=1`: bind `0.0.0.0`, Host/Origin не режутся; опционально `QWEN_SHARE_TOKEN` / `--token` — HTTP Basic (пароль = токен; `/api/ready` без пароля для лаунчера). Публичный URL гостя — из `share.json` (`public_ip` + `port`; IP можно обновить автодетектом). Один общий сеанс (не multi-user). Лаунчер `open_web.py` спрашивает режим при старте (`--local` / `--share`).
Параллельные сообщения / clear / delete сериализуются worker-lock’ом; после clear ответ старого worker не дописывается (`generation`).

`/api/health`: `ok` = есть ≥1 доступная модель пула (Ollama или OpenRouter); `missing` критичен; `missing_optional` — назначенные, но недоступные (не ломают ok);
`router_missing` — модель роутера из settings не найдена в Ollama.
`planner_missing` — модель планировщика не найдена (план пропускается, `ok` не ломает).
`pool` / `slots` (compat) — записи пула; `tiers` — derived map tier→лучшая модель.

`/api/metrics`: снимок для правой панели — `cpu`, `ram`, `gpu[]` (util, VRAM, `temp_gpu_c`, `temp_memory_c`, пороги), `ollama.models[]`
с `gpu_ratio`/`cpu_ratio`/`place` (из `size_vram`/`size`). Кэш ~0.6 с. Без NVIDIA секции GPU/температур пустые.

Чаты **in-memory** (пропадают при рестарте процесса).

## Правая панель (монитор)

Трёхколоночный layout: сайдбар · чат · монитор. По умолчанию открыта.

- Секции: модели в Ollama (размещение слоёв GPU/CPU + доля VRAM карты), GPU/VRAM, температуры (ядро + цепи памяти), RAM, CPU + sparklines.
- Ширина регулируется drag-ресайзером (220–960px, по ширине окна); частота опроса — селект 1…10 с.
- Высота каждого sparkline — отдельный вертикальный ресайзер (сохраняется в `localStorage`).
- Одна кнопка toggle `›`/`‹` в левом верхнем углу панели (свернуть / развернуть); в свёрнутом виде остаётся узкая полоска 40px.
- Настройки (`open` / `width` / `interval` / высоты графиков) в `localStorage` (`qwen.monitor.*`).
- Poll только когда панель открыта; метрики **не** через SSE чата.

## Запуск

```powershell
# CLI оркестр
python orchestra_chat.py

# Веб (локально)
python server.py
# → http://127.0.0.1:8787

# Веб в сеть (проброшенный порт / LAN), опционально пароль
python server.py --share --token СЕКРЕТ

# Лаунчер (одна консоль, открывает браузер; спросит локально/сеть)
python open_web.py
# или QwenChat.exe / QwenChat.bat
```

Лаунчер сначала поднимает **Ollama** (`ollama serve`, если порт 11434 молчит),
затем ждёт `/api/ready` веб-сервера (не `/api/health`), браузер — через
`os.startfile` / `cmd start`. Ollama, **запущенная лаунчером**, гасится вместе
с сервером при Ctrl+C / закрытии консоли (уже работавшую до старта не трогаем).
`QwenChat.exe` при старте сервера вызывает системный `python` (не себя). После правок `open_web.py` пересобрать:

```powershell
pyinstaller --noconfirm --onefile --console --name QwenChat --distpath . --workpath build\qwenchat --specpath build\qwenchat open_web.py
```

`start.bat` хранить в **CP866 без BOM**; всегда начинать с `cd /d "%~dp0"`.

## UI-ограничения продукта

- Только **тёмная** тема, без light-switch.
- Один пользователь / один общий сеанс (в `--share` гость видит то же, что вы); без аккаунтов.
- Нет редактора кода / файлового дерева / диффов (пока).
- Визуал: Cursor-like (серый/уголь), без фиолетовых AI-градиентов.
- Правая панель — монитор ресурсов (не Tools/Logs); свёртываемая, ресайз.

## Правила изменений для агентов

1. **Переиспользовать** `qwen_orchestra.Client` / `orchestra.handle` — не дублировать роутинг в `server.py`.
2. Стрим в UI — только через колбэки оркестра + SSE.
3. Новые проверки ответа — в `qwen_orchestra/selfcheck.py` (код проблемы + текст в `HINTS`);
   жёсткий floor/ceiling — в `router.tier_floor` / `tier_ceiling`;
   тексты «когда модель» для LLM-роутера и пул моделей — в `settings.py` / UI;
   размер окна / история — в `plan_worker_context` / связанные хелперы в `orchestra.py`;
   локальные данные ПК — в `tools_local.py` + детектор в `router.need_local_time`;
   прямой счёт — в `qwen_orchestra/calc.py` + `need_calc`;
   декомпозиция — в `qwen_orchestra/plan.py` (промпты и `planner_model` — settings/UI).
4. Лаунчер readiness — только `/api/ready`; тяжёлые проверки Ollama — в `/api/health`.
5. По умолчанию слушать `127.0.0.1`; `0.0.0.0` — только через явный `--share` / `QWEN_SHARE` (не делать share режимом по умолчанию).
6. Не добавлять светлую тему / аккаунты / IDE без запроса.
7. CLI (`orchestra_chat.py` и др.) оставлять рабочими; shim-модули в корне — для совместимости.
8. Отвечать пользователю **по-русски**, если не попросил иначе.
9. Коммиты — только по явной просьбе.
10. При смене тиров / контекста / API / SDK — обновлять `README.md` (обзор), `SETUP.md` (установка/запуск), `AGENTS.md`, `docs/SDK.md`, `.cursor/rules/qwen-orchestra.mdc`.
11. Перед полным «найди баги по всему проекту» — сначала `CODE_AUDIT.md`; повторный аудит только после крупных правок или по просьбе; при фиксе обновляй статусы там.
12. Для встраивания оркестра в чужой бот агенту другого репо достаточно [docs/SDK.md](docs/SDK.md) + `Client` — не копировать внутренности `orchestra`/`router`.

## Типичный roadmap (ещё не сделано)

- Персистентность чатов (SQLite), контракт API сохранить.
- Реестр многих моделей/агентов (`/api/agents`).
- В правой панели — вкладки Tools/Logs рядом с монитором.
- Редактор кода — отдельный этап по запросу.
- Точный подсчёт токенов через токенизатор модели вместо эвристики символов.
- GPU-метрики для AMD/Intel (сейчас только NVIDIA / nvidia-smi).
