# ML-handoff и история базового контракта

Актуальное состояние 18.10.2026: принят точный handoff
`6ca5e5bd21b9883d13a822bf58d06bc231220583` поверх сохранённой продуктовой интеграции.
Выбран `option_4b`: Q4_K_M + Q8_0 mmproj, v6, max_tokens=500. Омар подтвердил
MULTI-01 и CASH-03; [оговорки и пределы](ACCEPTED_RULES_V6.md) обязательны.
Интегратор добавил безопасную обработку незавершённого обязательного анализа,
проверку трёх различных кадров и регрессии, без изменения API или дизайна.

Фактические проверки, локальная квитанция, блокеры и запуск —
[ACCEPTANCE_6CA5_3060.md](ACCEPTANCE_6CA5_3060.md). Readiness определяется этим
прогоном, а не историческим benchmark. Старые квитанции split сохранены;
подтверждение пользователя о дубликатах не является новой независимой проверкой.

Ниже — **история согласования c32964a**. Утверждения «адаптер отсутствует»,
«файлы не проверялись» и прежнее ожидание ручного review относятся к моменту
bootstrap, а не к нынешнему состоянию. Текущие контрактные требования остаются
в CONTRACTS.md, результаты проверок — INTEGRATION_CPU.md / GPU_TECHNICAL_SMOKE.md.

## Согласование с техническим baseline Омара

Прочитан через `git show` точный commit **c32964a318d47a0ce7648a697044b48c2e40c40a**
ветки `origin/codex/ml`, основанный на `08682a5`: README, vlm.py, video.py,
fetch_runtime.py, bench_baseline.py, prompts.py и tests/inference/test_video.py.
Ветка не переключалась и не сливалась. Код Омара не скопирован и не изменён.
Видеоматериалы, кадры, локальные аннотации и ответы модели не читались.

## Что принято в общий контракт

```python
analyze_video(video_path: Path, job_dir: Path, deadline_monotonic: float) -> AnalysisResult
```

Синхронная функция будущего ML-адаптера; `VideoAnalyzer` в `app/contracts.py` — её
Protocol, не заглушка, выдающая ответ. Оба пути абсолютные. Backend передаёт
изолированную папку job и случайно названную загрузку, без labels/исходного ID.
ML сохраняет реальные JPEG в job_dir, возвращает типизированный AnalysisResult.
Типы и внешняя схема 0.1 сохранены; `analyze_video` в ML-коммите ещё отсутствует.
Пустой набор observations из baseline сам по себе не означает no_violation:
нужны завершённый пригодный анализ, утверждённые правила и проверка результата.

`remaining_seconds(deadline_monotonic)` — общий CPU-helper: deadline абсолютный
по `time.monotonic()` этого компьютера, не Unix timestamp и не длительность.
При исчерпании бюджета — AnalysisTimeoutError. Бюджет нельзя обнулять на каждом
окне/повторе. Проверка до и после probe, decode, каждого model request, валидации
и записи evidence; каждый I/O timeout ограничить оставшимся временем.

Backend фиксирует `request_deadline = accepted_monotonic + request_timeout_sec`
до чтения upload. При старте worker передаёт
`min(request_deadline - response_reserve_sec, monotonic() + analysis_budget_sec)`.
Стартовые лимиты: 300 / 240 / 5 секунд. Если оставшийся бюджет ≤ 0, job сразу
failed. После дедлайна успешный ответ не принимать. Сериализация/отправка входят
в общий бюджет; socket timeout не является wall-clock watchdog.

## Процессы: один владелец, один сервер, последовательные задания

В production **backend supervisor** владеет отдельными процессами Python-worker,
llama-server и декодера. ML предоставляет bounded lifecycle/client примитивы,
backend orchestration вызывает их при старте/остановке. Это целевой интерфейс
интеграции, ещё не реализованный в baseline; сейчас LlamaServer запускается самим
benchmark и останавливается в finally.

Supervisor запускает llama-server один раз до открытия приёма заданий; фиксирует
его handle/PID и принадлежность сразу после spawn, до ожидания health. Worker
получает внутренний loopback endpoint при инициализации, а не от пользователя.
Здоровый сервер переиспользуется между заданиями; analyze_video не скачивает
веса, не создаёт сервер на каждый вызов и не останавливает его после успеха.
Одновременно один принятый job и `-np 1`; во время запуска/восстановления — 503.

