$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot\..
py apps/dashboard/app.py
