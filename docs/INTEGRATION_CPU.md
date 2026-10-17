# CPU-интеграция, 17 октября 2026

**Статус: NOT READY. GPU не запускался.** Это отдельная ветка `codex/integration-cpu`
в `.worktrees/integration-cpu`, созданная от bootstrap
`5c748f9c94c8cd7b32aae04a5c8b49a0c79491d8`.

Прочитаны код и README и объединены без конфликтов:

- ML `f9f461defeb948843e2667c70f80cf821596122c`;
- backend `1a3bda0fd97cc3407fc8ea7d9440a53869285144`;
- merge ML: `ffb95d106f5196e587a312f75b8321db757b0381`;
- merge backend: `ddb35a59269d562d254639311d27462f589b92a9`.

Обнаруженный в remote `45f00ca89f1cd674157b7b5c2c2221987d9bf02a` **не включён**:
пользователь подтвердил проверку только переданных коммитов. Нужен отдельный handoff.
`main`, bootstrap и untracked `app/web/` исходного checkout не менялись.
Исправления production-кода владельцев не внесены; добавлены интеграционные регрессии,
этот отчёт и актуализация README/сообщения CPU-проверки. Публикация сервиса не выполнялась.

## Проверено

CPython 3.11.9, зависимости из общего `requirements.lock`; новых зависимостей нет.
FFmpeg/ffprobe: `N-122697-gfcfb33922f-20260210`. Использован существующий Python
из исходного checkout, пакеты в нём не менялись. Все входы тестов синтетические.
Содержимое банковских видео, кадров, аннотаций и результатов модели не читалось.

Из корня интеграционного checkout, PowerShell:

```powershell
$py = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
& $py -m pip check
& $py scripts/check_bootstrap.py
& $py -m pytest -q --tb=short --durations=10
& $py -m pytest tests/integration -q --runxfail --tb=short
& $py -m pytest tests/inference/test_video.py::test_vfr_uses_actual_frame_times tests/integration -q --tb=short
```

Фактические результаты:

- `pip check`: No broken requirements found.
- `check_bootstrap`: 5 synthetic fixtures valid, verified rules: **0**.
- Полный прогон до добавления последнего теста B7: **128 passed, 1 failed,
  5 xfailed**, 79.34 s. Failure: существующий `test_vfr_uses_actual_frame_times`.
- Окончательный `tests/integration --runxfail`: **1 passed, 6 failed**,
  1.56 s. Это воспроизводители B2–B7, не случайные падения.
- Повтор VFR + окончательные integration tests: **1 failed, 1 passed,
  6 xfailed**, 2.60 s. Полный suite после добавления только B7 повторно не запускался.
- Все 26 существующих backend API/runtime тестов прошли в полном прогоне:
  HTTP/schema/auth/upload, failed при ошибках, watchdog/recovery, обычный shutdown,
  Windows Job Object при смерти владельца, повторное использование здорового сервера.
  Вместо модели — тестовые Python-процессы; это не GPU-интеграция.

Первые прогоны в sandbox прерваны: faulthandler показал зависание `asyncio`
в `socket.socketpair/accept`. Приведённый полный результат получен вне sandbox,
с доступным loopback. Отказ запуска HTTP из-за sandbox не принят за успешный тест.
После прогонов проверка процессов не нашла оставшихся тестовых серверов
`tests.api.fakes` этого checkout.

`tests/integration/test_handoff.py` соединяет настоящий Analyzer с синтетическим
клиентом/декодером, затем настоящий serializer, SQLite, cleanup и повторное открытие
БД. Два разных observed_fact остаются разными в description вместе с шаблоном,
названием и URL источника; JPEG полностью декодируются из сохранённого JSON после
удаления upload/job. HTTP GET в backend читает именно этот сохранённый JSON.
Таким образом serializer не подменяет индивидуальное обоснование каталогом.
Реальность наблюдения самой моделью этим не доказана; см. B5.

