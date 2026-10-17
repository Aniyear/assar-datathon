# Dev-оценка CASH-01 на сервер

Цель: на одних и тех же dev-видео прогнать режим фактов `cash01-facts-v1` с **2B** и **4B** (кандидаты для
ноутбука) и с **Qwen3.8-27B** (только диагностика: «понимает ли сцену большая модель»; на RTX 3060 не
помещается и в продукт не идёт). Результат на сервер не доказывает скорость и память на ноутбуке — их потом
проверяет Анияр на 3060 одной командой.

Правила сервера: работать **только на GPU 0** (`CUDA_VISIBLE_DEVICES=0`; на нём свободно ~57 ГБ), чужие
процессы vLLM не трогать, свои процессы останавливать после прогона. Видео — только dev-список от Анияра,
без frozen test; в Git и в облачные чаты не попадают.

## 0. Что нужно заранее

- Список dev-видео (ID и исходные метки) — у Анияра, тот же, что в сравнении 2B/4B (X-4 + 3 dev), можно
  добавить остальные dev. Frozen test не брать.
- Диск: ~6 ГБ (llama.cpp + 2B + 4B) и ~30 ГБ под 27B FP8.
- Python 3.11 (или 3.12) и `git`; FFmpeg — системный или статический (шаг 2).

## 1. Код и окружение

```bash
git clone https://github.com/Aniyear/assar-datathon.git ~/assar-ml
cd ~/assar-ml
git checkout codex/ml
git log --oneline -1
```

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python -m pytest -q tests/inference
```

## 2. FFmpeg (если `ffmpeg -version` не работает)

```bash
mkdir -p ~/tools && cd ~/tools
curl -LO https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-30-13-08/ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl.tar.xz
echo "3a2a94e704752287833604501a79505c2319ad1d8b2f5577c63f2b1b7a6f2b66  ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl.tar.xz" | sha256sum -c
tar xf ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl.tar.xz
export PATH="$HOME/tools/ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl/bin:$PATH"
cd ~/assar-ml
```

## 3. llama.cpp (Linux, CUDA 12.8) и веса 2B/4B — с проверкой SHA256

```bash
.venv/bin/python -m app.inference.experiments.fetch --variants 2b-q8,4b-q4km --platform linux-x64
```

## 4. Видео (с ноутбука, PowerShell; путь к серверу подставить свой)

```powershell
scp "C:\Users\Omar\Desktop\assar-datathon\data\raw\videos\<dev-файл>.mov" user@remote-host:~/assar-ml/data/dev/
```

Каталог `data/` исключён из Git. На сервере — список `outputs/experiments/videos.dev.local.json`:

```json
[
  {"id": "diag-1", "path": "/home/user/assar-ml/data/dev/X-4.mov", "role": "diagnostic", "source_label": "violation"},
  {"id": "dev-1", "path": "/home/user/assar-ml/data/dev/<файл>.mov", "role": "dev", "source_label": "violation"},
  {"id": "dev-2", "path": "/home/user/assar-ml/data/dev/<файл>.mov", "role": "dev", "source_label": "no_violation"}
]
```

## 5. Прогон 2B и 4B (по очереди, один сервер на GPU 0)

```bash
CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m app.inference.experiments.dev_eval --videos outputs/experiments/videos.dev.local.json --variants 2b-q8,4b-q4km --platform linux-x64 --gpu 0
```

Печатает только агрегаты (их и прислать в чат). Подробности — `outputs/dev_eval/<время>/<вариант>/results.json`.

## 6. Диагностика Qwen3.8-27B через vLLM (необязательно, после шага 5)

`hf` — CLI пакета `huggingface_hub` (в старых версиях — `huggingface-cli download`).

Модель: `Qwen/Qwen3.8-27B-FP8` @ `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, Apache-2.0, ~28 ГБ.
vLLM — версия, поддерживающая Qwen3.8 (проверить `vllm --version` уже установленной на сервере; если её
нет или она старая — отдельный venv по инструкции карточки модели). Памяти: **ограничить** долю, иначе vLLM
попробует занять почти всю карту и помешает соседям.

```bash
hf download Qwen/Qwen3.8-27B-FP8 --revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a --local-dir models/qwen3.8-27b-fp8
```

```bash
CUDA_VISIBLE_DEVICES=0 vllm serve models/qwen3.8-27b-fp8 --served-model-name qwen3.8-27b --host 127.0.0.1 --port 8011 --gpu-memory-utilization 0.30 --max-model-len 16384 --max-num-seqs 1 --limit-mm-per-prompt '{"image": 16}'
```

`0.30` × 143,8 ГБ ≈ 43 ГБ — должно быть меньше свободного на GPU 0 (`nvidia-smi`). В другом терминале:

```bash
CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m app.inference.experiments.dev_eval --videos outputs/experiments/videos.dev.local.json --endpoint http://127.0.0.1:8011 --label qwen3.8-27b-fp8 --extra-body '{"model": "qwen3.8-27b", "chat_template_kwargs": {"enable_thinking": false}}' --gpu 0
```

После прогона остановить свой `vllm serve` (Ctrl+C). Важно: визуальный бюджет vLLM (размер картинки для
модели) задаётся его процессором и не равен `--image-max-tokens 256` у llama.cpp — сравнение 27B с 2B/4B
только диагностическое.

## 7. Проверка человеком (локально)

```bash
.venv/bin/python -m app.inference.experiments.condition_review build outputs/dev_eval/<время>
```

Скопировать каталог прогона на ноутбук (кадры остаются на машинах команды):

```powershell
scp -r user@remote-host:~/assar-ml/outputs/dev_eval/<время> C:\Users\Omar\Desktop\assar-ml\outputs\dev_eval\
```

Открыть `condition_review.html`, ответить на эталонный вопрос по каждому видео, проверить условия и события,
нажать «Скачать condition_review_export.json», положить в каталог прогона и посчитать:

```powershell
.\.venv\Scripts\python.exe -m app.inference.experiments.condition_review score outputs\dev_eval\<время> --review outputs\dev_eval\<время>\condition_review_export.json
```

В чат — только напечатанные агрегаты.
