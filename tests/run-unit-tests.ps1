# Ejecuta las pruebas unitarias en un contenedor Python aislado.
# No usa la red y monta el repositorio en solo lectura.
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

$mount = "${repoRoot}:/app:ro"
docker run --rm --network none `
    -v $mount `
    -e PYTHONPATH=/app/monitor `
    -e PYTHONDONTWRITEBYTECODE=1 `
    -w /app `
    python:3.12-slim-bookworm `
    python -m unittest discover -s tests -v

if ($LASTEXITCODE -ne 0) {
    throw 'Las pruebas unitarias fallaron.'
}
