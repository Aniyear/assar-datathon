# CPU-интеграция, 17 октября 2026

**Статус: NOT READY. GPU не запускался.** Это отдельная ветка `codex/integration-cpu`
в `.worktrees/integration-cpu`, созданная от bootstrap
`5c748f9c94c8cd7b32aae04a5c8b49a0c79491d8`.

Текущий проход продолжает commit `8b034185869f0d6710d5d0a9a3b7def852f833f9`
в том же checkout. Прочитаны diff, код и README; объединены без конфликтов:

- ML `f9f461defeb948843e2667c70f80cf821596122c`;
- backend `1a3bda0fd97cc3407fc8ea7d9440a53869285144`;
- merge ML: `ffb95d106f5196e587a312f75b8321db757b0381`;
- merge backend: `ddb35a59269d562d254639311d27462f589b92a9`.
- новый ML handoff: `45f00ca89f1cd674157b7b5c2c2221987d9bf02a`;
- merge нового handoff: `acbcc60eade9676a9d858ceb6f2dbab54e136adf`.

`45f00ca` включён по новому разрешению пользователя. Более поздние ML/backend
коммиты не включались. Backend остаётся точно на `1a3bda0`; владелец параллельно
исправляет B2/B3/B7 и устаревший тест отсутствия service.py.
`main`, bootstrap и untracked `app/web/` исходного checkout не менялись.
Production-код ML/backend и `tests/api/**` интегратором не исправлялись.
Свои изменения: актуализация регрессий, этого отчёта и README.
Публикация сервиса не выполнялась.

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
& $py -m pytest tests/inference/test_video.py::test_vfr_uses_actual_frame_times -q --tb=short
```

Фактические результаты:

- `pip check`: No broken requirements found.
- `check_bootstrap`: 5 synthetic fixtures valid, verified rules: **0**.
- Полный окончательный прогон: **147 passed, 2 failed, 5 xfailed**, 85.50 s,
  exit code 1. Failures: VFR и
  `tests/api/test_api_runtime.py::test_ready_model_still_needs_verified_rules_and_ml_module`.
  Второй тест ждёт текст `app.inference.service`, а теперь получает
  `model runtime check failed (ModelUnavailableError)`: модуль уже существует,
  его check_runtime отклоняет неполный тестовый config. Это устаревшее ожидание
  backend-теста, не новый отказ импорта ML. Тест не изменён и не скрыт xfail.
- Окончательный `tests/integration --runxfail`: **3 passed, 5 failed**, 1.62 s,
  exit code 1. Падают B2/B3/B5/исходный generic-случай B6/B7.
- Отдельный VFR-воспроизводитель: **1 failed**, 1.92 s,
  `DecodeError: frame PTS outside video duration`.
- Все **15** новых service-тестов прошли в полном suite: имена интерфейса,
  argv/порт/флаги, SHA/size на крошечных синтетических файлах, init/health,
  типы ошибок, сохранение soft errors, сериализуемый результат настоящего Analyzer
  на синтетическом MOV и тестовом loopback-сервере.
- **25 из 26** backend API/runtime тестов прошли; исключение — устаревший тест выше:
  HTTP/schema/auth/upload, failed при ошибках, watchdog/recovery, обычный shutdown,
  Windows Job Object при смерти владельца, повторное использование здорового сервера.
  Вместо модели — тестовые Python-процессы; это не GPU-интеграция.

Полный suite выполнен вне sandbox с доступным loopback: в предыдущем проходе
sandbox блокировал `asyncio socketpair/accept`. Реальные веса/llama-server не
запускались. Исторический результат на f9f461d хранится в версии отчёта на 8b03418;
его числа не смешиваются с текущими.

`tests/integration/test_handoff.py` соединяет настоящий Analyzer с синтетическим
клиентом/декодером, затем настоящий serializer, SQLite, cleanup и повторное открытие
БД. Два разных observed_fact остаются разными в description вместе с шаблоном,
названием и URL источника; JPEG полностью декодируются из сохранённого JSON после
удаления upload/job. HTTP GET в backend читает именно этот сохранённый JSON.
Таким образом serializer не подменяет индивидуальное обоснование каталогом.
Реальность наблюдения самой моделью этим не доказана; см. B5.

У B4 снят strict xfail: прежнее ожидание `UnassessableVideoError` проходит без
изменений. У B6 исходное требование и вход `other_notable_action` сохранены;
его xfail остаётся. Добавлен отдельный проходящий тест кандидатной категории
`cash_bag_opened`, ожидающий `InvalidAnalysisResultError`. Импорт синтетических
helpers переведён в новое расположение `tests.inference.fakes`.

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

## Диагноз B1–B11 после 45f00ca

«Исправлено и проверено» ниже означает CPU-проверку названного требования,
а не готовность реальной модели. У частичных исправлений указан остаток.

| ID | Статус | Фактическое состояние / владелец |
|---|---|---|
| B1 | **Изменилось, частично исправлено и проверено** | `app.inference.service` появился; все 15 service-тестов проходят. Есть `check_runtime`, чистый `server_command`, `create_analyzer`; SHA256 server проверяется при наличии `runtime.server_sha256_reported` (в закреплённом config он есть). Адаптер сам не запускает/не останавливает модель, argv остаётся за supervisor. ML+backend: `Settings.model_call_timeout_sec` всё ещё не доходит до Analyzer, используется default 120; нужна согласованная передача и тест переопределённого значения. Ошибочное ожидание отсутствия service в backend-тесте обновляет backend. Полного запуска нового адаптера под supervisor на GPU не было. |
| B2 | **Осталось, воспроизведено** | Backend: `_recover` заменяет STOPPED на RECOVERING и пытается spawn после close; регрессия всё ещё падает. Требуются необратимая остановка, синхронизация close/recovery и тест shutdown при BUSY/RECOVERING. |
| B3 | **Осталось, воспроизведено** | Backend: ffprobe и ffmpeg получают один полный timeout; при бюджете 5 s и первом шаге 4 s второму дают 5 s вместо ≤1 s. Нужен общий absolute deadline и проверка перед созданием job; владение pre-admission декодерами также требует проверки. |
| B4 | **Исправлено и проверено** | ML: часть окон непригодна, verified-событий нет → `UnassessableVideoError`. Прежняя регрессия проходит с тем же ожиданием, strict xfail удалён. В ML-тестах также проходят все непригодные окна и частичная непригодность с verified-событием: последний случай возвращает violation с limitations. |
| B5 | **Осталось, воспроизведено** | ML: whitespace description превращается в общий текст категории вместо отказа `InvalidAnalysisResultError`. Индивидуальное наблюдение должно быть непустым. Backend serializer корректно сохраняет непустое наблюдение вместе с источником; его менять для этого не требуется. |
| B6 | **Изменилось, частично исправлено и проверено** | ML: кандидатная категория без verified-правила при отсутствии событий теперь даёт `InvalidAnalysisResultError`; добавленная отдельная регрессия проходит. Но `other_notable_action` допускает no_violation: исходная регрессия всё ещё падает, ожидание не ослаблено. Это явное расхождение политики нового handoff с прежним требованием, требует согласования ML+интегратора. Совпадение category/type_id по-прежнему не проверяет scope/required_context/counterexamples; limitation теперь честно это сообщает, но проверку не заменяет. |
| B7 | **Осталось, воспроизведено** | Backend: gate принимает unrelated=true вместо обязательных validation-полей. Регрессия использует только временную копию config и тестовый service. Нужна проверка обязательных имён/типов/значений, а не удаление false-полей. Production config не менялся. |
| B8 | **Осталось, воспроизведено** | ML: существующий VFR-тест повторно падает `DecodeError: frame PTS outside video duration`. Диагностика предыдущего прохода на том же FFmpeg: duration=7.8 s, последний выбранный PTS=7.9 s, 28 кадров. В этом проходе повторён сам тест, значения отдельной диагностикой заново не измерялись. Нужна проверка duration/fixture/PTS без сокрытия ошибки clamp-ом. |
| B9 | **Пока только риск по чтению кода** | Backend, файлы не изменились: stop_tree подтверждает только выход root; assign происходит после spawn; при неудачной остановке ссылки на owned-процессы сбрасываются; `poll(deadline)` затем `recv()` не ограничивает чтение неполного IPC-ответа. Крайние сценарии не воспроизведены процессными тестами. Обычные watchdog/recovery/shutdown/orphan-тесты проходят — это не подтверждение всех этих границ. |
| B10 | **Осталось, проверено отсутствие правил** | Омар+интегратор: verified rules = 0. Нужен настоящий каталог с подтверждённой применимостью и связью наблюдений с нормами. Синтетические правила остаются только в тестах. |
| B11 | **Осталось, проверка не выполнялась** | Интегратор: новый analyze_video/service/supervisor на RTX 3060 не измерен. Успешный старый benchmark a6d5b6a не закрывает это требование. GPU сейчас запрещён; нужен отдельный согласованный прогон после исправлений. |

Дополнительные ограничения B1 по чтению кода: отсутствие server_sha256 отключает
его проверку; CUDA DLL не перечислены/не хешируются; extra_args могут добавлять
флаги поверх фиксированного argv. В текущем config хеш есть, extra_args пуст;
это не установленный сбой текущей конфигурации. Проверка SHA/size выполнена на
синтетических файлах, не на реальных весах/бинарнике этой машины.

## Точные задания владельцам

Ниже команды из корня **этой интеграционной ветки**, с `$py` из раздела проверки.
Для другого checkout использовать его собственный CPython 3.11 с общим lock.
Backend/ML исправления сюда не внесены; ожидается отдельный handoff с SHA.

**Backend — продолжить уже назначенные B2/B3/B7 и обновление теста service:**

```powershell
& $py -m pytest tests/integration/test_handoff.py -q --runxfail --tb=short -k 'recovery_after_close or admission_probe or readiness_requires'
& $py -m pytest tests/api/test_api_runtime.py::test_ready_model_still_needs_verified_rules_and_ml_module -q --tb=short
```

- B2: запретить любую повторную инициализацию runtime после начала close;
  дождаться остановки supervisor-loop перед закрытием store. Сохранить проверку
  отсутствия spawn после STOPPED и дополнить shutdown при BUSY/RECOVERING.
- B3: единый monotonic deadline на оба pre-admission subprocess, остаток ≤1 s
  во втором шаге существующей регрессии. После исчерпания бюджета job не создавать.
- B7: обязательные флаги с корректными типами и значениями; unrelated=true
  не может заменить обязательные проверки. Не менять production readiness для теста.
- Устаревший тест: отсутствие модуля моделировать явно, отдельно проверять
  доступный service с невалидным config/runtime. Не ожидать, что реального файла нет.
- B9: добавить управляемые CPU-процессные воспроизводители: потомок переживает
  root; потомок создаётся до assign; ошибка stop сохраняет владение и закрытый
  admission; частичная IPC-посылка не обходится без deadline. Только свои тестовые
  процессы. Сохранить проходящие штатные `tests/api/test_api_service.py` и orphan-тест.

**ML — B5/B6/B8; совместно с backend — остаток B1:**

```powershell
& $py -m pytest tests/integration/test_handoff.py -q --runxfail --tb=short -k 'empty_individual or unmapped_observation or unsupported_candidate or unreadable_window'
& $py -m pytest tests/inference/test_video.py::test_vfr_uses_actual_frame_times -q --tb=short
& $py -m pytest tests/inference/test_service.py -q --tb=short
```

- B5: отклонять пустой/whitespace observed_fact через InvalidAnalysisResultError;
  не генерировать общий текст вместо факта. Проверить, что исправление сохраняет
  тест индивидуального description/источника/JPEG после cleanup.
- B6: согласовать с интегратором generic-категорию и отсутствие доказательства
  отрицательного вывода. Исходная регрессия ожидает UnassessableVideoError для
  `other_notable_action`; её не заменяли новым кандидатным тестом. До согласованного
  решения она остаётся блокером. Сохранить исправленные B4 и candidate-case.
- B8: воспроизвести на `N-122697-gfcfb33922f-20260210`, определить причину расхождения
  container duration/PTS (включая корректность synthetic fixture). Передать фикс
  владельца и результаты VFR/nonzero-start/rotation без clamp неверных timestamp.
- B1: совместно согласовать параметр call_timeout_sec между RuntimeSpec/factory/
  Analyzer; проверить настройку, отличную от 120, и ограничение остатком deadline.
  Не добавлять второго владельца сервера. При усилении runtime validation отдельно
  определить политику обязательного server SHA, DLL manifest и extra_args.

**Интегратор + Омар:** подтвердить нормативную применимость B10 и политику B6;
после handoff повторить CPU-suite, затем отдельно согласовать B11. Не считать
добавленную строку limitations доказательством применимости нормы. До этого
readiness/validation остаются прежними; frozen-test для настройки не открывать.

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

1. Получить handoff с исправлениями открытых B1/B2/B3/B5/B6/B7/B8 и проверкой
   рисков B9, включить согласованные точные SHA в эту ветку; повторить CPU-команды.
   Закрытые регрессии должны стать pass; уже исправленный B4 должен оставаться pass.
2. Утвердить настоящий каталог B10 и его применимость. Проверить runtime bytes
   через новый `check_runtime`. До успешных измерений **не менять** validation/status.
3. Для первого измерения использовать явный validation entrypoint ниже. Он
   использует реальный адаптер и verified-каталог, не production readiness gate:
   именно поэтому он хранится только локально в outputs, не становится ASGI entrypoint
   продукта и не свидетельствует о production readiness. Синтетического analyzer нет.
   Команды сейчас НЕ выполнялись. Выполнять только после разрешения GPU, исправлений
   lifecycle и согласования оставшегося параметра service adapter. В `models/` этого checkout должны быть
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
