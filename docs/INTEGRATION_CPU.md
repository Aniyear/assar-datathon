# Интеграция CPU: согласованные ML/backend fixes

**Последующее состояние:** CPU-результат принят в `05ba3a68bf74b86003375151b51d486c5d89b7b0`.
Позже выполнен отдельный технический GPU-стенд; агрегаты и границы результата
описаны в [MVP_LAUNCH.md](MVP_LAUNCH.md). Реальные done/JPEG и качество ещё не
подтверждены. Ниже сохранена квитанция именно CPU-прохода, включая его тогдашнее
«GPU не запускался»; это не описание последующих проверок. Новых CPU/GPU-прогонов
при подготовке MVP-документации не выполнялось.

Дата: 17 октября 2026. Продолжение существующей `codex/integration-cpu` от
`9e835772c8f15d559e2cb3f896bf401b78ad29a8`. Новая ветка не создавалась.

Включены в указанном пользователем порядке:

1. ML `e083f79625f13b2bc516ec2a55cc0a052205d205` поверх `7e01e83`.
2. Backend `7c343939abbe12519fa14d44650fda9fc03686ce` поверх `a8581e3`.
3. Frontend `ef31b351bcec701e6b66e4229954e43b9770cc2e` уже был включён, не менялся.

Production-код ML/backend не исправлялся интегратором: деревья равны точным handoff.
Новые изменения интегратора — регрессии, тестовые стенды/каталог и документация.
`main`, исходный untracked `app/web`, config/readiness/validation, production-каталог,
зависимости, split и прежние квитанции не изменялись. GPU, веса и реальные видео
не запускались/не анализировались. Содержимое локальных аннотаций не читалось.

## Фактические проверки

Windows, CPython 3.11.9; FFmpeg/ffprobe
`N-122697-gfcfb33922f-20260210`. Все видео и ответы в этих проверках синтетические.
Из integration checkout используется существующий корневой venv:

```powershell
$py = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
& $py -m pytest -q --tb=short --durations=10 -ra --junitxml=outputs/cpu-validation/current.xml
& $py -m pytest tests/integration --runxfail -q --tb=short -ra --junitxml=outputs/cpu-validation/integration.xml
& $py -m pytest tests/inference/test_video.py -q --tb=short
& $py scripts/validation/vfr_probe.py
& $py -m pip check
& $py scripts/check_bootstrap.py
& $py scripts/validation/gpu_smoke.py --runtime-root '..\..'
```

| Проверка | Фактический результат |
|---|---|
| Финальный полный suite | **213 passed**, 165.14 s, exit 0; 0 failed/xfail/skip |
| Финальный integration --runxfail | **16 passed**, 28.12 s, exit 0; 0 failed/xfail/skip |
| Первый integration --runxfail до снятия B5/B6 xfail | 10 passed, 3.66 s |
| tests/inference/test_video.py | 8 passed, 5.30 s |
| B1: реальный service через backend factory и spawn-worker | 1 passed, 7.72 s |
| Дополнительные duration/offset/rotation и отсутствие clamp | 2 passed, 3.89 s |
| CPU-проверки самого GPU-стенда | 3 passed, 12.85 s |
| pip check | No broken requirements found |
| check_bootstrap.py | 5 синтетических fixtures валидны; production verified rules = 0 |
| GPU-скрипт без --run-gpu | prepared_not_run; exit 0, модель не запускалась |

Финальные JUnit XML остаются локально в ignored outputs. Сравнение total/skipped/
failures подтверждает отсутствие скрытых skip/xfail в выполненных наборах.
В исходниках остались только условия окружения FFmpeg/Windows: здесь они не
сработали. Старый filterwarnings одного backend-теста касается ожидаемого отказа
startup; он не исключает тест. Маркеров xfail больше нет. Две xfail B5/B6 сняты
после фактического PASS с --runxfail; исходные assertions сохранены. Уже адаптированные
B2/B3/B7 повторно не переписывались. Новые тесты владельца включены как есть.

