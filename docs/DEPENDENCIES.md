# Окружение стартовой основы

Проверено в отдельном `.venv`: CPython 3.11.9, Windows x64. `requirements.in`
содержит прямые зависимости, `requirements.lock` — все разрешённые транзитивные
версии, включая pytest. Установка: `python -m pip install -r requirements.lock`.
`pip check` проверяет совместимость метаданных; CPU-тесты проверяют поведение.
Linux/сервер и GPU ещё не проверены. Lock версионный, без хешей дистрибутивов.

| Прямая зависимость | Версия | Роль | Лицензия пакета |
|---|---|---|---|
| Pydantic | 2.12.4 | Общие модели и валидация | MIT |
| Pillow | 12.2.0 | Локальное декодирование JPEG, синтетические fixtures | MIT-CMU |
| FastAPI | 0.143.3 | Зависимость для будущего backend Омара | MIT |
| Uvicorn | 0.54.0 | Будущий ASGI runner | BSD-3-Clause |
| python-multipart | 0.0.32 | Будущая загрузка MOV | Apache-2.0 |
| pytest | 9.1.1 | CPU-тесты | MIT |

Лицензии указаны по metadata установленных пакетов; полный пакет лицензий и
транзитивных компонентов нужно сохранить при упаковке релиза. Подготовлены только
общие зависимости; ML loader, CUDA, модель и её лицензия выбираются после handoff.
У новой FastAPI транзитивная зависимость `opentelemetry-api`: SDK/exporter не установлен;
общие настройки дополнительно отключают OTEL. Содержательные логи и telemetry не включать.

На машине Анияра доступен FFmpeg `N-122697-gfcfb33922f-20260210`. Это системная
зависимость вне pip lock; конкретный бинарник/лицензию поставки должен подтвердить
deployment-владелец. Стартовые CPU-тесты не декодируют банковские видео.

Для установки после отключения сети заранее сохранить wheels для целевой ОС/Python:

```powershell
python -m pip download --only-binary=:all: -r requirements.lock -d .wheels
python -m pip install --no-index --find-links .wheels -r requirements.lock
```

Эта команда не скачивает модель. Все processor/tokenizer/weights и лицензии должны
быть подготовлены отдельно. `configure_offline_environment()` не заменяет тест
с отключённой сетью. Источники реализации: [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/),
[Pillow Image](https://pillow.readthedocs.io/en/stable/reference/Image.html).
