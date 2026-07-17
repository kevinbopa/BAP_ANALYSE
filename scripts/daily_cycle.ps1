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

$summary = Join-Path $logDir ("daily_cycle_" + (Get-Date -Format "yyyy-MM-dd_HHmmss") + ".json")
Log "Lancement du cycle centralise (profil daily)."
py scripts\run_cycle.py --profile daily --trigger SCHEDULED --log-file $log --output $summary *>> $log
$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    Log "Cycle termine. Resume JSON: $summary"
} else {
    Log "Cycle en echec (code $exitCode). Resume JSON: $summary"
}
exit $exitCode
