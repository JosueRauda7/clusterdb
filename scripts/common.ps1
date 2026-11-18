# Utilidades compartidas por los scripts de PowerShell.
# Uso: . "$PSScriptRoot/common.ps1"
$ErrorActionPreference = 'Stop'

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:MonitorContainer = 'clusterdb-monitor'
Set-Location $script:RepoRoot

function Assert-Docker {
    # Docker puede escribir avisos en stderr; con ErrorActionPreference=Stop
    # eso se convertiria en una excepcion falsa. Se aisla la comprobacion.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    docker info *> $null
    $dockerExit = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($dockerExit -ne 0) {
        throw 'Docker no responde. Inicie Docker Desktop y vuelva a intentarlo.'
    }
}

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArgs)
    & docker compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw ('docker compose fallo: ' + ($ComposeArgs -join ' '))
    }
}

function Get-InternalJson {
    param([Parameter(Mandatory = $true)][string]$Url)
    $raw = & docker exec $script:MonitorContainer curl -fsS $Url 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo consultar $Url"
    }
    return ($raw | ConvertFrom-Json)
}

function Get-MonitorJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    return Get-InternalJson ("http://127.0.0.1:8000" + $Path)
}

function Invoke-MonitorPost {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Json = '{}'
    )
    # El JSON se envia por stdin (--data-binary @-) para evitar que PowerShell
    # elimine las comillas al pasar argumentos a un ejecutable nativo.
    $raw = $Json | & docker exec -i $script:MonitorContainer curl -fsS -X POST `
        -H 'Content-Type: application/json' --data-binary '@-' `
        ("http://127.0.0.1:8000" + $Path)
    if ($LASTEXITCODE -ne 0) {
        throw "La accion POST $Path fallo."
    }
    return ($raw | ConvertFrom-Json)
}

function Get-ComposeServices {
    $lines = & docker compose ps --format json
    if ($LASTEXITCODE -ne 0) {
        throw 'No se pudo ejecutar docker compose ps.'
    }
    return @($lines | ForEach-Object { $_ | ConvertFrom-Json })
}

function Get-ContainerName {
    param([Parameter(Mandatory = $true)][string]$Service)
    return "clusterdb-$Service"
}

function Get-PatroniNode {
    param([Parameter(Mandatory = $true)][string]$Service)
    try {
        return Get-InternalJson ("http://${Service}:8008/patroni")
    }
    catch {
        return $null
    }
}

function Get-PrimaryService {
    $status = Get-MonitorJson '/api/status'
    $primary = $status.service.primary_name
    if (-not $primary) {
        throw 'El monitor no reporta un primario. Revise el estado del cluster.'
    }
    return [string]$primary
}

function Wait-For {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Condition,
        [int]$TimeoutSeconds = 60,
        [string]$Description = 'condicion',
        [int]$IntervalSeconds = 1
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        try {
            if (& $Condition) {
                return $true
            }
        }
        catch {
            # La condicion puede fallar mientras el servicio esta arrancando.
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
    throw ("Timeout esperando: " + $Description)
}

function Wait-ServiceHealthy {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [int]$TimeoutSeconds = 240
    )
    $container = Get-ContainerName $Service
    Wait-For -TimeoutSeconds $TimeoutSeconds -Description "$Service healthy" -Condition {
        $state = & docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $container 2>$null
        return ($LASTEXITCODE -eq 0 -and ("$state".Trim() -eq 'healthy'))
    }
}

function Test-TcpPort {
    param(
        [string]$HostName = '127.0.0.1',
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMs = 2000
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName, $Port)
        if (-not $task.Wait($TimeoutMs)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        try { $client.Dispose() } catch { }
    }
}

function Format-Seconds {
    param($Value)
    if ($null -eq $Value) {
        return 'Sin datos suficientes'
    }
    $seconds = [double]$Value
    if ($seconds -lt 1) {
        return ('{0:N0} ms' -f ($seconds * 1000))
    }
    if ($seconds -lt 60) {
        return ('{0:N1} s' -f $seconds)
    }
    if ($seconds -lt 3600) {
        return ('{0:N1} min' -f ($seconds / 60))
    }
    return ('{0:N1} h' -f ($seconds / 3600))
}

function Write-Header {
    param([string]$Text)
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
}
