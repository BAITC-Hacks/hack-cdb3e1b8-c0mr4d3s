# Актуальная проверка упрощённого main

Для `main` на `60879fb` используйте [qa/simplified/README.md](simplified/README.md): **68 тестов прошли**, команды и импорты адаптированы под `scripts/`.
Проверяйте отдельную копию main через `--project`; код приложения в qa/tests сам по себе не обновлён до main.
Новая реализация уже содержит `scripts.local_eval.run_demo`. Поле `bot.runner_module: demo_eval` в новой конфигурации недопустимо.
[Подробный отчёт](simplified/TEAM_QA_REPORT.md) и [результаты](simplified/RESULTS.json).

Ниже сохранена история проверки предыдущей структуры `19a9130`. Её команды, 56 тестов и отдельный demo_eval.py относятся к той версии, а не к текущему main.

---

# QA агента, оценки и Telegram

Проверенная основа приложения: `main` на коммите
`19a913086d840616f7deeebebf5b0a9d9187381f`, 23 сентября 2026.
Поверх неё проверены опубликованные здесь `demo_eval.py` и QA-файлы.
Агент, бот и исходный скоринг других участников не изменены.

**56 автоматических тестов прошли.** Агент, новый eval-модуль, обработчики
бота и временная SQLite проверены локально. Живой бот работает на компьютере
участника; его переключение и окончательная демонстрация ещё впереди.
Обещанное упрощение репозитория нужно проверить отдельно после нового коммита.

## Где что находится

| Файл | Назначение |
|---|---|
| [demo_eval.py](../demo_eval.py) | Реальный `run_demo(seed)` с одним запуском агента и исходным скорингом |
| [test_demo_eval.py](test_demo_eval.py) | 9 тестов eval и подключения к настоящим обработчикам бота |
| [qa_check.py](qa_check.py) | Строгие проверки агента, лимитов, экспорта и доступного eval |
| [test_qa_check.py](test_qa_check.py) | 11 регрессий самого QA-инструмента на подставных реализациях |
| [test_agent_resilience.py](test_agent_resilience.py) | 7 сценариев устойчивости настоящего агента без API |
| [check_gemini.py](check_gemini.py) | Явно выбранный live/offline-режим и диагностика fallback |
| [DEMO_INTEGRATION.md](DEMO_INTEGRATION.md) | Контракт, результаты и инструкция владельцу бота |
| [INTEGRATION_RESULTS.json](INTEGRATION_RESULTS.json) | Результаты текущей offline-проверки и сведения о наборах тестов |
| [CHECKLIST.md](CHECKLIST.md) | Закрытые проверки и оставшиеся действия |

Исторические отчёты сохраняются: [Gemini](GEMINI_TEST_REPORT.md),
[результаты API](TEST_RESULTS.json), [шаблон организаторов](BASELINE_REPORT.md).
Их результаты относятся к указанным внутри версиям; замечания к шаблону
не являются замечаниями к текущему агенту команды.

## Запуск без слияния веток

В `qa/tests` находятся наш модуль и тесты, а полное приложение — в `main`.
Для проверки нужна отдельная копия приложения. Следующие команды выполняются
из чистой локальной QA-ветки. Целевой каталог `beeline-demo-review` должен быть новым.

```powershell
git status
git fetch origin
git switch qa/tests
git pull --ff-only origin qa/tests
$qaRoot = (Get-Location).Path
$qa = Join-Path $qaRoot 'qa'
git worktree add --detach ..\beeline-demo-review origin/main
$project = (Resolve-Path ..\beeline-demo-review).Path
if (Test-Path -LiteralPath "$project\demo_eval.py") { throw 'В main уже есть demo_eval.py: сначала сравните версии.' }
Copy-Item -LiteralPath "$qaRoot\demo_eval.py" -Destination "$project\demo_eval.py"
```

`worktree add` создаёт отдельную рабочую папку, не меняя `main`. Для точного
повторения отчёта вместо `origin/main` укажите SHA выше. Если рабочее дерево
нечистое, сначала сохраните и разберите свои изменения. Не удаляйте уже
существующий каталог ради повторения команды: используйте другую новую папку.

Подготовка окружения:

```powershell
py -3.12 -m venv "$project\.venv"
$python = Join-Path $project '.venv\Scripts\python.exe'
& $python -m pip install -r "$project\requirements.txt"
& $python -m pip check
```

Проверены Windows, Python 3.12.14 и 32 закреплённых пакета, включая pandas
3.0.6, NumPy 2.5.3, google-genai 2.25.0, python-telegram-bot 22.6,
python-dotenv 1.2.3. README приложения описывает Python 3.14; этот отчёт
не подтверждает запуск на каждой версии Python.

