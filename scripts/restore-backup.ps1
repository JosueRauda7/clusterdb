# Restaura un respaldo SOLO a una base temporal cluster_demo_restore_*.
# Requiere ALLOW_RESTORE=true en postgres-backup y monitor.
[CmdletBinding()]
param(
    [string]$File,
    [switch]$Latest,
    [switch]$Yes,
    [int]$TimeoutSeconds = 900
)

. "$PSScriptRoot/common.ps1"
Assert-Docker

if (-not $File) {
    $Latest = $true
}
if ($File -and $File -notmatch '^[A-Za-z0-9_.-]+\.dump$') {
    throw 'El nombre del archivo debe terminar en .dump y no puede contener rutas.'
}

$backupAllow = ''
$monitorAllow = ''
try {
    $backupAllow = (& docker exec clusterdb-postgres-backup printenv ALLOW_RESTORE 2>$null).Trim()
} catch { }
try {
    $monitorAllow = (& docker exec $script:MonitorContainer printenv ALLOW_RESTORE 2>$null).Trim()
} catch { }

if ($backupAllow -ne 'true' -or $monitorAllow -ne 'true') {
    Write-Host 'Restauracion deshabilitada por seguridad.' -ForegroundColor Yellow
    Write-Host "ALLOW_RESTORE en postgres-backup: $backupAllow"
    Write-Host "ALLOW_RESTORE en monitor:         $monitorAllow"
    Write-Host ''
    Write-Host 'Para habilitarla en esta sesion de demo:' -ForegroundColor Cyan
    Write-Host "  `$env:ALLOW_RESTORE='true'"
    Write-Host '  docker compose up -d --force-recreate postgres-backup monitor dashboard'
    Write-Host '  .\scripts\restore-backup.ps1 -Latest'
    Write-Host ''
    Write-Host 'La restauracion NUNCA sobrescribe cluster_demo: solo crea una base temporal.' -ForegroundColor DarkGray
    exit 2
}

$target = if ($File) { $File } else { 'el ultimo respaldo' }
if (-not $Yes) {
    Write-Host "Se restaurara $target en una base temporal nueva." -ForegroundColor Yellow
    Write-Host 'La base activa cluster_demo no se modifica.'
    $answer = Read-Host 'Escriba SI para continuar'
    if ($answer -notmatch '^(si|s|yes|y)$') {
        Write-Host 'Cancelado.' -ForegroundColor Yellow
        exit 0
    }
}

$payload = @{
    filename = if ($File) { $File } else { $null }
    latest   = [bool]$Latest
} | ConvertTo-Json -Compress

Write-Header 'Restaurando respaldo a base temporal'
Write-Host 'Esto puede tardar varios segundos...' -ForegroundColor DarkGray
$response = Invoke-MonitorPost '/api/actions/restore' $payload
$restore = $response.restore

Write-Host ("Base temporal:  {0}" -f $restore.database) -ForegroundColor Green
Write-Host ("Archivo:        {0}" -f $restore.filename)
Write-Host ("Duracion:       {0} s" -f $restore.duration_s)
Write-Host 'Conteos verificados:'
$restore.counts | ConvertTo-Json -Depth 5
Write-Host ''
Write-Host 'La base activa cluster_demo no fue modificada.' -ForegroundColor Green
Write-Host ("La base temporal {0} se conserva para inspeccion; las mas antiguas se limpian." -f $restore.database) -ForegroundColor DarkGray
