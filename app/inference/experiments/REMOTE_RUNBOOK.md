# сервер: рабочая папка ML с нуля

Цель ночи: (1) узнать, какие нарушения реально есть в train/dev; (2) построить проверку под них;
(3) измерить на dev 2B/4B (идут на ноутбук RTX 3060) и Qwen3.8-27B (диагностика или сервер на сервер — решает
команда). Результат на сервер не доказывает скорость/память на ноутбуке — их потом проверяет Анияр на 3060.

Правила сервера: **только GPU 0** (`CUDA_VISIBLE_DEVICES=0`), чужие процессы vLLM не трогать, свои
останавливать после работы. Видео — только train/dev (без frozen test); в Git и облачные чаты не попадают.
В чат — только напечатанные агрегаты.

## 1. Папка, код, окружение (на сервер)

```bash
mkdir -p ~/assar && cd ~/assar
git clone https://github.com/Aniyear/assar-datathon.git code
cd ~/assar/code
git checkout codex/ml
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python -m pytest -q tests/inference
nvidia-smi
which vllm || docker ps
```

## 2. FFmpeg (если `ffmpeg -version` не работает)

```bash
mkdir -p ~/assar/tools && cd ~/assar/tools
curl -LO https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-30-13-08/ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl.tar.xz
echo "3a2a94e704752287833604501a79505c2319ad1d8b2f5577c63f2b1b7a6f2b66  ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl.tar.xz" | sha256sum -c
tar xf ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl.tar.xz
echo 'export PATH="$HOME/assar/tools/ffmpeg-N-127021-ge0c94b2d1c-linux64-gpl/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
cd ~/assar/code
```

## 3. Модели

llama.cpp b11669 (Linux, CUDA 12.8) + 2B Q8_0 + 4B Q4_K_M, всё с проверкой SHA256:

```bash
.venv/bin/python -m app.inference.experiments.fetch --variants 2b-q8,4b-q4km --platform linux-x64
```

Qwen3.8-27B FP8 (`Qwen/Qwen3.8-27B-FP8` @ `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, Apache-2.0, ~28 ГБ).
`hf` — CLI пакета `huggingface_hub` (в старых версиях `huggingface-cli download`):

```bash
hf download Qwen/Qwen3.8-27B-FP8 --revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a --local-dir models/qwen3.8-27b-fp8
```

Сервер 27B в отдельном терминале (`tmux new -s vllm`). vLLM — версия с поддержкой Qwen3.8 (уже установленная
на сервере или отдельный venv по карточке модели). **Ограничить память**: `0.30` × 143,8 ГБ ≈ 43 ГБ должно
быть меньше свободного на GPU 0:

```bash
CUDA_VISIBLE_DEVICES=0 vllm serve models/qwen3.8-27b-fp8 --served-model-name qwen3.8-27b --host 127.0.0.1 --port 8011 --gpu-memory-utilization 0.30 --max-model-len 16384 --max-num-seqs 1 --limit-mm-per-prompt '{"image": 24}'
```

Проверка (в другом терминале):

```bash
curl -s http://127.0.0.1:8011/v1/models
```

## 4. Видео (с ноутбука, PowerShell)

Списки собираются из CSV split'а Анияра с проверкой SHA256: одна запись на группу (X-47 — копия X-4),
метки и таймкоды только для выбранных файлов и только для оценки/разметки. Скрипт загрузки кладёт на сервер
видео и список. Если на сервер `echo $HOME` не `/home/user` — заменить в `--remote-dir`.

Dev (8 групп):

```powershell
cd C:\Users\Omar\Desktop\assar-ml
.\.venv\Scripts\python.exe -m app.inference.experiments.make_video_list --split-csv outputs\experiments\dev_files_for_omar.csv --videos-dir C:\Users\Omar\Desktop\assar-datathon\data\raw\videos --labels-csv C:\Users\Omar\Desktop\assar-datathon\data\labels_normalized.csv --diagnostic X-4 --splits dev --out outputs\experiments\videos.dev.local.json --remote-dir /home/user/assar/code/data/dev
powershell -ExecutionPolicy Bypass -File outputs\experiments\videos.dev.local.scp.ps1
```

Train (22 группы) — когда Анияр пришлёт `train_files_for_omar.csv` в том же формате:

```powershell
.\.venv\Scripts\python.exe -m app.inference.experiments.make_video_list --split-csv outputs\experiments\train_files_for_omar.csv --videos-dir C:\Users\Omar\Desktop\assar-datathon\data\raw\videos --labels-csv C:\Users\Omar\Desktop\assar-datathon\data\labels_normalized.csv --diagnostic none --splits train --out outputs\experiments\videos.train.local.json --remote-dir /home/user/assar/code/data/train
powershell -ExecutionPolicy Bypass -File outputs\experiments\videos.train.local.scp.ps1
```

## 5. Разведка типов нарушений (27B; положительные train/dev)

```bash
CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m app.inference.experiments.discover run --videos outputs/experiments/videos.dev.local.json --endpoint http://127.0.0.1:8011 --extra-body '{"model": "qwen3.8-27b", "chat_template_kwargs": {"enable_thinking": false}}'
```

Для train — `--videos outputs/experiments/videos.train.local.json`. Скопировать каталог на ноутбук, открыть
`discover.html`, выбрать тип у каждого видео, «Скачать discover_export.json», затем посчитать:

```powershell
scp -r user@remote-host:~/assar/code/outputs/discover/<время> C:\Users\Omar\Desktop\assar-ml\outputs\discover\
.\.venv\Scripts\python.exe -m app.inference.experiments.discover count outputs\discover\<время> --review outputs\discover\<время>\discover_export.json
```

В чат — только напечатанные счётчики типов.

## 6. Dev-оценка анализатора

2B и 4B (по очереди, собственный llama-server на GPU 0):

```bash
CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m app.inference.experiments.dev_eval --videos outputs/experiments/videos.dev.local.json --variants 2b-q8,4b-q4km --platform linux-x64 --gpu 0
```

27B (через уже запущенный vLLM):

```bash
CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m app.inference.experiments.dev_eval --videos outputs/experiments/videos.dev.local.json --endpoint http://127.0.0.1:8011 --label qwen3.8-27b-fp8 --extra-body '{"model": "qwen3.8-27b", "chat_template_kwargs": {"enable_thinking": false}}' --gpu 0
```

Визуальный бюджет vLLM задаётся его процессором и не равен `--image-max-tokens 256` у llama.cpp: сравнение
27B с 2B/4B — диагностическое.

## 7. Проверка человеком

```bash
.venv/bin/python -m app.inference.experiments.condition_review build outputs/dev_eval/<время>
```

```powershell
scp -r user@remote-host:~/assar/code/outputs/dev_eval/<время> C:\Users\Omar\Desktop\assar-ml\outputs\dev_eval\
.\.venv\Scripts\python.exe -m app.inference.experiments.condition_review score outputs\dev_eval\<время> --review outputs\dev_eval\<время>\condition_review_export.json
```

## 8. Уборка

Остановить свой `vllm serve` (Ctrl+C в tmux). Чужие процессы не трогать.
