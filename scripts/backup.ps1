# Solicita un respaldo real y, con -Wait, espera el resultado verificado.
[CmdletBinding()]
param(
    [switch]$Wait,
    [int]$TimeoutSeconds = 180
)

. "$PSScriptRoot/common.ps1"
Assert-Docker

$before = Get-MonitorJson '/api/backups'
if (-not $before.reachable) {
    throw 'El servicio de respaldo no esta accesible.'
}

if ($before.status.running) {
    Write-Host 'Ya hay un respaldo en ejecucion; se esperara su resultado.' -ForegroundColor Yellow
    $beforeSequence = -1
}
else {
    $beforeSequence = $null
    if ($null -ne $before.status.last_attempt) {
        $beforeSequence = [int]$before.status.last_attempt.sequence
    }
    $result = Invoke-MonitorPost '/api/actions/backup'
    Write-Host ("Solicitud enviada: {0}" -f $result.message) -ForegroundColor Green
}

if (-not $Wait) {
    Write-Host 'Use -Wait para esperar y mostrar el resultado.' -ForegroundColor DarkGray
    exit 0
}

Write-Host 'Esperando a que termine el respaldo...' -ForegroundColor DarkGray
Wait-For -TimeoutSeconds $TimeoutSeconds -Description 'respaldo completado' -Condition {
    $current = Get-MonitorJson '/api/backups'
    $attempt = $current.status.last_attempt
    if ($null -eq $attempt) { return $false }
    if ($attempt.status -eq 'running') { return $false }
    if ($attempt.status -notin @('success', 'failed')) { return $false }
    if ($null -ne $beforeSequence -and [int]$attempt.sequence -le [int]$beforeSequence) { return $false }
    return $true
} | Out-Null

$after = Get-MonitorJson '/api/backups'
$attempt = $after.status.last_attempt

Write-Header 'Resultado del respaldo'
Write-Host ("Estado:        {0}" -f $attempt.status)
Write-Host ("Archivo:       {0}" -f $attempt.filename)
Write-Host ("Tamano:        {0} bytes" -f $attempt.size_bytes)
Write-Host ("Duracion:      {0} s" -f $attempt.duration_s)
Write-Host ("SHA-256:       {0}" -f $attempt.sha256)
Write-Host ("Verificacion:  {0}" -f $attempt.verification)
Write-Host ("Respaldos:     {0}" -f $after.status.count)

if ($attempt.status -ne 'success') {
    Write-Host ("Error: {0}" -f $attempt.error) -ForegroundColor Red
    exit 1
}

Write-Host 'Respaldo completado y verificado.' -ForegroundColor Green
