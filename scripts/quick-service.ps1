param(
    [ValidateSet('start', 'stop', 'status', 'check')]
    [string]$Action = 'start'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$repo = Split-Path $PSScriptRoot -Parent
$config = Join-Path $repo 'outputs/service/production/model.local.json'
$ngrok = Join-Path $repo 'outputs/tools/ngrok/ngrok.exe'
$statePath = Join-Path $repo 'outputs/service/production/state.json'

# A second click must not start a second GPU server or stop the running one.
if ($Action -eq 'start' -and (Test-Path -LiteralPath $statePath)) {
    try {
        $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 - [double]$state.heartbeat
        if ($state.mode -eq 'production' -and $state.phase -in @('starting', 'running') -and $age -ge 0 -and $age -lt 5) {
            Write-Host 'assar уже запущен. Повторный процесс не создаётся.'
            if ($state.public_url) { Write-Host ('Адрес: ' + $state.public_url) }
            elseif ($state.phase -eq 'starting') { Write-Host 'Модель ещё загружается. Проверить состояние: STATUS.cmd' }
            else { Write-Host ('Локальный адрес: ' + $state.url); Write-Host 'Если нужен ngrok: STOP.cmd, затем START.cmd.' }
            exit 0
        }
    } catch {
        # Existing service owns its lock. Never remove it or terminate an unknown PID.
        Write-Host 'Состояние запуска не удалось прочитать; штатный launcher проверит блокировку.'
    }
}
if ($Action -in @('start', 'check') -and -not (Test-Path -LiteralPath $config)) {
    Write-Host 'Нет локальной конфигурации модели: outputs/service/production/model.local.json'
    Write-Host 'Нужна настройка путей весов и runtime по docs/ACCEPTANCE_6CA5_3060.md. Ничего не скачано и не запущено.'
    exit 2
}
$serviceArgs = @($Action, '--mode', 'production')
if ($Action -in @('start', 'check')) { $serviceArgs += @('--model-config', $config) }
if ($Action -eq 'start') {
    if (-not (Test-Path -LiteralPath $ngrok)) {
        Write-Host 'Нет закреплённого ngrok: outputs/tools/ngrok/ngrok.exe. См. docs/NGROK_TEST.md.'
        exit 2
    }
    Write-Host 'Запуск assar с публичным HTTPS через ngrok. Проверки окружения и готовности обязательны.'
    Write-Host 'Оставьте это окно открытым. Штатная остановка: STOP.cmd или Ctrl+C.'
    $serviceArgs += @('--port', '8000', '--ngrok', $ngrok, '--publish')
}
& (Join-Path $PSScriptRoot 'assar.ps1') @serviceArgs
exit $LASTEXITCODE
