# MVP: локальный запуск и передача жюри

Подготовлено 17.10.2026 на `codex/integration-cpu` от
`b1715e38cecd70b2fea7395594c59c30df0bc25e`. Это инструкция будущего запуска,
**не квитанция готовности или публикации**. По последнему указанию пользователя
деплой сейчас отложен; домен/внешний адрес не предоставлены и не настраивались.

## Что уже известно и чего ждём

- Принят CPU-результат b1715e3: 213 passed, integration --runxfail 16 passed,
  без skip/xfail. Этот suite в проходе подготовки документации не повторяется.
- Технический GPU-стенд b1715e3 завершён с настоящими llama-server/service/
  supervisor/HTTP API. На синтетике проверены reuse, finish_reason=stop,
  deadline/recovery и остановка принадлежащих стенду процессов. Обычные задания
  закончились `failed/UnassessableVideoError`, поэтому **реальные done и JPEG
  этим стендом не подтверждены**. Локальная квитанция:
  `outputs/gpu-smoke/20261017-195607-8febcf/receipt.json`.
- Качество 2B на X-4 признано командой неудовлетворительным. Результата текущего
  сравнения 2B/4B ещё нет; GPU занят другим чатом. Нового GPU-запуска здесь нет.
- [Правовая проверка](MVP_RULES_REVIEW.md): три основания подтверждены публично,
  но применимость и проверка предпосылок анализатором не подтверждены.
  Production-каталог пуст; readiness/validation остаются прежними.

## Условия перед запуском сборки для сдачи

Омар передаёт выбранную модель, точный commit, GGUF/mmproj/runtime SHA256,
параметры, лицензии, локальные измерения и ограничения. Он же подтверждает
применимость правил по листу проверки. Владелец ML подтверждает проверку
нормативного контекста, а не только совпадение категории. Интегратор фиксирует
принятый release commit/config/catalog после этих входов и итоговой проверки.
**Не выставлять все flags=true и не подставлять тестовый каталог для старта.**

Сейчас `production_spec` обязан отказать: `status` не ready и нет verified-правил.
Поэтому приведённый запуск сервиса выполняется только после отдельного решения
о готовности и освобождения GPU. Синтетический gpu_smoke не является режимом сдачи.

## Окружение и пути на машине Анияра

PowerShell, CPython 3.11.9, существующий общий venv. Из исходной папки не выполнять
git switch/reset/clean; её untracked `app/web` принадлежит другому агенту.

```powershell
$ErrorActionPreference = 'Stop'
$repo = 'C:\Users\aniya\Рабочий стол\assar-datathon\.worktrees\integration-cpu'
$py = 'C:\Users\aniya\Рабочий стол\assar-datathon\.venv\Scripts\python.exe'
Set-Location -LiteralPath $repo
git branch --show-current
git rev-parse HEAD
git status --porcelain
& $py --version
& $py -m pip check
ffmpeg -version | Select-Object -First 1
ffprobe -version | Select-Object -First 1
```

Сверить HEAD с согласованным release commit и отсутствие tracked/untracked
изменений. На новом компьютере установить `requirements.lock` в отдельный venv
Python 3.11; torch/transformers не нужны. Зависимости и веса загружаются заранее.
GPU свободен только после подтверждения владельца текущего прогона; чужие PID
не останавливать. Чтение `nvidia-smi` и списка PID не заменяет это согласование.

**Фактический runtime технического стенда** находится в
`C:\Users\aniya\Рабочий стол\assar-datathon\.worktrees\ml-portability-3060\models`.
Это llama.cpp b11669 win-cuda12.4 и Qwen3-VL-2B-Instruct Q8_0 + mmproj Q8_0;
SHA256 закреплены в `configs/model.yaml`. Это не выбор финальной модели.

Важное отличие production: относительные пути внутри config разрешаются от
**integration checkout**, а не от каталога config, cwd runtime или `--runtime-root`.
У uvicorn такого параметра нет. После выбора финальной модели интегратор должен
разместить закреплённый комплект в `integration-cpu/models/...` либо зафиксировать
действительные абсолютные пути в согласованной локальной конфигурации. Не копировать
старый config вслепую, не скачивать новый комплект и не создавать ссылки сейчас.

