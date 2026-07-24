# Move giantstudio media/cache from C:\flowboard\storage to D:\flowboard-storage.
# Stops the backend first (no open handles / no writes mid-move), then restarts.
# Run ELEVATED.
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path "C:\flowboard\logs" | Out-Null
Start-Transcript -Path "C:\flowboard\logs\move-storage.log" -Force | Out-Null

$src = "C:\flowboard\storage"
$dst = "D:\flowboard-storage"

Write-Host "== stop giantstudio-agent =="
Stop-Service giantstudio-agent -Force
Start-Sleep -Seconds 3
Write-Host ("service status: " + (Get-Service giantstudio-agent).Status)

Write-Host "== move $src -> $dst (robocopy /E /MOVE) =="
robocopy $src $dst /E /MOVE /R:2 /W:2 /NP /NFL /NDL
Write-Host ("robocopy exit code: $LASTEXITCODE  (0-7 = success)")

Write-Host "== start giantstudio-agent =="
Start-Service giantstudio-agent
Start-Sleep -Seconds 4
Write-Host ("service status: " + (Get-Service giantstudio-agent).Status)

Write-Host "== dest listing =="
Get-ChildItem -Recurse "$dst\media" -ErrorAction SilentlyContinue |
  Measure-Object -Property Length -Sum |
  ForEach-Object { Write-Host ("dest media: {0} files, {1:N1} MB" -f $_.Count, ($_.Sum/1MB)) }

Stop-Transcript | Out-Null
