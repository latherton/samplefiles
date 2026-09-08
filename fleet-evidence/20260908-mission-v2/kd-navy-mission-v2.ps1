$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# App-only update of this exact, already-paused mission. No source or KD writes.
$missionExpectedRun = '93bf75145eaa4a2c9b73ddbdbeaaafab'
$missionArchive = 'D:\KDDeployment\navy-demo-mission-v2.zip'
$missionArchiveHash = '76be14c711e4990bec8d67faa5e045bf1f8008f6170909f06372bec58efb3b82'
$missionRunner = 'D:\KDDeployment\NavyDemo\mission-update-runner-v2\deploy_mission_update.py'
$missionRunnerHash = 'fca3167ea970dea0e87e228accb153ade665298bb3093a86bb1ab5cac1486d35'
$missionPrefix = 'D:\KDDeployment\navy-mission-v2'
$missionReport = 'D:\KDDeployment\navy-mission-20260908-mission-v2.json'

function Get-MissionBaseline($mission) {
    if ($mission.available -ne $true -or $mission.run.state -ne 'paused' -or
        $mission.run.id -ne $missionExpectedRun -or $mission.run.source_id -ne 'mission-pump-feed') {
        throw 'The exact existing mission must be paused before this update.'
    }
    $missionRows = @($mission.assets | Sort-Object asset_id)
    if ($missionRows.Count -ne 2 -or (($missionRows.asset_id) -join ',') -ne 'A-17,A-18') {
        throw 'Both paused mission assets are required.'
    }
    $missionPreservedRows = @(foreach ($missionRow in $missionRows) {
        if ($missionRow.delivery.state -ne 'verified' -or @($missionRow.delivery.pending_index_ids).Count -ne 0 -or
            $missionRow.delivery.error -or -not $missionRow.telemetry -or
            $missionRow.telemetry.snapshot_id -notmatch '^[0-9a-f]{64}$' -or
            $missionRow.telemetry.source.through_kd -ne $true -or $missionRow.telemetry.synthetic -ne $true -or
            $missionRow.telemetry.source.label -ne 'mission-pump-feed') {
            throw 'Finish retained KD publication and case-callback reconciliation before this update.'
        }
        [ordered]@{
            asset_id = $missionRow.asset_id
            scenario = $missionRow.scenario
            outcome = $missionRow.outcome
            step = $missionRow.step
            acquired_samples = $missionRow.acquired_samples
            last_observed_at = $missionRow.last_observed_at
            latest_sample = $missionRow.latest_sample
            delivery = $missionRow.delivery
            telemetry = $missionRow.telemetry
        }
    })
    return [ordered]@{
        run = [ordered]@{ id=$mission.run.id; state=$mission.run.state; source_id=$mission.run.source_id; step=$mission.run.step }
        total_history_samples = $mission.total_history_samples
        case_counts = [ordered]@{ open=$mission.case_counts.open; reviewed=$mission.case_counts.reviewed; closed=$mission.case_counts.closed }
        assets = $missionPreservedRows
    }
}

function Assert-MissionHealth($health) {
    if ($health.ok -ne $true -or $health.indexed_records -ne 48 -or $health.database -ne 'FLEET_EVIDENCE_DEMO') {
        throw 'The Windows application route must have verified 48-record evidence health.'
    }
}

