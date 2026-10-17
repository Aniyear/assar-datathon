# CPU-интеграция — backend c8fc26b + frontend 6b0d09d

17 октября 2026. **Реальная GPU-интеграция не подтверждена, production NOT READY.**
Работа продолжена в существующей `codex/integration-cpu`, checkout
`.worktrees/integration-cpu`, от `f2bf612595973f474088a2cc6f89d7916bef6d78`.
Начальная база ветки — bootstrap `5c748f9c94c8cd7b32aae04a5c8b49a0c79491d8`.

Приняты только разрешённые handoff:

- backend: `c8fc26b54d4110a85e1f45b973a2214e0bdc5e58`;
- frontend: `6b0d09d892ab51a4d8ef64a111605fd44639b897`;
- ML остаётся `45f00ca89f1cd674157b7b5c2c2221987d9bf02a`;
- merge backend: `6f4061a0d6e2a6338068f0c080bb244ba7e9ef00`;
- merge frontend: `a58a04f125c2dfef7efb10ef252a0666419e0561`.

Оба merge без конфликтов. Production-код ML/backend/frontend после handoff не
редактировался; свои изменения — интеграционные проверки и документация.
Frontend принят как функциональная основа; последующее изменение дизайна Argon
в этот проход не входит. `main`, bootstrap и исходный untracked `app/web` сохранены.
Каталог, readiness, validation, зависимости и квитанции split не менялись.
Видео/аннотации команды не читались, GPU и веса не запускались и не скачивались.

## Проверки общей сборки

CPython 3.11.9; общий requirements.lock, без установки новых зависимостей;
FFmpeg/ffprobe `N-122697-gfcfb33922f-20260210`, Node 22.22.3, Windows.
В проекте нет отдельной npm-сборки: frontend — статические HTML/CSS/JS.

Из корня интеграционного checkout:

```powershell
$py = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
& $py -m pip check
& $py scripts/check_bootstrap.py
node --check app/web/app.js
node --check app/web/tests/browser.js
node --check app/web/tests/setup.js
& $py -m py_compile app/web/tests/serve.py
& $py -m pytest -q --tb=short --durations=10
& $py -m pytest tests/integration -q --runxfail --tb=short
```

Фактический полный suite: **185 passed, 1 failed, 2 xfailed**, 116.14 s, exit 1.
Единственное обычное падение — существующий ML VFR-тест
`tests/inference/test_video.py::test_vfr_uses_actual_frame_times`,
`DecodeError: frame PTS outside video duration`. Xfail остались только B5 и
исходный generic-случай B6. Backend-тест отсутствия service исправлен владельцем
и теперь проходит с существующим ML-модулем.

Предварительная проверка адаптированных `test_handoff.py --runxfail`:
**6 passed, 2 failed**, 1.65 s (B5/B6). Окончательный `tests/integration --runxfail`
с новыми проверками static/API и дерева процессов: **8 passed, 2 failed**,
4.12 s, exit 1. Остаются только B5/B6.

`pip check`: No broken requirements found. `check_bootstrap`: **5 fixtures valid,
0 verified rules**. Три `node --check` и `py_compile` прошли. Проверки loopback и
дочерних процессов выполнялись вне sandbox (его socketpair ранее зависал).
Все тестовые видео/правила/ответы синтетические; реальные модель и данные не использованы.

### Frontend вместе с backend

Новый `tests/integration/test_web_api.py` использует реальные create_app/lifespan,
ASGI/HTTP, ProcessOwner, supervisor-worker и SQLite; только analyzer — тестовый
`tests.api.fakes:create`. В полном suite прошёл:

- `/`, `/index.html`, `/styles.css`, `/app.js` → 200, правильные MIME, SHA256
  ответов совпадают с файлами принятого frontend;
- API не перехватывается StaticFiles: 401 без ключа, 404 для неизвестного job,
  `/api/internal/status` доступен с ключом;
- synthetic MOV → POST 202 → GET done/violation, валидные JSON/timestamps/JPEG;
- индивидуальное синтетическое наблюдение сохранено; результат GET идентичен
  после удаления upload/job, статика и API работают на одном origin.

Отдельно настоящий браузерный стенд frontend: **39 PASS / 0 FAIL** во встроенном
браузере Codex. Команда:

