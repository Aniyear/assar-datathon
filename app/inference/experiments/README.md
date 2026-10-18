# Эксперимент: 2B Q8_0 против 4B Q4_K_M

`exp-2b-q8-vs-4b-q4km-v1`. Меняется **только модель**. Кадры, окна, промпт `obs-v0.1`, визуальный бюджет
(`--image-max-tokens 256`), ctx 8192 и параметры генерации — общие для обоих вариантов и совпадают с
`configs/model.yaml` (проверяется тестом). Production-конфигурация, общий контракт и baseline не меняются;
настройки эксперимента — только в `app/inference/experiments/model_compare.json`.

Это диагностическое сравнение, а не оценка качества сервиса и не F1 событий: эталонной интервальной
разметки нет. X-4 — диагностический пример (на нём уже смотрели ошибки), не независимая проверка.
Frozen test в эксперимент не входит.

## Модели

| Вариант | Источник и ревизия | Файлы (SHA256) | Лицензия |
|---|---|---|---|
| `2b-q8` (baseline) | `Qwen/Qwen3-VL-2B-Instruct-GGUF` @ `52d6c8ffea26cc873ac5ad116f8631268d7eb503`; база `Qwen/Qwen3-VL-2B-Instruct` @ `89644892e4d85e24eaac8bacfd4f463576704203` | `Qwen3VL-2B-Instruct-Q8_0.gguf` 1 834 427 424 Б `1e8db19207c8ce0733ddd78c2eff8a9e22c27c82f4443df94c25792ed8fe04f2`; `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf` 445 053 216 Б `f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82` | Apache-2.0 |
| `4b-q4km` | `Qwen/Qwen3-VL-4B-Instruct-GGUF` @ `1cd86afb9a95c410a6038ab3b40d8b578c892266`; база `Qwen/Qwen3-VL-4B-Instruct` @ `ebb281ec70b05090aa6165b016eac8ec08e71b17` | `Qwen3VL-4B-Instruct-Q4_K_M.gguf` 2 497 281 664 Б `66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a`; `mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf` 453 974 304 Б `30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d` | Apache-2.0 |

Лицензия указана в метаданных Hugging Face и front-matter карточки базовой модели (файла LICENSE в
репозиториях Qwen нет); карточка сохраняется как `BASE_MODEL_CARD.md` рядом с весами. mmproj у 4B — того же
семейства и того же квантования Q8_0, что у baseline.

Runtime: llama.cpp `b11669` (`620d2225c`). Windows — те же бинарники, что у baseline
(`models/llama.cpp-b11669`, CUDA 12.4). Для Linux x64 (опционально) — сборка того же тега:

| Архив | Байт | SHA256 |
|---|---:|---|
| `llama-b11669-bin-ubuntu-cuda-12.8-x64.tar.gz` | 172 525 113 | `4703ed1a3cd6e8504affb6cb9866f501e96d634d68a002dc9a8251c9e2819b49` |
| `cudart-llama-b11669-bin-ubuntu-cuda-12.8-x64.tar.gz` | 594 377 811 | `ab3eda89a82096bcba00b6448218c3c11eec2016b0f38d64884dc29736549f26` |

## Требования к GPU

| | 2B Q8_0 | 4B Q4_K_M |
|---|---|---|
| Пик VRAM | **3301 МиБ, измерено** (RTX 3050, весь GPU) | **≈ 4,2–4,6 ГиБ, оценка, не измерено**: веса 2,33 + mmproj 0,42 + KV-кэш f16 1,13 (36 слоёв × 8 KV-голов × 128 × ctx 8192) + буферы и CUDA-контекст |
| RTX 3050 4 ГБ | подходит | **не подходит** |
| RTX 3060 Laptop 6 ГБ | подходит | только если до старта свободно **≥ 5 ГиБ** (`nvidia-smi`); при аудите было свободно ~3,9 ГБ — закрыть другие GPU-приложения |

- Одновременно на GPU работает один llama-server: варианты запускаются по очереди. Не запускать во время
  интеграционной проверки на 3060 и рядом с другим ML-процессом.