Статика + живой HTTP API с тестовым analyzer снова входят в полный suite и проходят.
Браузерные 39 PASS / 0 FAIL относятся к предыдущему проходу `9e83577`: браузерный
стенд сейчас не повторялся, frontend не менялся. Ни один из этих результатов не
является настоящим GPU inference.

## B1: проверено поведение, не только сигнатуры

`tests/integration/test_service_timeout.py` строит production RuntimeSpec через
настоящий `production_spec(Settings.from_env(...))`: `ASSAR_MODEL_CALL_TIMEOUT_SEC=2`.
Только readiness и проверка файлов явно заменены CPU-seam внутри теста; flags не
переписываются, серверный argv заменён синтетическим HTTP server. Factory остаётся
`app.inference.service:create_analyzer`, без recorder вместо адаптера.

Путь: HTTP upload → backend → настоящий spawn-worker → service → Analyzer →
LlamaClient → реальный loopback HTTP-запрос. Сервер задерживает ответ на 5 s,
analysis budget = 30 s. Job становится failed за 1.8–4.5 s, SQLite reason строго
`AnalysisTimeoutError`; с default 120 s этот ответ успел бы завершиться успешно.
После ошибки оба PID меняются, admission снова открывается. Передача 2.0 подтверждена
действием ограничения, а не одним содержимым kwargs. Default 120.0 и запись override
7.0 покрываются отдельными backend-тестами. ML-тест также проверяет более короткий
общий deadline при call_timeout 60 s.

## VFR и точная граница исправления B8

Новый input `-t <format.duration>` ограничивает декодирование интервалом контейнера.
Это не clamp и не независимая проверка того, что duration достоверна.
Локальный `scripts/validation/vfr_probe.py` заново измерил:

| Синтетический MOV | duration, s | последний PTS без -t | последний выбранный PTS | число выбранных |
|---|---:|---:|---:|---:|
| VFR с B-frames | 7.8 | 7.9 | 7.7 | 88 |
| VFR без B-frames | 7.933333 | 7.9 | 7.9 | 90 |

При шаге 0.05 s у первого файла два декодируемых кадра находятся на/за duration;
они исключены. У второго последний кадр сохранён. Отдельный исходный VFR-тест с
шагом 0.25 s теперь проходит без изменения требований. Комбинация start_time=2 s
и rotation=90° даёт PTS `[0, 1, 2, 3]` и JPEG 284×160; обычные nonzero-start и
rotation регрессии владельца также проходят. Подставленный PTS 3.1 при duration 3
по-прежнему приводит к DecodeError, а не clamp.

**Измеренное ограничение:** при заведомо ложном VideoInfo.duration=1 для настоящего
4 s синтетического MOV sample_frames молча отсекает хвост (последний выбранный PTS
0.8). Это отрицательная характеристика, не успешная поддержка испорченного MOV.
Сам mvhd/edit-list вручную не повреждался, поддержку произвольных неверных duration
не заявляем. Новая регрессия сохраняет этот факт явно. Различение ошибочной duration
и корректной edit list не реализовано. Sampling всё ещё сравнивает float и может
пропускать соседний кадр при равном шаге; переработка декодера не входит в проход.

## Статус B1–B11

