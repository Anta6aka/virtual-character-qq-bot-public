$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
foreach ($dir in 'runtime/astrbot','runtime/napcat-config','runtime/ntqq','runtime/plugin-data') {
    New-Item -ItemType Directory -Force $dir | Out-Null
}
Copy-Item examples/demo/relationship.json runtime/plugin-data/relationship.json -Force
docker compose up -d
docker compose ps
Write-Host ''
Write-Host 'AstrBot: http://localhost:6185'
Write-Host 'NapCat:  http://localhost:6099/webui'
