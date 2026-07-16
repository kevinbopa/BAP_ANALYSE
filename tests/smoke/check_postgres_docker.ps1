param(
    [string]$ContainerName = "spe-pg-smoke",
    [string]$Image = "postgres:16-alpine",
    [string]$Database = "spe_test",
    [string]$Password = "postgres",
    [int]$Port = 55432
)

$ErrorActionPreference = "Stop"

function Invoke-Docker {
    param([string[]]$DockerArgs)
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($DockerArgs -join ' ')"
    }
}

try {
    $existing = (& docker ps -a --filter "name=^${ContainerName}$" --format "{{.Names}}")
    if ($existing) {
        Invoke-Docker -DockerArgs @("rm", "-f", $ContainerName)
    }

    Invoke-Docker -DockerArgs @(
        "run",
        "--name", $ContainerName,
        "-e", "POSTGRES_PASSWORD=$Password",
        "-e", "POSTGRES_DB=$Database",
        "-p", "${Port}:5432",
        "-d",
        $Image
    )

    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        & docker exec $ContainerName pg_isready -U postgres -d $Database | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }

    if (-not $ready) {
        throw "PostgreSQL container did not become ready in time."
    }

    Invoke-Docker -DockerArgs @("cp", "db/migrations/.", "${ContainerName}:/migrations")

    $migrationFiles = Get-ChildItem -Path "db/migrations" -Filter "*.sql" |
        Sort-Object Name |
        ForEach-Object { "/migrations/$($_.Name)" }

    foreach ($migration in $migrationFiles) {
        Invoke-Docker -DockerArgs @(
            "exec",
            $ContainerName,
            "psql",
            "-v", "ON_ERROR_STOP=1",
            "-U", "postgres",
            "-d", $Database,
            "-f", $migration
        )
    }

    $query = "SELECT table_schema || '.' || table_name FROM information_schema.tables WHERE table_schema IN ('ops','raw','core','model','reporting') ORDER BY table_schema, table_name;"
    & docker exec $ContainerName psql -U postgres -d $Database -t -A -c $query
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query created tables."
    }

    Write-Output "PostgreSQL smoke test succeeded."
}
finally {
    & docker rm -f $ContainerName | Out-Null
}
