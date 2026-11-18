# Verificacion integral NO destructiva del stack.
# Comprueba contenedores, quorum, Patroni, replicacion, respaldo, dashboard y puertos.
[CmdletBinding()]
param(
    [switch]$RunBackup
)

. "$PSScriptRoot/common.ps1"
Assert-Docker

$script:Checks = @()
function Add-Check {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Ok,
        [string]$Detail = ''
    )
    $script:Checks += [pscustomobject]@{
        Name   = $Name
        Ok     = $Ok
        Detail = $Detail
    }
}

Write-Header 'Verificacion integral de ClusterDB Monitor'

# 1. Contenedores y healthchecks.
$expected = @(
    'etcd1', 'etcd2', 'etcd3',
    'postgres-master', 'postgres-replica', 'haproxy',
    'postgres-backup', 'monitor', 'txgen', 'dashboard'
)
$services = Get-ComposeServices
$bad = @()
foreach ($name in $expected) {
    $item = $services | Where-Object { $_.Service -eq $name } | Select-Object -First 1
    if ($null -eq $item) {
        $bad += "$name=ausente"
        continue
    }
    if ($item.State -ne 'running') {
        $bad += "$name=$($item.State)"
        continue
    }
    if ($item.Health -and $item.Health -ne 'healthy') {
        $bad += "$name=$($item.Health)"
    }
}
Add-Check 'contenedores y healthchecks' ($bad.Count -eq 0) ($(if ($bad.Count) { $bad -join ', ' } else { '10/10 activos y saludables' }))

# 2. Quorum de etcd.
$etcdOut = & docker exec clusterdb-etcd1 etcdctl `
    --endpoints=http://etcd1:2379,http://etcd2:2379,http://etcd3:2379 `
    endpoint health --cluster 2>&1
$etcdHealthy = @($etcdOut | Select-String 'is healthy').Count
Add-Check 'quorum etcd (3/3)' ($LASTEXITCODE -eq 0 -and $etcdHealthy -ge 3) ("endpoints saludables=$etcdHealthy")

# 3. Patroni: exactamente un primario y una replica.
$masterNode = Get-PatroniNode 'postgres-master'
$replicaNode = Get-PatroniNode 'postgres-replica'
$nodes = @(@($masterNode, $replicaNode) | Where-Object { $null -ne $_ })
$primaries = @($nodes | Where-Object { $_.role -eq 'primary' })
$replicas = @($nodes | Where-Object { $_.role -eq 'replica' })
$running = @($nodes | Where-Object { $_.state -eq 'running' })
$topologyOk = ($nodes.Count -eq 2 -and $primaries.Count -eq 1 -and $replicas.Count -eq 1 -and $running.Count -eq 2)
$topologyDetail = ($nodes | ForEach-Object { "$($_.patroni.name)=$($_.role)/$($_.state)" }) -join '; '
Add-Check 'topologia Patroni (1 primario + 1 replica)' $topologyOk $topologyDetail

# 4. Verificacion activa de replicacion (escribe un latido y lo busca en la replica).
$verification = $null
try {
    $verification = Invoke-MonitorPost '/api/actions/verify-replication'
}
catch {
    Add-Check 'replicacion activa' $false $_.Exception.Message
}
if ($null -ne $verification) {
    $failed = @($verification.checks | Where-Object { -not $_.ok })
    $detail = if ($failed.Count) {
        ($failed | ForEach-Object { "$($_.name): $($_.detail)" }) -join ' | '
    }
    else {
        "$($verification.checks.Count) comprobaciones OK en $($verification.elapsed_seconds) s"
    }
    Add-Check 'replicacion activa' ([bool]$verification.ok) $detail
}

# 5. API del monitor y muestras reales.
$monitorOk = $false
$monitorDetail = ''
try {
    $health = Get-MonitorJson '/health'
    $kpis = Get-MonitorJson '/api/kpis?window_minutes=0'
    $monitorOk = ($health.status -eq 'ok' -and [int]$kpis.monitor.samples -gt 0)
    $monitorDetail = "muestras=$($kpis.monitor.samples), disponibilidad=$(if ($null -ne $kpis.availability.pct) { '{0:N2}%' -f $kpis.availability.pct } else { 'sin datos' })"
}
catch {
    $monitorDetail = $_.Exception.Message
}
Add-Check 'API del monitor y muestras' $monitorOk $monitorDetail

# 6. Respaldo verificado.
if ($RunBackup) {
    Write-Host 'Ejecutando un respaldo nuevo para la verificacion...' -ForegroundColor DarkGray
    try {
        & "$PSScriptRoot/backup.ps1" -Wait | Out-Host
    }
    catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
    }
}
$backupOk = $false
$backupDetail = ''
try {
    $backups = Get-MonitorJson '/api/backups'
    $lastBackup = $backups.status.last_backup
    if ($backups.reachable -and $null -ne $lastBackup) {
        $backupOk = ($lastBackup.status -eq 'success' -and "$($lastBackup.verification)" -match '^ok')
        $backupDetail = "$($lastBackup.filename), $($lastBackup.size_bytes) bytes, $($lastBackup.verification)"
    }
    else {
        $backupDetail = 'sin respaldo completado'
    }
}
catch {
    $backupDetail = $_.Exception.Message
}
Add-Check 'respaldo verificado (SHA/TOC)' $backupOk $backupDetail

# 7. Dashboard, stats de HAProxy y puertos publicados.
$dashboardOk = $false
try {
    $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8501/_stcore/health' -UseBasicParsing -TimeoutSec 8
    $dashboardOk = ($response.StatusCode -eq 200)
}
catch { }
Add-Check 'dashboard en 127.0.0.1:8501' $dashboardOk $(if ($dashboardOk) { 'HTTP 200' } else { 'sin respuesta' })

$statsOk = $false
try {
    $response = Invoke-WebRequest -Uri 'http://127.0.0.1:7000/' -UseBasicParsing -TimeoutSec 8
    $statsOk = ($response.StatusCode -eq 200)
}
catch { }
Add-Check 'HAProxy stats en 127.0.0.1:7000' $statsOk $(if ($statsOk) { 'HTTP 200' } else { 'sin respuesta' })

$portOk = Test-TcpPort -Port 15432
Add-Check 'PostgreSQL en 127.0.0.1:15432' $portOk $(if ($portOk) { 'puerto acepta conexiones' } else { 'puerto cerrado' })

# 8. Sin dos primarios.
$splitBrain = ($primaries.Count -gt 1)
Add-Check 'sin split-brain' (-not $splitBrain) $(if ($splitBrain) { 'dos nodos primarios' } else { 'un solo primario' })

Write-Header 'Resultados'
foreach ($check in $script:Checks) {
    if ($check.Ok) {
        Write-Host ("  [OK]    {0}" -f $check.Name) -ForegroundColor Green
    }
    else {
        Write-Host ("  [FALLA] {0}" -f $check.Name) -ForegroundColor Red
    }
    if ($check.Detail) {
        Write-Host ("          {0}" -f $check.Detail) -ForegroundColor DarkGray
    }
}

$failed = @($script:Checks | Where-Object { -not $_.Ok })
Write-Host ''
if ($failed.Count -eq 0) {
    Write-Host ("Verificacion correcta: {0}/{0} comprobaciones OK." -f $script:Checks.Count) -ForegroundColor Green
    exit 0
}

Write-Host ("Verificacion con {0} fallo(s) de {1}." -f $failed.Count, $script:Checks.Count) -ForegroundColor Red
exit 1
