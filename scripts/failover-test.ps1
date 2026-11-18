# Failover REAL: detiene el primario, espera la promocion de Patroni,
# reincorpora el nodo caido como replica y verifica que no haya split-brain.
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoRejoin,
    [int]$TimeoutSeconds = 120
)

. "$PSScriptRoot/common.ps1"
Assert-Docker

$status = Get-MonitorJson '/api/status'
if ($status.service.available -ne $true) {
    throw 'El servicio no esta disponible; no se puede iniciar la prueba de failover.'
}

$oldPrimary = [string]$status.service.primary_name
$oldNode = $status.nodes | Where-Object { $_.name -eq $oldPrimary } | Select-Object -First 1
$newNode = $status.nodes | Where-Object { $_.name -ne $oldPrimary -and $_.role -eq 'replica' } | Select-Object -First 1
if (-not $oldNode -or -not $newNode) {
    throw 'No se encontro un primario y una replica activos mediante el monitor.'
}
$newExpected = [string]$newNode.name

Write-Header 'Prueba de failover real'
Write-Host ("Primario actual:   {0} ({1})" -f $oldPrimary, $oldNode.label)
Write-Host ("Replica candidata: {0} ({1})" -f $newExpected, $newNode.label)
Write-Host ("Timeout:           {0} s" -f $TimeoutSeconds)
Write-Host ''
Write-Host 'Se ejecutara: docker compose stop ' -NoNewline
Write-Host $oldPrimary -ForegroundColor Yellow
Write-Host 'Patroni debe promover la replica. Luego se reinicia el nodo caido.' -ForegroundColor DarkGray

if (-not $Yes) {
    $answer = Read-Host 'Escriba SI para provocar el failover real'
    if ($answer -notmatch '^(si|s|yes|y)$') {
        Write-Host 'Cancelado. No se detuvo ningun nodo.' -ForegroundColor Yellow
        exit 0
    }
}

$kpiBefore = Get-MonitorJson '/api/kpis?window_minutes=0'
Write-Host ''
Write-Host 'Deteniendo el primario...' -ForegroundColor Yellow
$sw = [System.Diagnostics.Stopwatch]::StartNew()
& docker compose stop $oldPrimary
if ($LASTEXITCODE -ne 0) {
    throw "No se pudo detener $oldPrimary."
}

$promoted = $false
$promotedAt = $null
try {
    Wait-For -TimeoutSeconds $TimeoutSeconds -Description 'promocion del nuevo primario' -Condition {
        try {
            $current = Get-MonitorJson '/api/status'
            return ($current.service.available -eq $true -and $current.service.primary_name -eq $newExpected)
        }
        catch {
            return $false
        }
    } | Out-Null
    $promotedAt = $sw.Elapsed.TotalSeconds
    $promoted = $true
}
catch {
    $promoted = $false
}

$elapsed = $sw.Elapsed.TotalSeconds

if (-not $promoted) {
    Write-Host 'No se detecto la promocion dentro del timeout.' -ForegroundColor Red
    Write-Host "Reiniciando $oldPrimary para restaurar el servicio..." -ForegroundColor Yellow
    & docker compose start $oldPrimary | Out-Null
    throw 'La prueba de failover no pudo confirmar la promocion. Revise los logs de Patroni y etcd.'
}

Write-Host ("Promocion detectada por el script en {0:N2} s." -f $promotedAt) -ForegroundColor Green

# El monitor mide con sus propias muestras; se espera a que cierre el incidente.
Wait-For -TimeoutSeconds 40 -Description 'KPI de failover actualizado' -Condition {
    $k = Get-MonitorJson '/api/kpis?window_minutes=0'
    return ($null -ne $k.last_failover -and $k.last_failover.new_primary -eq $newExpected)
} | Out-Null

