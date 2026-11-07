# Remotion studio on the home PC (owner 07.11.2026, doc 147): episode 1 in three templates, opened at http://localhost:3000
# Run in PowerShell:
#   irm https://raw.githubusercontent.com/kobimazuz/kq-media/main/studio/studio_home.ps1 | iex
# It uses its own folder D:\kq\studio, so the Codex service in D:\kq\youtube-ai-channel is not touched.
$ErrorActionPreference = "Stop"
$Dir    = "D:\kq\studio"
$Branch = "claude/remotion-templates-0711"
$Media  = "https://raw.githubusercontent.com/kobimazuz/kq-media/main/studio/e01_01"

if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Write-Host "Node.js is missing: install it from https://nodejs.org (LTS) and run again." -ForegroundColor Red; return }
if (-not (Get-Command git  -ErrorAction SilentlyContinue)) { Write-Host "Git is missing: install it from https://git-scm.com and run again." -ForegroundColor Red; return }
Write-Host "node $(node -v)"

# 1. only the remotion folder of the branch (a separate, light copy)
if (-not (Test-Path "$Dir\.git")) {
  git clone --filter=blob:none --no-checkout --depth 1 --branch $Branch https://github.com/kobimazuz/youtube-ai-channel.git $Dir
  git -C $Dir sparse-checkout set --no-cone /remotion/
  git -C $Dir checkout $Branch
} else {
  git -C $Dir fetch --depth 1 origin $Branch
  git -C $Dir reset --hard "origin/$Branch"
}
Set-Location "$Dir\remotion"

# 2. the episode files (clips, music, end card) into public\e01_01 (a folder, not the Linux link)
$pub = "public\e01_01"
if ((Test-Path $pub) -and -not (Get-Item $pub).PSIsContainer) { Remove-Item $pub -Force }
New-Item -ItemType Directory -Force $pub | Out-Null
$list = (Invoke-WebRequest "$Media/files.txt" -UseBasicParsing).Content -split "`n" | Where-Object { $_.Trim() }
$i = 0
foreach ($f in $list) {
  $i++; $f = $f.Trim(); $out = Join-Path $pub ($f -replace "/", "\")
  if (Test-Path $out) { continue }
  New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
  Write-Host "[$i/$($list.Count)] $f"
  Invoke-WebRequest "$Media/$f" -OutFile $out -UseBasicParsing
}

# 3. install and open the studio (it opens the browser at http://localhost:3000)
npm ci
npx remotion studio --port 3000
