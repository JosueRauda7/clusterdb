# Detiene el stack. Por defecto conserva los volumenes con datos.
[CmdletBinding()]
param(
    [switch]$Volumes,
    [switch]$Yes
)

. "$PSScriptRoot/common.ps1"
Assert-Docker

if ($Volumes -and -not $Yes) {
    Write-Host 'ADVERTENCIA: -Volumes elimina PostgreSQL, etcd, respaldos y metricas.' -ForegroundColor Red
    $answer = Read-Host 'Escriba SI para confirmar'
    if ($answer -notmatch '^(si|s|yes|y)$') {
        Write-Host 'Cancelado. No se elimino nada.' -ForegroundColor Yellow
        exit 0
    }
}

$composeArgs = @('down', '--remove-orphans')
if ($Volumes) {
    $composeArgs += '-v'
}

Write-Header 'Deteniendo ClusterDB Monitor'
Invoke-Compose -ComposeArgs $composeArgs

if ($Volumes) {
    Write-Host 'Volumenes eliminados. El proximo arranque hara bootstrap desde cero.' -ForegroundColor Yellow
}
else {
    Write-Host 'Datos conservados en los volumenes de Docker.' -ForegroundColor Green
}
