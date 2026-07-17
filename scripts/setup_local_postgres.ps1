param(
    [string]$ComposeFile = "infra/docker/postgres-compose.yml",
    [string]$ContainerName = "spe-postgres",
    [string]$Database = "sports_prediction_engine",
    [string]$AdminUser = "postgres",
    [string]$AdminPassword = "postgres",
    [string]$AppUser = "spe_app_rw",
    [string]$AppPassword = "spe_app_password",
    [string]$IngestUser = "spe_ingest_rw",
    [string]$IngestPassword = "spe_ingest_password",
    [string]$ReadOnlyUser = "spe_readonly",
    [string]$ReadOnlyPassword = "spe_readonly_password",
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"

if ($Recreate) {
    docker compose -f $ComposeFile down -v
}

docker compose -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) {
    throw "Failed to start PostgreSQL with Docker Compose."
}

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    docker exec $ContainerName pg_isready -U $AdminUser -d $Database *> $null
    if ($LASTEXITCODE -eq 0) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 2
}

if (-not $ready) {
    throw "PostgreSQL container did not become ready in time."
}

docker cp db/migrations/. ${ContainerName}:/migrations | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to copy migrations into container."
}

$env:POSTGRES_MIGRATE_HOST = "localhost"
$env:POSTGRES_MIGRATE_PORT = "5432"
$env:POSTGRES_MIGRATE_DB = $Database
$env:POSTGRES_MIGRATE_USER = $AdminUser
$env:POSTGRES_MIGRATE_PASSWORD = $AdminPassword
node scripts/run_python.js scripts/apply_migrations.py --prefix POSTGRES_MIGRATE --fallback-prefix POSTGRES
if ($LASTEXITCODE -ne 0) {
    throw "Failed applying SQL migrations."
}

$escapedAppPassword = $AppPassword.Replace("'", "''")
$escapedIngestPassword = $IngestPassword.Replace("'", "''")
$escapedReadOnlyPassword = $ReadOnlyPassword.Replace("'", "''")

$roleSql = @"
ALTER ROLE $AppUser WITH PASSWORD '$escapedAppPassword';
ALTER ROLE $IngestUser WITH PASSWORD '$escapedIngestPassword';
ALTER ROLE $ReadOnlyUser WITH PASSWORD '$escapedReadOnlyPassword';
"@

docker exec -i $ContainerName psql -v ON_ERROR_STOP=1 -U $AdminUser -d $Database -c $roleSql
if ($LASTEXITCODE -ne 0) {
    throw "Failed to configure role passwords."
}

Write-Output "Local PostgreSQL is ready."
Write-Output "Admin connection  : host=localhost port=5432 db=$Database user=$AdminUser password=$AdminPassword"
Write-Output "App connection    : host=localhost port=5432 db=$Database user=$AppUser password=$AppPassword"
Write-Output "Ingest connection : host=localhost port=5432 db=$Database user=$IngestUser password=$IngestPassword"
Write-Output "Readonly connection: host=localhost port=5432 db=$Database user=$ReadOnlyUser password=$ReadOnlyPassword"
