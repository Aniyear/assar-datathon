# Interactive only. Token never enters shell history, argv, Git or console output.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$python = @((Join-Path $repo '.venv/Scripts/python.exe'), (Join-Path $repo '../../.venv/Scripts/python.exe')) |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) { throw 'Project Python environment missing.' }
$configPath = Join-Path $repo 'outputs/service/network-test/private/ngrok.json'
Push-Location $repo
try {
    & $python -X utf8 -c "from scripts.service import ROOT, private_directory; private_directory(ROOT / 'outputs/service/network-test/private')"
    if ($LASTEXITCODE -ne 0) { throw 'Could not protect the local credential directory.' }
    Write-Host 'Copy ONLY the authtoken from ngrok Dashboard > Your Authtoken.'
    Write-Host 'Paste below. Input is hidden. Do not paste the whole ngrok command.'
    $secureToken = Read-Host 'Authtoken' -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr).Trim()
        if ($plainToken -notmatch '^[A-Za-z0-9_-]{20,200}$') { throw 'Invalid token format; nothing saved.' }
        $config = [ordered]@{version='3'; agent=[ordered]@{
            authtoken=$plainToken; console_ui=$false; web_addr=$false;
            remote_management=$false; update_check=$false; log='false'
        }}
        [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        $plainToken = $null
        $config = $null
        $secureToken.Dispose()
    }
    Write-Host 'Saved locally. Tell the chat: token saved. Do not send the token itself.'
} finally { Pop-Location }
