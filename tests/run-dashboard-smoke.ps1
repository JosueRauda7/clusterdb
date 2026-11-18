# Smoke test del dashboard dentro del contenedor.
# Requiere el stack levantado (monitor + dashboard saludables).
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
docker info *> $null
$dockerExit = $LASTEXITCODE
$ErrorActionPreference = $previousPreference
if ($dockerExit -ne 0) {
    throw 'Docker no responde. Inicie Docker Desktop y vuelva a intentarlo.'
}

$state = docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' clusterdb-dashboard 2>$null
if ($state -ne 'healthy') {
    throw "El contenedor clusterdb-dashboard no esta healthy (estado: $state). Ejecute .\scripts\up.ps1."
}

Get-Content (Join-Path $PSScriptRoot 'dashboard_smoke.py') -Raw |
    docker exec -i clusterdb-dashboard python -

if ($LASTEXITCODE -ne 0) {
    throw 'El smoke test del dashboard fallo.'
}
