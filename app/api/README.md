# Backend assar: запуск и интеграция

Ветка `codex/backend`, base `5c748f9`. Контракт v0.1 и зависимости не менялись.
Интерфейс ML — [app/jobs/ML_INTERFACE.md](../jobs/ML_INTERFACE.md).

## Запуск

```powershell
$env:ASSAR_API_KEY = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Только `--workers 1`, без `--reload`: второй экземпляр на том же `outputs/` отказывает
(файловая блокировка). FFmpeg/ffprobe должны быть в PATH.

Production-старт **отклоняется** (lifespan error с перечнем причин), пока нет:
`configs/model.yaml` со `status: "ready"`, точным набором трёх `validation`-флагов
`true` и неизменными типами/флагами безопасности (см. ML_INTERFACE.md); ≥1 `verified`
правила в `app/rules/catalog.json`; модуля `app.inference.service`; успешного
`check_runtime`. Сейчас старт отклоняется — это ожидаемо. Demo/mock-режима нет.

## Поведение

| Ситуация | Ответ |
|---|---|
| неверный/нет `X-API-Key` (оба метода) | 401 |
| занят единственный слот, старт/восстановление, runtime сломан | 503 (до чтения тела) |
| не multipart, расширение не `.mov` | 415 |
| > `max_upload_bytes` | 413 (потоково, без буферизации) |
| нет поля `file`, пустой файл, не MOV, первый кадр не декодируется | 422 |
| загрузка дольше бюджета запроса | 408 |
| принято | 202 `{"job_id"}` |
| неизвестный job | 404 |

Отсчёт 300 с — от входа в handler до чтения тела. Анализ получает
`min(request_deadline - 5, now + 240)`. Внутренние queued/running → `processing`.
Любая ошибка анализа, невалидный результат, путь вне job_dir, неизвестное правило,
время за пределами длительности, поздний результат → `failed` (никогда не no_violation).
`type` = `type_name_ru` правила; `description` = наблюдение + `claim_template_ru` +
`Источник: <source_title>, <clause>. <source_url>` — только из каталога.

Supervisor (`app/jobs/supervisor.py`): запускает llama-server и worker до приёма,
каждый — в отдельном Windows Job Object (kill-on-close: при смерти backend дерево
процессов завершает ОС). llama-server создаётся suspended и возобновляется уже в job;
worker ничего не импортирует и не запускает до сигнала `go` после привязки.
Остановка считается подтверждённой, только когда корень завершён и в job нет
живых процессов; иначе handles сохраняются, состояние BROKEN, новый runtime не
запускается. `close()` необратим (STOPPED), все ожидания его учитывают, start/stop
процессов сериализованы. Сообщения worker читает отдельный поток — зависший
`recv` не блокирует watchdog.

Проверка загрузки (`probe_mov`): ffprobe и ffmpeg делят один абсолютный deadline
`min(now+30, request_deadline-5)`, результат после deadline отклоняется; каждый
инструмент — в своём Job Object, все они завершаются при остановке сервиса. Watchdog по deadline; при зависании, смерти worker,
`ModelUnavailableError`/`AnalysisTimeoutError`/прочих исключениях — job failed,
остановка worker **и** сервера с подтверждением, перезапуск (2 попытки), до health
приём закрыт. `DecodeError`/`UnassessableVideoError`/`InvalidAnalysisResultError`
переиспользуют сервер, если `/health` ok. Чужие процессы не трогаются.

Хранение: `outputs/jobs.sqlite3`; после результата `uploads/<id>/` и
`outputs/jobs/<id>/` удаляются. При рестарте сервиса незавершённые job → failed.
Логи сервера модели: `outputs/logs/llama-server.log`.

Статика: `app/web/` монтируется в `/`, если папка есть; иначе только API.
Внутренний маршрут UI `GET /api/internal/status` (с ключом) → `{"accepting": bool}`.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

`tests/api/` используют синтетический анализатор и фейковый HTTP-сервер вместо
llama-server (`tests/api/fakes.py`, только в тестах), синтетическое MOV из ffmpeg
`testsrc`. Датасет не читается. Реальный GPU/llama-server не проверялся.