- Диск: ещё ≈ 3,0 ГБ под 4B (`models/qwen3-vl-4b/`); для Linux-runtime ещё ≈ 0,8 ГБ.
- Время: 2B на 3050 обрабатывал 72,7 с видео за ~35 с. Для 4B ожидаемо медленнее; насколько — это и
  измеряет эксперимент.
- Решение по скорости и памяти для сдачи принимается только по замеру на RTX 3060.

## Список видео (выбирает человек до запуска)

`outputs/experiments/videos.local.json` (каталог `outputs/` исключён из Git). Только X-4 как `diagnostic` и
заранее выбранные dev-видео из утверждённого split, положительные и отрицательные. Frozen test запрещён
(скрипт принимает только `role` = `diagnostic` или `dev`). `source_label` нужен лишь для разбивки агрегатов
и в модель не передаётся.

```json
[
  {"id": "diag-1", "path": "C:\\path\\to\\SYNTHETIC-EXAMPLE.mov", "role": "diagnostic", "source_label": "violation"},
  {"id": "dev-1", "path": "C:\\path\\to\\dev_positive.mov", "role": "dev", "source_label": "violation"},
  {"id": "dev-2", "path": "C:\\path\\to\\dev_negative.mov", "role": "dev", "source_label": "no_violation"}
]
```

`id` — нейтральные метки; они попадают в локальные файлы, но не в печатаемые агрегаты.

## Команды (PowerShell, из корня checkout `codex/ml`)

Окружение — `.venv` из `requirements.lock` (см. `docs/OMAR_START.md`).

```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv
```

Baseline-runtime и 2B (если ещё не скачаны):

```powershell
python app/inference/fetch_runtime.py --dest models
```

Файлы 4B (проверка SHA256):

```powershell
.\.venv\Scripts\python.exe -m app.inference.experiments.fetch --variants 4b-q4km
```

Только для Linux x64, из корня checkout:

```bash
.venv/bin/python -m app.inference.experiments.fetch --variants 2b-q8,4b-q4km --platform linux-x64
```

Сравнение (GPU; запускать только в согласованное окно):

```powershell
.\.venv\Scripts\python.exe -m app.inference.experiments.compare --videos outputs\experiments\videos.local.json --variants 2b-q8,4b-q4km
```

На Linux добавить `--platform linux-x64`. Результат — `outputs\experiments\<время>\`:
`frames\` (общие кадры), `responses.jsonl` (ответы обоих вариантов по одинаковым окнам), `llama-server-*.log`,
`summary.json` (агрегаты; он же печатается).

Страница слепой проверки (A/B в случайном порядке по окнам; ключ `blind_key.json` не открывать до конца):

```powershell
.\.venv\Scripts\python.exe -m app.inference.experiments.review outputs\experiments\<время>
```

Открыть `outputs\experiments\<время>\review.html` локально, оценить каждое наблюдение
(«Подтверждено» / «Не подтверждено» / «Неясно»), отметить окна с пропущенным видимым действием, нажать
«Скачать review_export.json» и положить файл в каталог прогона.

```powershell
.\.venv\Scripts\python.exe -m app.inference.experiments.score outputs\experiments\<время> --review outputs\experiments\<время>\review_export.json --videos outputs\experiments\videos.local.json
```

## Что попадает в отчёт (только агрегаты)

- `summary.json`, по варианту: загрузка, среднее/максимальное время окна, максимальное время видео,
  пик VRAM (весь GPU) и прирост, статусы окон (`ok`, `truncated`, `invalid_json`, `invalid_schema`,
  `error:<тип>`), перезапуски сервера, непригодные окна, число наблюдений и их распределение по категориям;
  по группам `all` / `role=…` / `label=…`: complete, `failed_technical` и его доля, `all_unassessable`.
  «failed_technical» — хотя бы одно окно не принято; полный вердикт сервиса здесь не считается, потому что
  verified-каталог пуст.
- `score.json`: подтверждённые, неподтверждённые, неясные и неоценённые наблюдения, доля подтверждённых среди
  решённых, окна с отмеченным пропуском — по вариантам и группам. Это ручная оконная сверка, не F1 событий.

Содержательные ответы, кадры и описания сцен в облачные чаты не передаются.

Пример с `SYNTHETIC-EXAMPLE` иллюстрирует только формат; это не идентификатор
или результат реального ролика. Реальные метки и результаты остаются локально.