Strict xfail — открытые блокеры. `--runxfail` делает их обычными красными тестами;
после исправлений владельцем удалить соответствующий xfail, не менять ожидание
под дефект. Новые интерфейсы должны сохранить общий контракт 0.1:

```python
analyze_video(video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult
```

Абсолютные пути; абсолютный deadline по monotonic этого компьютера; один общий
бюджет, ошибки через AnalysisError → failed. Supervisor — единственный владелец
worker/server/декодеров. Analyzer только использует переданный loopback-клиент,
не скачивает, не стартует и не останавливает модель на каждом задании.

## Блокеры и требования к следующему handoff

| ID | Владелец / место | Что требуется |
|---|---|---|
| B1 | ML + backend; `app/jobs/ml_interface.py`, `runtime.py` | В f9f461d отсутствует `app.inference.service`: нужны `check_runtime`, чистый `server_command`, `create_analyzer`. Проверить SHA256/размер весов и mmproj, SHA256 server binary. Существующий `verify_runtime_files` проверяет server только на существование. Не совмещать Popen supervisor с отдельным spawn через LlamaServer. Согласовать передачу `Settings.model_call_timeout_sec` (сейчас не передаётся factory; совпадает лишь default 120). |
| B2 | backend; `Supervisor.close/_recover` | `_recover` безусловно заменяет STOPPED на RECOVERING; поздняя ошибка активного job после close может запустить новый runtime и открыть admission. Нужна необратимая остановка, синхронизация close/recovery и тест shutdown при BUSY/RECOVERING. CPU-воспроизводитель вызывает recovery после STOPPED и фиксирует попытку spawn. |
| B3 | backend; `media.probe_mov` | ffprobe и ffmpeg получают один и тот же полный timeout. При бюджете 5 s и первом шаге 4 s второму дают 5 s, а не ≤1 s. Нужен единый absolute deadline, проверка после probe до создания job; pre-admission декодеры также должны иметь владельца при смерти API. |
| B4 | ML; `Analyzer.analyze_video` | Одно непригодное окно среди пригодных без событий приводит к no_violation. Частично непригодный анализ не доказывает отсутствие нарушения; нужен failed/UnassessableVideoError, если отрицательный вывод не обоснован. |
| B5 | ML; `Analyzer.analyze_video`, `prompts.validate` | Пустой/whitespace description разрешён и превращается в общий текст «модель отметила категорию». Требовать непустое индивидуальное наблюдение; недостоверный результат — InvalidAnalysisResultError. Backend должен сохранить полученное наблюдение, источник добавлять отдельно. |
| B6 | ML + интегратор; mapping категорий | Наблюдения категории без verified-правила отбрасываются, после чего получается no_violation. Нужна консервативная политика неопределённости, согласованная с B4. Само совпадение category/type_id не проверяет scope/required_context/counterexamples правила. |
| B7 | backend; `production_spec` | Вместо фиксированного набора validation-полей принимается любой непустой словарь из true, например unrelated=true. Проверять имена обязательных полей, типы и evidence для актуального набора code/config; не удалять false-поля для допуска. Тест делает это только в tmp_path с тестовым service, production config неизменён. |
| B8 | ML; `video.py`, существующий VFR-тест | Локально стабильно падает DecodeError: frame PTS outside video duration. На заново созданном синтетическом ролике duration=7.8 s, последний выбранный PTS=7.9 s (28 кадров). Разобраться в определении duration/FFmpeg fixture/PTS; не замазывать clamp-ом и не считать поддержку VFR подтверждённой. |
| B9 | backend; `procs.py`, `_start_*`, `_stop_runtime`, `_run` | По коду stop_tree подтверждает выход только root, не всего Job Object; assign выполняется после запуска и может пропустить рано созданных потомков; после неудачной остановки ссылки на owned-процессы сбрасываются. Нужны проверка дерева и сохранение принадлежности до подтверждённого завершения. `poll(deadline)` затем `recv()` не ограничивает ожидание неполного IPC-ответа. Эти крайние сценарии пока не воспроизведены процессными тестами; обычный kill-on-close/recovery прошёл. |
| B10 | Омар + интегратор; каталог | Каталог пуст. Нужны настоящие проверенные источники, scope, условия применимости, individual observation → rule mapping. Синтетические правила только в tests; нельзя вставлять их в production для запуска. |
| B11 | интегратор, отдельное GPU-задание | Нет замера актуальных analyze_video + service adapter + supervisor на RTX 3060. Сообщённый успешный benchmark a6d5b6a — только старый baseline; требуется новый холодный/тёплый сквозной прогон и lifecycle fault tests на согласованном коде. |

