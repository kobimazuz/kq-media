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

# stop an older copy, then (re)register the task: at logon it opens a VISIBLE PowerShell window "KQ Home Agent" where
# every job is printed; if the agent stops (or updates itself) the window starts it again after 5 seconds
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*home_agent*agent.py*" -or $_.CommandLine -like "*KQ Home Agent*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$loop = "`$host.UI.RawUI.WindowTitle='KQ Home Agent'; Set-Location '$Dir'; while (`$true) { & '$py' agent.py; Write-Host 'agent stopped, starting again in 5 s'; Start-Sleep 5 }"
$action   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoExit -ExecutionPolicy Bypass -Command `"$loop`"" -WorkingDirectory $Dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $Task
Start-Sleep 15
Write-Host ""
Write-Host "A window titled 'KQ Home Agent' is now open: leave it open (minimize it), every job shows there." -ForegroundColor Green
Write-Host "Log file: D:\kq\home_agent.log"