$kpiAfter = Get-MonitorJson '/api/kpis?window_minutes=0'
$lastFailover = $kpiAfter.last_failover
Write-Host ''
Write-Host 'Resultado medido por el monitor:' -ForegroundColor Cyan
if ($null -ne $lastFailover) {
    Write-Host ("  Failover:        {0} -> {1}" -f $lastFailover.old_primary, $lastFailover.new_primary)
    Write-Host ("  Recuperacion:    {0}" -f (Format-Seconds $lastFailover.recovery_s))
    Write-Host ("  RTO objetivo:    {0:N0} s (cumple={1})" -f $kpiAfter.rto.target_seconds, $kpiAfter.rto.met)
}
if ($kpiAfter.rpo.verified) {
    Write-Host ("  RPO observado:   {0} registro(s) perdido(s)" -f $kpiAfter.rpo.lost_records)
}
else {
    Write-Host ("  RPO observado:   {0}" -f $kpiAfter.rpo.reason)
}
Write-Host ("  Fallos totales:  {0}" -f $kpiAfter.failures.detected)

if ($NoRejoin) {
    Write-Host ''
    Write-Host ("-NoRejoin: {0} queda detenido. Para recuperarlo: docker compose start {0}" -f $oldPrimary) -ForegroundColor Yellow
    exit 0
}

Write-Host ''
Write-Host "Reincorporando $oldPrimary como replica..." -ForegroundColor Yellow
$swRejoin = [System.Diagnostics.Stopwatch]::StartNew()
& docker compose start $oldPrimary | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "No se pudo iniciar $oldPrimary."
}

try {
    Wait-For -TimeoutSeconds 120 -Description "$oldPrimary reincorporado como replica" -Condition {
        $node = Get-PatroniNode $oldPrimary
        return ($null -ne $node -and $node.role -eq 'replica' -and $node.state -eq 'running')
    } | Out-Null

    Wait-For -TimeoutSeconds 60 -Description 'streaming del nodo reincorporado' -Condition {
        $primaryNode = Get-PatroniNode $newExpected
        if ($null -eq $primaryNode -or $null -eq $primaryNode.replication) { return $false }
        $standby = @($primaryNode.replication | Where-Object {
            $_.application_name -eq $oldPrimary -and $_.state -eq 'streaming'
        })
        return ($standby.Count -gt 0)
    } | Out-Null
}
catch {
    Write-Host ("El nodo {0} no se reincorporo correctamente: {1}" -f $oldPrimary, $_.Exception.Message) -ForegroundColor Red
    throw
}

$rejoinSeconds = $swRejoin.Elapsed.TotalSeconds
Write-Host ("Reincorporacion confirmada en {0:N2} s." -f $rejoinSeconds) -ForegroundColor Green

Write-Host 'Verificando topologia y replicacion activa...' -ForegroundColor DarkGray
$verification = $null
for ($attempt = 1; $attempt -le 4; $attempt++) {
    $verification = Invoke-MonitorPost '/api/actions/verify-replication'
    if ($verification.ok) { break }
    Start-Sleep -Seconds 3
}

if ($null -eq $verification -or -not $verification.ok) {
    Write-Host 'La verificacion de replicacion encontro hallazgos:' -ForegroundColor Red
    foreach ($check in $verification.checks) {
        Write-Host ("  [{0}] {1}: {2}" -f $(if ($check.ok) { 'OK' } else { 'FALLA' }), $check.name, $check.detail)
    }
    exit 1
}

Write-Host 'Verificacion de replicacion: OK' -ForegroundColor Green
foreach ($check in $verification.checks) {
    Write-Host ("  [OK] {0}: {1}" -f $check.name, $check.detail)
}

$primaryNode = Get-PatroniNode $newExpected
$oldNodeAfter = Get-PatroniNode $oldPrimary
$primaries = @(@($primaryNode, $oldNodeAfter) | Where-Object {
    $null -ne $_ -and $_.role -eq 'primary'
})
if ($primaries.Count -gt 1) {
    throw 'SPLIT-BRAIN: dos nodos se reportan como primarios.'
}
if ($primaries.Count -ne 1 -or $null -eq $oldNodeAfter -or $oldNodeAfter.role -ne 'replica') {
    throw 'La topologia final no es la esperada: un primario y una replica.'
}
Write-Host 'Topologia final: un solo primario; no hay split-brain.' -ForegroundColor Green

Write-Host ''
Write-Host 'Listado final de Patroni:' -ForegroundColor Cyan
& docker exec (Get-ContainerName $newExpected) patronictl -c /etc/patroni/patroni.yml list

Write-Host ''
Write-Host 'Failover completado. El dashboard ya debe mostrar RTO, RPO y el incidente.' -ForegroundColor Green