## Автоматические проверки

В новой копии без `config.json` Gemini по умолчанию выключен. Если используете
существующую конфигурацию, для offline-прогонов задайте `gemini.enabled: false`.
`qa_check.py` использует реальную конфигурацию и сам API не отключает.
Для тестов бота и eval ключи Telegram/Gemini не нужны.

```powershell
$env:QA_PROJECT = $project
& $python -B -m unittest discover -s "$qa" -p 'test_qa_check.py' -v
& $python -B "$qa\test_agent_resilience.py" --project "$project"
& $python -B "$qa\test_demo_eval.py" --project "$project"
Push-Location $project
try { & $python -B -m unittest discover -s tests -v } finally { Pop-Location }
& $python -B "$qa\qa_check.py" --project "$project" --runs 10 --report "$project\qa_report.json"
```

Ожидаемые результаты на проверенной версии: 11 + 7 + 9 + 29 = 56 тестов.
Первые 11 проверяют сам инструмент на подставных реализациях. Девять новых
тестируют настоящий `demo_eval`; последние 29 принадлежат участнику бота.
Они проверяют реальные обработчики и SQLite с заменой доставки Telegram моками.
Команды выполняются отдельно; проверяйте итог `OK` каждой команды.

`qa_check.py --runs 10` выполняет seed 42 и 0–9, затем два экспорта seed 42
в отдельных процессах. Таймаут — 600 секунд на процесс. CSV сравниваются
после нормализации перевода строк в LF, файл сдачи не перезаписывается.
Оценка использует исходные `local_eval`, `scoring_core`, `make_submission`.

Проверяются 1–10 финальных кампаний, тарифы/каналы, пилоты, лимиты и экспорт.
Одинаковые финальные сегменты и зависимость от обрезки общего бюджета/охвата —
ошибки по договору команды. Пересечение разных сегментов и обрезка отдельной
кампании до 5000 контактов — предупреждения; сам скоринг может их допускать.

**Коды выхода `qa_check.py`:** `1` — ошибка; `2` — доступные проверки прошли,
но набор неполный (`PARTIAL`). Этот скрипт сам не запускает отдельные тесты
бота, поэтому его `bot: NOT_TESTED` не отменяет результат отдельного набора
из 29 тестов. Общий итог сверяйте с [отчётом интеграции](DEMO_INTEGRATION.md).
`metrics.status` — отдельный экономический статус, не статус всей QA-проверки.

## Gemini: отдельная диагностика

```powershell
& $python -B "$qa\check_gemini.py" --project "$project" --mode offline --seeds 42 --report "$project\gemini_offline.json"
& $python -B "$qa\check_gemini.py" --project "$project" --mode timeout --seeds 42 --report "$project\gemini_timeout.json"
& $python -B "$qa\check_gemini.py" --project "$project" --mode invalid_json --seeds 42 --report "$project\gemini_json.json"
& $python -B "$qa\check_gemini.py" --project "$project" --mode invalid_ids --seeds 42 --report "$project\gemini_ids.json"
```

Эти режимы не обращаются к API. Скрипт показывает результат; равенство кампаний
и метрик между fallback и offline сравнивается отдельно. Для опубликованных
исторических сценариев это сравнение уже выполнено.

Для отдельного live-запуска нужен локальный `config.json` с `gemini.api_key`
и настройками модели. Проверить исключение файла: `git -C "$project" check-ignore config.json`.

```powershell
& $python -B "$qa\check_gemini.py" --project "$project" --mode live --seeds 42 --report "$project\gemini_live.json"
```

Live расходует квоту. Скрипт различает `MODEL_USED`, `FALLBACK`,
`MODEL_NOT_CALLED`, `OFFLINE`. Код `1` — ошибка агента; `2` в live — успешное
применение модели не подтверждено; `0` — выбранная проверка прошла. Пакет live
останавливается после первой ошибки провайдера. Сырые отчёты содержат локальный
путь и должны быть просмотрены перед публикацией.

## Передача владельцу бота

Полная процедура — в [DEMO_INTEGRATION.md](DEMO_INTEGRATION.md).
Владелец получает `demo_eval.py`, переключает только `bot.runner_module`
в своём приватном конфиге и перезапускает единственный процесс бота.
Существующие настройки, ключи и рабочую базу нужно сохранить.
Публикация в `qa/tests` сама по себе рабочий бот не переключает.

Перед сдачей после последнего изменения приложения повторить штатные
`python local_eval.py`, `python local_eval.py --runs 10`,
`python make_submission.py`, воспроизводимость CSV и полный показ Telegram.
