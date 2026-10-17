# Проверка дополнения стартовой основы — 17.10.2026

Ветка: codex/bootstrap; предыдущая основа b4c2d19fb995490abd3a6a79ddcd33e260377b76.
ML-источник: c32964a318d47a0ce7648a697044b48c2e40c40a, base 08682a5.
Чтение только git show, без переключения checkout, merge или выполнения ML-кода.

Фактически изменены общие contracts/settings, конфигурация модели с provenance,
проверка dev-пары, CPU-тесты и инструкции. Внешний JSON и AnalysisResult v0.1
сохранены. Пять fixtures остались синтетическими и неизменными. Backend, ML и UI
не добавлены. requirements.in и requirements.lock неизменны; torch/transformers
не добавлены. Bundle предыдущей основы не обновлялся, получать ветку через GitHub.

CPython 3.11.9 / Windows, изолированное .venv; команды из корня:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/check_bootstrap.py
.\.venv\Scripts\python.exe scripts/data/freeze_split.py validate --require-dev-pair X-4 X-47
git diff --check
```

Результаты: **76 passed**, pip: **No broken requirements found**, **5/5** fixtures,
split: **development_pair_verified=true**, **group_overlap=0**, diff без ошибок
пробелов. Пара X-4/X-47 — одна SHA256-группа только dev. Полное распределение:

| Часть | Уникальных | Файлов | Положительных / отрицательных |
|---|---:|---:|---:|
| train | 22 | 22 | 12 / 10 |
| dev | 8 | 9 | 4 / 4 |
| frozen test | 10 | 11 | 5 / 5 |

Полный приватный manifest и публичная квитанция не изменены байт-в-байт.
SHA256 manifest: `57fd3795cdd714f15a775112cc30a8ee783ca14badd4df6cb1cca1656bdcd49a`.
Проверялись технические манифесты/хеши; содержательные аннотации и сцены не читались.

Дополнительно локальным read-only скриптом сопоставлены значения configs/model.yaml
с текстом git show точного ML-коммита: repos/revisions, SHA256 архивов/двух GGUF,
размеры, reported server hash, параметры README и generation defaults в коде.
Числовые 10 и 10.0 сравнивались как числа. Проверка прошла; это проверка provenance,
не загрузка/проверка байтов весов. Модель/llama-server/FFmpeg не запускались.

Остаётся реализовать analyze_video и backend supervisor с остановкой собственных
процессов, loader общей конфигурации, verified-каталог и UI. Human near-duplicate
review pending: последующая настройка модели и экспорт пакета заблокированы.
Offline end-to-end, watchdog под реальным зависанием, перенос/качество/скорость на
RTX 3060 и публичный API не проверены. GPU и скачивание весов — отдельное задание.