```powershell
& $py app/web/tests/serve.py --backend-checkout . --port 8765
```

Открыть `http://127.0.0.1:8765/tests/browser.html`, нажать «Запустить проверки».
В этом стенде lifespan выключен, fetch подменён только внутри тестового iframe.
Проверены UI-состояния, ошибки, race/cancel, смена demo/real, schema/JPEG, ссылки,
синтетический MOV/seek и освобождение object URL. Это **не реальный inference**
и не браузерный E2E с настоящей моделью. Отдельный HTTP-тест выше использует живой
backend с тестовым analyzer, также не подтверждает GPU. Скриншот текущего прогона:
`outputs/ui-tests/frontend-cpu.png` (ignored, только синтетический стенд).

## Статус B1–B11

| ID | Статус | Доказательство и остаток |
|---|---|---|
| B1 | Частично исправлено ранее; остаток без изменений | Service 45f00ca совместим с текущей factory, его тесты проходят. Но Settings.model_call_timeout_sec по-прежнему не передаётся в Analyzer (default 120). ML+backend должны согласовать параметр и проверить значение, отличное от 120. Проверки настоящих весов/бинарника здесь не было. |
| B2 | **Исправлено и проверено на CPU** | Собственная регрессия вызывает настоящий close(), затем поздний _recover/_reopen: нет spawn, остаётся STOPPED. Backend-тесты close при BUSY и RECOVERING проходят, job failed, admission закрыт, нового сервера нет, принадлежащие процессы завершены. |
| B3 | **Исправлено и проверено на CPU** | probe_mov принимает absolute deadline и ProcessOwner. Наш тест перехватывает communicate(timeout): первый этап получает 5 s, тратит 4 s, второй получает ≤1 s. Дополнительно проходят реальные синтетические slow-tool/expired/owner-shutdown тесты backend. |
| B4 | Исправлено и проверено ранее, регрессия проходит | Непригодное окно без verified-событий → UnassessableVideoError; ожидание сохранено. |
| B5 | **Осталось, воспроизведено** | ML: пустое/whitespace индивидуальное наблюдение заменяется общим текстом категории. Ожидается InvalidAnalysisResultError; исходный strict xfail сохранён. |
| B6 | Частично исправлено; generic-случай остаётся | Кандидатная категория без правила отклоняется, тест проходит. other_notable_action при отсутствии verified-событий всё ещё даёт no_violation; прежнее ожидание UnassessableVideoError не ослаблено. Политику и fix ведут ML+интегратор. |
| B7 | **Исправлено и проверено на CPU** | Нужны точные имена трёх validation-флагов и JSON bool true; unrelated=true, пропуски, лишние поля, строки/числа отвергаются. Наша регрессия и новые параметризованные тесты backend проходят. Production-флаги не менялись. |
| B8 | **Осталось, воспроизведено** | VFR-тест падает на прежнем FFmpeg. В прошлой диагностике synthetic duration=7.8 s, последний PTS=7.9 s; в этом проходе повторён тест, отдельные числа заново не измерялись. Исправление у ML, clamp не допускается как сокрытие ошибки. |
| B9 | **Проверенные CPU-сценарии закрыты на Windows** | suspended spawn+assign до resume; worker ждёт go; stop_tree ждёт root exit И active()==0; при неподтверждённой остановке handles сохраняются, admission BROKEN; блокирующий recv перенесён в reader. Новые тесты владельца и наша проверка потомка после выхода root прошли. Границы покрытия указаны ниже. |
| B10 | **Осталось** | Каталог пуст, verified=0. Омар+интегратор подтверждают настоящие нормы и применимость; тестовые правила не переносить в production. |
| B11 | **Не выполнялось** | Актуальная связка analyze_video/service/supervisor на RTX 3060 не измерялась. Старый benchmark a6d5b6a не является её подтверждением. |

Strict xfail B2/B3/B7 сняты **после успешного --runxfail**. B2 использует полный
Supervisor и реальный close вместо ручного частичного object.__new__; B3 адаптирован
к новой сигнатуре и проверяет фактический timeout communicate, а не старый аргумент
_run. Требования не ослаблены. B4 остаётся pass; B5/B6 сохранены.