Проверка закреплённых файлов без запуска GPU после разрешения путей:

```powershell
@'
import json
from pathlib import Path
from app.inference.service import check_runtime
try:
    root = Path.cwd()
    cfg = json.loads((root / 'configs/model.yaml').read_text(encoding='utf-8'))
    check_runtime(cfg, root)
except Exception as exc:
    print('Runtime files: FAIL (' + type(exc).__name__ + ')')
    raise SystemExit(1)
print('Runtime files: SHA256/size PASS; model not started')
'@ | & $py -
if ($LASTEXITCODE -ne 0) { throw 'Runtime preflight failed' }
```

## Ключ и локальный запуск

Создать ключ не короче 32 случайных байт в менеджере паролей; сохранить его для
передачи только жюри. Вставлять через скрытый ввод, не в команду/URL/файл Git.
Не включать PowerShell transcript, отладочный HTTP trace или дамп окружения.
`.env` автоматически не читается. В каждом терминале, где нужны API-запросы,
ввести **тот же** ключ, а не создавать новый.

```powershell
$secureKey = Read-Host 'API key (hidden)' -AsSecureString
$keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:ASSAR_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr)
    $secureKey.Dispose()
}
if ([string]::IsNullOrWhiteSpace($env:ASSAR_API_KEY)) { throw 'Empty API key' }
$env:ASSAR_UPLOADS_DIR = Join-Path $repo 'uploads'
$env:ASSAR_OUTPUTS_DIR = Join-Path $repo 'outputs'
$env:ASSAR_MODEL_CONFIG_PATH = Join-Path $repo 'configs/model.yaml'
$env:ASSAR_RULES_CATALOG_PATH = Join-Path $repo 'app/rules/catalog.json'
$env:ASSAR_REQUEST_TIMEOUT_SEC = '300'
$env:ASSAR_ANALYSIS_BUDGET_SEC = '240'
$env:ASSAR_RESPONSE_RESERVE_SEC = '5'
$env:ASSAR_MODEL_CALL_TIMEOUT_SEC = '120'
$env:ASSAR_MODEL_HOST = '127.0.0.1'
$env:ASSAR_MODEL_PORT = '0'
$env:ASSAR_GPU_WORKERS = '1'
$env:ASSAR_MAX_INFLIGHT_JOBS = '1'

# Только после принятого release и освобождения GPU:
& $py -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

120 s — значение baseline для одного обращения, дополнительно ограниченное
остатком deadline; оно не обещает скорость финальной модели. Стендовый override
17 s не переносится в сдачу автоматически. Любое принятое изменение параметров
записывается в release-квитанции перед финальным сценарием.
Не запускать llama-server вручную: supervisor сам владеет им и worker, переиспользует
между заданиями и завершает при shutdown/recovery. Не применять `--reload` или
несколько ASGI workers. Если startup отказал, сохранить только код/тип причины
локально и устранить входной блокер; не обходить readiness.

## Проверить ключ и UI без загрузки видео

Во втором PowerShell 7 задать `$repo`, `$py`, скрыто ввести тот же ключ.
Один API origin для UI и запросов; сначала loopback, позднее утверждённый HTTPS URL.
Встроенный demo-переключатель UI **выключен**; запросы должны идти в настоящий API.

```powershell
$base = 'http://127.0.0.1:8000'
$auth = @{ 'X-API-Key' = $env:ASSAR_API_KEY }
$probe = '/api/v1/jobs/00000000000000000000000000000000'
$checks = @(
    @{ Path = '/'; Headers = @{}; Code = 200 },
    @{ Path = $probe; Headers = @{}; Code = 401 },
    @{ Path = $probe; Headers = @{ 'X-API-Key' = 'invalid-test-only' }; Code = 401 },
    @{ Path = $probe; Headers = $auth; Code = 404 },
    @{ Path = '/api/internal/status'; Headers = @{}; Code = 401 },
    @{ Path = '/api/internal/status'; Headers = $auth; Code = 200 }
)
foreach ($check in $checks) {
    $r = Invoke-WebRequest -Uri ($base + $check.Path) -Headers $check.Headers -SkipHttpErrorCheck -MaximumRedirection 0
    if ([int]$r.StatusCode -ne $check.Code) { throw 'Unexpected authentication/status code' }
}
$status = Invoke-RestMethod -Uri ($base + '/api/internal/status') -Headers $auth -MaximumRedirection 0
if ($status.accepting -ne $true) { throw 'Service is not accepting jobs' }
Write-Output 'UI/auth/status: PASS'
```

GET `/` открыт намеренно, ключ защищает API. Внешний запрос без ключа должен
получить 401, не 200 от другой статики/прокси. Не использовать `curl -v`,
отключение TLS-проверки, ключ в query или общий онлайн API-тестер.
После этого выполнить **один** [итоговый сценарий](MVP_ACCEPTANCE.md).

## Внешний доступ — отложенный шаг перед сдачей

По [разбору предоставленного кейса](CASE_ANALYSIS.md#что-требует-кейс) жюри нужны
URL интерфейса, URL API и ключ. Требования покупать домен или отдельный сервер
в этом разборе нет. Пока развёртывание не выполняется; неизвестный внешний доступ
блокирует только внешнюю приёмку/передачу, а не текущую подготовку правил.

Один подготовленный вариант: HTTPS завершается **на машине команды**, затем
прокси передаёт запрос на `127.0.0.1:8000`. GPU inference остаётся локальным.
Если этот вариант будет выбран, человек должен:

1. Предоставить управляемое DNS-имя и входящий публичный адрес. При CGNAT получить
   у провайдера доступный адрес либо согласовать другой маршрут; смена имени
   сама по себе CGNAT не устраняет. Покупка услуг сейчас не выполняется.
2. В кабинете DNS направить A на этот адрес (AAAA только при работающем IPv6).
   На роутере перенаправить TCP 80/443 на ноутбук и разрешить эти входы в firewall.
   Порт 8000 и динамический порт модели не публиковать.
3. Установить Caddy из [официального источника](https://caddyserver.com/docs/install),
   записать версию/SHA256 бинарника. Caddy в PATH не найден; бинарник и конфигурация
   здесь не проверялись.
   В отдельном терминале **без API-ключа** сохранить следующий пример как
   `outputs/deploy/Caddyfile` (заменить имя) и выполнить команды ниже после
   отдельного разрешения публикации. Аккаунт Caddy не требуется; нужен доступ
   человека к DNS/роутеру или провайдеру.

```caddyfile
{
    admin off
    log default {
        format filter {
            wrap json
            fields {
                request delete
            }
        }
    }
}
assar.example.kz {
    header Cache-Control "no-store"
    reverse_proxy 127.0.0.1:8000
}
```

Это пример, `assar.example.kz` не является выданным команде адресом.
Access log не включён; объект request удалён из служебных логов, чтобы исключить
X-API-Key при ошибке прокси. Перед реальными данными проверить это на отдельном
несекретном тестовом ключе. Не включать debug/trace и не выводить окружение.

```powershell
caddy version
caddy validate --config outputs/deploy/Caddyfile --adapter caddyfile
# Только после разрешения публикации, в своём foreground-терминале:
caddy run --config outputs/deploy/Caddyfile --adapter caddyfile
```

Механизм сертификатов и требования DNS/портов проверены по
[Automatic HTTPS](https://caddyserver.com/docs/automatic-https);
маршрут — по [reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy);
запуск — по [CLI](https://caddyserver.com/docs/command-line);
настройки журналов — по [global log](https://caddyserver.com/docs/caddyfile/options#log)
и [filter/delete](https://caddyserver.com/docs/caddyfile/directives/log#filter).
Это подготовка по документации, **не выполненная проверка Caddy, TLS или сети**.
Сертификат требует внешнего доступа к CA; это не внешний inference.
Облачный туннель с завершением TLS у провайдера в этот вариант не входит:
его условия передачи банковских данных не согласованы.

После настройки с устройства команды **в другой сети**, например через мобильную
точку доступа, повторить проверки UI/ключа и сценарий из MVP_ACCEPTANCE.md на HTTPS.
Не пересылать dev-видео в сторонние web-инструменты; клиент и хранение результатов
остаются под контролем команды. Ошибка TLS/недоступность — блокер, не повод отключать
проверку сертификата. Ссылку не считать доступной по одному открытию на ноутбуке.

## Передача и остановка

После успешной приёмки капитан передаёт по установленному организаторами каналу:
HTTPS URL UI, API base URL с двумя маршрутами, ключ отдельно в закрытом канале,
release commit, краткую инструкцию, зафиксированные модель/параметры, список
подтверждённых правил и ограничения. Канал сдачи капитан уточняет у организаторов;
агент ничего сам не отправляет. Ключ не помещать в README, issue, презентацию,
скриншот, URL или публичное сообщение. UI хранит ключ только в памяти вкладки.

Для безопасной остановки прекратить новые загрузки; при опубликованном сервисе
сначала Ctrl+C **своему** foreground Caddy. Через локальный API дождаться терминальных
статусов принятых job и `accepting=true` после восстановления. Затем Ctrl+C своему
uvicorn; lifespan закрывает supervisor и принадлежащие ему процессы. Если нужно
остановить во время задания, оно может стать failed; это не успешное завершение.
Не убивать все python/llama-server, не удалять lock у работающего экземпляра.

Проверить исчезновение только записанных собственных PID и сделать снимок
`nvidia-smi` до/после (не называть его пиком VRAM). Очистить ключ из терминала
`Remove-Item Env:ASSAR_API_KEY`, закрыть вкладку UI. После сдачи отменить именно
созданные для неё правила входа/проброса портов.

`outputs/jobs.sqlite3` содержит результаты и base64 JPEG после очистки временных
файлов задания. Сохранять весь SQLite-комплект вместе с WAL/SHM при работающем
сервисе через SQLite backup, либо копировать **после штатной остановки**; проще
последний вариант. Не удалять uploads/outputs ради проверки. Не считать файл
одной SQLite базы, скопированный во время записи без WAL, полной резервной копией.
Повторный запуск использует те же абсолютные outputs и тот же согласованный
release; незавершённые задания предыдущего процесса получают failed.

## Оставшиеся входы по владельцам

| Владелец | Блокер / требуемый результат |
|---|---|
| ML / чат сравнения, Омар | Результат 2B/4B, приемлемость наблюдений и точный комплект для финальной сборки; нового решения здесь нет |
| Омар + владелец ML | Применимость короткого набора правил и реальная проверка предпосылок; category/type_id недостаточно |
| Интегратор после handoff | Принятый release/config/catalog, действительные пути и SHA256; readiness только по доказательствам |
| Команда, после освобождения GPU | Реальные положительный/отрицательный dev, done/JPEG, время, повтор и сохранение — по единому сценарию |
| Анияр / владелец сети, затем капитан | Выбор и проверка внешнего доступа, HTTPS/ключ, канал передачи жюри; сейчас отложено |

Неверная container duration может отсечь хвост; input `-t` не делает произвольный
повреждённый MOV поддержанным. Редкая выборка может пропустить действие. Два dev-ролика
не дают оценки качества на всём наборе. Эти ограничения сохраняются и после PASS
итогового сценария; техническая работоспособность не равна качеству распознавания.

## Проверки этого прохода подготовки

Выполнены только проверки документации: 7 блоков PowerShell разобраны штатным
парсером без ошибок, 2 вложенных блока Python прошли `ast.parse`, 26 локальных
Markdown-ссылок ведут на существующие файлы; пять упомянутых category ID совпадают
с `app/inference/prompts.py`. `git diff --check` — без ошибок. Production-код,
config/catalog/readiness, зависимости, тесты, scripts и split не изменены.
Полный CPU-suite не повторялся, GPU/сервис/прокси не запускались. Эти проверки
синтаксиса не являются выполнением инструкции запуска или итоговой приёмки.
