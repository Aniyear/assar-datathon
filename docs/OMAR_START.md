# Омару: старт assar v0.1

Общая основа — commit ветки **codex/bootstrap**, base до изменений
`08682a5837ea2fe4680c7a88dbc54c993314fccd`. Анияр передаёт точный новый hash вместе
с этим handoff. Для передачи без push подготовлен Git bundle
`outputs/assar-bootstrap.bundle` (только история кода, без данных и весов).

1. Получить bundle от Анияра, клонировать и создать отдельные рабочие папки:

   ```powershell
   git clone -b codex/bootstrap C:\path\assar-bootstrap.bundle assar
   cd assar
   git rev-parse HEAD
   git worktree add ..\assar-backend -b codex/backend HEAD
   git worktree add ..\assar-ml -b codex/ml HEAD
   ```

2. В каждой рабочей папке создать собственное окружение CPython 3.11:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.lock
   .\.venv\Scripts\python.exe scripts/check_bootstrap.py
   .\.venv\Scripts\python.exe -m pytest -q
   ```

3. Backend-чат Claude получает общий контекст и промпт № 3 из `docs/PROMPTS.md`;
   пишет только `app/api/**`, `app/jobs/**`, `tests/api/**`. ML-чат — промпт № 2,
   только `app/inference/**`, `tests/inference/**`. Argon у Анияра пишет `app/web/**`.

Контракт в `app/contracts.py` и `docs/CONTRACTS.md`: `analyze_video(Path, Path,
deadline_monotonic) -> AnalysisResult`. Backend вызывает общие проверки длительности,
путей/JPEG, verified rules и внешнего результата. Источник берётся из каталога и
включается в description; внешние поля не расширять. Ошибка/таймаут → failed без
verdict. Нужен отдельный worker и watchdog; Future timeout сам inference не прерывает.
На старте только одно принятое задание; следующий запрос при занятости — 503.
Рекомендуемый импорт ASGI — `app.api.main:app`; он пока **не реализован**.

Ключ задаётся через `ASSAR_API_KEY`, остальные настройки — `.env.example`.
До импорта модели вызвать `configure_offline_environment()`, загружать локальные
файлы. Производственный startup должен отказать без готовых весов и verified
каталога. Сейчас `configs/model.yaml` не настроен, `app/rules/catalog.json` пуст.
`tests/fixtures/contracts/*.json` — только синтетические примеры для тестов/UI.

Данные: split **22/8/10** зафиксирован у Анияра. До настройки модели Анияр должен
локально завершить проверку похожих сцен; сейчас статус pending. Для ручной
проверки Омар получает только train/dev. После проверки Анияр экспортирует пакет
командой из `docs/DATA_SPLIT.md`; полный архив/labels/frozen test в ML не передавать.
Train — обучение/teacher, dev — выбор версии; test — независимая оценка у Анияра.

Зависимости, каталог, model config и общий контракт меняет Анияр. Запрос на ML-стек
передать с точными Python/CUDA/PyTorch/Transformers версиями и лицензиями. В handoff:
base commit, файлы, фактически выполненные тесты, команда запуска/импорт analyzer,
revision/hash весов, ресурсы/время и открытые риски. Наблюдения и видео остаются локально.

Общая основа проверена 57 CPU-тестами (включая 15 data/eval), проверкой `pip check`,
пятью синтетическими API-примерами и повторной валидацией приватного split.
Реальный API, inference, GPU, offline end-to-end и публичная доступность пока не проверены.
