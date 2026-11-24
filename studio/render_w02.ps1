# רינדור פרק 2 המלא בחלקים + חיבור + הדבקת קול. הרצה: irm https://raw.githubusercontent.com/kobimazuz/kq-media/main/studio/render_w02.ps1 | iex
$ErrorActionPreference = "Stop"
$Total = 21822; $Step = 4000
$Root = "D:\kq\studio\remotion"; $Out = "D:\kq\parts"
New-Item -ItemType Directory -Force $Out | Out-Null
Set-Location $Root
$n = 0
for ($a = 0; $a -lt $Total; $a += $Step) {
  $b = [Math]::Min($a + $Step - 1, $Total - 1); $f = "$Out\part_{0:D2}.mp4" -f $n
  if (-not (Test-Path $f)) { Write-Host "חלק $n : פריימים $a-$b"; npx remotion render W02-Full $f --muted --frames="$a-$b" --concurrency=3 --timeout=180000 }
  $n++
}
(Get-ChildItem "$Out\part_*.mp4" | Sort-Object Name | ForEach-Object { "file '$($_.FullName -replace '\\','/')'" }) | Set-Content "$Out\list.txt" -Encoding ascii
ffmpeg -y -f concat -safe 0 -i "$Out\list.txt" -c copy "$Out\silent.mp4"
ffmpeg -y -i "$Out\silent.mp4" -i "$Root\public\w02\mix.m4a" -map 0:v -map 1:a -c:v copy -c:a copy -shortest "$Out\W02_FULL.mp4"
Write-Host "מוכן: $Out\W02_FULL.mp4"
