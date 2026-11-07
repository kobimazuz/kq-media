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
  git -C $Dir sparse-checkout set --no-cone /remotion/ /assets/fonts/
  git -C $Dir checkout $Branch
} else {
  git -C $Dir sparse-checkout set --no-cone /remotion/ /assets/fonts/
  git -C $Dir fetch --depth 1 origin $Branch
  git -C $Dir reset --hard "origin/$Branch"
}
Set-Location "$Dir\remotion"

# the fonts: in git public\fonts is a Linux link (a small text file on Windows), so the real folder is copied in
if ((Test-Path "public\fonts") -and -not (Get-Item "public\fonts").PSIsContainer) { Remove-Item "public\fonts" -Force }
New-Item -ItemType Directory -Force "public\fonts" | Out-Null
Copy-Item "$Dir\assets\fonts\*" "public\fonts\" -Recurse -Force

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

# 3. install
npm ci

# 4. live updates (owner 07.11.2026: "can't it update by itself when you push?"): a background job fetches the branch every
# 15 s and, when something new arrived, refreshes only remotion\src and remotion\props. The studio reloads on its own.
Get-Job -Name kq-studio-sync -ErrorAction SilentlyContinue | Remove-Job -Force
Start-Job -Name kq-studio-sync -ArgumentList $Dir, $Branch -ScriptBlock {
  param($Dir, $Branch)
  $last = ""
  while ($true) {
    git -C $Dir fetch -q --depth 1 origin $Branch 2>$null
    $head = (git -C $Dir rev-parse FETCH_HEAD 2>$null)
    if ($head -and $head -ne $last) {
      if ($last) { git -C $Dir checkout FETCH_HEAD -- remotion/src remotion/props 2>$null; "updated $(Get-Date -Format HH:mm:ss) $head" }
      $last = $head
    }
    Start-Sleep 15
  }
} | Out-Null
Write-Host "Live updates are on: changes pushed from the cloud appear in the studio by themselves within ~15 s." -ForegroundColor Green

# 5. open the studio (it opens the browser at http://localhost:3000)
npx remotion studio --port 3000
