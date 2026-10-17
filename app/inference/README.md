# ML inference (codex/ml): baseline без обучения

Qwen3-VL-2B-Instruct (GGUF) исполняется локальным `llama-server` из llama.cpp. Сервер слушает только
`127.0.0.1` и запускается с `--offline`. FFmpeg декодирует видео; время каждого кадра — фактический PTS
из фильтра `showinfo`, rotation metadata применяется autorotate FFmpeg. Обучение не выполнялось.
Интерфейс `analyze_video` будет подключён после общего контракта (`app/contracts.py`).

## Зафиксированные версии

| Компонент | Версия / ревизия | Лицензия |
|---|---|---|
| Веса | `Qwen/Qwen3-VL-2B-Instruct-GGUF` @ `52d6c8ffea26cc873ac5ad116f8631268d7eb503` | Apache-2.0 |
| Базовая модель (карточка) | `Qwen/Qwen3-VL-2B-Instruct` @ `89644892e4d85e24eaac8bacfd4f463576704203` | Apache-2.0 (front-matter `README.md`; отдельного файла LICENSE в репозитории нет) |
| Runtime | llama.cpp `b11669` (commit `620d2225c`), сборка `win-cuda-12.4-x64` | MIT (`models/llama.cpp-b11669/LICENSE`) |
| CUDA runtime DLL | `cudart-llama-bin-win-cuda-12.4-x64.zip` из того же релиза `b11669` | NVIDIA CUDA redistributable |
| Промпт | `obs-v0.1` (`app/inference/prompts.py`) | — |

SHA256 скачиваемых файлов (их проверяет `fetch_runtime.py`):

| Файл | Размер, байт | SHA256 |
|---|---:|---|
| `Qwen3VL-2B-Instruct-Q8_0.gguf` | 1 834 427 424 | `1e8db19207c8ce0733ddd78c2eff8a9e22c27c82f4443df94c25792ed8fe04f2` |
| `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf` | 445 053 216 | `f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82` |
| `mmproj-Qwen3VL-2B-Instruct-F16.gguf` (не используется в рабочем запуске) | 819 394 848 | `c3d5afbef5287953acd57b4043d2269456e5761a4eaccb3b71b062996970aea5` |
| `llama-b11669-bin-win-cuda-12.4-x64.zip` | 266 582 698 | `c2da1f73ed176ae093ac5aceb978a7b167a16113f6d74c6789eeaee2135cb8dc` |
| `cudart-llama-bin-win-cuda-12.4-x64.zip` | 391 443 627 | `8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6` |

Распакованные файлы, которые проверялись в рабочем запуске:

| Файл | SHA256 |
|---|---|
| `llama-server.exe` | `6a4635aeb35656153d4bfeeddf761d24ed84aada9c9108d580737cbed465141b` |
| `ggml-cuda.dll` | `152681bf91cf4bca0fe5939108645a8f83cbcc61982758e6974004cb89c4c8d5` |
| `cudart64_12.dll` | `d28e42265da7462162a54da6b7a99ea4fa2caf8139d862bb500db875d0b32dfc` |
| `cublas64_12.dll` | `e40202fe4223c1cd2d2dce7beec59e1ed61c7801bd827309183be9b50e358f4c` |
| `cublasLt64_12.dll` | `2a896460bef60ed57ef32b0875812f355a6984e671d638bb632f5e8c1d7a831f` |

## CUDA: драйвер, runtime DLL и Toolkit — разные вещи

- **Драйвер NVIDIA** даёт только поддержку CUDA API (`nvidia-smi` показывает максимальную версию). Сборке
  CUDA 12.4 по таблице NVIDIA нужен драйвер ≥ 551.61 для Windows. Проверено на драйвере 565.90.
- **CUDA runtime DLL** (`cudart64_12.dll`, `cublas64_12.dll`, `cublasLt64_12.dll`) драйвер не устанавливает.
  Без них `ggml-cuda.dll` не загрузится, и llama.cpp не сможет использовать GPU. Эти DLL берутся из архива
  `cudart-llama-bin-win-cuda-12.4-x64.zip`; `fetch_runtime.py` распаковывает его в тот же каталог, что и `llama-server.exe`.
- **CUDA Toolkit** ставить не нужно. На машине рабочего запуска Toolkit не установлен, `CUDA_PATH` не задан.
  Если Toolkit другой версии стоит в `PATH`, DLL из каталога `llama-server.exe` всё равно грузятся первыми.

## 1. Подготовка (один раз, нужен интернет)

