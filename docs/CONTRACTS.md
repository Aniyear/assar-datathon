# Контракты сервиса assar

Зафиксированная версия 0.1, 17 октября 2026. Внешняя схема сверена с разделом 10 исходного кейса и FAQ; Python-модели находятся в `app/contracts.py`. Endpoint, worker, inference и UI в стартовую основу не входят. Дополнение к версии 0.1 согласовано с техническим ML baseline c32964a318d47a0ce7648a697044b48c2e40c40a: сигнатура и JSON-типы сохранены, уточнены deadline и владелец процесса llama-server. Омар подключает свои локальные ошибки к общей иерархии и добавляет адаптер analyze_video; подробная миграция — docs/ML_HANDOFF.md. Внешних полей не добавлено.

## API жюри

Оба метода требуют заголовок `X-API-Key`. Проверять ключ и для получения результатов. Ключ хранится в окружении backend, не в Git, URL, клиентском JavaScript или логах.

`POST /api/v1/analyze`: multipart/form-data, поле `file`, файл `.mov`. Успех возвращает JSON `{"job_id":"..."}`. Предлагаемый HTTP-код 202; он не задан кейсом. Не ждать завершения inference внутри POST.

`GET /api/v1/jobs/{job_id}`: `status` только `processing`, `done`, `failed`. Состояния очереди и отдельных стадий хранить внутри; queued снаружи отображать processing. Не добавлять новые обязательные поля в запросы.

```json
{"job_id":"example-job","status":"processing"}
```

```json
{
  "job_id": "example-job",
  "status": "done",
  "verdict": "no_violation",
  "violations": []
}
```

При `violation` список содержит минимум одно событие:

```text
job_id: string
status: "done"
verdict: "violation"
violations: [
  {
    type: string,
    timecode_sec: finite number >= 0,
    description: string,
    evidence_frames: [
      {timestamp_sec: finite number >= 0, image_base64: string}
    ]
  }
]
```

`image_base64` — действительный JPEG в base64 без префикса `data:image/jpeg;base64,`. На событие 1–3 кадра. `timecode_sec` и `timestamp_sec` — секунды от начала входного видео, в пределах его длительности; хранить фактический PTS извлечённого кадра, не вычислять время из номинального FPS. События сортировать по времени. Несколько событий допускаются.

Правило и нормативный источник обязательны в продукте, но отдельного поля для них в схеме жюри нет. Поэтому совместимый вариант — включать в `description`: наблюдаемый факт; почему это признак возможного нарушения; название акта, пункт и URL. Расширения JSON не использовать без уточнения организаторов. UI может получать дополнительные внутренние данные отдельным собственным маршрутом.

Для ошибки: `{"job_id":"example-job","status":"failed"}`. Расширение `error` требует согласования схемы; диагностические подробности держать в локальном журнале. Принятые командой HTTP-ответы: 401 при неверном ключе; 404 для неизвестного job; 413 при превышении лимита; 415 для неподдерживаемого типа; 422 для повреждённого файла, если это выявлено до создания job; 503 при занятом worker/перегрузке до создания job. После создания job сбой анализа даёт `failed`. Эти коды и минимальная форма failed — наши решения, а не дополнительные требования кейса.

На старте допускается одно принятое задание, без ожидающих GPU-заданий: `max_inflight_jobs=1`, `gpu_workers=1`. Это ограничение общей конфигурации до измерения очереди. Консервативный отсчёт 300 секунд — от принятия HTTP-запроса на загрузку, включая чтение файла, очередь и ответ; кейс не уточняет точку старта. Внутренний бюджет анализа 240 секунд не является измеренной скоростью.

## Интерфейс модели и backend

```python
analyze_video(video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult
```

Функция синхронная, выполняется в изолированном Python-worker с заранее инициализированным клиентом собственного llama-server на 127.0.0.1. Backend supervisor владеет процессами worker, llama-server и декодера, запускает сервер один раз до приёма заданий и переиспользует его между успешными job. analyze_video не загружает веса, не запускает и не останавливает сервер на каждый вызов. Не передавать модели labels, исходный ID, эталонный таймкод или истинный вердикт. Случайное имя загрузки не должно влиять на результат.

`deadline_monotonic` — абсолютный момент `time.monotonic()` на том же компьютере. Backend вычисляет request_deadline от принятия запроса до чтения upload, затем передаёт `min(request_deadline - response_reserve_sec, monotonic() + analysis_budget_sec)`; значения по умолчанию 300 / 5 / 240 секунд. `remaining_seconds()` проверять до/после каждой стадии; I/O timeout ограничивать остатком. Не сбрасывать бюджет при новом окне/повторе. Просроченный ответ не публиковать.

