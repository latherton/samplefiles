$ErrorActionPreference = 'Stop'
$sensorDistros = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | Get-ItemProperty | Where-Object DistributionName -eq 'Ubuntu')
if ($sensorDistros.Count -ne 1) { throw 'Existing Ubuntu owner required.' }
$sensorBase = [IO.Path]::GetFullPath($sensorDistros[0].BasePath.Replace('\\?\', '')).TrimEnd('\')
if ($sensorBase -ne 'D:\KDDeployment\Ubuntu') { throw 'Unexpected Ubuntu deployment path.' }
$sensorRun = Start-Process -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList @('-d','Ubuntu','-u','root','--exec','python3','/mnt/d/KDDeployment/NavyDemo/sensor-update-runner-20260908-v2/deploy_sensor_update.py','--archive','/mnt/d/KDDeployment/navy-demo-sensors-v2.zip','--release','20260908-sensors-v2','--sha256','1b995a5e87398c894e7cffb26454f4a5957ba31d648c801c1967b22497b411a6') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput 'D:\KDDeployment\navy-sensors-v2.stdout.log' -RedirectStandardError 'D:\KDDeployment\navy-sensors-v2.stderr.log'
if ($sensorRun.ExitCode -ne 0) { exit $sensorRun.ExitCode }
$sensorHealth = Invoke-RestMethod -Uri 'http://localhost:8095/api/health' -TimeoutSec 20
if (-not $sensorHealth.ok -or $sensorHealth.indexed_records -ne 48) { throw 'Windows browser route did not pass health.' }
$sensorSources = Invoke-RestMethod -Uri 'http://localhost:8095/api/telemetry/sources' -TimeoutSec 20
if (-not $sensorSources.available -or $sensorSources.database -ne 'FLEET_SENSOR_DEMO') { throw 'Windows sensor source route did not pass.' }
[IO.File]::WriteAllText('D:\KDDeployment\navy-sensors-v2.windows.json', (@{complete=$true;at=[DateTime]::UtcNow.ToString('o');health=$sensorHealth;source_inventory=$sensorSources} | ConvertTo-Json -Depth 10))