| ID | Статус сейчас | Основание / ограничение |
|---|---|---|
| B1 | **Исправлено и проверено CPU** | Реальная передача нестандартного timeout до ML HTTP-вызова и recovery, default/invalid/deadline тесты проходят. Настоящий llama-server ещё не проверен. |
| B2 | Исправлено ранее, PASS сохранён | Настоящий close → поздние recover/reopen не создают процесс, STOPPED. |
| B3 | Исправлено ранее, PASS сохранён | Единый absolute deadline двух admission-стадий; новые тесты владельца также отвергают опоздавший decode. |
| B4 | Исправлено ранее, PASS сохранён | Непригодное окно без событий не доказывает no_violation. |
| B5 | **Исправлено и проверено CPU** | Пустое/пробельное индивидуальное описание → InvalidAnalysisResultError; общий текст больше не подставляется. Xfail снят. |
| B6 | **Исправлено и проверено CPU** | generic other_notable_action без событий → UnassessableVideoError; unsupported candidate → InvalidAnalysisResultError. Generic рядом с verified-событием сохраняется в limitations. Xfail снят. |
| B7 | Исправлено ранее, PASS сохранён | Нужны точные имена/JSON bool трёх флагов; произвольные true не обходят gate. Production-флаги всё ещё false. |
| B8 | **Исходная регрессия исправлена и проверена** | VFR проходит на установленном N-122697; tail/без B-frames/offset/rotation проверены. Ограничение ложной duration остаётся явно описанным. |
| B9 | Проверенные Windows CPU-сценарии закрыты | Дерево потомков, root already exited, stop confirmation, BUSY/RECOVERING close, блокирующий recv. В новом стенде CPU-проверен suspended owned-server deadline/recovery. Реальный CUDA lifecycle не проверен. |
| B10 | **Остаётся production-блокером** | Настоящий verified-каталог пуст. Отдельный SYNTH-GPU каталог разрешён только тестовому стенду. |
| B11 | **Не выполнялось** | RTX 3060 + текущий real service/supervisor/HTTP API не запускались. Старый baseline benchmark не подтверждает их. |

Воспроизведённых незакрытых CPU-падений согласованного ML/backend handoff после
финальных проверок нет. Не превращать отсутствие падений в доказательство качества.
B6 консервативнее: no_violation только при пригодных окнах и полном отсутствии
наблюдений; возможное увеличение failed на настоящих видео **не измерено**.
Связь правила всё ещё category ↔ type_id; required_context/counterexamples машинно
не проверяются. Не все OS-error/IPC-partial-message/POSIX сценарии покрыты.

## Следующий технический GPU-прогон

Готовы скрипт, отдельный тестовый каталог, CPU-тесты observer/lifecycle и
[точная инструкция](GPU_TECHNICAL_SMOKE.md). После отдельного разрешения:

```powershell
$py = (Resolve-Path '..\..\.venv\Scripts\python.exe').Path
& $py scripts/validation/gpu_smoke.py --runtime-root '..\..' --call-timeout-sec 17 --run-gpu
```

Проверяются настоящий llama-server/service/supervisor/API, finish_reason/schema,
повторное использование PID, forced deadline, recovery, cleanup и освобождение
GPU. Сгенерированное видео и SYNTH-GPU каталог не подтверждают качество/нормы.
Runtime files проверит сам runner перед запуском; здесь их SHA на хосте не измеряли.
Readiness остаётся закрытой: технический тест использует явный test RuntimeSpec,
не выставляет все validation=true и не становится production entrypoint.

## Остаток по владельцам

- **ML:** не новые подтверждённые CPU-дефекты, а ограничения duration/float sampling,
  неизмеренная доля failed, соответствие категорий реальным правилам и качество.
  Произвольные файлы с неверной duration пока не обещать поддерживать.
- **Backend + ML + интегратор:** выполнить следующий согласованный GPU lifecycle
  smoke, зафиксировать фактический finish_reason, дедлайны и освобождение ресурсов.
  Искусственный suspend собственного процесса не доказывает обработку CUDA deadlock.
- **Омар + интегратор:** B10 — настоящий подтверждённый каталог и применимость норм;
  только потом production readiness по действительным результатам.
- **Frontend:** функциональная основа сохранена; дизайн Argon отдельным изменением.

Подтверждение пользователя об отсутствии дополнительных дубликатов остаётся в
`DATA_SPLIT_USER_CONFIRMATION.md`, без повторного просмотра и без переписывания
квитанций. Split не перераспределён: 22/8/10 групп, X-4/X-47 только dev.
Это ранее зафиксированные агрегаты, не новый автоматический аудит этого прохода.
