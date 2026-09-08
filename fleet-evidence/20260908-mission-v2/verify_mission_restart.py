#!/usr/bin/env python3
"""Explicit app-only restart proof for a paused, reconciled synthetic episode.

Run as Linux root with --restart-demo-app. Reads application state and KD ACI
GetStatus/Query only, then restarts exactly fleet-evidence-demo.service once.
It never starts/resumes acquisition, indexes data, flushes KD or restarts Docker.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


SERVICE = 'fleet-evidence-demo.service'
UNIT = Path('/etc/systemd/system') / SERVICE
RELEASES = Path('/mnt/d/KDDeployment/NavyDemo/releases')
ORIGIN = 'http://127.0.0.1:8095'
SOURCE = 'mission-pump-feed'
MAX_BODY = 16 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def no_symlinks(path):
    for item in (path, *path.parents):
        require(not item.is_symlink(), 'A protected path traverses a symbolic link.')


def fresh_write(path, raw):
    no_symlinks(path)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())


def command(argv, timeout=30):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    require(len(result.stdout) <= 2 * 1024 * 1024 and len(result.stderr) <= 1024 * 1024,
            'A status command exceeded its output bound.')
    require(result.returncode == 0, Path(argv[0]).name + ' failed with exit code ' + str(result.returncode) + '.')
    return result.stdout.strip()


def property_value(name):
    require(name in {'MainPID', 'FragmentPath', 'DropInPaths', 'WorkingDirectory', 'User', 'Group', 'StateDirectory'},
            'Only reviewed nonsecret service properties may be inspected.')
    return command(['systemctl', 'show', SERVICE, '--property=' + name, '--value'])


def container_snapshot():
    ids = command(['docker', 'ps', '-a', '--quiet', '--no-trunc']).split()
    require(1 <= len(ids) <= 500 and len(set(ids)) == len(ids)
            and all(re.fullmatch(r'[0-9a-f]{64}', ident) for ident in ids), 'Docker inventory could not be verified.')
    raw = command(['docker', 'inspect', '--format', '{{.Id}} {{.State.StartedAt}}', *sorted(ids)])
    result = {}
    for line in raw.splitlines():
        parts = line.split()
        require(len(parts) == 2 and parts[0] in ids and parts[0] not in result
                and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\S+Z', parts[1]), 'Unexpected Docker lifecycle record.')
        result[parts[0]] = parts[1]
    require(set(result) == set(ids), 'Incomplete Docker lifecycle inventory.')
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, message, headers, newurl):
        raise RuntimeError('Unexpected redirect; no other destination followed.')


def read_url(url, json_result=True, timeout=10):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        require(response.status == 200, 'A read-only HTTP check did not succeed.')
        raw = response.read(MAX_BODY + 1)
        content_type = response.headers.get('Content-Type', '')
    require(len(raw) <= MAX_BODY, 'An HTTP response exceeded its bound.')
    if not json_result:
        return raw
    require('application/json' in content_type, 'Expected an application JSON response.')
    value = json.loads(raw)
    require(isinstance(value, dict), 'Expected a JSON object.')
    return value


def app(path, json_result=True, timeout=10):
    require(path.startswith('/api/') and '\\' not in path, 'Unexpected application route.')
    return read_url(ORIGIN + path, json_result, timeout)


def inspect_unit(raw):
    text = raw.decode('utf-8')
    require('\x00' not in text and 'Description=Fleet Evidence synthetic demonstration' in text.splitlines(),
            'The installed unit has an unexpected identity.')
    require(not re.search(r'^\s*(EnvironmentFile|UnsetEnvironment)\s*=', text, re.M),
            'This helper requires explicit service environment settings.')
    required = {'User': 'nobody', 'Group': 'nogroup', 'ProtectSystem': 'strict',
                'StateDirectory': 'fleet-evidence', 'StateDirectoryMode': '0700'}
    for name, expected in required.items():
        require(re.findall(r'^' + name + r'=([^\r\n]+)', text, re.M) == [expected],
                'Unexpected ' + name + ' service setting.')
    working = re.findall(r'^WorkingDirectory=([^\r\n]+)', text, re.M)
    commands = re.findall(r'^ExecStart=([^\r\n]+)', text, re.M)
    require(len(working) == len(commands) == 1, 'Expected one explicit release and application command.')
    release = Path(working[0])
    no_symlinks(release)
    require(release.parent == RELEASES and release.is_dir(), 'The application release is outside the retained release directory.')
    expected = ['/usr/bin/python3', str(release / 'server.py'), '--host', '127.0.0.1', '--port', '8095',
                '--corpus', str(release / 'corpus'), '--static', str(release / 'static')]
    require(shlex.split(commands[0]) == expected, 'The service command differs from the reviewed loopback app.')
    settings = {}
    for line in text.splitlines():
        if line.startswith('Environment='):
            for item in shlex.split(line.partition('=')[2]):
                name, separator, value = item.partition('=')
                if name in {'KD_CONTENT_URL', 'FLEET_STATE_DIR'}:
                    require(separator and name not in settings, 'A required setting is duplicated or malformed.')
                    settings[name] = value
    require(settings.get('FLEET_STATE_DIR') == '/var/lib/fleet-evidence', 'The persistent application state path changed.')
    endpoint = settings.get('KD_CONTENT_URL', '')
    parsed = urllib.parse.urlsplit(endpoint)
    require(parsed.scheme == 'http' and parsed.hostname and parsed.port and not parsed.username and not parsed.password
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment, 'The configured Content endpoint is not a bare internal HTTP origin.')
    if parsed.hostname != 'localhost':
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            raise RuntimeError('The configured Content endpoint must be an explicit private or loopback address.')
        require(address.is_private or address.is_loopback, 'The configured Content endpoint is not internal.')
    return release, endpoint.rstrip('/')


def local(tag):
    return tag.rsplit('}', 1)[-1].lower()


def children(node, name):
    return [item for item in node if local(item.tag) == name]


def value(node, name):
    items = children(node, name)
    return (items[0].text or '').strip() if len(items) == 1 else ''


def read_aci(endpoint, action, **parameters):
    require(action in ('GetStatus', 'Query'), 'Only read-only KD inventory actions are supported.')
    raw = read_url(endpoint + '/?' + urllib.parse.urlencode({'Action': action, **parameters}), False)
    require(b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(), 'Unsupported KD XML declarations.')
    root = ET.fromstring(raw)
    require(local(root.tag) == 'autnresponse' and value(root, 'response').upper() == 'SUCCESS', 'KD inventory request did not succeed.')
    return root


def database_snapshot(endpoint):
    root = read_aci(endpoint, 'GetStatus')
    names = [value(node, 'name') for node in root.iter() if local(node.tag) == 'database']
    require(names and all(names) and len(names) == len(set(names)), 'The KD database inventory is incomplete or ambiguous.')
    require({'FLEET_EVIDENCE_DEMO', 'FLEET_SENSOR_DEMO'}.issubset(names), 'The expected demo databases were not found.')
    result = {}
    for database in sorted(names):
        root = read_aci(endpoint, 'Query', Text='*', DatabaseMatch=database, MaxResults=10000,
                        TotalResults='True', Print='None', Summary='None', Combine='Simple', AnyLanguage='True', MinScore=0)
        data = children(root, 'responsedata')
        require(len(data) == 1, 'KD query response data is missing.')
        total = value(data[0], 'totalhits')
        hits = children(data[0], 'hit')
        require(total.isdigit() and int(total) == len(hits) <= 10000, 'KD reference inventory is truncated.')
        references = [value(hit, 'reference') for hit in hits]
        require(all(references) and all(value(hit, 'database').casefold() == database.casefold() for hit in hits),
                'KD query returned missing references or a different database.')
        result[database] = {'count': int(total), 'references': sorted(references)}
    require(result['FLEET_EVIDENCE_DEMO']['count'] == 48, 'The evidence count changed before restart acceptance.')
    return result


def retained_case(case):
    # Latest is a fresh lookup whose retrieval timestamp may change. Everything
    # else, including every captured provenance timestamp, must stay identical.
    return {key: value for key, value in case.items() if key not in {'latest', 'latest_error'}}


def paused_mission(mission):
    require(mission.get('available') is True and mission.get('run', {}).get('state') == 'paused'
            and mission['run'].get('source_id') == SOURCE
            and re.fullmatch(r'[0-9a-f]{32}', mission['run'].get('id') or ''), 'Pause the expected mission before a restart check.')
    rows = mission.get('assets')
    require(isinstance(rows, list) and {row.get('asset_id') for row in rows} == {'A-17', 'A-18'}, 'The paused source asset inventory is incomplete.')
    require(all(row['delivery']['state'] == 'verified' and not row['delivery'].get('pending_index_ids')
                and not row['delivery'].get('error') and row.get('telemetry') for row in rows),
            'Existing publication/callback work must finish before the app is restarted.')
    return {'run': {key: mission['run'][key] for key in ('id', 'state', 'source_id', 'step')},
            'total_history_samples': mission['total_history_samples'], 'case_counts': mission['case_counts'],
            'assets': [{'asset_id': row['asset_id'], 'acquired_samples': row['acquired_samples'],
                        'last_observed_at': row['last_observed_at'], 'delivery': row['delivery'],
                        'snapshot_id': row['telemetry']['snapshot_id']} for row in sorted(rows, key=lambda row: row['asset_id'])]}


def acquisition_history():
    after, result = 0, []
    for _ in range(25):
        page = app('/api/mission/history?' + urllib.parse.urlencode({'after': after, 'limit': 1000}))
        rows = page.get('readings')
        require(isinstance(rows, list) and len(rows) <= 1000, 'Unexpected acquisition history page.')
        if not rows:
            return result
        ids = [row['id'] for row in rows]
        require(all(type(ident) is int for ident in ids) and ids == sorted(set(ids)) and ids[0] > after
                and page.get('next_cursor') == ids[-1], 'Acquisition history pagination is inconsistent.')
        result.extend(rows)
        after = ids[-1]
    raise RuntimeError('Acquisition history exceeds the 25,000-row restart acceptance bound.')


class RestartVerification:
    def __init__(self, ident, output):
        self.ident, self.path = ident, Path(output).resolve()
        no_symlinks(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh_write(self.path, b'{}\n')
        self.r = {'complete': False, 'passed': False, 'case_id': ident, 'started_at': now(),
                  'restart_service': SERVICE, 'restart_count': 0, 'kd_index_writes': False,
                  'cache_sync_requested': False, 'container_restarts_requested': False,
                  'browser_acceptance': False, 'host_reboot_test': False,
                  'scope': 'One app-service restart and retained synthetic state/API proof; not host reboot, unattended boot or visual browser acceptance.',
                  'stages': [], 'checks': []}
        self.save()

    def save(self):
        self.r['updated_at'] = now()
        temporary = self.path.with_name(self.path.name + '.updating')
        fresh_write(temporary, json.dumps(self.r, indent=2, allow_nan=False).encode() + b'\n')
        os.replace(temporary, self.path)

    def stage(self, name):
        item = {'stage': name, 'at': now()}
        self.r['stages'].append(item)
        self.save()
        print(json.dumps(item), flush=True)

    def check(self, name, result, **details):
        self.r['checks'].append({'name': name, 'passed': bool(result), **details})
        self.save()
        require(result, name)

    def capture(self, endpoint):
        mission = paused_mission(app('/api/mission'))
        case = retained_case(app('/api/cases/' + self.ident))
        require(case.get('id') == self.ident and case.get('status') == 'closed'
                and case.get('episode') == mission['run']['id'] + ':A-17'
                and case.get('source_id') == SOURCE, 'The requested case is not the closed A-17 episode in this paused run.')
        history = acquisition_history()
        require(len(history) == mission['total_history_samples'], 'The complete acquisition history count differs from mission status.')
        exported = app('/api/cases/' + self.ident + '/export', False)
        inventory = app('/api/cases')
        return {'case': case, 'case_sha256': digest(canonical(case)),
                'history': history, 'history_count': len(history), 'history_sha256': digest(canonical(history)),
                'export_sha256': digest(exported), 'export_bytes': len(exported),
                'case_inventory': inventory, 'mission': mission,
                'containers': container_snapshot(), 'databases': database_snapshot(endpoint)}

    def run(self):
        self.stage('checking-paused-state-and-service-contract')
        no_symlinks(UNIT)
        require(UNIT.is_file() and stat.S_ISREG(UNIT.stat().st_mode), 'The application unit is not a regular file.')
        unit_raw = UNIT.read_bytes()
        release, endpoint = inspect_unit(unit_raw)
        require(property_value('FragmentPath') == str(UNIT) and not property_value('DropInPaths'), 'Unexpected service source/drop-ins.')
        require(property_value('WorkingDirectory') == str(release) and property_value('User') == 'nobody'
                and property_value('Group') == 'nogroup' and property_value('StateDirectory') == 'fleet-evidence',
                'Loaded service properties do not match the reviewed unit.')
        require(command(['systemctl', 'is-active', SERVICE]) == 'active', 'The app must already be active before this restart check.')
        before_pid = property_value('MainPID')
        require(before_pid.isdigit() and int(before_pid) > 1, 'The initial app process ID is unavailable.')
        self.r.update(unit_sha256=digest(unit_raw), release=str(release), before_main_pid=int(before_pid),
                      kd_connection_sha256=digest(endpoint.encode()))
        before = self.capture(endpoint)
        self.r['before'] = before
        self.check('expected-existing-21-container-baseline', len(before['containers']) == 21,
                   containers=len(before['containers']))
        self.save()
        # Refuse a concurrent presenter change or an edited unit between capture
        # and restart. Nothing automatically pauses somebody else's active run.
        require(UNIT.read_bytes() == unit_raw and paused_mission(app('/api/mission')) == before['mission'],
                'The app unit or paused source changed during preparation; no restart was attempted.')
        self.stage('restarting-only-fleet-evidence-app')
        self.r['restart_intent_at'] = now()
        self.r['restart_count'] = 1
        self.save()
        command(['systemctl', 'restart', SERVICE], timeout=45)
        self.r['restart_command_returned_at'] = now()
        self.stage('waiting-for-app-health')
        deadline = time.monotonic() + 30
        health = None
        while time.monotonic() < deadline:
            try:
                candidate = app('/api/health', timeout=min(5, max(.1, deadline - time.monotonic())))
                if candidate.get('ok') is True and candidate.get('indexed_records') == 48:
                    health = candidate
                    break
            except Exception:
                pass
            time.sleep(1)
        self.check('app-health-restored-within-30-seconds', health is not None)
        after_pid = property_value('MainPID')
        self.check('app-process-replaced', after_pid.isdigit() and int(after_pid) > 1 and after_pid != before_pid,
                   before=int(before_pid), after=int(after_pid) if after_pid.isdigit() else None)
        self.r['after_main_pid'] = int(after_pid)
        self.stage('verifying-retained-case-history-export-and-runtime')
        after = self.capture(endpoint)
        self.r['after'] = after
        for key, label in (('case', 'entire-retained-case-unchanged'), ('history', 'complete-acquisition-history-unchanged'),
                           ('export_sha256', 'case-export-bytes-unchanged'), ('case_inventory', 'case-inventory-unchanged'),
                           ('mission', 'same-paused-run-and-delivery-state'), ('containers', 'container-identities-and-start-times-unchanged'),
                           ('databases', 'all-kd-database-references-and-counts-unchanged')):
            self.check(label, before[key] == after[key])
        self.check('service-unit-bytes-and-release-unchanged', UNIT.read_bytes() == unit_raw
                   and property_value('WorkingDirectory') == str(release) and property_value('FragmentPath') == str(UNIT)
                   and not property_value('DropInPaths'))
        self.check('existing-runtime-inventory-count', len(after['containers']) == len(before['containers']),
                   containers=len(after['containers']), database_counts={name: value['count'] for name, value in after['databases'].items()})
        self.r.update(complete=True, passed=True, completed_at=now())
        self.stage('app-restart-persistence-accepted')

    def execute(self):
        try:
            self.run()
            return 0
        except BaseException as error:
            self.r.update(complete=False, passed=False, error={'type': type(error).__name__, 'message': str(error)[:500]},
                          failure_policy='No retry, unit rewrite, second restart or database action. Inspect the retained receipt and current service state.')
            self.save()
            print(json.dumps({'complete': False, 'report': str(self.path), 'error': self.r['error'],
                              'restart_attempted': bool(self.r['restart_count'])}), flush=True)
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restart-demo-app', action='store_true')
    parser.add_argument('--case-id', required=True)
    parser.add_argument('--output', type=Path, required=True, help='Fresh private JSON restart receipt.')
    args = parser.parse_args()
    if not args.restart_demo_app:
        parser.error('--restart-demo-app is required; this check restarts the app service once.')
    if not re.fullmatch(r'FE-[A-F0-9]{12}', args.case_id):
        parser.error('--case-id must identify the exact retained FE- case.')
    require(sys.platform == 'linux' and os.geteuid() == 0, 'Run the reviewed restart proof as Linux root.')
    return RestartVerification(args.case_id, args.output).execute()


if __name__ == '__main__':
    raise SystemExit(main())
