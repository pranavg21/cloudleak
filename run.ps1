# CloudLeak launcher for Windows (PowerShell).
#
#   Right-click this file > "Run with PowerShell"
#   ...or, in PowerShell, from the cloudleak folder:
#       .\run.ps1
#
# If Windows blocks the script, run this once in the same window:
#       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#
# Mints an API key, wires it into both halves, starts the backend and the web
# app. Close the two windows it opens to stop everything.

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendPort = 8000
$FrontendPort = 3000

function Say($msg) { Write-Host "`n$msg" -ForegroundColor Cyan }
function Die($msg) { Write-Host "`n$msg" -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }

# --- prerequisites ------------------------------------------------------------
# Note: written for PowerShell 5.1, which is what Windows ships by default,
# so no PS7-only syntax here.
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) { Die "Python is not installed. Get it from python.org, and tick 'Add Python to PATH' during setup." }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Die "Node.js is not installed. Get it from nodejs.org (the LTS version)." }

# --- backend ------------------------------------------------------------------
Say "Setting up the audit engine (first run takes a minute or two)..."
Set-Location "$Root\backend"

if (-not (Test-Path "venv")) { & $python.Source -m venv venv }
$venvPython = "$Root\backend\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Die "Could not create the Python environment in backend\venv." }

& $venvPython -m pip install -q --upgrade pip
& $venvPython -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die "Installing Python dependencies failed. Check your internet connection." }

# --- mint a key for this session ---------------------------------------------
Say "Minting an API key for this session..."
$apiKey = "cl_demo_" + (& $venvPython -c "import secrets; print(secrets.token_urlsafe(18))").Trim()
$apiKeyHash = (& $venvPython -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" $apiKey).Trim()

# --- frontend -----------------------------------------------------------------
Set-Location "$Root\frontend"
@"
# Written by run.ps1. Regenerated on every run.
CLOUDLEAK_API_BASE_URL=http://localhost:$BackendPort
CLOUDLEAK_API_KEY=$apiKey
"@ | Set-Content -Path ".env.local" -Encoding ASCII

if (-not (Test-Path "node_modules")) {
  Say "Installing web app dependencies (first run only)..."
  cmd /c "npm install --no-audit --no-fund"
  if ($LASTEXITCODE -ne 0) { Die "npm install failed. Check your internet connection." }
}

# --- launch both in their own windows ----------------------------------------
Say "Starting the audit engine on port $BackendPort..."
$backendCmd = "set CLOUDLEAK_API_KEY_HASHES=$apiKeyHash && " +
              "set CLOUDLEAK_ALLOWED_ORIGINS=http://localhost:$FrontendPort && " +
              "`"$Root\backend\venv\Scripts\uvicorn.exe`" main:app --port $BackendPort"
Start-Process cmd -ArgumentList "/k", $backendCmd -WorkingDirectory "$Root\backend"

Start-Sleep -Seconds 6

Say "Starting the web app on port $FrontendPort..."
Start-Process cmd -ArgumentList "/k", "npm run dev -- --port $FrontendPort" -WorkingDirectory "$Root\frontend"

Start-Sleep -Seconds 12

Write-Host @"

  CloudLeak is running.

    Demo this:     http://localhost:$FrontendPort
    API docs:      http://localhost:$BackendPort/docs
    Sample files:  $Root\samples\

  Drag samples\azure_cost_export.csv onto the page to run an audit.

  Two command windows opened. Close them both to stop CloudLeak.
  If the page does not load yet, wait 20 seconds and refresh -- the web app
  compiles on first visit.

"@ -ForegroundColor Green

Start-Process "http://localhost:$FrontendPort"
Read-Host "Press Enter to close this window (CloudLeak keeps running)"
