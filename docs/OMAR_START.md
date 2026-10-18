# Омару: общий контракт поверх готового ML baseline

Общая ветка — **origin/codex/bootstrap**. Первый стартовый commit fe5305a уже
содержал типы и fixtures; текущая версия уточняет lifecycle/deadline под твой
**ca336d8c7caa8a7191eea6dd4aeba244f0799191** (base main c9f8dda).
Анияр передаёт полный новый hash после push. Старый Git bundle содержит только
fe5305a; для этого дополнения получать актуальную ветку с GitHub.

Твой ML-код прочитан через git show, не слит, не изменён. Не пересоздавай ML-ветку
и не сбрасывай свою работу. В своём чистом ML checkout после проверки diff:

```powershell
git fetch origin
git diff HEAD...origin/codex/bootstrap -- app/contracts.py app/settings.py configs/model.yaml docs/CONTRACTS.md docs/ML_HANDOFF.md
# При принятии общего контракта тобой, только в собственной ML-папке:
git merge --no-ff origin/codex/bootstrap
```

Если есть незакоммиченные изменения — сначала сохрани их своим commit; не reset/clean.
Backend и ML остаются в разных worktree/клонах. Для НОВОЙ backend-папки, если
ветка ещё не создана: `git worktree add -b codex/backend ../assar-backend origin/codex/bootstrap`.
Frontend делает Argon; общие файлы и зависимости меняет Анияр.

В каждом checkout своё окружение CPython 3.11:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/check_bootstrap.py
.\.venv\Scripts\python.exe -m pytest -q
```

Сигнатура для реализации ML:

```python
analyze_video(video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult
```

Оба пути абсолютные; результат/ошибки из app/contracts.py. Deadline — абсолютный
time.monotonic(), единый для всего видео; helper remaining_seconds перед/после
стадий и cap I/O timeouts. Backend supervisor запускает свой llama-server один
раз и передаёт worker loopback-клиент. Здоровый сервер переиспользуется. При
зависании supervisor прекращает собственные worker, server и декодер; до
подтверждённой остановки/health нового сервера приём закрыт. Анализ не запускает
сервер на каждый job и не подключается к чужому процессу. Полное сопоставление
старых ошибок, timeout и ответственности — **docs/ML_HANDOFF.md**.

configs/model.yaml закрепляет точный llama.cpp b11669 и GGUF Q8_0 + mmproj Q8_0,
SHA256 и параметры рабочего baseline. Это JSON-совместимый YAML; твой текущий
benchmark ещё не читает его. Нужен loader/adapter, не изменение схемы API.
Не добавляй torch/transformers. analyze_video и production orchestration
пока отсутствуют; каталог verified-правил пуст. Команда будущего API после их
готовности: `python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --workers 1`;
сначала задать ASSAR_API_KEY и применить offline-настройки, без --reload.

Для backend/Argon: **tests/fixtures/contracts/** — accepted, processing, failed,
done_no_violation, done_violation. Это только синтетические JSON/JPEG; не брать
их в runtime. Backend применяет общие проверки результата, длительности,
путей/JPEG и каталога, затем сериализует точную внешнюю схему.

Split **22/8/10** не менялся; X-4/X-47 — одна группа, **только dev**.
Анияр проверяет: `python scripts/data/freeze_split.py validate --require-dev-pair X-4 X-47`.
До локального human review похожих сцен нельзя продолжать подбор промптов,
порогов, checkpoint, teacher/LoRA. Код и синтетические тесты разрешены.
Омару только train/dev; полный архив, общие labels и frozen test остаются у Анияра.
Видео, кадры, аннотации и model outputs не передавать облачным AI/в Git.

В этом шаге GPU и скачивание весов не запускаются. Замер 3060 — отдельное следующее
задание. Handoff: base commit, изменённые файлы, точный импорт analyzer/ASGI,
команды и фактические результаты проверок, unresolved risks. Скорость/качество
на 3060, deadline watchdog и offline end-to-end пока не подтверждены.