При deadline, зависании, transport failure с неизвестным состоянием generation
supervisor отмечает job failed, закрывает admission и прекращает **свой worker
и свой llama-server**, а также оставшиеся дочерние декодеры. Закрыть HTTP-запрос
или убить только Python-worker недостаточно: llama-server может продолжать GPU.
Graceful terminate ограничен бюджетом остановки; затем kill и bounded wait.
До подтверждения завершения старых процессов и health нового сервера задания
не принимать. Не освобождать слот только по исключению Future/HTTP timeout.
Защита от orphan при смерти supervisor обязательна: на Windows — выделенный
Job Object с kill-on-close либо эквивалентный проверенный механизм владельца.
Не завершать процессы по имени, чужому порту или PID без проверки принадлежности.
Произвольно запущенный пользователем сервер автоматически не подхватывать.

В baseline `start(timeout=180)` использует health timeout=2 и sleep=0.25,
`stop()` — два ожидания до 10 секунд, chat — urllib timeout. Это локальные
ограничения, не доказанная гарантия общего deadline. Омар адаптирует их под
остаток бюджета; supervisor остаётся обязательным даже после этой адаптации.
Вызовы loopback должны обходить системные HTTP(S)-proxy (`ProxyHandler({})`
или эквивалент); редиректы наружу запрещены. Общие offline-флаги не блокируют
сеть сами. Только `127.0.0.1`, никакого внешнего inference.

## Ошибки и перенос результатов (владелец реализации — Омар)

| Baseline / ситуация | Общая ошибка |
|---|---|
| video.DecodeError, чтение/декодирование | app.contracts.DecodeError |
| Просроченный общий deadline / timeout операции | AnalysisTimeoutError |
| vlm.ModelError: запуск, health, connection, выход сервера | ModelUnavailableError |
| malformed/truncated JSON, finish_reason=length, неверный frame index, invalid rule/type | InvalidAnalysisResultError |
| Всё видео визуально непригодно | UnassessableVideoError |

Общие ошибки наследуют AnalysisError. Старые классы не идентичны новым: заменить
импорт или явно преобразовать с chaining. Сбой не превращать в no_violation.
Частичный валидный JSON из оборванной генерации не принимать. HTTP `error`,
внутренние пути, сырой model text и traceback наружу не передавать.
Связь frame_indices → реальные Frame.pts_sec/evidence_path, объединение событий,
rule_id и формирование AnalysisResult остаются задачей ML; юридический источник
и официальный serializer — backend. Проверка времён не должна скрывать ошибку PTS
clamp-ом в 0/duration; нештатные start_time/VFR требуют отдельного теста Омара.

## Зафиксированная конфигурация

`configs/model.yaml` — JSON-совместимый YAML (читается stdlib `json`, PyYAML не нужен).
Содержит ревизии и SHA256 из fetch_runtime.py и README точного коммита;
распакованный server SHA помечен reported: здесь байты не проверялись.
GGUF Q8_0 + **mmproj Q8_0**, llama.cpp **b11669 / 620d2225c**,
win-cuda-12.4-x64; ctx=8192, image_max_tokens=256, max_tokens=384, ngl=99,
parallel=1, step=1, window=10, model_height=0, temperature=0, top_k=1, seed=0,
cache_prompt=false, obs-v0.1. Это рабочие параметры из README/кода, не утверждение
о качестве или пригодности для RTX 3060. Defaults benchmark отличаются:
mmproj=F16, ctx=16384, image_max_tokens=0, max_tokens=160 — их не наследовать молча.
Baseline получает `ServerConfig`/CLI args, общий config пока не читает: loader и
перенос этих значений должен добавить Омар; формат метаданных изменён с пустого v0.1.

Зависимости Python не изменены; torch/transformers не добавлены. Runtime — бинарники
llama.cpp/CUDA DLL + FFmpeg, лицензии и веса вне Git. Состояние config:
`pending_integration_and_3060_validation`. Ни скачивание, ни запуск GPU здесь не
выполнялись. Пустой verified-каталог и отсутствие analyze_video блокируют production.

README Омара сообщает технический benchmark на development-группе X-4/X-47;
это не наша повторная проверка и не измерение качества. В готовом split обе копии
уже в dev, назначение не менялось. Human review похожих сцен всё ещё pending;
новую настройку промптов/порогов/teacher/LoRA не начинать до review.
