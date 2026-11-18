# Levanta el stack completo, espera healthchecks y muestra las URLs locales.
[CmdletBinding()]
param(
    [switch]$NoBuild,
    [switch]$Offline,
    [int]$TimeoutSeconds = 360
)

. "$PSScriptRoot/common.ps1"
Assert-Docker

if (-not (Test-Path (Join-Path $script:RepoRoot '.env'))) {
    Write-Host 'No existe .env; ejecutando init-env.ps1...' -ForegroundColor Yellow
    & "$PSScriptRoot/init-env.ps1"
}

$composeArgs = @('up', '-d')
if (-not $NoBuild) {
    $composeArgs += '--build'
}
if ($Offline) {
    $composeArgs += @('--pull', 'never')
}

Write-Header 'Levantando ClusterDB Monitor'
Invoke-Compose -ComposeArgs $composeArgs

$services = @(
    'etcd1', 'etcd2', 'etcd3',
    'postgres-master', 'postgres-replica',
    'haproxy', 'postgres-backup', 'monitor', 'txgen', 'dashboard'
)

foreach ($service in $services) {
    Write-Host ("Esperando {0}..." -f $service) -ForegroundColor DarkGray
    Wait-ServiceHealthy -Service $service -TimeoutSeconds $TimeoutSeconds
}

Write-Header 'Estado de los contenedores'
& docker compose ps

Write-Host ''
Write-Host 'Comprobando puertos publicados en 127.0.0.1...' -ForegroundColor DarkGray
$ports = @(
    @{ Port = 15432; Name = 'PostgreSQL via HAProxy' },
    @{ Port = 7000; Name = 'HAProxy stats' },
    @{ Port = 8501; Name = 'Dashboard' }
)
foreach ($item in $ports) {
    if (Test-TcpPort -Port $item.Port) {
        Write-Host ("  OK   {0}: {1}" -f $item.Port, $item.Name) -ForegroundColor Green
    }
    else {
        Write-Host ("  FALLA {0}: {1}" -f $item.Port, $item.Name) -ForegroundColor Red
    }
}

Write-Host ''
Write-Host 'URLs locales:' -ForegroundColor Cyan
Write-Host '  Dashboard:        http://127.0.0.1:8501'
Write-Host '  HAProxy stats:    http://127.0.0.1:7000'
Write-Host '  PostgreSQL:       postgresql://app@127.0.0.1:15432/cluster_demo'
Write-Host ''
Write-Host 'Siguiente paso: .\scripts\status.ps1' -ForegroundColor DarkGray
