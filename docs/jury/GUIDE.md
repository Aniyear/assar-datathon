# Инструкция проверяющему

Адрес и время доступности сообщает капитан. API-ключ передаётся в отдельном поле формы.
Текущий пакет — черновик: действующий production URL ещё не подтверждён.

## Веб-интерфейс

1. Откройте переданный HTTPS-адрес. Если ngrok показывает промежуточную страницу,
   нажмите Visit Site. Это страница провайдера доступа, не результат анализа.
2. Раскройте «Доступ к сервису» и введите API-ключ. Демонстрационный режим должен
   быть **выключен**: синтетические ответы не являются анализом выбранного видео.
3. Выберите один MOV размером до 100 MiB и нажмите «Начать проверку».
   Расширение файла не гарантирует декодируемость; содержимое проверяет сервер.
4. Дождитесь результата. «Остановить ожидание» останавливает опрос в браузере,
   но не отменяет уже принятое сервером задание. Не загружайте копию немедленно.
5. В результате выберите событие в ленте или кнопками «Назад / Далее».
   Открыта одна карточка; видео остаётся рядом. Таймкод относится к исходному видео,
   JPEG можно увеличить. Для локального просмотра исходный файл нужен браузеру;
   возможность воспроизведения его кодека зависит от браузера.
6. Закончив работу, нажмите «Сбросить» рядом с ключом и закройте вкладку.
   Ключ хранится в памяти вкладки, не в localStorage/cookie.

`violation` означает найденный признак **возможного** нарушения в заявленном
покрытии. Прочитайте индивидуальное объяснение и нормативную оговорку.
`no_violation` означает отсутствие обнаруженных событий после выполненных
обязательных проверок двух поддерживаемых правил, а не проверку всех кассовых норм.
`failed` — анализ не дал допустимого результата; считать его отрицательным нельзя.

## HTTP API 0.1

Origin интерфейса и API один. Каждый из двух методов требует заголовок
`X-API-Key`. Ключ не передаётся в URL/query и не помещается в командную строку.
Swagger `/docs` и `/openapi.json` на сервере отключены; спецификация находится
в репозитории: [openapi.json](openapi.json). Архив для работы с API не нужен.

| Метод | Запрос | Успешный ответ |
|---|---|---|
| POST `/api/v1/analyze` | multipart/form-data, поле `file`, один непустой `.mov` | 202, JSON `{"job_id":"..."}` |
| GET `/api/v1/jobs/{job_id}` | Без тела | 200, processing / done / failed |

Опрос — примерно раз в секунду. Один GPU worker и одно принятое задание
одновременно; очереди нет. 503 означает занятость/неготовность. Не запускать
бесконечные автоматические повторы POST: повторный POST создаёт новое задание.
На сетевом обрыве после загрузки факт принятия может быть неизвестен клиенту.

```json
{"job_id":"synthetic-example","status":"processing"}
```
```json
{"job_id":"synthetic-example","status":"done","verdict":"no_violation","violations":[]}
```
```json
{"job_id":"synthetic-example","status":"failed"}
```

Это **синтетические примеры**, не результаты модели. Полный синтетический
`done/violation` с валидным JPEG включён в `tests/fixtures/contracts/`.
У каждого события: `type`, `timecode_sec`, `description`, `evidence_frames`.
В каждом из 1–3 кадров: `timestamp_sec` и `image_base64` — обычный base64 JPEG,
без префикса `data:`. Таймкоды — секунды от начала показываемого видео, не номера кадров.
События упорядочены по времени. Техническая причина failed во внешний JSON не входит.

| HTTP | Значение |
|---|---|
| 401 | Ключ отсутствует или неверен |
| 404 | Неизвестный job_id |
| 408 | Истёк срок загрузки |
| 413 | Превышен лимит размера |
| 415 | Неверный тип запроса или расширение файла |
| 422 | Пустой, некорректный, прерванный или недекодируемый файл |
| 503 | Сервис занят, восстанавливается или не готов |

Терминальный результат сохраняется в локальной SQLite вместе с JPEG и доступен
повторным GET после удаления временных файлов задания. Срок долговременного
хранения после завершения мероприятия отдельно не гарантирован. `job_id` стоит
сохранить: отдельного API списка всех заданий нет.

## Пример запроса из PowerShell 7

Выполнять только после получения разрешённого адреса и ключа. Команды не выводят
ключ, description или JPEG. Ответ сохраняется **локально**, не отправляйте его
облачным AI-инструментам. При проверке сети используйте синтетическое видео.

```powershell
$ErrorActionPreference = 'Stop'
$base = (Read-Host 'HTTPS origin сервиса').TrimEnd('/')
$uri = [Uri]$base
if ($uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') {
    throw 'Нужен HTTPS origin без пути, ключа и query'
}
$secret = Read-Host 'API-ключ' -AsSecureString
$headers = @{ 'X-API-Key' = [Net.NetworkCredential]::new('', $secret).Password }
if ($uri.Host -match '\.ngrok-free\.(dev|app)$') { $headers['ngrok-skip-browser-warning'] = '1' }
$video = Get-Item -LiteralPath (Read-Host 'Локальный путь MOV')
$folder = Join-Path $env:LOCALAPPDATA ('assar-jury/' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $folder | Out-Null
$clock = [Diagnostics.Stopwatch]::StartNew()
try {
    $accepted = Invoke-WebRequest -Method Post -Uri ($base + '/api/v1/analyze') `
        -Headers $headers -Form @{ file = $video } -TimeoutSec 300 -MaximumRedirection 0
    if ([int]$accepted.StatusCode -ne 202) { throw 'POST failed' }
    $id = ($accepted.Content | ConvertFrom-Json).job_id
    if ($id -notmatch '^[0-9a-f]{32}$') { throw 'Invalid job_id' }
    Set-Content -LiteralPath (Join-Path $folder 'job-id.txt') -Value $id
    do {
        $left = 300 - $clock.Elapsed.TotalSeconds
        if ($left -le 0) { throw 'Client deadline exceeded' }
        $response = Invoke-WebRequest -Uri ($base + '/api/v1/jobs/' + $id) `
            -Headers $headers -TimeoutSec ([Math]::Max(1, [int][Math]::Ceiling($left))) -MaximumRedirection 0
        $body = $response.Content | ConvertFrom-Json
        if ($body.job_id -ne $id) { throw 'Mismatched job_id' }
        if ($body.status -notin @('processing', 'done', 'failed')) { throw 'Invalid status' }
        if ($body.status -eq 'processing') { Start-Sleep -Milliseconds 500 }
    } while ($body.status -eq 'processing')
    if ($clock.Elapsed.TotalSeconds -gt 300) { throw 'Client deadline exceeded' }
    [IO.File]::WriteAllText((Join-Path $folder 'result.json'), $response.Content, [Text.UTF8Encoding]::new($false))
    Write-Output ('Результат сохранён локально: ' + $folder)
} catch {
    Write-Error 'Запрос не завершён успешно. Сохранённый job-id.txt позволяет повторить GET; не повторяйте POST вслепую.'
} finally {
    $headers.Clear()
    $secret.Dispose()
}
```
