# Установка и запуск

Практическая инструкция к [Qwen Orchestra](README.md). Карта кода для агентов — [AGENTS.md](AGENTS.md).

---

## Требования

- Windows 10/11 (лаунчер и `start.bat` рассчитаны на Windows)
- [Python 3.10+](https://www.python.org/downloads/) в `PATH`
- [Ollama](https://ollama.com/download) (лаунчер сам поднимет `ollama serve`, если сервис не запущен)
- Для 14b желательно ≥8 ГБ VRAM (на 8 ГБ возможен CPU/GPU hybrid)

---

## Установка

```powershell
git clone https://github.com/bebra228hacer/qwen-orchestra.git
cd qwen-orchestra
python -m pip install -e ".[web]"
# или: python -m pip install -r requirements.txt
# только ядро без веб-сервера: python -m pip install -e .
```

Пакет `qwen-orchestra`: ядро — `ddgs`, `psutil`; extra `[web]` — `fastapi`, `uvicorn`, `pydantic`.

### Модели Ollama

```powershell
ollama pull qwen3.5:0.8b
ollama pull qwen3.5:4b
ollama pull qwen3.5:9b
# опционально (xlarge / coder) — лучше по одной:
ollama pull qwen2.5:14b
ollama pull qwen2.5-coder:14b
```

Проверка: `ollama list`. Сервис — `http://localhost:11434`.  
Теги 14b по умолчанию **Q4_K_M** (~9 ГБ); `qwen3.5:9b` ≈ 6.6 ГБ.

### OpenRouter (опционально)

Ключ: env `OPENROUTER_API_KEY` или UI «Модели и промпты» → `secrets.json`.  
Каталог id моделей: [openrouter.ai/models](https://openrouter.ai/models) (формат `provider/model`, напр. `anthropic/claude-sonnet-4`).

### Фронтенд

`npm` / CDN не нужны: библиотеки уже в `web/vendor/` (marked, DOMPurify, highlight.js, KaTeX). Обновление — заменить файлы теми же артефактами с CDN/release.

---

## Запуск

### Веб-чат

```powershell
python server.py
# → http://127.0.0.1:8787

# Открыть в сеть (LAN / уже проброшенный порт) — один общий сеанс:
python server.py --share
python server.py --share --token СЕКРЕТ   # браузер спросит пароль (логин любой)
# IP и порт гостя — из share.json (рядом с settings.json); IP подтягивается сам.
# Пример: share.json.example → share.json  {"public_ip":"…","port":ВАШ_ПОРТ}
```

Лаунчер (Ollama при необходимости + сервер + браузер; спросит локально / в сеть):

```powershell
python open_web.py
python open_web.py --share --token СЕКРЕТ
python open_web.py --local                # без вопроса, только этот ПК
# или QwenChat.bat / QwenChat.exe
```

Гостю: ссылка вида `http://ВАШ_IP:порт` печатается при `--share` (из `share.json`). Тот же UI/чаты/настройки, что у вас. Без `--token` доступен любому, кто дойдёт до порта (в т.ч. OpenRouter-ключ в настройках).

Сборка exe после правок `open_web.py`:

```powershell
pip install pyinstaller
pyinstaller --noconfirm --onefile --console --name QwenChat --distpath . --workpath build\qwenchat --specpath build\qwenchat open_web.py
```

Меню режимов: `start.bat` (CP866 без BOM, `cd /d "%~dp0"`).

### CLI

```powershell
python orchestra_chat.py
python ask_orchestra.py "Привет!"

# без оркестра (только mid):
python chat.py
python chat_web.py
```

В `orchestra_chat.py`: `/tier <id>`, `/tiers`, `/auto`, `/clear`, `/exit`.

### SDK

Полный контракт для ботов и AI-агентов другого проекта: **[docs/SDK.md](docs/SDK.md)**.  
Шпаргалка в чужой репо: [docs/SDK_FOR_AGENTS.md](docs/SDK_FOR_AGENTS.md).

```python
from qwen_orchestra import Client, GenOptions

client = Client()  # ollama_host=..., settings_path=... (изолируй путь в боте!)
print(client.health())
print(client.route("напиши функцию сортировки"))
result = client.ask("2+2", temperature=0.1)
# result = client.ask("…", gen=GenOptions(temperature=0.2, seed=42, num_predict=256))
print(result.text, result.tier, result.model, result.gen)
```

Примеры: `examples/ask_sdk.py`, `examples/ask_sdk_bot.py` (Ollama + OpenRouter).  
`settings.json`: в корне репо при разработке; иначе `%LOCALAPPDATA%/qwen-orchestra/` (Windows) или `~/.config/qwen-orchestra/`.
Блок `selfcheck` (опц.): `flag_refusal` / `flag_uncertain` / `flag_irrelevant` и списки фраз — браковать ли короткие отказы («не могу»), неуверенность («не знаю») и LLM-вердикт «не по теме». По умолчанию все включены; UI — «Модели и промпты».
Блок `plan`: тумблер декомпозиции, `max_steps` (2–6), селект **модели планировщика**. Составленный план в чате нужно **утвердить** кнопкой, иначе оркестр ответит одним воркером. Классификатор «нужен ли план» — модель роутера; один вопрос план не строит, два независимых («курс доллара и описание одуванчика») — строит. Голое «посчитай 15% от 2400» считает CPU сразу, без плана. Пункты стримятся в сворачиваемые окна над итоговым ответом.

---

## Веб-UI (кратко)

- Слева — чаты; Composer; селектор Auto / модели пула
- Markdown + код + LaTeX; чипы tier / ctx / история / selfcheck / план / счёт ПК
- «Модели и промпты» — пул моделей (тир опционален), Ollama/OpenRouter, модель роутера и планировщика; «Удалить» убирает из пула
- Правая панель — монитор CPU/RAM/GPU/температуры/Ollama
- Чаты in-memory (сброс при рестарте сервера)

---

## Auto-режим (кратко)

| Запрос | Минимум |
|--------|---------|
| приветствие, `2+2` | tiny |
| объяснения, перевод, простой код, web | mid |
| сравнение, архитектура, длинный анализ, план | heavy |
| сервис/API, рефакторинг, тесты кода, traceback | coder |

«Кратко» → потолок mid. Эскалация: tiny → mid → heavy → xlarge (`coder` → xlarge).  
Полная таблица и формула `num_ctx` — в [AGENTS.md](AGENTS.md).

---

## Структура репозитория

| Путь | Назначение |
|------|------------|
| `qwen_orchestra/` | SDK: `Client` + фасады `orchestra` / `plan` / `router` / `settings` / `selfcheck` |
| `orchestra.py` и др. | Shim-модули для старых импортов |
| `server.py` | FastAPI + health/settings/чаты; SSE — `server_sse.py`, порт `8787` |
| `web/` | UI: `js/` + `css/` (чат + монитор) |
| `open_web.py` | Лаунчер |
| `examples/ask_sdk.py` | Пример SDK |
| `AGENTS.md` | Документация для AI-агентов |
