# Portable test tool only. No system service, automatic update or PATH change.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$folder = Join-Path $repo 'outputs/tools/ngrok'
$binary = Join-Path $folder 'ngrok.exe'
$expected = 'd339bcbd0713233337e860163f5249eea679cf26750a5700510dbc241d201748'
New-Item -ItemType Directory -Force -Path $folder | Out-Null
if (-not (Test-Path -LiteralPath $binary)) {
    $archive = Join-Path $folder 'ngrok.zip'
    Invoke-WebRequest 'https://bin.ngrok.com/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip' -OutFile $archive
    Expand-Archive -LiteralPath $archive -DestinationPath $folder
}
if ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
    throw 'Different ngrok build. Do not execute; review the publisher version/signature and update the pin explicitly.'
}
Write-Host 'Pinned ngrok 3.39.11 verified. Official ngrok config add-authtoken setup is imported locally at launch.'
