# Optional TEST-ONLY network tool. Does not install a system service or change PATH.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$version = '2026.10.1'
$sha256 = '4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6'
$folder = Join-Path $repo 'outputs/tools'
$binary = Join-Path $folder "cloudflared-$version.exe"
New-Item -ItemType Directory -Path $folder -Force | Out-Null
if (-not (Test-Path -LiteralPath $binary)) {
    Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/download/$version/cloudflared-windows-amd64.exe" -OutFile $binary
}
if ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sha256) {
    throw 'cloudflared SHA256 mismatch. File preserved for investigation; NOT executed.'
}
Write-Host "Verified cloudflared $version. Test only: $binary"