B2–B7 подтверждены CPU-тестами, B8 повторён отдельно; B1/B10 установлены по
наличию модулей/каталога. B9 — требования по результатам чтения кода и пробелы тестов,
а не утверждение, что штатный тест остановки провалился.

`configs/model.yaml` полностью сохранён от bootstrap: Qwen3-VL-2B-Instruct GGUF
Q8_0 + mmproj Q8_0, llama b11669/620d2225c, ctx=8192, image_max_tokens=256,
ngl=99, parallel=1, max_tokens=384, step/window=1/10, model_height=0,
temperature/top_k/seed=0/1/0, cache_prompt=false, prompt obs-v0.1.
ML loader и существующий CPU-тест подтверждают чтение этих параметров; байты весов
и server в этой задаче не хешировались. Defaults старого benchmark не наследовать.
Status остаётся pending, все три validation-флага false: наличие функции ещё
не закрывает её интеграционную валидацию. Новых production-правил и зависимостей нет.

Приватный split не копировался и не перераспределялся; receipt/policy не менялись.
Сохраняются 22 train / 8 dev / 10 frozen-test групп; X-4/X-47 — одна dev-группа.
Human near-duplicate review остаётся обязательным до настройки; frozen-test
не использовать для промптов, обучения или подбора порогов.

## Следующий сквозной прогон — только после отдельного разрешения GPU

1. Получить согласованные handoff с исправлениями B1–B9, включить их точные SHA
   в эту ветку, повторить команды CPU выше; закрытые регрессии должны стать pass.
2. Утвердить настоящий каталог B10 и его применимость. Проверить runtime bytes
   через новый `check_runtime`. До успешных измерений **не менять** validation/status.
3. Для первого измерения использовать явный validation entrypoint ниже. Он
   использует реальный адаптер и verified-каталог, не production readiness gate:
   именно поэтому он хранится только локально в outputs, не становится ASGI entrypoint
   продукта и не свидетельствует о production readiness. Синтетического analyzer нет.
   Команды сейчас НЕ выполнялись. Выполнять только после разрешения GPU, исправлений
   lifecycle и появления service adapter. В `models/` этого checkout должны быть
   согласованные локальные файлы; worktree не наследует ignored models другого checkout.

```powershell
$py = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
$env:ASSAR_API_KEY = (& $py -c "import secrets; print(secrets.token_urlsafe(32))")
New-Item -ItemType Directory -Force outputs | Out-Null
@'
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    from app.settings import Settings, configure_offline_environment
    configure_offline_environment()
    from app.api.main import create_app
    from app.jobs.runtime import load_catalog, verified_only
    from app.jobs.supervisor import RuntimeSpec
    from app.inference import service
    import uvicorn

    settings = Settings.from_env()
    cfg = json.loads(settings.model_config_path.read_text(encoding="utf-8"))
    cat = verified_only(load_catalog(settings.rules_catalog_path))
    if not cat.rules:
        raise SystemExit("No verified rules: validation blocked")
    service.check_runtime(cfg, ROOT)  # SHA/size, no download or GPU.
    spec = RuntimeSpec(
        analyzer_factory="app.inference.service:create_analyzer",
        analyzer_kwargs={"model_cfg": cfg, "catalog": cat, "repo_root": ROOT},
        catalog=cat, server_command=lambda port: service.server_command(cfg, ROOT, port),
        log_dir=settings.outputs_dir / "logs")
    uvicorn.run(create_app(settings, lambda _: spec, web_dir=None),
                host="127.0.0.1", port=8000, workers=1, log_level="warning")
'@ | Set-Content -Encoding utf8 outputs/validate_local.py
# Следующая строка запускает GPU; сейчас не выполнять:
& $py outputs/validate_local.py
```

