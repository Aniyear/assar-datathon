# Один итоговый сценарий приёмки MVP

**Подготовлен, не выполнен.** Начинать после входов из [MVP_LAUNCH.md](MVP_LAUNCH.md),
принятого выбора модели/правил и освобождения RTX 3060. Сначала локальная часть;
внешняя часть отложена до отдельного решения о публикации. Не повторять весь CPU-suite.
Синтетический demo UI и тестовый каталог в этой приёмке не используются.

## Подготовка человеком

1. Омар локально выбирает **один положительный и один отрицательный реальный dev MOV**
   по подтверждённым правилам. Проверить принадлежность dev по неизменённому split;
   X-4/X-47 — одна dev-группа, не два независимых примера. Frozen-test не использовать.
   Прежние бинарные метки не заменяют проверку именно выбранного нормативного покрытия.
2. Положительный пример должен позволять установить все предпосылки правила и
   индивидуальное событие; отрицательный — отсутствие нарушения при достаточной
   наблюдаемости. Если такой пары нет, зафиксировать пробел, не заменять её синтетикой.
   Все заметки/пути/наблюдения остаются в `analysis/local/` или `outputs/`.
3. Зафиксировать release commit, хеши config/catalog/весов, параметры, Python/FFmpeg,
   собственные PID API/worker/llama-server, снимок VRAM и время старта. Настройки после
   этого не менять. Старт модели измеряется отдельно от времени задания.
4. Выполнить проверки ключа/UI из инструкции запуска. Убедиться в loopback модели,
   отсутствии внешнего inference, пустой очереди и выключенном demo UI.

## Локальный проход через HTTP

PowerShell **7**, тот же скрыто введённый `$env:ASSAR_API_KEY`, `$repo`, `$py`
и `$base` из инструкции запуска. Скрипт ниже сохраняет ответы только локально,
не печатает description/base64/ключ; терминал с transcript/debug не использовать.
Не показывать файлы результатов облачным инструментам.

