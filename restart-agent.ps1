# Restart the giantstudio backend service to load freshly-pulled code. Run elevated.
$ErrorActionPreference = "Continue"
Restart-Service giantstudio-agent -Force
Start-Sleep -Seconds 4
Get-Service giantstudio-agent | Select-Object Name, Status |
  Out-File -FilePath "C:\flowboard\logs\restart.log" -Encoding utf8