$missionFreshPaths = @(
    "$missionPrefix.preflight.stdout.log", "$missionPrefix.preflight.stderr.log",
    "$missionPrefix.stdout.log", "$missionPrefix.stderr.log", "$missionPrefix.windows.json", $missionReport
)
foreach ($missionPath in $missionFreshPaths) {
    if (Test-Path -LiteralPath $missionPath) { throw 'A retained v2 attempt exists. Inspect its receipts; do not overwrite or replay it.' }
}
if ((Get-FileHash -LiteralPath $missionArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $missionArchiveHash -or
    (Get-FileHash -LiteralPath $missionRunner -Algorithm SHA256).Hash.ToLowerInvariant() -ne $missionRunnerHash) {
    throw 'The prepared archive or updater differs from its reviewed SHA-256.'
}
$missionDistros = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | Get-ItemProperty | Where-Object DistributionName -eq 'Ubuntu')
if ($missionDistros.Count -ne 1) { throw 'Existing Ubuntu owner required.' }
$missionBase = [IO.Path]::GetFullPath($missionDistros[0].BasePath.Replace('\\?\', '')).TrimEnd('\')
if ($missionBase -ne 'D:\KDDeployment\Ubuntu') { throw 'Unexpected Ubuntu deployment path.' }
$missionBeforeHealth = Invoke-RestMethod -Uri 'http://localhost:8095/api/health' -TimeoutSec 20
Assert-MissionHealth $missionBeforeHealth
$missionBefore = Get-MissionBaseline (Invoke-RestMethod -Uri 'http://localhost:8095/api/mission' -TimeoutSec 20)
$missionBeforeJson = $missionBefore | ConvertTo-Json -Depth 30 -Compress

# Inspect only the current app working directory; never dump service environment.
$missionPreflight = Start-Process -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList @('-d','Ubuntu','-u','root','--exec','systemctl','show','fleet-evidence-demo.service','--property=WorkingDirectory','--value') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput "$missionPrefix.preflight.stdout.log" -RedirectStandardError "$missionPrefix.preflight.stderr.log"
if ($missionPreflight.ExitCode -ne 0 -or (Get-Content -LiteralPath "$missionPrefix.preflight.stdout.log" -Raw).Trim() -ne '/mnt/d/KDDeployment/NavyDemo/releases/20260908-mission-v1') {
    throw 'The active app is not the exact expected mission-v1 release; no update was attempted.'
}

$missionRun = Start-Process -FilePath "$env:SystemRoot\System32\wsl.exe" -ArgumentList @('-d','Ubuntu','-u','root','--exec','python3','/mnt/d/KDDeployment/NavyDemo/mission-update-runner-v2/deploy_mission_update.py','--archive','/mnt/d/KDDeployment/navy-demo-mission-v2.zip','--release','20260908-mission-v2','--sha256',$missionArchiveHash) -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput "$missionPrefix.stdout.log" -RedirectStandardError "$missionPrefix.stderr.log"
if ($missionRun.ExitCode -ne 0) { exit $missionRun.ExitCode }
$missionLinux = Get-Content -LiteralPath $missionReport -Raw | ConvertFrom-Json
if ($missionLinux.complete -ne $true -or $missionLinux.release -ne '20260908-mission-v2' -or
    $missionLinux.archive_sha256 -ne $missionArchiveHash -or
    $missionLinux.previous_release -ne '/mnt/d/KDDeployment/NavyDemo/releases/20260908-mission-v1' -or
    $missionLinux.native_index_writes -ne 0 -or $missionLinux.automatic_feed_start -ne $false -or
    $missionLinux.original_databases_preserved -ne $true -or $missionLinux.container_lifecycles_preserved -ne $true -or
    $missionLinux.preserved_container_count -ne 21 -or $missionLinux.corpus_unchanged -ne $true -or
    $missionLinux.mission_acceptance.mission_state -ne 'paused' -or
    $missionLinux.mission_acceptance.existing_history_rows_preserved -ne $true -or
    $missionLinux.mission_acceptance.case_rows_preserved -ne $true) {
    throw 'The retained Linux update receipt did not satisfy the exact app-only preservation checks.'
}
$missionAfterHealth = Invoke-RestMethod -Uri 'http://localhost:8095/api/health' -TimeoutSec 20
Assert-MissionHealth $missionAfterHealth
$missionAfter = Get-MissionBaseline (Invoke-RestMethod -Uri 'http://localhost:8095/api/mission' -TimeoutSec 20)
if (($missionAfter | ConvertTo-Json -Depth 30 -Compress) -cne $missionBeforeJson) {
    throw 'Paused mission, acquired history, delivery, condition or case-count baseline changed.'
}
$missionReceipt = [ordered]@{
    complete=$true; at=[DateTime]::UtcNow.ToString('o'); release='20260908-mission-v2'
    archive_sha256=$missionArchiveHash; updater_sha256=$missionRunnerHash
    linux_receipt=$missionReport; linux_receipt_sha256=(Get-FileHash -LiteralPath $missionReport -Algorithm SHA256).Hash.ToLowerInvariant()
    paused_mission_preserved=$true; native_index_writes=0; automatic_feed_start=$false
    health=$missionAfterHealth; before=$missionBefore; after=$missionAfter
}
$missionReceiptBytes = [Text.Encoding]::UTF8.GetBytes(($missionReceipt | ConvertTo-Json -Depth 30))
$missionReceiptStream = [IO.File]::Open("$missionPrefix.windows.json", [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
try { $missionReceiptStream.Write($missionReceiptBytes, 0, $missionReceiptBytes.Length); $missionReceiptStream.Flush($true) }
finally { $missionReceiptStream.Dispose() }
