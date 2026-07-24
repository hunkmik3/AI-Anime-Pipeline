# Installs giantstudio-agent (backend :8102) + giantstudio-tunnel as NSSM services.
# Run ELEVATED. Idempotent: re-running reinstalls cleanly.
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path "C:\flowboard\logs" | Out-Null
Start-Transcript -Path "C:\flowboard\logs\nssm-install.log" -Force | Out-Null

$nssm   = "C:\nssm\nssm-2.24\win64\nssm.exe"
$python = "C:\flowboard\agent\.venv\Scripts\python.exe"
$cfd    = "C:\Program Files (x86)\cloudflared\cloudflared.exe"

Write-Host "== free port 8102 (stop manual test backend) =="
Get-NetTCPConnection -LocalPort 8102 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Write-Host "  killing PID $($_.OwningProcess) on 8102"; Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

Write-Host "== stop manual cloudflared running giantstudio config =="
Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'giantstudio\.yml' } |
  ForEach-Object { Write-Host "  killing cloudflared PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

function Install-Svc($name, $app, $params, $log) {
  Write-Host "== (re)install service: $name =="
  & $nssm stop $name 2>$null | Out-Null
  & $nssm remove $name confirm 2>$null | Out-Null
  Start-Sleep -Milliseconds 500
  & $nssm install $name $app $params
  & $nssm set $name AppDirectory (Split-Path $app)
  & $nssm set $name AppStdout $log
  & $nssm set $name AppStderr $log
  & $nssm set $name Start SERVICE_AUTO_START
  & $nssm set $name AppExit Default Restart
  & $nssm set $name AppRestartDelay 5000
  & $nssm set $name AppStdoutCreationDisposition 4
  & $nssm set $name AppStderrCreationDisposition 4
}

# Backend: python -m uvicorn (mirrors the working giantflow-agent). AppDirectory
# = agent so find_dotenv(usecwd=True) walks up to C:\flowboard\.env.
Install-Svc "giantstudio-agent" $python "-m uvicorn flowboard.main:app --host 127.0.0.1 --port 8102" "C:\flowboard\logs\backend.log"
& $nssm set giantstudio-agent AppDirectory "C:\flowboard\agent"

# Tunnel: dedicated giantstudio.yml (does NOT touch giantflow's config.yml).
Install-Svc "giantstudio-tunnel" $cfd "tunnel --config C:\Users\pp\.cloudflared\giantstudio.yml run giantstudio" "C:\flowboard\logs\tunnel.log"
& $nssm set giantstudio-tunnel AppDirectory "C:\Program Files (x86)\cloudflared"

Write-Host "== start services =="
& $nssm start giantstudio-agent
Start-Sleep -Seconds 4
& $nssm start giantstudio-tunnel
Start-Sleep -Seconds 2

Write-Host "== status =="
Get-Service giantstudio-agent, giantstudio-tunnel, giantflow-agent, giantflow-tunnel |
  Select-Object Name, Status, StartType | Format-Table -AutoSize | Out-String | Write-Host

Stop-Transcript | Out-Null