Supervisor обеспечивает wall-clock watchdog: при зависании/timeout помечает job failed, прекращает собственные worker **и отдельный llama-server**, завершает дочерний декодер и подтверждает их остановку. Одно завершение Python-worker либо HTTP/Future timeout не останавливает GPU-сервер. До bounded recovery и успешного health нового сервера приём закрыт (503); нельзя объявлять слот свободным по одному исключению. Supervisor хранит handles/идентичность процессов до ожидания health, предусматривает cleanup при своей аварии, не убивает чужие процессы по имени/порту. Production не подключается к произвольному пользовательскому серверу. Loopback HTTP должен обходить proxy и запрещать внешние редиректы. Детали владения, переноса baseline и ошибок — [ML_HANDOFF.md](ML_HANDOFF.md).

`AnalysisResult` (все поля обязательны):

```text
schema_version: "0.1"
verdict: "violation" | "no_violation"
events: list[Event]
limitations: list[str]
timings_ms: dict[str, number]
model_revision: str
```

`Event` (все поля обязательны):

```text
event_id, type_id, type_name_ru: string
start_sec, end_sec, timecode_sec: number
observed_fact: string
rule_id: string
evidence: list[{timestamp_sec: number, local_jpeg_path: string}]
```

ML возвращает абсолютные локальные пути к доказательствам внутри `job_dir`; backend проверяет границы пути, размер и JPEG, затем кодирует base64 и подставляет источник из каталога. Свободный текст модели не определяет URL или пункт закона. Интервалы и limitations внутренние; официальный serializer выдаёт ровно согласованную внешнюю схему. Невалидный ответ модели требует ограниченной повторной попытки или failed, а не автоматического «нарушений нет».

Общие классы: `AnalyzeAccepted`, `ProcessingJob`, `DoneJob`, `FailedJob`, `Violation`, `EvidenceFrame`, `AnalysisResult`, `Event`, `LocalEvidence`, `Rule`, `RuleCatalog`. `JobResponse` — union с discriminator `status`; `JOB_RESPONSE_ADAPTER` проверяет JSON. `VideoAnalyzer` — Protocol сигнатуры функции, без реализации модели.

Все неизвестные поля запрещены. Строки непустые, времена — конечные числа ≥ 0, строки чисел и bool отклоняются; `start_sec <= timecode_sec <= end_sec`. События отсортированы, внутренние event_id уникальны. `processing/failed` не принимают verdict/violations даже со значением null. Пустой список событий нельзя сочетать с violation и наоборот. Для JPEG выполняются строгая base64-проверка и полное локальное декодирование Pillow.

Backend обязан вызвать `validate_analysis_for_video(result, duration_sec, job_dir)` и `validate_analysis_rules(result, catalog)`, затем создать публичный `DoneJob` и вызвать `validate_done_for_video(done, duration_sec)`. Они повторно валидируют изменяемые списки, проверяют длительность, разрешённые пути и verified rule/type. Реальность PTS и соответствие кадра событию доказывает декодер/оценка, не структура JSON. Ошибки файловой системы, ValueError/Pydantic и `AnalysisError` должны приводить к failed, без вывода приватного входа в HTTP/телеметрию. Наследники AnalysisError: `DecodeError`, `ModelUnavailableError`, `AnalysisTimeoutError`, `UnassessableVideoError`, `InvalidAnalysisResultError`. Общий валидатор не заменяет watchdog Омара.

До уточнения организаторов предлагается явная политика: полностью непригодное для визуальной оценки видео вызывает внутренний `UnassessableVideoError`, снаружи job получает failed без verdict. Это наше консервативное решение, которое может считаться необработанным видео при оценке; кейс не задаёт отдельного состояния неопределённости. Если есть подтверждённое событие и другой неразборчивый фрагмент, вернуть violation и сохранить limitation внутри. Если анализ выполнен на пригодном видео и обоснованных событий не найдено, вернуть no_violation. Не использовать failed как способ избегать трудных, но обрабатываемых примеров.

## Каталог правил

`app/rules/catalog.json`: объект `{"schema_version":"0.1","rules":[]}`. Поля правила: `rule_id`, `type_id`, `type_name_ru`, `source_title`, `source_url`, `clause`, `effective_version_checked_at`, `scope`, `observable_condition`, `required_context`, `counterexamples`, `claim_template_ru`, `status`. `required_context` и `counterexamples` — списки строк. `clause` может быть null: FAQ 31 допускает источник без точного пункта. `effective_version_checked_at` может быть null только у candidate/disabled. URL только HTTP(S), без учётных данных.

