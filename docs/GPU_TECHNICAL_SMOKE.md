# Технический GPU-прогон: инструкция и последующее состояние

**Дополнение 17.10.2026:** по отдельному разрешению выполнен один прогон на
`05ba3a68bf74b86003375151b51d486c5d89b7b0`, `call_timeout_sec=17`.
Квитанция: `outputs/gpu-smoke/20261017-195607-8febcf/receipt.json`.
Проверки связки, reuse, finish_reason, deadline/recovery/cleanup пройдены;
обычные jobs завершились failed/UnassessableVideoError, поэтому реальный done/JPEG
и качество не подтверждены. Фактический runtime-root —
`C:\Users\aniya\Рабочий стол\assar-datathon\.worktrees\ml-portability-3060`,
а не `../..`. Подробности границ и дальнейшая приёмка — [MVP_LAUNCH.md](MVP_LAUNCH.md).
Ниже историческая инструкция подготовленного стенда. Сейчас GPU занят другим чатом;
повторный запуск не назначен, readiness/validation/каталог не менялись.

Точка входа: `scripts/validation/gpu_smoke.py`. Это отдельный тестовый стенд,
не production entrypoint. В текущей подготовке MVP запуск с `--run-gpu` запрещён.
Для возможного нового согласованного запуска проверить commit и runtime-root заново.

## Команды из integration-cpu

```powershell
$py = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
# Без GPU: только подготовить сгенерированный MOV и квитанцию prepared_not_run.
& $py scripts/validation/gpu_smoke.py --runtime-root '..\..'

# ТОЛЬКО В СЛЕДУЮЩЕМ СОГЛАСОВАННОМ ЗАДАНИИ: запускает настоящий llama-server.
& $py scripts/validation/gpu_smoke.py --runtime-root '..\..' --call-timeout-sec 17 --run-gpu
```

`--runtime-root '..\..'` указывает на исходную папку проекта, содержащую `models/`.
Код/конфигурация берутся из integration checkout; веса и бинарник проверяются по
закреплённым размерам/SHA256 `configs/model.yaml`, без копирования и скачивания.
Если файлы находятся в другом корне, передать его абсолютный путь. Нужны Windows,
CPython 3.11 с requirements.lock, FFmpeg/ffprobe в PATH и nvidia-smi.
GPU-слот должен быть согласован; скрипт не освобождает его остановкой чужих процессов.

Все артефакты — в новой ignored-папке `outputs/gpu-smoke/<timestamp-random>/`:
синтетический MOV, `receipt.json`, `normal/phase.json`, `deadline/phase.json`,
метаданные вызовов, локальные JSON API и логи сервера. API-ключ случаен и живёт
только в памяти. API и модель слушают исключительно loopback; API-порт временный.
В консоль выводятся только статус и путь квитанции, не результаты распознавания.

## Какие компоненты настоящие

Настоящие `llama-server`, `app.inference.service.create_analyzer`, Analyzer,
ML HTTP-клиент, supervisor, spawn-worker, FFmpeg, Job Objects, SQLite и HTTP API.
`observed_create_analyzer` лишь записывает технические метаданные реального клиента
(`finish_reason`, timeout, elapsed, schema-valid, exception class); ответ не меняет.
Чтение/проверка model JSON происходит локально. Тексты/кадры не выводятся в чат.

Тестовые входы: только `testsrc` MOV 3 секунды и
`tests/fixtures/gpu_smoke/catalog.json`. Пять `SYNTH-GPU-*` правил покрывают
кандидатные категории, источник — `example.invalid`. Их `verified` означает
только прохождение схемы **этого теста**, не проверку нормативного источника.
Не копировать их в `app/rules/catalog.json` и не использовать для оценки качества.
Пользовательский видеофайл или произвольный каталог скрипт не принимает.

Стенд явно передаёт test RuntimeSpec через существующий `create_app` seam.
Он не вызывает production readiness как будто она пройдена, не меняет status или
validation ни на диске, ни в копии config. Production startup остаётся закрытым.
CPU-тест B1 отдельно проверяет production_spec → настоящий service в worker;
стенд не заменяет эту проверку передачи параметров.

## Последовательность и критерии

1. Проверить runtime SHA/размеры; снять исходные GPU/driver/memory/process metadata.
2. Запустить реальную связку. Два последовательных POST/GET на один MOV:
   terminal state, общая схема, timestamps/JPEG при `done`, cleanup и повторный GET;
   одинаковые PID worker/server между заданиями; реальный `finish_reason=stop`,
   схема model response валидна, factory получает нестандартный timeout 17 s.
   `UnassessableVideoError` допускается как честный результат консервативной политики
   на тестовой картинке, но не маскирует `length`, invalid JSON или сетевой сбой.
   Стенд не требует и не интерпретирует определённый вердикт как качество.
3. Завершить первый сервис. Запустить второй с budget анализа 2 s и call timeout 17 s.
   После health/worker init приостановить **только процесс собственного llama-server**
   через уже принадлежащий supervisor handle (`NtSuspendProcess`, Windows).
   Это искусственное зависание ответа, не воспроизведение CUDA deadlock.
   POST должен завершиться `failed` с AnalysisTimeoutError/WatchdogTimeout,
   попасть в настоящий adapter и запрос к модели, уложиться в 8 s вместе с
   admission/decode; оба старых PID должны исчезнуть, новый runtime открыть admission.
   Если обработка не успела дойти до model request, тест не засчитывается.
4. На каждом выходе закрыть API/supervisor; проверить исчезновение всех записанных
   собственных root PID. Снять GPU память после shutdown с ожиданием освобождения.
   Требуется наблюдаемый рост памяти при загрузке и возврат к baseline +256 MiB.
   Это снимки, **не измерение peak VRAM**. WDDM/N/A или изменение чужой нагрузки
   могут сделать проверку неубедительной: тогда статус incomplete_or_failed,
   нужна локальная проверка, а не изменение порога ради PASS.

CPU-проверка самого стенда использует настоящий service/API/supervisor, но
синтетический Python HTTP server вместо llama-server и подменённую GPU телеметрию.
В ней проверены normal/reuse, принудительный deadline/recovery и cleanup. Она
не является запуском модели. Для имитации зависания используется прямой базовый
Python, чтобы приостановить обслуживающий процесс, а не родительский venv launcher.

`prepared_not_run` — подготовка; `technical_checks_passed` — только перечисленные
технические проверки. Никакой статус не подтверждает precision/recall, юридическую
применимость, поддержку произвольных duration, или готовность сдачи. При failed
посмотреть локальные phase/receipt/logs; наружу передавать только агрегаты.
Отдельно остаются проверка настоящего CUDA зависания, смерть владельца с CUDA,
качество на разрешённом development, подтверждение норм и фактическая готовность.
Production-флаги обновлять только отдельным обоснованным изменением по результатам.