```powershell
$ErrorActionPreference = 'Stop'
$auth = @{ 'X-API-Key' = $env:ASSAR_API_KEY }
$receiptDir = Join-Path $repo ('outputs/mvp-acceptance/' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $receiptDir -ErrorAction Stop | Out-Null
$positive = Read-Host 'Local positive dev MOV path'
$negative = Read-Host 'Local negative dev MOV path'

function Invoke-AcceptanceJob([string]$video, [string]$expected, [string]$label) {
    $file = Get-Item -LiteralPath $video
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $accepted = Invoke-WebRequest -Method Post -Uri ($base + '/api/v1/analyze') `
            -Headers $auth -Form @{ file = $file } -TimeoutSec 300 -MaximumRedirection 0
        if ([int]$accepted.StatusCode -ne 202) { throw 'POST must return 202' }
        $jobId = ($accepted.Content | ConvertFrom-Json).job_id
        if ($jobId -notmatch '^[0-9a-f]{32}$') { throw 'Invalid job id' }
        do {
            $remaining = 300 - $watch.Elapsed.TotalSeconds
            if ($remaining -le 0) { throw 'Total processing exceeded 300 seconds' }
            $response = Invoke-WebRequest -Uri ($base + '/api/v1/jobs/' + $jobId) `
                -Headers $auth -TimeoutSec ([Math]::Max(1, [int][Math]::Ceiling($remaining))) -MaximumRedirection 0
            $body = $response.Content | ConvertFrom-Json
            if ($body.status -notin @('processing', 'done', 'failed')) { throw 'Invalid state' }
            if ($body.status -eq 'processing') { Start-Sleep -Milliseconds 250 }
        } while ($body.status -eq 'processing')
        $watch.Stop()
        $path = Join-Path $receiptDir ($label + '.json')
        [IO.File]::WriteAllText($path, $response.Content, [Text.UTF8Encoding]::new($false))
        if ($watch.Elapsed.TotalSeconds -gt 300) { throw 'Total processing exceeded 300 seconds' }
        if ($body.status -ne 'done' -or $body.verdict -ne $expected) { throw 'Required real done/verdict not obtained' }
        if ($body.job_id -ne $jobId) { throw 'Mismatched job id' }
        $again = Invoke-WebRequest -Uri ($base + '/api/v1/jobs/' + $jobId) -Headers $auth -MaximumRedirection 0
        if ($again.Content -cne $response.Content) { throw 'Repeated GET changed the result' }
        [pscustomobject]@{ label=$label; job_id=$jobId; verdict=$body.verdict;
            total_sec=[Math]::Round($watch.Elapsed.TotalSeconds,3); json_path=$path }
    } catch {
        # Do not print the HTTP exception/response, key or private observation text.
        throw ('Acceptance failed at ' + $label + '; inspect local artifacts privately')
    }
}

$pos = Invoke-AcceptanceJob $positive 'violation' 'positive'
$neg = Invoke-AcceptanceJob $negative 'no_violation' 'negative'
$repeat = Invoke-AcceptanceJob $positive 'violation' 'positive-repeat'
if ($repeat.job_id -eq $pos.job_id) { throw 'Repeated POST must create a new job' }
@($pos,$neg,$repeat) | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $receiptDir 'timings.json') -Encoding utf8
Write-Output 'Three HTTP done/verdict/time/repeated-GET checks passed; content review still required'
```

Время отсчитывается до POST, включает отправку файла, серверный probe/очередь/
inference и получение терминального JSON. Это более строгий клиентский замер,
а не сумма внутренних timings. При 503 не начинать скрытые повторы до удачи:
зафиксировать busy/неготовность и причину. Не считать failed отрицательным ответом.
Если первый проход не прошёл, сначала причина и владелец исправления, затем
обоснованное изменение; не подбирать параметры/ролик до получения PASS.

## JPEG, таймкоды и смысл результата

Выполнить для положительного ответа, затем для отрицательного и повторного,
задавая соответствующие `$env:ASSAR_CHECK_VIDEO`/`$env:ASSAR_CHECK_RESULT`.
Экспорт JPEG предназначен только для **локального просмотра человеком**.

```powershell
$env:ASSAR_CHECK_VIDEO = $positive
$env:ASSAR_CHECK_RESULT = $pos.json_path
@'
import base64, json, os, subprocess
from pathlib import Path
from app.contracts import DoneJob, validate_done_for_video
try:
    video = os.environ['ASSAR_CHECK_VIDEO']
    result_path = Path(os.environ['ASSAR_CHECK_RESULT'])
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'json', video], capture_output=True, check=True, timeout=30)
    duration = float(json.loads(probe.stdout)['format']['duration'])
    done = validate_done_for_video(DoneJob.model_validate_json(result_path.read_bytes()), duration)
    frames = [f for event in done.violations for f in event.evidence_frames]
    target = result_path.with_suffix('')
    target.mkdir(exist_ok=False)
    for i, frame in enumerate(frames):
        (target / f'evidence-{i:03d}.jpg').write_bytes(base64.b64decode(frame.image_base64, validate=True))
except Exception as exc:
    print('Contract/JPEG validation FAIL (' + type(exc).__name__ + ')')
    raise SystemExit(1)
print('Contract/JPEG/time bounds PASS; events=' + str(len(done.violations)) + '; JPEG=' + str(len(frames)))
'@ | & $py -
if ($LASTEXITCODE -ne 0) { throw 'Contract/JPEG check failed' }
```

Далее человек сверяет локально:

- положительный: `done/violation`, хотя бы одно событие, 1–3 настоящих JPEG на
  каждое; кадры и таймкоды относятся к исходному MOV, а не к другой job;
- каждый `description` содержит индивидуальное наблюдение, основание применимости,
  акт/пункт/проверенную ссылку; выполнены условия/исключения листа правил;
- отрицательный: `done/no_violation`, пустые violations, пригодный обзор;
  отсутствие события модели не заменяет эталонную проверку человеком;
- повторный POST: отдельная job и повторно корректный результат, без смешивания
  кадров. Побайтовое равенство разных inference не требуется; различия оценить
  локально. Сравнить собственные PID worker/server до/после успешных jobs:
  совпадение подтверждает reuse. Изменившиеся PID не объявлять reuse.

## Очистка, сохранение и UI

После терминальных ответов дождаться очистки `uploads/<job_id>` и
`outputs/jobs/<job_id>` для всех трёх job (без ручного удаления). Повторный GET
должен возвращать тот же JSON и JPEG уже при отсутствии временных файлов.
Если файлы остались или результат недоступен — зафиксировать отдельный FAIL.

Штатно остановить сервис по MVP_LAUNCH.md, сохранить SQLite после остановки,
снова запустить **тот же release** с теми же outputs/ключом. Для сохранённых job_id
повторить GET и сравнить с исходными JSON; JPEG должны снова декодироваться.
Новые job для этой проверки создавать не нужно. После перезапуска в новом терминале
восстановить `$pos/$neg/$repeat` из локального `timings.json`, а ключ — скрытым вводом.

Локально открыть UI, выключить demo. Пройти один настоящий положительный upload
через UI, проверить processing → done, события/JPEG и совпадение с API этой job.
Это дополнительная UI-проверка в рамках сценария, её время записать отдельно.
Сброс UI/закрытие вкладки убирает ключ; после нового открытия API без ввода ключа
не должен быть доступен. Не считать заранее встроенную картинку доказательством.

## Внешняя часть — после отдельного разрешения публикации

На управляемом командой клиенте в другой сети использовать HTTPS origin в `$base`,
не localhost. Сначала повторить матрицу ключа/UI из MVP_LAUNCH.md, затем тот же
проход positive/negative/repeated POST и проверку JPEG. Передавать dev-видео только
между разрешёнными устройствами команды и её сервисом; никаких облачных AI/browser
инструментов. Не считать проверку домашнего Wi-Fi на том же ноутбуке другой сетью.

Критерии: доверенный TLS без отключения проверки, доступность статики/API вместе,
401 без/с неверным ключом, настоящий done/JPEG с правильным ключом, каждый полный
запрос ≤300 s. Проверить, что тестовый несекретный ключ не попал в proxy/API logs,
до передачи настоящего ключа жюри. Записать тип сети и время; секреты не записывать.

## Квитанция и решение

В `outputs/mvp-acceptance/<run>/` сохранить metadata/timings, исходные JSON,
локально декодированные JPEG, результаты GET после очистки/перезапуска, отметки
ручной проверки и сетевого этапа. Наблюдения/кадры не включать в Git/чат.
Общая квитанция содержит только commit/config/catalog hashes, число проверок,
вердикт проверки, секунды и незакрытые пункты. Пропущенный этап = **not run**,
не PASS. Снимки VRAM не называть пиковым измерением.

Сценарий PASS только если все выполненные обязательные локальные и внешние пункты
прошли, а смысл результатов подтверждён человеком. Пока внешняя часть отложена,
можно сообщить «локальная часть пройдена», но не «готово к передаче жюри».
Даже полный PASS пары dev-примеров подтверждает работоспособность конкретной
сборки на них, **не качество распознавания на всём датасете**.
