# Windows HTTP acceptance only: no indexing, deployment, service or device calls.
# Compatible with Windows PowerShell 5.1. Existing acceptance artifacts are never overwritten.
$ErrorActionPreference = 'Stop'
$sensorOrigin = 'http://localhost:8095'
$sensorReceiptPath = 'D:\KDDeployment\navy-sensor-http-v2.json'
$sensorHtmlPath = 'D:\KDDeployment\NavyDemo\sensor-review-v2.html'
$sensorExpectedSnapshot = '27d252e825241f8361fa380d496584eb1039333e9ccea0924c08ced209c08179'
$sensorExpectedPayload = '7678d9d4bb430eb85e7eebed22d0fde5f5635e216409b956a20d4a31c3fc9edd'
$sensorChecks = New-Object 'System.Collections.Generic.List[object]'
$sensorUtf8 = New-Object System.Text.UTF8Encoding($false)
$script:sensorStage = 'Initialize acceptance'

function Assert-SensorCheck([bool]$Passed, [string]$Name, [string]$Detail = '') {
    $sensorChecks.Add([ordered]@{name=$Name; passed=$Passed; detail=$Detail})
    if (-not $Passed) { throw ('Acceptance check failed: ' + $Name) }
}

function Invoke-SensorHttp([string]$Path, [string]$Method = 'GET', [object]$Data = $null) {
    if (-not $Path.StartsWith('/api/') -or $Method -notin @('GET','POST') -or ($Method -eq 'POST' -and $Path -ne '/api/packet')) {
        throw 'The acceptance script attempted an unsupported HTTP route.'
    }
    $script:sensorStage = $Method + ' ' + $Path
    $request = [System.Net.HttpWebRequest]::Create($sensorOrigin + $Path)
    $request.Method = $Method
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.KeepAlive = $false
    $request.Timeout = 45000
    $request.ReadWriteTimeout = 45000
    if ($Method -eq 'POST') {
        $bytes = $sensorUtf8.GetBytes((ConvertTo-Json -InputObject $Data -Depth 12 -Compress))
        $request.ContentType = 'application/json; charset=utf-8'
        $request.ContentLength = $bytes.Length
        $upload = $request.GetRequestStream()
        try { $upload.Write($bytes, 0, $bytes.Length) } finally { $upload.Dispose() }
    }
    $response = $null
    try {
        try { $response = $request.GetResponse() }
        catch [System.Net.WebException] {
            if ($null -eq $_.Exception.Response) { throw ('HTTP transport failed: ' + $_.Exception.Status) }
            $response = $_.Exception.Response
        }
        if ($response.ContentLength -gt 2097152) { throw 'HTTP response exceeds the 2 MiB acceptance bound.' }
        $stream = $response.GetResponseStream()
        $memory = New-Object System.IO.MemoryStream
        try {
            $buffer = New-Object byte[] 8192
            while (($count = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                if ($memory.Length + $count -gt 2097152) { throw 'HTTP response exceeds the 2 MiB acceptance bound.' }
                $memory.Write($buffer, 0, $count)
            }
            return [pscustomobject]@{Status=[int]$response.StatusCode; ContentType=$response.ContentType;
                Disposition=$response.Headers['Content-Disposition']; Body=$sensorUtf8.GetString($memory.ToArray())}
        } finally { $stream.Dispose(); $memory.Dispose() }
    } finally { if ($null -ne $response) { $response.Dispose() } }
}

function Get-SensorJson([string]$Path) {
    $response = Invoke-SensorHttp $Path
    Assert-SensorCheck ($response.Status -eq 200) ('GET ' + $Path) ('HTTP ' + $response.Status)
    if ($response.ContentType -notlike 'application/json*') { throw 'A sensor endpoint returned a non-JSON content type.' }
    return ($response.Body | ConvertFrom-Json)
}

function Write-SensorReceipt {
    $sensorReport.checks = @($sensorChecks.ToArray())
    $sensorReport.passed_checks = @($sensorChecks | Where-Object {$_.passed}).Count
    $sensorReport.checked_at = [DateTimeOffset]::UtcNow.ToString('o')
    $bytes = $sensorUtf8.GetBytes((ConvertTo-Json -InputObject $sensorReport -Depth 12))
    $sensorReceiptStream.Position = 0
    $sensorReceiptStream.SetLength(0)
    $sensorReceiptStream.Write($bytes, 0, $bytes.Length)
    $sensorReceiptStream.Flush($true)
}

foreach ($path in @($sensorReceiptPath, $sensorHtmlPath)) {
    if (Test-Path -LiteralPath $path) { throw ('Fresh acceptance path already exists: ' + $path) }
    if (-not [System.IO.Directory]::Exists([System.IO.Path]::GetDirectoryName($path))) { throw 'The sensor release output directories must exist before acceptance.' }
}
$sensorReceiptStream = [System.IO.File]::Open($sensorReceiptPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
$sensorReport = [ordered]@{schema='fleet.sensor.http-acceptance.v2'; complete=$false; started_at=[DateTimeOffset]::UtcNow.ToString('o');
    origin=$sensorOrigin; kind='Windows loopback HTTP acceptance'; read_only_backend=$true; packet_path=$sensorHtmlPath;
    checks=@(); passed_checks=0; checked_at=$null; snapshot_id=$null; payload_sha256=$null; packet_sha256=$null; error=$null}
$sensorExitCode = 1
try {
    Write-SensorReceipt
    foreach ($case in @(
        @{scenario='degradation';step=80;status='watch';forecast=$true},
        @{scenario='load_change';step=80;status='normal';forecast=$false},
        @{scenario='sensor_gap';step=80;status='insufficient';forecast=$false},
        @{scenario='normal';step=80;status='normal';forecast=$false},
        @{scenario='degradation';step=180;status='review';forecast=$false}
    )) {
        $reading = Get-SensorJson ('/api/telemetry?asset=A-17&mode=simulation&scenario=' + $case.scenario + '&step=' + $case.step)
        $label = $case.scenario + ' minute ' + $case.step
        Assert-SensorCheck ($reading.synthetic -eq $true -and $reading.source.mode -eq 'simulation' -and $reading.source.through_kd -eq $false) ($label + ' simulation origin')
        Assert-SensorCheck ($reading.analysis.status -eq $case.status -and $reading.analysis.forecast.available -eq $case.forecast) ($label + ' condition and forecast')
        if ($case.forecast) {
            Assert-SensorCheck ($reading.analysis.forecast.minutes_to_review -gt 0 -and $reading.analysis.forecast.minutes_to_review -le 120) ($label + ' bounded threshold projection')
        }
    }
    $inventory = Get-SensorJson '/api/telemetry/sources'
    $seed = @($inventory.sources | Where-Object {$_.source_id -eq 'demo-pump-replay'})
    Assert-SensorCheck ($inventory.available -eq $true -and $inventory.database -eq 'FLEET_SENSOR_DEMO' -and $seed.Count -eq 1) 'Seeded source inventory'
    Assert-SensorCheck ($seed[0].synthetic -eq $true -and @($seed[0].asset_ids).Count -eq 2 -and $seed[0].asset_ids -contains 'A-17' -and $seed[0].asset_ids -contains 'A-18') 'Synthetic source covers both assets'
    $snapshot = Get-SensorJson '/api/telemetry?asset=A-17&mode=kd&source=demo-pump-replay'
    Assert-SensorCheck ($snapshot.asset_id -eq 'A-17' -and $snapshot.synthetic -is [bool] -and $snapshot.synthetic -eq $true -and $snapshot.source.synthetic -eq $true -and $snapshot.source.mode -eq 'kd' -and $snapshot.source.through_kd -eq $true -and $snapshot.source.label -eq 'demo-pump-replay') 'KD reading retains exact asset and synthetic origin'
    Assert-SensorCheck ($snapshot.snapshot_id -ceq $sensorExpectedSnapshot -and $snapshot.source.provenance.payload_sha256 -ceq $sensorExpectedPayload) 'Exact seeded snapshot and payload digests'
    Assert-SensorCheck ($snapshot.source.provenance.database -eq 'FLEET_SENSOR_DEMO' -and $snapshot.source.provenance.reference -eq 'urn:fleet-sensor:demo-pump-replay:A-17' -and $snapshot.source.provenance.source_id -eq 'demo-pump-replay' -and $snapshot.source.provenance.asset_id -eq 'A-17' -and $snapshot.source.provenance.synthetic -eq $true -and $snapshot.source.provenance.live -eq $true -and $snapshot.samples.Count -eq 81) 'Verified KD sensor provenance'
    Assert-SensorCheck ($snapshot.analysis.status -eq 'watch' -and $snapshot.analysis.forecast.available -eq $true) 'KD replay condition analysis'
    $sensorReport.snapshot_id = $snapshot.snapshot_id
    $sensorReport.payload_sha256 = $snapshot.source.provenance.payload_sha256

    $condition = @{asset_id='A-17';mode='kd';source_id='demo-pump-replay';snapshot_id=$snapshot.snapshot_id}
    $packetData = @{asset_id='A-17';ids=@('TM-P200-C','WO-219');notes='Synthetic sensor HTTP acceptance; human review remains required.';condition=$condition}
    $packet = Invoke-SensorHttp '/api/packet' 'POST' $packetData
    Assert-SensorCheck ($packet.Status -eq 200 -and $packet.ContentType -like 'text/html*' -and $packet.Disposition -like '*attachment*') 'Draft packet HTML download' ('HTTP ' + $packet.Status)
    foreach ($text in @('Equipment condition snapshot','SIMULATED READINGS','KD Content readback','fleet-demo-condition-v1',
        'urn:fleet-sensor:demo-pump-replay:A-17','urn:fleet-evidence:TM-P200-C','urn:fleet-evidence:WO-219',
        'minutes to the illustrative review threshold',$snapshot.snapshot_id)) {
        Assert-SensorCheck ($packet.Body.Contains($text)) ('Packet retains ' + $text)
    }
    $htmlBytes = $sensorUtf8.GetBytes($packet.Body)
    $htmlStream = [System.IO.File]::Open($sensorHtmlPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try { $htmlStream.Write($htmlBytes,0,$htmlBytes.Length); $htmlStream.Flush($true) } finally { $htmlStream.Dispose() }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $sensorReport.packet_sha256 = ([BitConverter]::ToString($sha.ComputeHash($htmlBytes))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }

    $condition.snapshot_id = '0' * 64
    $badHash = Invoke-SensorHttp '/api/packet' 'POST' $packetData
    $badHashJson = $badHash.Body | ConvertFrom-Json
    Assert-SensorCheck ($badHash.Status -eq 400 -and $badHashJson.error -eq 'invalid_request') 'Changed snapshot hash rejected' ('HTTP ' + $badHash.Status + '; ' + $badHashJson.error)
    $condition.snapshot_id = $snapshot.snapshot_id
    $packetData.asset_id = 'A-18'
    $wrongAsset = Invoke-SensorHttp '/api/packet' 'POST' $packetData
    $wrongAssetJson = $wrongAsset.Body | ConvertFrom-Json
    Assert-SensorCheck ($wrongAsset.Status -eq 400 -and $wrongAssetJson.error -eq 'invalid_request') 'Cross-asset packet rejected' ('HTTP ' + $wrongAsset.Status + '; ' + $wrongAssetJson.error)
    $missing = Invoke-SensorHttp '/api/telemetry?asset=A-17&mode=kd&source=missing-sensor-acceptance-v2'
    $missingJson = $missing.Body | ConvertFrom-Json
    Assert-SensorCheck ($missing.Status -eq 503 -and $missingJson.error -eq 'sensor_snapshot_missing' -and $missingJson.live -eq $false -and $null -eq $missingJson.samples) 'Unknown source fails without simulation fallback' ('HTTP ' + $missing.Status + '; ' + $missingJson.error)
    $sensorReport.complete = $true
    $sensorExitCode = 0
} catch {
    # Retain bounded diagnostics, never raw response bodies, environment or credentials.
    $safeMessage = 'Acceptance request, parsing or artifact retention failed.'
    if ($_.Exception.Message -match '^(Acceptance check failed: |HTTP transport failed: |HTTP response exceeds |A sensor endpoint returned )') {
        $safeMessage = ([string]$_.Exception.Message).Substring(0,[Math]::Min(240,([string]$_.Exception.Message).Length))
    }
    $sensorReport.error = @{type=$_.Exception.GetType().Name; stage=$script:sensorStage; message=$safeMessage}
} finally {
    try { Write-SensorReceipt } finally { $sensorReceiptStream.Dispose() }
}
ConvertTo-Json -InputObject @{complete=$sensorReport.complete;passed_checks=$sensorReport.passed_checks;receipt=$sensorReceiptPath;packet=$sensorHtmlPath;error=$sensorReport.error} -Depth 4 -Compress
exit $sensorExitCode
