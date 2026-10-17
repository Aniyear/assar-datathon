# SYNTHETIC — только технический GPU-стенд

`catalog.json` содержит пять вымышленных правил для проверки прохождения категорий
через реальный ML/backend. Это не банковские наблюдения и не нормативный каталог.
`verified` здесь — тестовое значение для проверки интерфейса; источники
`https://example.invalid/gpu-smoke` ничего не подтверждают.

Потребитель только `scripts/validation/gpu_smoke.py` и его CPU-тесты.
Скрипт генерирует MOV локально из FFmpeg testsrc. Не переносить этот каталог
в production, не направлять на него `ASSAR_RULES_CATALOG_PATH` production-сервиса.
Команды и границы проверки: `docs/GPU_TECHNICAL_SMOKE.md`.