### Что именно подтверждено для B9

`tests/api/test_api_supervisor.py` подтверждает владение немедленно созданным
потомком, остановку потомка worker, сохранение handles/запрет restart при
stop_tree=False, close при BUSY/RECOVERING и ограниченное ожидание reader.
`tests/integration/test_process_guards.py` дополняет это: root уже завершён,
потомок ещё жив и active()>0; stop_tree возвращает true только при active()==0,
потомок мёртв, время ограничено. Pre-admission ProcessOwner закрывает декодер
при shutdown (tests/api/test_api_media.py).

IPC-проверка владельца использует управляемый блокирующий recv, а не реальный
частично переданный повреждённый Windows pipe. Не проверены поведение реального
llama-server/CUDA при зависании/смерти владельца, все OS-error ветви и POSIX.
Это ограничения покрытия, не новые воспроизведённые падения backend. Перед
сдачей нужен согласованный GPU lifecycle smoke; чужие процессы не трогать.

## Данные: последующее подтверждение пользователя

Пользователь сообщил, что по информации команды дополнительных дубликатов нет,
повторный полный ручной просмотр сейчас не требуется. Принято как **подтверждение
пользователя**, а не результат автоматического/независимого анализа. Отдельная
фиксация: [DATA_SPLIT_USER_CONFIRMATION.md](DATA_SPLIT_USER_CONFIRMATION.md).
Старые публичная/приватные квитанции не изменены, split не пересоздан: 22/8/10
групп, X-4/X-47 только dev. Frozen-test остаётся закрытым для настройки. Старый
CLI export пока требует review своего формата; ложный complete для него не создан.

## Остаток работ по владельцам и GPU

**ML:** B5/B6/B8; сохранить уже исправленные B4/candidate-case, индивидуальный
факт+источник и JPEG после cleanup. Точные воспроизводители:

```powershell
& $py -m pytest tests/integration/test_handoff.py -q --runxfail --tb=short -k 'empty_individual or unmapped_observation or unsupported_candidate or unreadable_window'
& $py -m pytest tests/inference/test_video.py::test_vfr_uses_actual_frame_times -q --tb=short
```

**Backend + ML:** остаток B1 — согласованный call_timeout_sec, тест override и
ограничения остатком deadline. Закрытые B2/B3/B7 и CPU-часть B9 не переоткрывать
без новых данных; следующие handoff проверять командами:

```powershell
& $py -m pytest tests/api/test_api_supervisor.py tests/api/test_api_media.py tests/api/test_api_runtime.py tests/integration/test_process_guards.py -q --tb=short
& $py -m pytest tests/integration/test_handoff.py -q --runxfail --tb=short -k 'recovery_after_close or admission_probe or readiness_requires'
```

**Frontend:** функциональная основа включена; дизайн Argon — следующий отдельный
commit. **Интегратор+Омар:** B10 и следующий согласованный GPU-прогон B11.
Backend/frontend CPU-контур подготовлен. Сквозной реальный analyze_video ещё
блокируют ML-остатки и verified-каталог. Ограниченный технический GPU-smoke можно
планировать отдельной задачей с явными тестовыми условиями, но не выдавать за
готовность production, оценку качества или основание поставить все флаги true.
Ни runtime, ни API из реального entrypoint в этом проходе с GPU не запускались.

## Следующий сквозной прогон — только после отдельного разрешения GPU

1. Получить согласованный ML handoff B5/B6/B8 и решение остатка B1; повторить
   CPU-suite. B2/B3/B4/B7 и проверенные сценарии B9 должны оставаться pass.
2. Утвердить настоящий каталог B10 и его применимость. Проверить runtime bytes
   через новый `check_runtime`. До успешных измерений **не менять** validation/status.
3. Для первого измерения использовать явный validation entrypoint ниже. Он
   использует реальный адаптер и verified-каталог, не production readiness gate:
   именно поэтому он хранится только локально в outputs, не становится ASGI entrypoint
   продукта и не свидетельствует о production readiness. Синтетического analyzer нет.
   Команды сейчас НЕ выполнялись. Выполнять только после разрешения GPU, исправлений
   остатков ML и согласования параметра service adapter. В `models/` этого checkout должны быть
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
