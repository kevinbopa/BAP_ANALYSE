param(
    [string]$ComposeFile = "infra/docker/postgres-compose.yml"
)

$ErrorActionPreference = "Stop"

docker compose -f $ComposeFile down
if ($LASTEXITCODE -ne 0) {
    throw "Failed to stop local PostgreSQL."
}

Write-Output "Local PostgreSQL stopped."
