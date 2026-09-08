$ErrorActionPreference = 'Stop'
$missionDistros = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | Get-ItemProperty | Where-Object DistributionName -eq 'Ubuntu')
if ($missionDistros.Count -ne 1) { throw 'Existing Ubuntu owner required.' }
$missionBase = [IO.Path]::GetFullPath($missionDistros[0].BasePath.Replace('\\?\', '')).TrimEnd('\')
if ($missionBase -ne 'D:\KDDeployment\Ubuntu') { throw 'Unexpected Ubuntu deployment path.' }
$missionRun = Start-Process -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList @('-d','Ubuntu','-u','root','--exec','python3','/mnt/d/KDDeployment/NavyDemo/mission-update-runner-v1/deploy_mission_update.py','--archive','/mnt/d/KDDeployment/navy-demo-mission-v1.zip','--release','20260908-mission-v1','--sha256','3932b6de50888cf1b637e2107ccef7c2348f2620ba1eee48454490ea35b054e9') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput 'D:\KDDeployment\navy-mission-v1.stdout.log' -RedirectStandardError 'D:\KDDeployment\navy-mission-v1.stderr.log'
if ($missionRun.ExitCode -ne 0) { exit $missionRun.ExitCode }
$missionHealth = Invoke-RestMethod -Uri 'http://localhost:8095/api/health' -TimeoutSec 20
if (-not $missionHealth.ok -or $missionHealth.indexed_records -ne 48) { throw 'Windows browser route failed health.' }
$missionStatus = Invoke-RestMethod -Uri 'http://localhost:8095/api/mission' -TimeoutSec 20
if (-not $missionStatus.available -or $missionStatus.run.state -ne 'idle') { throw 'Fresh mission route did not remain idle.' }
[IO.File]::WriteAllText('D:\KDDeployment\navy-mission-v1.windows.json', (@{complete=$true;at=[DateTime]::UtcNow.ToString('o');health=$missionHealth;mission=$missionStatus} | ConvertTo-Json -Depth 12))
