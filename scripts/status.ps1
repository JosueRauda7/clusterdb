# Muestra el estado real del cluster, los KPIs acumulados y Patroni.
[CmdletBinding()]
param()

. "$PSScriptRoot/common.ps1"
Assert-Docker

Write-Header 'Contenedores'
& docker compose ps

try {
    $status = Get-MonitorJson '/api/status'
    $kpis = Get-MonitorJson '/api/kpis?window_minutes=0'
}
catch {
    Write-Host 'No se pudo consultar la API del monitor.' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor DarkGray
    exit 1
}

Write-Header 'Servicio y nodos'
$service = $status.service
Write-Host ("Servicio HAProxy:     {0}" -f $(if ($service.available -eq $true) { 'DISPONIBLE' } elseif ($service.available -eq $false) { 'NO DISPONIBLE' } else { 'SIN DATOS' }))
Write-Host ("Primario actual:      {0}" -f $service.primary_name)
Write-Host ("Latencia:             {0} ms" -f $service.latency_ms)
Write-Host ("Ultima muestra:       {0} (hace {1:N1} s)" -f $service.last_sample_iso, $service.sample_age_seconds)
Write-Host ''
foreach ($node in $status.nodes) {
    Write-Host ("  {0,-18} rol={1,-8} estado={2,-8} lag={3}" -f $node.name, $node.role, $node.state, $node.lag_bytes)
}
Write-Host ''
$replication = $status.replication
Write-Host ("Replicacion:          {0}" -f $replication.state)
Write-Host ("Lag:                  {0} bytes" -f $replication.lag_bytes)
Write-Host ("Sync standby:         {0}" -f $replication.sync_state)

Write-Header 'KPIs acumulados (datos reales)'
$availability = $kpis.availability
if ($null -ne $availability.pct) {
    Write-Host ("Disponibilidad:       {0:N2}%  ({1:N0} s observados, {2:N1} s de caida)" -f $availability.pct, $availability.observed_seconds, $availability.downtime_seconds)
}
else {
    Write-Host ("Disponibilidad:       {0}" -f $availability.reason)
}

$sla = $kpis.sla
if ($null -ne $sla.met) {
    Write-Host ("SLA {0:N1}%:            {1}  (margen {2:+0.000;-0.000} pp)" -f $sla.target_pct, $(if ($sla.met) { 'CUMPLE' } else { 'NO CUMPLE' }), $sla.gap_pp)
}
else {
    Write-Host ("SLA:                   {0}" -f $sla.reason)
}

Write-Host ("MTTR:                  {0}" -f (Format-Seconds $kpis.mttr.seconds))
Write-Host ("MTBF:                  {0}" -f (Format-Seconds $kpis.mtbf.seconds))
Write-Host ("Fallos detectados:     {0} (resueltos {1}, abiertos {2})" -f $kpis.failures.detected, $kpis.failures.resolved, $kpis.failures.open)

$rto = $kpis.rto
if ($null -ne $rto.actual_seconds) {
    Write-Host ("RTO observado:         {0}  (objetivo {1:N0} s, cumple={2})" -f (Format-Seconds $rto.actual_seconds), $rto.target_seconds, $rto.met)
}
else {
    Write-Host ("RTO observado:         {0}" -f $rto.reason)
}

$rpo = $kpis.rpo
if ($rpo.verified) {
    Write-Host ("RPO observado:         {0} registro(s) perdido(s)  (objetivo {1:N0} s)" -f $rpo.lost_records, $rpo.target_seconds)
}
else {
    Write-Host ("RPO observado:         {0}" -f $rpo.reason)
}

$last = $kpis.last_failover
if ($null -ne $last) {
    Write-Host ("Ultimo failover:       {0} -> {1} a las {2}" -f $last.old_primary, $last.new_primary, $last.detected_iso)
}
else {
    Write-Host 'Ultimo failover:       Sin failovers registrados.'
}

Write-Host ("Transacciones:         ventana OK={0} fallidas={1}; acumulado OK={2} fallidas={3}" -f $kpis.transactions.window_ok, $kpis.transactions.window_failed, $kpis.transactions.total_ok, $kpis.transactions.total_failed)

Write-Header 'Respaldos'
$backup = $status.backup
if ($backup.reachable) {
    $lastBackup = $backup.last_backup
    if ($null -ne $lastBackup) {
        Write-Host ("Ultimo respaldo:       {0} ({1})" -f $lastBackup.filename, $lastBackup.status)
        Write-Host ("Tamano:                {0} bytes" -f $lastBackup.size_bytes)
        Write-Host ("Verificacion:          {0}" -f $lastBackup.verification)
        Write-Host ("SHA-256:               {0}" -f $lastBackup.sha256)
    }
    else {
        Write-Host 'Ultimo respaldo:       Sin respaldos completados.'
    }
    Write-Host ("Proximo respaldo:      {0}" -f $backup.next_run_at)
    Write-Host ("Restauracion:          {0}" -f $(if ($backup.allow_restore) { 'HABILITADA (solo base temporal)' } else { 'deshabilitada' }))
}
else {
    Write-Host ("Servicio de respaldo:  NO DISPONIBLE - {0}" -f $backup.error) -ForegroundColor Red
}

Write-Header 'Patroni'
$primary = $service.primary_name
if ($primary) {
    $container = Get-ContainerName $primary
    & docker exec $container patronictl -c /etc/patroni/patroni.yml list
}
else {
    Write-Host 'Sin primario identificado; no se puede consultar patronictl.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Dashboard: http://127.0.0.1:8501  |  HAProxy stats: http://127.0.0.1:7000' -ForegroundColor DarkGray
