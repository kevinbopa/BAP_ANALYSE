# Cycle quotidien Sports Prediction Engine.
#
# Fait grandir le track record automatiquement :
#   scores frais -> reglement des deals (ROI reel) -> cotes fraiches
#   -> effectifs API-Football (goutte-a-goutte quotidien)
#   -> scan de correlations -> predictions calibrees.
#
# Enregistre en tache planifiee Windows (08:00) :
#   schtasks /query /tn "SPE Daily Cycle"          # voir
#   schtasks /run /tn "SPE Daily Cycle"            # lancer a la main
#   schtasks /delete /tn "SPE Daily Cycle" /f      # supprimer
#
# Prerequis : Docker Desktop lance. Le script demarre/attend Postgres si le
# conteneur n'est pas encore pret au moment du cycle planifie.

$ErrorActionPreference = "Continue"
Set-Location "$PSScriptRoot\.."
$logDir = Join-Path (Get-Location) "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("daily_cycle_" + (Get-Date -Format "yyyy-MM-dd") + ".log")

function Step($name, $cmd) {
    "=== $(Get-Date -Format 'HH:mm:ss') $name ===" | Out-File $log -Append
    Invoke-Expression $cmd *>> $log
}

function Log($message) {
    "$(Get-Date) - $message" | Out-File $log -Append
}

function Test-PostgresReady {
    $pg = docker exec spe-postgres pg_isready -U postgres -d sports_prediction_engine 2>$null
    return ($LASTEXITCODE -eq 0 -and ($pg -match "accepting connections"))
}

function Ensure-PostgresReady {
    for ($i = 1; $i -le 6; $i++) {
        if (Test-PostgresReady) {
            Log "Postgres disponible."
            return $true
        }
        Start-Sleep -Seconds 5
    }

    # Docker Desktop lui-meme eteint ? On le demarre et on attend le demon.
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Log "Demon Docker eteint; demarrage de Docker Desktop."
        $dd = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
        if (Test-Path $dd) { Start-Process -FilePath $dd -WindowStyle Hidden }
        for ($i = 1; $i -le 45; $i++) {
            Start-Sleep -Seconds 4
            docker info *> $null
            if ($LASTEXITCODE -eq 0) { Log "Demon Docker pret."; break }
        }
    }
    # Conteneur simplement arrete ? docker start suffit (plus leger que compose).
    docker start spe-postgres *> $null
    for ($i = 1; $i -le 15; $i++) {
        if (Test-PostgresReady) {
            Log "Postgres disponible apres docker start."
            return $true
        }
        Start-Sleep -Seconds 2
    }

    Log "Postgres non disponible; tentative de demarrage Docker Compose."
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_local_postgres.ps1 *>> $log
    if ($LASTEXITCODE -ne 0) {
        Log "Echec du demarrage Postgres. Ouvre Docker Desktop puis relance npm run cycle."
        return $false
    }

    for ($i = 1; $i -le 30; $i++) {
        if (Test-PostgresReady) {
            Log "Postgres disponible apres demarrage automatique."
            return $true
        }
        Start-Sleep -Seconds 2
    }

    Log "Postgres demarre mais pas pret apres attente; cycle annule pour eviter une mise a jour partielle."
    return $false
}

# 0. Postgres disponible ? Sinon on le demarre et on attend.
if (-not (Ensure-PostgresReady)) {
    exit 1
}

# 1. Un backfill lourd tourne deja ? On ne se marche pas dessus (rate limit).
$stale = docker exec spe-postgres psql -U spe_app_rw -d sports_prediction_engine -t -A -c "UPDATE ops.ingestion_runs SET status_code='FAILED', finished_at=now(), error_message=COALESCE(error_message, 'Auto-closed by daily cycle: stale RUNNING ingestion') WHERE status_code='RUNNING' AND started_at < now() - interval '2 hours' RETURNING ingestion_run_id;"
if (($stale | Out-String).Trim()) {
    "$(Get-Date) - Runs d'ingestion bloques fermes automatiquement: $stale" | Out-File $log -Append
}

$busy = docker exec spe-postgres psql -U spe_app_rw -d sports_prediction_engine -t -A -c "SELECT COUNT(*) FROM ops.ingestion_runs WHERE status_code='RUNNING' AND started_at >= now() - interval '2 hours';"
if ([int]$busy -gt 0) {
    "$(Get-Date) - Un run d'ingestion est deja actif, sync scores sautee (settle/cotes/predictions seulement)." | Out-File $log -Append
} else {
    Step "Sync scores TheSportsDB" "py services\ingestion\run_ingest_thesportsdb.py"
}

Step "Reglement des deals termines" "py services\prediction\run_settle_deals.py"
Step "Sync cotes The Odds API" "py services\ingestion\run_ingest_theoddsapi_odds.py"
Step "Meteo previsionnelle Open-Meteo" "py services\ingestion\run_ingest_weather.py"
Step "Couche joueurs API-Football (squads + blessures + stats/match)" "py services\ingestion\run_ingest_apifootball.py --stats"
Step "Backfill xG stats de match (grandes ligues, incrementiel)" "py services\ingestion\run_ingest_apifootball_stats.py"
Step "Scan de correlations" "py services\prediction\run_correlation_scan.py"
Step "Pipeline de predictions" "py services\prediction\run_prediction_pipeline.py"
Step "Cotes outright (vainqueurs)" "py services\ingestion\run_ingest_outright_odds.py"
Step "Faits divers (simulations long terme)" "py services\prediction\run_outright_predictions.py"
Step "Cotes buteur (anytime scorer)" "py services\ingestion\run_ingest_scorer_odds.py"
Step "Predictions buteur + deals" "py services\prediction\run_scorer_predictions.py"
Step "Golf DataGolf (tournois/joueurs/SG/cotes/matchups/resultats)" "py services\ingestion\run_ingest_datagolf.py"
Step "Reglement des deals golf (tournois termines)" "py services\prediction\run_settle_golf.py"
Step "Predictions golf + deals (modele DataGolf)" "py services\prediction\run_golf_predictions.py"

"=== $(Get-Date -Format 'HH:mm:ss') CYCLE TERMINE ===" | Out-File $log -Append
