param(
    [string]$BaseUrl = "https://odds-data.stake.com"
)

$ErrorActionPreference = "Stop"

function Invoke-StakeRequest {
    param(
        [string]$Url,
        [string]$Method,
        [hashtable]$Headers,
        [string]$Body
    )

    try {
        if ($Method -eq "GET") {
            $response = Invoke-WebRequest -Uri $Url -Method $Method -Headers $Headers -UseBasicParsing -TimeoutSec 20
        } else {
            $response = Invoke-WebRequest -Uri $Url -Method $Method -Headers $Headers -Body $Body -ContentType "application/json" -UseBasicParsing -TimeoutSec 20
        }
        return [pscustomobject]@{
            Success = $true
            StatusCode = [int]$response.StatusCode
            Body = $response.Content
        }
    }
    catch {
        if ($_.Exception.Response) {
            $statusCode = [int]$_.Exception.Response.StatusCode
            $body = ""
            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $body = $reader.ReadToEnd()
                $reader.Dispose()
            }
            catch {
                $body = ""
            }
            return [pscustomobject]@{
                Success = $false
                StatusCode = $statusCode
                Body = $body
            }
        }

        throw
    }
}

$token = [Environment]::GetEnvironmentVariable("STAKE_API_TOKEN")
$authHeader = [Environment]::GetEnvironmentVariable("STAKE_AUTH_HEADER")
if (-not $authHeader) {
    $authHeader = "X-API-KEY"
}
$headers = @{
    "Accept" = "application/json"
}
if ($token) {
    $headers[$authHeader] = $token
}

$sportsResult = Invoke-StakeRequest -Url "$BaseUrl/sports" -Method "GET" -Headers $headers -Body $null
Write-Output "Stake odds smoke test /sports status: $($sportsResult.StatusCode)"
if ($sportsResult.Body) {
    $preview = $sportsResult.Body
    if ($preview.Length -gt 300) {
        $preview = $preview.Substring(0, 300)
    }
    Write-Output "Stake odds smoke test /sports preview: $preview"
}

$categoriesResult = Invoke-StakeRequest -Url "$BaseUrl/sports/soccer/categories" -Method "GET" -Headers $headers -Body $null
Write-Output "Stake odds smoke test /sports/soccer/categories status: $($categoriesResult.StatusCode)"
if ($categoriesResult.Body) {
    $preview = $categoriesResult.Body
    if ($preview.Length -gt 300) {
        $preview = $preview.Substring(0, 300)
    }
    Write-Output "Stake odds smoke test categories preview: $preview"
}

if ($sportsResult.StatusCode -eq 200 -and $categoriesResult.StatusCode -eq 200) {
    Write-Output "Stake odds smoke test completed."
    exit 0
}

Write-Error "Unexpected Stake odds status codes: sports=$($sportsResult.StatusCode) categories=$($categoriesResult.StatusCode)"
