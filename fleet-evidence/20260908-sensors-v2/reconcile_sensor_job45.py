#!/usr/bin/env python3
"""Reconcile the known zero-document sensor job 45, without submitting data.

This one-shot Linux-root helper retains job/config evidence, optionally sends
one explicitly authorized DRESYNC, and polls the SAME original job to -1/0.
An existing receipt blocks another run; uncertain writes are never retried.
"""
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse

BASE = Path('/mnt/d/KDDeployment')
RELEASE_ID = '20260908-sensors-v2'
STAGED = BASE / 'NavyDemo' / 'releases' / RELEASE_ID
OLD_DETAILS = BASE / 'NavyDemo' / 'receipts' / RELEASE_ID
OLD_RECEIPT = OLD_DETAILS / 'A-17-sensor-index.json'
DETAILS = OLD_DETAILS / 'job45-reconciliation'
RECEIPT = BASE / 'sensor-job45-reconciliation.json'
BODY_SHA256 = 'ce882c5edfad0d7dfd3b1d97043df806a2862898cb58cc2e6453ea0b644f396f'
DATABASE = 'FLEET_SENSOR_DEMO'
UNIT = Path('/etc/systemd/system/fleet-evidence-demo.service')
CONFIG_PATHS = ('/content/cfg/content.cfg', '/content/cfg/original.content.cfg', '/content/cfg/idol.common.cfg')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def main():
    require(sys.platform == 'linux' and os.geteuid() == 0, 'This reviewed reconciliation requires Linux root.')
    require(len(sys.argv) == 1, 'This helper accepts no arguments and reconciles only the retained job 45.')
    deadline = time.monotonic() + 90

    def remaining(cap=5):
        seconds = deadline - time.monotonic()
        require(seconds > 0, 'The 90-second reconciliation bound expired; inspect the retained outcome before further action.')
        return min(cap, seconds)

    def run(argv, allowed=(0,)):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=remaining())
        require(len(result.stdout) < 512 * 1024 and len(result.stderr) < 64 * 1024,
                'A read-only status command exceeded its output bound.')
        require(result.returncode in allowed, Path(argv[0]).name + ' returned unexpected status ' + str(result.returncode) + '.')
        return result

    sys.dont_write_bytecode = True
    for path in (STAGED, OLD_DETAILS, OLD_RECEIPT, DETAILS, RECEIPT):
        for component in (path, *path.parents):
            require(not component.is_symlink(), 'A protected reconciliation path contains a symbolic link.')
    require(STAGED.is_dir() and OLD_RECEIPT.is_file(), 'The original staged release and exact sensor receipt are required.')
    require(not DETAILS.exists() and not RECEIPT.exists(), 'Reconciliation already has a receipt; no retry or overwrite is permitted.')
    sys.path.insert(0, str(STAGED))
    for name in ('deploy_sensor_update', 'kd_client', 'sensor_ingest'):
        require(name not in sys.modules, 'A deployment module was imported before the retained release was selected.')
    deployment = importlib.import_module('deploy_sensor_update')
    kd = importlib.import_module('kd_client')
    ingest = importlib.import_module('sensor_ingest')
    require(all(Path(module.__file__).resolve().parent == STAGED.resolve() for module in (deployment, kd, ingest)),
            'Reconciliation helpers did not load from the retained v2 release.')
    deployment.fresh_write(RECEIPT, b'{}\n')
    state = {'schema': 'fleet.sensor.job45-reconciliation.v1', 'complete': False, 'started_at': now(),
             'original_job_id': 45, 'original_body_sha256': BODY_SHA256, 'document_submissions': 0,
             'service_changes': False, 'old_receipt_changes': False, 'sync': None, 'steps': []}

    def save(stage):
        state['steps'].append({'stage': stage, 'at': now()})
        deployment.atomic_replace(RECEIPT, json.dumps(state, indent=2).encode() + b'\n', 'updating')

    def retain(name, value):
        raw = value if isinstance(value, bytes) else json.dumps(value, indent=2).encode() + b'\n'
        deployment.fresh_write(DETAILS / name, raw)

    def containers():
        ids = run(['docker', 'ps', '-a', '--quiet', '--no-trunc']).stdout.split()
        require(1 <= len(ids) <= 500 and len(ids) == len(set(ids))
                and all(re.fullmatch(r'[0-9a-f]{64}', value) for value in ids), 'Docker inventory has an unexpected shape.')
        raw = run(['docker', 'inspect', '--format', '{{.Id}} {{.State.StartedAt}}', *sorted(ids)]).stdout
        values = {}
        for line in raw.splitlines():
            parts = line.split()
            require(len(parts) == 2 and parts[0] in ids and parts[0] not in values, 'Docker lifecycle response is incomplete.')
            require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\S+Z', parts[1]), 'Docker start time is invalid.')
            values[parts[0]] = parts[1]
        require(set(values) == set(ids), 'Docker lifecycle response omits a container.')
        return values

    client = None
    old_bytes, unit_bytes = None, None
    try:
        DETAILS.mkdir(mode=0o700)
        save('fresh-reconciliation-intent')
        old_bytes = OLD_RECEIPT.read_bytes()
        old = json.loads(old_bytes)
        jobs = old.get('jobs', [])
        require(old.get('database') == DATABASE and old.get('complete') is False
                and old.get('request_sha256') == BODY_SHA256 and len(jobs) == 2,
                'The retained operation is not the expected incomplete sensor attempt.')
        created, added = jobs
        require(created.get('action') == 'DRECREATEDBASE' and created.get('index_id') == 44
                and created.get('complete') is True and created.get('last_status') == -1
                and created.get('documents_processed') == 0, 'The retained database-creation job differs from job 44.')
        require(added.get('action') == 'DREADDDATA' and added.get('index_id') == 45
                and added.get('complete') is False and added.get('last_status') == -34
                and added.get('documents_processed') == 0 and added.get('body_sha256') == BODY_SHA256
                and added.get('commit') is None, 'The retained add-data job differs from the known uncommitted zero-document job 45.')
        require(sha((OLD_DETAILS / 'A-17-snapshot.xml').read_bytes()) == BODY_SHA256,
                'The retained XML request body differs from the original job intent.')
        retain('original-A17-sensor-index.json', old_bytes)
        state['old_receipt_sha256'] = sha(old_bytes)
        deployment.no_symlinks(UNIT)
        unit_bytes = UNIT.read_bytes()
        _, current_release, environment = deployment.inspect_unit(unit_bytes)
        require(current_release != STAGED, 'The app has already switched to v2; this pre-switch reconciliation no longer applies.')
        os.environ.update({key: environment[key] for key in ('KD_CONTENT_URL', 'KD_INDEX_URL')})
        os.environ['KD_SENSOR_DATABASE'] = DATABASE
        client = kd.KDClient(timeout=5)
        request = client.request

        def bounded_request(url, data=None, content_type='application/x-www-form-urlencoded'):
            client.timeout = remaining()
            return request(url, data, content_type)

        client.request = bounded_request
        state['connection_sha256'] = sha((client.aci_url + '\n' + client.index_url).encode())
        require(state['connection_sha256'] == old.get('connection_sha256'), 'The KD connection differs from the retained write intent.')

        def empty_sensor_database():
            result = client.query('*', database=DATABASE, limit=1, print_fields=False)
            require(type(result.get('total')) is int and result['total'] == 0 and result.get('hits') == [],
                    'The sensor database is not verifiably empty; no recovery sync is permitted.')
            return {'database': DATABASE, 'total': 0, 'hits': []}

        def job45(name):
            raw = client.aci('IndexerGetStatus', Index=45, MaxResults=1)
            retain(name, raw)
            root = kd.parse_xml(raw)
            items = [node for node in root.iter() if kd.local(node.tag) == 'item']
            require(len(items) == 1 and kd.value(items[0], 'id') == '45', 'The exact original job 45 was not returned.')
            item = items[0]
            status, count = kd.value(item, 'status'), kd.value(item, 'documents_processed')
            require(status in {'-34', '-1'} and count == '0', 'Job 45 is not the known pending/finished zero-document operation.')
            command = kd.value(item, 'index_command')
            parsed = urllib.parse.urlsplit(command)
            params = {key.casefold(): value for key, value in urllib.parse.parse_qs(parsed.query).items()}
            require(parsed.path.lstrip('/').upper() == 'DREADDDATA' and params.get('dredbname') == [DATABASE],
                    'Job 45 native command does not match the expected sensor add-data action.')
            require(params.get('killduplicates') == ['REFERENCE']
                    and params.get('killduplicatesmatchdbs') == [DATABASE], 'Job 45 native duplicate scope differs from the retained action.')
            return {'index_id': 45, 'status': int(status), 'documents_processed': 0,
                    'description': kd.value(item, 'description'), 'docidrange': kd.value(item, 'docidrange')}

        state['job_before'] = job45('job45-before.xml')
        state['sensor_before'] = empty_sensor_database()
        before_databases = deployment.database_snapshot(client)
        before_containers = containers()
        retain('database-references-before.json', before_databases)
        retain('containers-before.json', before_containers)
        require(before_databases == json.loads((OLD_DETAILS / 'database-references-before.json').read_bytes()),
                'Original database references/counts already differ from the preserved deployment baseline.')
        require(before_containers == json.loads((OLD_DETAILS / 'containers-before.json').read_bytes()),
                'Container lifecycles already differ from the preserved deployment baseline.')
        content_ids = run(['docker', 'ps', '--quiet', '--no-trunc', '--filter',
                           'label=com.docker.compose.project=basic-idol', '--filter',
                           'label=com.docker.compose.service=idol-content']).stdout.split()
        require(len(content_ids) == 1 and content_ids[0] in before_containers,
                'One existing Content container is required for the bounded setting inspection.')
        config_evidence = []
        for path in CONFIG_PATHS:
            exists = run(['docker', 'exec', content_ids[0], 'test', '-f', path], allowed=(0, 1)).returncode == 0
            matches = ''
            if exists:
                matches = run(['docker', 'exec', content_ids[0], 'grep', '-n', '-i', '-E',
                               r'^[[:space:]]*DocumentDelimiterCSVs[[:space:]]*=', path], allowed=(0, 1)).stdout
            config_evidence.append({'path': path, 'exists': exists, 'document_delimiter_lines': matches.splitlines()})
        retain('document-delimiter-config.json', config_evidence)
        state['delimiter_config'] = config_evidence
        save('exact-zero-document-job-and-preservation-baseline-confirmed')
        if state['job_before']['status'] == -34:
            require(remaining(90) >= 15, 'Insufficient remaining time for the one-shot cache reconciliation; nothing was submitted.')
            state['sync'] = {'action': 'DRESYNC', 'intent_at': now(), 'index_id': None,
                             'scope': 'Content-wide pending-cache flush, explicitly authorized; submits no documents.'}
            save('one-authorized-sync-intent')
            state['sync']['index_id'] = ingest.submit_native(client, DATABASE, 'DRESYNC')
            save('sync-native-index-id-retained')
        else:
            state['sync_skipped'] = 'Original job 45 was already finished with zero processed documents.'
        sequence = 0
        while True:
            remaining()
            sequence += 1
            current = job45('job45-poll-%03d.xml' % sequence)
            state['job_after'] = current
            save('original-job45-polled')
            if current['status'] == -1:
                break
            time.sleep(min(1, remaining()))
        state['sensor_after'] = empty_sensor_database()
        after_databases, after_containers = deployment.database_snapshot(client), containers()
        retain('database-references-after.json', after_databases)
        retain('containers-after.json', after_containers)
        require(before_databases == after_databases, 'Original database reference/count preservation failed.')
        require(before_containers == after_containers, 'Docker lifecycle preservation failed.')
        require(OLD_RECEIPT.read_bytes() == old_bytes and UNIT.read_bytes() == unit_bytes,
                'The original sensor receipt or app service unit changed during reconciliation.')
        state.update(complete=True, original_databases_preserved=True, containers_preserved=True,
                     old_receipt_unchanged=True, app_unit_unchanged=True, elapsed_seconds=round(90 - (deadline - time.monotonic()), 2))
        save('original-job45-finished-with-zero-documents-no-data-submitted')
        print(json.dumps({'complete': True, 'receipt': str(RECEIPT), 'original_job_id': 45,
                          'documents_processed': 0, 'sensor_database_records': 0, 'document_submissions': 0}))
        return 0
    except BaseException as error:
        state['complete'] = False
        state['error'] = {'type': type(error).__name__, 'message': str(error)[:500]}
        state['retry_policy'] = 'Retain this receipt and inspect the known job/sync outcome; this script must not be rerun or used to submit documents.'
        if old_bytes is not None:
            state['old_receipt_unchanged'] = OLD_RECEIPT.read_bytes() == old_bytes
        if unit_bytes is not None:
            state['app_unit_unchanged'] = UNIT.read_bytes() == unit_bytes
        save('reconciliation-stopped-no-automatic-retry')
        print(json.dumps({'complete': False, 'receipt': str(RECEIPT), 'sync_index_id': (state.get('sync') or {}).get('index_id')}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
