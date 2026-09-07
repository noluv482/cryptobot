# research_pass.ps1 - wrapper the Windows scheduled task 'CryptoResearchPass'
# runs weekly (Sunday 05:30, StartWhenAvailable). Runs research_pass.py with the
# WindowsApps python and logs to data\research_pass.log.
#
#   .\research_pass.ps1              scheduled weekly pass (idempotent per ISO week)
#   .\research_pass.ps1 --dry-run    no ssh writes, no events
#   .\research_pass.ps1 --once       manual single run
#   .\research_pass.ps1 --force      ignore the per-week guard (manual rerun)
#
# Paper-only research: this wrapper starts nothing else, restarts nothing, and
# never touches the bot's process. Python's own log lines go to
# data\research_pass.log (the script writes them); the raw console stream is
# kept in data\research_pass.console.log (truncated past 2 MB).
$ErrorActionPreference = "Continue"
$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$dataDir = Join-Path $root "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir | Out-Null }
$log     = Join-Path $dataDir "research_pass.log"
$console = Join-Path $dataDir "research_pass.console.log"

$py = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Encoding utf8 -Value "$stamp  INF RESEARCH  research_pass.ps1 start (python=$py args=$($args -join ' '))"
if ((Test-Path $console) -and ((Get-Item $console).Length -gt 2MB)) { Clear-Content $console }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
Set-Location $root
& $py (Join-Path $root "research_pass.py") @args *>> $console
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Encoding utf8 -Value "$stamp  INF RESEARCH  research_pass.ps1 exit $code"
exit $code
