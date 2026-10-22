# Starts JARVIS Zero in the background (if it isn't running yet), then opens the dashboard and starts listening.
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

# The little floating blue bot (it only shows while JARVIS is running; a second copy exits by itself).
$botRunning = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'bot_overlay' }
if (-not $botRunning) {   # its errors go to data\overlay.err.log, so a vanished bot can be explained
  Start-Process -FilePath "$Root\.venv\Scripts\pythonw.exe" -ArgumentList "`"$Root\scripts\bot_overlay.py`"" -WorkingDirectory $Root `
    -RedirectStandardError "$Root\data\overlay.err.log" -RedirectStandardOutput "$Root\data\overlay.out.log"
}

# JARVIS only obeys requests that carry its secret token (from .env), so other programs and websites can't command it.
$Token = (Get-Content "$Root\.env" -ErrorAction SilentlyContinue | Where-Object { $_ -like 'JARVIS_API_TOKEN=*' } |
          Select-Object -First 1) -replace '^JARVIS_API_TOKEN=', ''
Start-Process ("$Url/?listen=1" + $(if ($Token) { "&token=$([uri]::EscapeDataString($Token.Trim()))" } else { "" }))
