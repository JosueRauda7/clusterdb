# Crea .env a partir de .env.example con contrasenas aleatorias.
# No sobrescribe un .env existente salvo que se use -Force.
[CmdletBinding()]
param(
    [switch]$Force
)

. "$PSScriptRoot/common.ps1"

$envPath = Join-Path $script:RepoRoot '.env'
$examplePath = Join-Path $script:RepoRoot '.env.example'

if (-not (Test-Path $examplePath)) {
    throw "No existe $examplePath"
}

if ((Test-Path $envPath) -and -not $Force) {
    Write-Host "Ya existe .env. No se modifico. Use -Force para regenerarlo." -ForegroundColor Yellow
    exit 0
}

function New-Secret {
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 24
        $rng.GetBytes($bytes)
        return (([BitConverter]::ToString($bytes)) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $rng.Dispose()
    }
}

$content = Get-Content -Raw -Encoding UTF8 $examplePath
$secrets = [ordered]@{
    POSTGRES_PASSWORD   = New-Secret
    REPLICATOR_PASSWORD = New-Secret
    APP_PASSWORD        = New-Secret
    BACKUP_PASSWORD     = New-Secret
    MONITOR_PASSWORD    = New-Secret
}

foreach ($key in $secrets.Keys) {
    $pattern = '(?m)^' + [regex]::Escape($key) + '=.*$'
    $replacement = "$key=$($secrets[$key])"
    $content = [regex]::Replace($content, $pattern, $replacement)
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($envPath, $content, $utf8NoBom)

Write-Host "Creado .env con contrasenas aleatorias." -ForegroundColor Green
Write-Host "ALLOW_RESTORE queda en false. El archivo .env esta en .gitignore." -ForegroundColor DarkGray
