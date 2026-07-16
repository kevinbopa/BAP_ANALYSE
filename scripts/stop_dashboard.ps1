$ErrorActionPreference = "SilentlyContinue"

$lines = netstat -ano -p tcp | Select-String ":8501"
$pids = @()

foreach ($line in $lines) {
    $parts = ($line.ToString() -split "\s+") | Where-Object { $_ -ne "" }
    if ($parts.Count -lt 5) {
        continue
    }
    $localAddress = $parts[1]
    $state = $parts[3]
    $processId = $parts[4]
    if ($state -ne "LISTENING") {
        continue
    }
    if (-not $localAddress.EndsWith(":8501")) {
        continue
    }
    if ($processId -match "^\d+$" -and [int]$processId -gt 0) {
        $pids += [int]$processId
    }
}

$pids = $pids | Sort-Object -Unique

if (-not $pids) {
    Write-Host "Aucun dashboard actif sur le port 8501."
    exit 0
}

foreach ($processId in $pids) {
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300

    $stillListening = netstat -ano -p tcp |
        Select-String ":8501" |
        Where-Object { $_.ToString() -match "\sLISTENING\s+$processId\s*$" }

    if ($stillListening) {
        taskkill /PID $processId /T /F | Out-Null
        Start-Sleep -Milliseconds 300
    }

    $stillListening = netstat -ano -p tcp |
        Select-String ":8501" |
        Where-Object { $_.ToString() -match "\sLISTENING\s+$processId\s*$" }

    if ($stillListening) {
        Write-Host "Impossible d'arreter le PID $processId automatiquement. Lance PowerShell en administrateur si Windows refuse."
    } else {
        Write-Host "Dashboard arrete (PID $processId)."
    }
}
