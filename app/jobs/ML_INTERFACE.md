# Интерфейс ML, который ожидает backend

Base: `5c748f9`, контракт v0.1 (`app/contracts.py`). Контракт не меняется; это
описание точки подключения ML к supervisor. Типы — `app/jobs/ml_interface.py`.

## Модуль `app.inference.service`

```python
def check_runtime(model_cfg: dict, repo_root: Path) -> None
def server_command(model_cfg: dict, repo_root: Path, port: int) -> list[str]
def create_analyzer(endpoint: str, model_cfg: dict, catalog: RuleCatalog,
                    repo_root: Path) -> VideoAnalyzer
```

- `model_cfg` — `configs/model.yaml`, прочитанный stdlib `json`; `repo_root` — корень
  checkout; относительные пути config разрешать от него.
- `check_runtime` — в процессе API при старте: существование и SHA256/размер
  `runtime.server_bin`, `weights`, `mmproj`. Без запуска процессов, сети и GPU.
  Ошибка → `ModelUnavailableError`; backend отказывает в старте.
- `server_command` — чистая функция: argv llama-server по закреплённым параметрам
  (`--host 127.0.0.1 --port <port> --offline -np 1 --no-webui --jinja`, ctx, ngl,
  image_max_tokens, extra_args). Порт выбирает backend. Процесс запускает только
  supervisor (Popen + Windows Job Object kill-on-close), он же читает `/health`
  (200 и `{"status":"ok"}`) без proxy и редиректов, и он же останавливает сервер.
- `create_analyzer` — в отдельном Python-worker (multiprocessing spawn), один раз
  на запуск worker, после health. `endpoint` = `http://127.0.0.1:<port>`.
  `catalog` содержит только `verified` правила. Возвращает `analyze_video`.

## Требования к `analyze_video(video_path, job_dir, deadline_monotonic)`

- Последовательные вызовы в одном worker; клиент и сервер переиспользуются.
- `video_path` — случайное имя `input.mov`, абсолютный путь; labels/ID не передаются.
- Evidence JPEG — внутри `job_dir`, абсолютные пути. Backend сам кодирует base64,
  формирует description из каталога (`observed_fact` + `claim_template_ru` + источник).
- `rule_id`, `type_id`, `type_name_ru` события — точно из переданного `catalog`.
- Ошибки — только наследники `AnalysisError`. Поведение backend:
  - `DecodeError`, `UnassessableVideoError`, `InvalidAnalysisResultError` → job failed,
    сервер переиспользуется, если `/health` ok;
  - `ModelUnavailableError`, `AnalysisTimeoutError`, любое другое исключение,
    смерть worker, отсутствие ответа к deadline → failed и полный перезапуск
    worker + llama-server, приём закрыт (503) до health нового сервера.
- Дочерние ffmpeg/ffprobe — обычный `subprocess`, **без** `CREATE_BREAKAWAY_FROM_JOB`,
  `DETACHED_PROCESS`, двойного fork; иначе supervisor не сможет их завершить.
- Loopback HTTP: `urllib.request.build_opener(ProxyHandler({}))`, без редиректов;
  каждый timeout ≤ `remaining_seconds(deadline_monotonic)`.
- Не печатать кадры/описания сцен в stdout/stderr (журнал worker локальный, но
  содержательные логи не нужны).
- Результат должен проходить pickle (обычный `AnalysisResult`).

## Готовность модели в config

Backend стартует production только если в `configs/model.yaml`:
`status == "ready"` и все значения `validation` равны `true`, а в каталоге есть
хотя бы одно `verified` правило. Значения выставляет интегратор после проверки на
RTX 3060. Сейчас `pending_integration_and_3060_validation` → старт отклоняется.
