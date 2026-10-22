# JARVIS Zero installer for Windows. Free and local: no accounts, no API keys, no paid services.
# Usage (PowerShell):  powershell -ExecutionPolicy Bypass -File .\install.ps1
# Re-running is safe; finished steps are skipped.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ---- 1. Prerequisites ----
Step "Checking prerequisites"
$py = (Get-Command py -ErrorAction SilentlyContinue)
$python = if ($py) { "py -3.12" } else { "python" }
$ver = & cmd /c "$python --version" 2>&1
if ($ver -notmatch "Python 3\.(1[0-2])") { Fail "Python 3.10-3.12 is required (found: $ver). Install from https://www.python.org/downloads/ (free)." }
Write-Host "  $ver"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail "Node.js 18+ is required for the dashboard. Install the free LTS from https://nodejs.org" }
Write-Host "  Node $(node --version)"
$freeGB = [math]::Round((Get-PSDrive -Name ($Root.Substring(0,1))).Free / 1GB, 1)
Write-Host "  Free disk: $freeGB GB (need about 4 GB)"
if ($freeGB -lt 4) { Write-Host "  Warning: low disk space." -ForegroundColor Yellow }

# ---- 2. Python environment ----
Step "Creating Python environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & cmd /c "$python -m venv .venv" }
& .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .venv\Scripts\pip.exe install --quiet -r requirements.txt
Write-Host "  Python packages installed"

# ---- 3. Voices (Piper, ~63 MB each; ryan-high 120 MB) ----
Step "Downloading Piper voices"
New-Item -ItemType Directory -Force "models\piper" | Out-Null
$base = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en"
$voices = @(
  "en_GB/northern_english_male/medium/en_GB-northern_english_male-medium",  # default: most natural fast male voice
  "en_US/amy/medium/en_US-amy-medium",                                      # female
  "en_US/ryan/high/en_US-ryan-high",                                        # natural US male, ~4x slower (120 MB)
  "en_GB/alan/medium/en_GB-alan-medium",
  "en_US/ryan/medium/en_US-ryan-medium"
)
foreach ($v in $voices) {
  $name = Split-Path $v -Leaf
  foreach ($ext in @(".onnx", ".onnx.json")) {
    $dest = "models\piper\$name$ext"
    if (-not (Test-Path $dest)) {
      Invoke-WebRequest "$base/$v$ext" -OutFile "$dest.part" -UseBasicParsing
      Move-Item -Force "$dest.part" $dest
      Write-Host "  got $name$ext"
    }
  }
}

# Optional, most human-sounding voice: Kokoro-82M (Apache-2.0), int8 ONNX ~92 MB + voices ~28 MB.
# Select "kokoro:am_michael" (male) or "kokoro:af_heart" (female) in Settings. Slower to start speaking than Piper.
Step "Downloading Kokoro voice (optional, ~120 MB)"
New-Item -ItemType Directory -Force "models\kokoro" | Out-Null
$kbase = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
$kfiles = @{ "kokoro-v1.0.int8.onnx" = "6E742170D309016E5891A994E1CE1559C702A2CCD0075E67EF7157974F6406CB";
             "voices-v1.0.bin"       = "BCA610B8308E8D99F32E6FE4197E7EC01679264EFED0CAC9140FE9C29F1FBF7D" }
foreach ($f in $kfiles.Keys) {
  $dest = "models\kokoro\$f"
  if (-not (Test-Path $dest)) {
    try {
      Invoke-WebRequest "$kbase/$f" -OutFile "$dest.part" -UseBasicParsing
      if ((Get-FileHash "$dest.part" -Algorithm SHA256).Hash -ne $kfiles[$f]) { throw "checksum mismatch" }
      Move-Item -Force "$dest.part" $dest
      Write-Host "  got $f"
    } catch {
      Remove-Item -Force -ErrorAction SilentlyContinue "$dest.part"
      Write-Host "  Kokoro download failed ($_). Piper voices still work." -ForegroundColor Yellow
    }
  } else { Write-Host "  $f present" }
}

# ---- 4. Speech recognition model (Whisper base.en, ~145 MB, cached for offline use) ----
Step "Caching the Whisper speech model"
& .venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8'); print('  base.en ready')"

# ---- 5. Local language model (Ollama) ----
Step "Checking Ollama (local LLM runtime)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
  Write-Host "  Ollama is not installed. It is free and open source:" -ForegroundColor Yellow
  Write-Host "    winget install Ollama.Ollama      (or download from https://ollama.com/download)"
  Write-Host "  Then re-run this installer."
} else {
  $models = (& ollama list) | Out-String
  if ($models -notmatch "llama3\.2:3b") { Write-Host "  Pulling llama3.2:3b (~2 GB)…"; & ollama pull llama3.2:3b }
  Write-Host "  llama3.2:3b ready"
}

# ---- 6. Dashboard ----
Step "Building the dashboard"
Push-Location frontend
& npm install --no-audit --no-fund --silent
& npm run build --silent
Pop-Location

# ---- 7. Config ----
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; (Get-Content ".env") -replace "change-me-to-a-long-random-string", "" | Set-Content ".env" -Encoding utf8 }

Step "Running quick tests (no microphone needed)"
& .venv\Scripts\python.exe -m pytest -q -m "not slow"

Write-Host "`nDone. Start JARVIS with start-jarvis.bat, then open http://127.0.0.1:8600" -ForegroundColor Green
Write-Host "Optional: python scripts\calibrate_echo.py   (measures speaker echo so you can interrupt JARVIS reliably)"