Статусы `candidate`, `verified`, `disabled` внутренние. Только правила verified после проверки Омаром допускаются в финальные выводы. Формулировка различает видимый факт и предположение. Кандидаты из docs/normative_candidates.md не являются готовой разметкой датасета.

Стартовый каталог пуст. `configs/model.yaml` фиксирует метаданные baseline Омара: llama.cpp b11669, Qwen3-VL-2B-Instruct GGUF Q8_0 + mmproj Q8_0; `status: pending_integration_and_3060_validation`. Это JSON-совместимый YAML, читаемый стандартным json; сторонний YAML-loader не требуется. Локальные веса не скачивались/не проверялись, GPU не запускался. analyze_video пока отсутствует; статус не означает готовность к production. Backend должен явно отказать в production-старте без готовой модели и проверенного каталога, а не выдавать no_violation. Числовые лимиты команды: upload 100 MiB, JPEG ≤ 5 MiB и ≤ 16 млн пикселей. Это настраиваемые ресурсные ограничения; кейс их не устанавливает.

`app/settings.py` читает `ASSAR_*` только из окружения; `.env.example` — справка, автоматической загрузки .env нет. `configure_offline_environment()` вызывается до импорта ML-библиотек. Offline-флаги дополняют `llama-server --offline` и загрузку GGUF только по локальному пути; отсутствие сетевых обращений проверяется отдельно при интеграции. Общий стартовый commit не содержит server entrypoint и не выполняет inference.

Синтетические ответы: `tests/fixtures/contracts/`; генератор `scripts/make_contract_examples.py` не читает датасет. Внешние примеры не содержат дополнительных полей-маркеров: синтетический характер указан в ID/type/description и README. Не подключать их в runtime.

## Локальные данные и оценка

Сохранять исходные имена, но сопоставлять файл/строку по нормализованному ID `X-N`, заменяя только начальную кириллическую `Х`/`х` на латинскую `X`. Не переименовывать исходники. SHA256 задаёт минимальную группу дубликатов; после ручной проверки расширить группы повторяющихся сцен.

Каноническая CSV-заготовка v0.1 уже создана в `analysis/local/human_annotation_template.csv`: `canonical_id`, `sha256`, `duplicate_group_ids`, `source_verdict`, `source_timestamp_sec`, `reviewer`, `review_status`, `event_index`, `event_type`, `start_sec`, `end_sec`, `timecode_sec`, `observable_action`, `rule_id`, `source_url`, `source_clause`, `uncertainty`, `notes`. Несколько событий — несколько строк с различным event_index; значения ручных полей первоначально пустые. Исходные текстовые метка и отметка сохраняются в `labels_normalized.csv` как label_raw и timestamp_raw; source_verdict и source_timestamp_sec уже нормализованы.

Для импорта JSON из review.html нужен явный адаптер: `observation` → `observable_action`, `timecode_sec` → число, `interval` → проверенные start_sec/end_sec, текстовое `rule` → проверенные rule_id/source_url/source_clause. Не парсить свободное rule как подтверждённый источник автоматически. HTML — инструмент первичных заметок, CSV — формат обмена для оценки. Ни один импорт не перезаписывает заполненную аннотацию молча. Минимальный group_id равен SHA256, расширенные группы сходных сцен определяются отдельным манифестом split.

`split` хранится по group_id, а не по кадрам. Все кадры, копии и производные клипы одной группы остаются в одном split. Эталонные таймкоды разрешены только для разметки и оценки, не для выбора окон при runtime/validation inference.

Порядок фиксации, контрольная сумма приватного manifest и ограничения независимости — в `docs/DATA_SPLIT.md`. Проверка `validate --require-dev-pair X-4 X-47` подтверждает одну SHA256-группу исключительно в dev и отсутствие пересечения групп; исходное распределение v1 и его SHA256 не изменены. Полный split/labels остаются в ignored `analysis/local/`; ML получает только выделенный train/dev пакет. До настройки модели обязательна локальная проверка близких сцен человеком.

## Минимальные проверки контракта

1. Корректный ключ позволяет загрузить видео; неверный ключ не позволяет загрузить или прочитать job.
2. done/no_violation имеет пустой список; done/violation имеет непустой список и валидные 1–3 JPEG на событие.
3. Таймкоды доказательств соответствуют реально выбранным кадрам и входят в длительность.
4. MOV H.264 с переменным FPS декодируется; ошибка чтения даёт failed.
5. Реальный endpoint завершает обработку самого длинного видео в пределах 300 секунд от принятия задания, включая очередь. Внутренний целевой бюджет — 240 секунд.
6. При отключённом внешнем доступе анализ и UI работают с заранее загруженными весами, скриптами, стилями и шрифтами.
