# Starts JARVIS Zero in the background (if it isn't running yet), then shows spidey with JARVIS's small panel.
# Used by the Desktop shortcut (Ctrl+Alt+Shift+J). Safe to run repeatedly.
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Url = "http://127.0.0.1:8600"

function Test-Port($port) {
  [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# Ollama (the local AI) must be running.
if (-not (Test-Port 11434)) {
  $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
  if ($ollama) { Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden }
}

if (-not (Test-Port 8600)) {
  New-Item -ItemType Directory -Force "$Root\data" | Out-Null
  Start-Process -FilePath "$Root\.venv\Scripts\python.exe" -ArgumentList "-m", "jarvis" `
    -WorkingDirectory "$Root\backend" -WindowStyle Hidden `
    -RedirectStandardOutput "$Root\data\server.log" -RedirectStandardError "$Root\data\server.err.log"
  # Wait up to 30 s for the backend to load its models.
  for ($i = 0; $i -lt 60 -and -not (Test-Port 8600); $i++) { Start-Sleep -Milliseconds 500 }
}

# JARVIS only obeys requests that carry its secret token (from .env), so other programs and websites can't command it.
$Token = (Get-Content "$Root\.env" -ErrorAction SilentlyContinue | Where-Object { $_ -like 'JARVIS_API_TOKEN=*' } |
          Select-Object -First 1) -replace '^JARVIS_API_TOKEN=', ''
$Headers = @{ "x-jarvis-token" = "$Token".Trim() }

# The little floating bot, with JARVIS's small panel open (no browser tab: lighter on the laptop). If spidey is
# already running, JARVIS asks it to show the panel. The full dashboard is still a click away in the panel.
$botRunning = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'bot_overlay' }
if (-not $botRunning) {   # its errors go to data\overlay.err.log, so a vanished bot can be explained
  Start-Process -FilePath "$Root\.venv\Scripts\pythonw.exe" -ArgumentList "`"$Root\scripts\bot_overlay.py`"", "--panel" `
    -WorkingDirectory $Root -RedirectStandardError "$Root\data\overlay.err.log" -RedirectStandardOutput "$Root\data\overlay.out.log"
} else {
  try { Invoke-RestMethod -Method Post -Uri "$Url/api/overlay/panel" -Headers $Headers -TimeoutSec 5 | Out-Null } catch { }
}

# Hands-free voice mode listens all the time, so switch the mic on (push-to-talk and chat mode don't need it).
$S = try { Get-Content "$Root\data\settings.json" -Raw -ErrorAction Stop | ConvertFrom-Json } catch { $null }
if ($S -and $S.io_mode -ne "chat" -and $S.mode -eq "hands_free") {
  try { Invoke-RestMethod -Method Post -Uri "$Url/api/listen/start" -Headers $Headers -TimeoutSec 5 | Out-Null } catch { }
}
