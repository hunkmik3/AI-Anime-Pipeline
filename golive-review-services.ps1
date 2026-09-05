# =====================================================================
# Go-live: make the REVIEW deployment durable (parallel to production).
# Installs two auto-start / auto-restart NSSM services that mirror the
# production ones, then hands off from the manual processes.
#
#   giantstudio-review-agent   -> uvicorn backend on :8103
#   giantstudio-review-tunnel  -> cloudflared for review-giantstudio.reelmind.co
#
# Touches ONLY the review deployment. Never stops/edits giantstudio-agent,
# giantstudio-tunnel, giantflow-*, or the production flowboard DB.
# RUN ELEVATED (Administrator).  Self-logs to logs\golive-run.txt.
# ASCII only (PowerShell 5.1 reads BOM-less files as ANSI).
# =====================================================================
New-Item -ItemType Directory -Force -Path "C:\flowboard-review\logs" | Out-Null
Start-Transcript -Path "C:\flowboard-review\logs\golive-run.txt" -Force | Out-Null
$ErrorActionPreference = "Stop"
try {
  $nssm = "C:\nssm\nssm-2.24\win64\nssm.exe"
  if (-not (Test-Path $nssm)) { throw "nssm not found at $nssm" }
  if (-not (Test-Path "C:\flowboard-review\agent\.venv\Scripts\python.exe")) {
    throw "review venv python not found - wrong machine or paths"
  }

  Write-Host "== 1. Stop the MANUAL review processes (frees :8103 and the review tunnel) =="
  Get-NetTCPConnection -LocalPort 8103 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Host "  stopping backend PID $($_.OwningProcess)"; Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
  Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
    Where-Object { $_.CommandLine -like '*giantstudio-review*' } |
    ForEach-Object { Write-Host "  stopping review tunnel PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2

  function Install-Or-Update($name, $app, $dir, $params, $log) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
      Write-Host "  '$name' exists - updating config"
    } else {
      Write-Host "  installing '$name'"
      & $nssm install $name $app | Out-Null
    }
    & $nssm set $name Application      $app    | Out-Null
    & $nssm set $name AppDirectory     $dir    | Out-Null
    & $nssm set $name AppParameters    $params | Out-Null
    & $nssm set $name AppStdout        $log    | Out-Null
    & $nssm set $name AppStderr        $log    | Out-Null
    & $nssm set $name AppExit Default  Restart | Out-Null
    & $nssm set $name AppRestartDelay  5000    | Out-Null
    & $nssm set $name Start SERVICE_AUTO_START | Out-Null
  }

  Write-Host "== 2. Install/refresh the review AGENT service (:8103) =="
  Install-Or-Update "giantstudio-review-agent" "C:\flowboard-review\agent\.venv\Scripts\python.exe" "C:\flowboard-review\agent" "-m uvicorn flowboard.main:app --host 127.0.0.1 --port 8103" "C:\flowboard-review\logs\backend.log"
  & $nssm set giantstudio-review-agent AppEnvironmentExtra "FLOWBOARD_FFMPEG_BIN=C:\ffmpeg\bin\ffmpeg.exe" "FLOWBOARD_FFPROBE_BIN=C:\ffmpeg\bin\ffprobe.exe" | Out-Null

  Write-Host "== 3. Install/refresh the review TUNNEL service =="
  Install-Or-Update "giantstudio-review-tunnel" "C:\Program Files (x86)\cloudflared\cloudflared.exe" "C:\Program Files (x86)\cloudflared" "tunnel --config C:\Users\pp\.cloudflared\giantstudio-review.yml run giantstudio-review" "C:\flowboard-review\logs\tunnel.log"

  Write-Host "== 4. Start both services =="
  Start-Service giantstudio-review-agent
  Start-Service giantstudio-review-tunnel

  Write-Host "== 5. Verify =="
  $ok = $false
  for ($i = 0; $i -lt 40; $i++) {
    try { $r = Invoke-RestMethod "http://127.0.0.1:8103/api/health" -TimeoutSec 2; if ($r.ok) { $ok = $true; break } } catch {}
    Start-Sleep -Milliseconds 500
  }
  (Get-Service giantstudio-review-agent, giantstudio-review-tunnel | Format-Table Status, StartType, Name -AutoSize | Out-String) | Write-Host
  Write-Host ("  /api/health :8103 ok = {0}" -f $ok)
  Write-Host "  (production giantstudio-agent / giantstudio-tunnel were NOT touched)"
  Write-Host "DONE - review is now an auto-start, auto-restart service parallel to production."
}
catch {
  Write-Host ("FATAL: " + $_.Exception.Message)
  Write-Host $_.ScriptStackTrace
}
finally {
  Stop-Transcript | Out-Null
}