Нужны Python ≥ 3.10 (стандартная библиотека; для тестов — `pytest`) и FFmpeg/ffprobe в `PATH`.
Около 3,3 ГБ на диске для `models/` (исключён из Git).

```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv
ffmpeg -version | Select-Object -First 1
python --version
```

```powershell
python app/inference/fetch_runtime.py --dest models
```

Повторный запуск ничего не перекачивает, только проверяет SHA256. При несовпадении хеша скрипт удаляет файл
и завершается с кодом 1.

```powershell
.\models\llama.cpp-b11669\llama-server.exe --version
```

Ожидается `version: 0.7.0-dev (build 11669, commit 620d2225c)`.

## 2. Тесты (синтетическое видео, без данных датасета)

```powershell
python -m pytest -q tests/inference
```

## 3. Benchmark одного видео — параметры рабочего запуска

```powershell
$v = 'C:\path\to\video.mov'
```

```powershell
python -m app.inference.bench_baseline --video $v --repeat 2 --quant Q8_0 --mmproj Q8_0 --ctx 8192 --image-max-tokens 256 --max-tokens 384 --step 1.0 --window 10 --model-height 0
```

Остальные параметры по умолчанию: `-ngl 99`, `-np 1`, `temperature 0`, `top_k 1`, `seed 0`, JSON-схема
через `response_format`. В консоль выводятся только технические агрегаты. Ответы модели (`windows.jsonl`),
кадры и лог сервера сохраняются в `outputs/bench/<время>/` (исключён из Git) и не передаются в облачные сервисы.

Сравнение ответов двух прогонов по хешам, без вывода содержимого:

```powershell
python -c "import json,hashlib,sys; L=lambda d:[hashlib.sha256(json.loads(l)['content'].encode()).hexdigest() for l in open(d+'/windows.jsonl',encoding='utf-8')]; a,b=L(sys.argv[1]),L(sys.argv[2]); print(f'identical {sum(x==y for x,y in zip(a,b))}/{len(a)}')" outputs/bench/<run1> outputs/bench/<run2>
```

## Измерения: RTX 3050 Laptop 4 ГБ, 17.10.2026

Одно development-видео (группа X-4/X-47 закреплена за development): 72,7 с, 464×848, H.264, rotation 0.
Шаг 1 с → 73 кадра, 8 окон по 10 с, до 11 кадров в окне.

| Показатель | Холодный проход | Тёплый проход |
|---|---:|---:|
| Загрузка модели | 2,6 с | — |
| Декодирование и выборка кадров | 0,8 с | 1,0 с |
| Модель, сумма по окнам | 34,6 с | 34,8 с |
| Окно, среднее / максимум | 4,3 / 5,3 с | 4,3 / 5,5 с |
| Полный проход | 35,4 с | 35,7 с |
| Валидный JSON | 8/8 | 8/8 |
| Ответ обрезан `max_tokens` | 0 | 0 |
| Prompt / completion токенов | 19 937 / 1 202 | 19 937 / 1 202 |

Пик VRAM всего GPU — 3301 МиБ (до запуска занято 0 МиБ), после загрузки — 3271 МиБ. Второй независимый запуск
сервера с теми же параметрами: 34,8 / 34,4 с, пик 3301 МиБ.

**Воспроизводимость.** Совпадение числа токенов не доказывает детерминированность. Поэтому ответы сравнивались
по SHA256 содержимого: внутри сеанса сервера холодный и тёплый проходы совпали в 8/8 окнах, а два независимых
сеанса сервера — в 16/16 ответах; PTS кадров тоже совпали. Это проверено только на одной машине, одном GPU,
одной сборке и одном видео. На RTX 3060, другом драйвере или другой сборке llama.cpp результат может
отличаться; это нужно проверить тем же сравнением хешей.

**Без успеха на RTX 3050 4 ГБ:** mmproj F16 без `--image-max-tokens` (~420 токенов на кадр, `--ctx` 16384 и 8192)
— после нескольких окон `llama-server` падает с `bad allocation`; при `--max-tokens 160` ответы обрезаются.

## Ограничения

- На целевой RTX 3060 Laptop 6 ГБ не измерено.
- Самое длинное видео не измерялось.
- Качество наблюдений и локализации не оценивалось.
- `--image-max-tokens 256` снижает детализацию мелких объектов. llama.cpp предупреждает, что для grounding
  Qwen-VL нужно ≥ 1024 токенов на изображение.
- Выборка 1 кадр/с может пропустить короткие действия.
- Категории промпта предварительные, до таксономии v1.
