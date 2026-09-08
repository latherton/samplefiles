$ErrorActionPreference = 'Stop'
$sensorDistros = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | Get-ItemProperty | Where-Object DistributionName -eq 'Ubuntu')
if ($sensorDistros.Count -ne 1) { throw 'Existing Ubuntu owner required.' }
$sensorBase = [IO.Path]::GetFullPath($sensorDistros[0].BasePath.Replace('\\?\', '')).TrimEnd('\')
if ($sensorBase -ne 'D:\KDDeployment\Ubuntu') { throw 'Unexpected Ubuntu deployment path.' }
$sensorRun = Start-Process -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList @('-d','Ubuntu','-u','root','--exec','python3','/mnt/d/KDDeployment/reconcile_sensor_job45.py') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput 'D:\KDDeployment\sensor-job45-reconciliation.stdout.log' -RedirectStandardError 'D:\KDDeployment\sensor-job45-reconciliation.stderr.log'
exit $sensorRun.ExitCode