При изменении factory signature в новом handoff сначала обновить эту команду
(в частности, согласованную передачу model_call_timeout_sec).
В другом локальном терминале того же checkout задать тот же API-ключ без вывода
в чат, `ASSAR_SMOKE_VIDEO` — абсолютный путь одного разрешённого development MOV.
Не выбирать frozen-test. Содержательный ответ хранится только в ignored outputs:

```powershell
$py = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
$headers = @{ 'X-API-Key' = $env:ASSAR_API_KEY }
Invoke-RestMethod http://127.0.0.1:8000/api/internal/status -Headers $headers
$timer = [Diagnostics.Stopwatch]::StartNew()
curl.exe --silent --show-error --fail-with-body `
  -H "X-API-Key: $env:ASSAR_API_KEY" `
  -F "file=@$env:ASSAR_SMOKE_VIDEO;filename=input.mov" `
  -o outputs/accepted.json http://127.0.0.1:8000/api/v1/analyze
if ($LASTEXITCODE -ne 0) { throw 'Upload failed; inspect local response' }
$job = (Get-Content outputs/accepted.json -Raw | ConvertFrom-Json).job_id
do {
  Invoke-WebRequest "http://127.0.0.1:8000/api/v1/jobs/$job" -Headers $headers -OutFile outputs/result.json
  $state = (Get-Content outputs/result.json -Raw | ConvertFrom-Json).status
  if ($timer.Elapsed.TotalSeconds -gt 300) { throw 'End-to-end deadline exceeded' }
  if ($state -eq 'processing') { Start-Sleep -Milliseconds 250 }
} while ($state -eq 'processing')
Write-Output "status=$state elapsed_sec=$($timer.Elapsed.TotalSeconds)"
# Повторить GET после cleanup, сохранив локально; не печатать JSON/description/base64.
Invoke-WebRequest "http://127.0.0.1:8000/api/v1/jobs/$job" -Headers $headers -OutFile outputs/result-after-cleanup.json
$env:ASSAR_SMOKE_DURATION = (& ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 $env:ASSAR_SMOKE_VIDEO)
@'
import os
from pathlib import Path
from app.contracts import DoneJob, JOB_RESPONSE_ADAPTER, validate_done_for_video
first = Path("outputs/result.json").read_bytes()
second = Path("outputs/result-after-cleanup.json").read_bytes()
assert first == second
result = JOB_RESPONSE_ADAPTER.validate_json(second)
assert isinstance(result, DoneJob), "Expected done; inspect failure locally"
validate_done_for_video(result, float(os.environ["ASSAR_SMOKE_DURATION"]))
assert not (Path("uploads") / result.job_id).exists()
assert not (Path("outputs/jobs") / result.job_id).exists()
print("done schema/timestamps/JPEG valid; persisted result survived cleanup")
'@ | & $py -
```

Повторить upload для тёплого прохода; записать commit/config/catalog hashes,
GPU/driver/runtime, cold/warm timings, peak VRAM и PID переиспользуемого сервера
локально. Человек локально проверяет соответствие каждого description кадрам и
применимость источника. Отдельно проверить согласованные сценарии timeout/crash,
shutdown при BUSY, смерть supervisor, отсутствие принадлежащих ему потомков и
невмешательство в чужие процессы. Не останавливать чужие GPU-процессы ради теста.

Только после этих фактических результатов интегратор обновляет соответствующие
validation-флаги со ссылками на локальный receipt; status=ready — когда закрыты
все условия. Production-команда после этого:

```powershell
& $py -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Без reload, без второго владельца модели, без mock-каталога. Успех команды
validation entrypoint сам по себе не разрешает публикацию/сдачу сервиса.
