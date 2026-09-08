$ErrorActionPreference = 'Stop'
$sensorDistros = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | Get-ItemProperty | Where-Object DistributionName -eq 'Ubuntu')
if ($sensorDistros.Count -ne 1) { throw 'Existing Ubuntu owner required.' }
$sensorBase = [IO.Path]::GetFullPath($sensorDistros[0].BasePath.Replace('\\?\', '')).TrimEnd('\')
if ($sensorBase -ne 'D:\KDDeployment\Ubuntu') { throw 'Unexpected Ubuntu deployment path.' }
$sensorStdout = 'D:\KDDeployment\sensor-index-diagnostic-v1.stdout.log'
$sensorStderr = 'D:\KDDeployment\sensor-index-diagnostic-v1.stderr.log'
foreach ($sensorPath in @($sensorStdout, $sensorStderr, 'D:\KDDeployment\sensor-index-diagnostic-v1.json')) {
    if (Test-Path -LiteralPath $sensorPath) { throw 'Prior diagnostic outputs must be retained; do not rerun.' }
}
$sensorRun = Start-Process -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList @('-d','Ubuntu','-u','root','--exec','python3','/mnt/d/KDDeployment/diagnose_sensor_index.py') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $sensorStdout -RedirectStandardError $sensorStderr
exit $sensorRun.ExitCode
