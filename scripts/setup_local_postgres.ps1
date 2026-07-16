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

$ledgerSql = @"
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"@
docker exec -i $ContainerName psql -v ON_ERROR_STOP=1 -U $AdminUser -d $Database -c $ledgerSql
if ($LASTEXITCODE -ne 0) {
    throw "Failed to prepare migration ledger."
}

$migrationFiles = Get-ChildItem -Path "db/migrations" -Filter "*.sql" |
    Sort-Object Name |
    ForEach-Object {
        [PSCustomObject]@{
            Name = $_.Name
            ContainerPath = "/migrations/$($_.Name)"
        }
    }

foreach ($migration in $migrationFiles) {
    $escapedMigrationName = $migration.Name.Replace("'", "''")
    $alreadyApplied = docker exec $ContainerName psql -U $AdminUser -d $Database -t -A -c "SELECT 1 FROM public.schema_migrations WHERE migration_name = '$escapedMigrationName' LIMIT 1;"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed checking migration ledger for $($migration.Name)"
    }
    if (($alreadyApplied | Out-String).Trim() -eq "1") {
        Write-Output "Skipping migration $($migration.Name) (already applied)."
        continue
    }

    docker exec $ContainerName psql -v ON_ERROR_STOP=1 -U $AdminUser -d $Database -f $migration.ContainerPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed applying migration $($migration.ContainerPath)"
    }
    docker exec $ContainerName psql -v ON_ERROR_STOP=1 -U $AdminUser -d $Database -c "INSERT INTO public.schema_migrations (migration_name) VALUES ('$escapedMigrationName') ON CONFLICT DO NOTHING;"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed recording migration $($migration.Name)"
    }
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
