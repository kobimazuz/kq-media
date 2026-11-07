# Home agent installer (doc 146). One command in PowerShell:
#   irm https://raw.githubusercontent.com/kobimazuz/kq-media/main/studio/home_agent_install.ps1 | iex
# It: (1) gets the latest agent.py into D:\kq\home_agent, (2) installs the Python packages mcp and requests,
# (3) asks once for the Supabase key and keeps it as a Windows user environment variable (never in git),
# (4) registers a scheduled task that starts the agent at logon and restarts it if it stops, and starts it now.
$ErrorActionPreference = "Stop"
$Dir  = "D:\kq\home_agent"
$Src  = "https://raw.githubusercontent.com/kobimazuz/kq-media/main/studio/home_agent.py"
$Task = "KeptQuiet Home Agent"

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Host "Python is missing: install it from https://www.python.org (tick 'Add to PATH') and run again." -ForegroundColor Red; return }
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "python: $py"

New-Item -ItemType Directory -Force $Dir | Out-Null
Invoke-WebRequest $Src -OutFile "$Dir\agent.py" -UseBasicParsing
& $py -m pip install --quiet --upgrade mcp requests

if (-not [Environment]::GetEnvironmentVariable("KQ_SUPABASE_SERVICE_KEY", "User")) {
  $sec = Read-Host "Paste the Supabase service key (KQ_SUPABASE_SERVICE_KEY) and press Enter" -AsSecureString
  $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  [Environment]::SetEnvironmentVariable("KQ_SUPABASE_SERVICE_KEY", $key.Trim(), "User")
  Write-Host "key saved in your Windows user environment"
}
$env:KQ_SUPABASE_SERVICE_KEY = [Environment]::GetEnvironmentVariable("KQ_SUPABASE_SERVICE_KEY", "User")

# stop an older copy, then (re)register the task: at logon, restart every minute if it stops, no time limit
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*home_agent*agent.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$action   = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Dir\agent.py`"" -WorkingDirectory $Dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $Task
Start-Sleep 20
Write-Host ""
Write-Host "Last lines of the agent log (D:\kq\home_agent.log):"
if (Test-Path "D:\kq\home_agent.log") { Get-Content "D:\kq\home_agent.log" -Tail 8 } else { Write-Host "(no log yet)" }
Write-Host "Done. The agent runs in the background and starts by itself when you log in." -ForegroundColor Green
