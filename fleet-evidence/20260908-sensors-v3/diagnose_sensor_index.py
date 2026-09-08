#!/usr/bin/env python3
"""Read-only diagnosis of sensor index jobs 45/46; no index or service writes."""
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time

BASE = Path('/mnt/d/KDDeployment')
STAGED = BASE / 'NavyDemo/releases/20260908-sensors-v3'
REPORT = BASE / 'sensor-index-diagnostic-v1.json'
DETAILS = BASE / 'sensor-index-diagnostic-v1'
UNIT = Path('/etc/systemd/system/fleet-evidence-demo.service')
DATABASE = 'FLEET_SENSOR_DEMO'
CONFIGS = ('/content/cfg/content.cfg', '/content/cfg/original.content.cfg', '/content/cfg/idol.common.cfg')
# Section/inheritance markers and only indexing/field-selection settings.
CONFIG_PATTERN = (r'^[[:space:]]*(\[[^]]+\]|<|'
                  r'(DocumentDelimiter[^=]*|IndexFields|IndexFieldCSVs|ReferenceField[^=]*|ReferenceType|'
                  r'TitleField[^=]*|TitleType|Min[^=]*Words|Discard[^=]*Doc[^=]*|XML[^=]*|'
                  r'FieldCheck[^=]*|CantHave[^=]*|MustHave[^=]*|SectionField[^=]*|'
                  r'PropertyFieldCSVs|Property|Index|SourceType)[[:space:]]*=)')
SECRET = re.compile(r'password|passwd|secret|authorization|api[_-]?key|access[_-]?token|bearer\s', re.I)
LOG_RELEVANT = re.compile(r'(?<!\d)(?:45|46)(?!\d)|sensor|skip|discard|xml|error', re.I)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def clean(line):
    return '[redacted line containing a sensitive marker]' if SECRET.search(line) else line[:2000]


def timestamp_in(line):
    match = re.search(r'\b(20\d{2})[/-](\d{2})[/-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})', line)
    if match:
        try:
            return datetime(*map(int, match.groups()))
        except ValueError:
            return None
    return None


def main():
    require(sys.platform == 'linux' and os.geteuid() == 0 and len(sys.argv) == 1,
            'Run this fixed read-only diagnostic once as Linux root, without arguments.')
    deadline = time.monotonic() + 60

    def remaining(cap=5):
        duration = deadline - time.monotonic()
        require(duration > 0, 'The 60-second diagnostic bound expired; retained partial evidence remains available.')
        return min(cap, duration)

    def run(argv, allowed=(0,)):
        result = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=remaining())
        require(len(result.stdout) <= 256 * 1024 and len(result.stderr) <= 32 * 1024,
                'Diagnostic command output exceeded its bound.')
        require(result.returncode in allowed, Path(argv[0]).name + ' returned unexpected status ' + str(result.returncode) + '.')
        return result

    sys.dont_write_bytecode = True
    for path in (STAGED, REPORT, DETAILS, UNIT):
        for part in (path, *path.parents):
            require(not part.is_symlink(), 'Diagnostic paths must not contain symbolic links.')
    require(STAGED.is_dir() and UNIT.is_file(), 'The retained v3 release and original app unit are required.')
    require(not REPORT.exists() and not DETAILS.exists(), 'Diagnostic receipt/details already exist; do not overwrite them.')
    sys.path.insert(0, str(STAGED))
    require('kd_client' not in sys.modules and 'deploy_sensor_update' not in sys.modules,
            'Load diagnostic helpers only from the retained staged release.')
    kd = importlib.import_module('kd_client')
    helper = importlib.import_module('deploy_sensor_update')
    require(all(Path(module.__file__).resolve().parent == STAGED.resolve() for module in (kd, helper)),
            'The diagnostic imported a helper from an unexpected location.')
    helper.fresh_write(REPORT, b'{}\n')
    state = {'schema': 'fleet.sensor.index-diagnostic.v1', 'complete': False, 'read_only_backend': True,
             'index_writes': 0, 'service_changes': 0, 'started_at': datetime.now(timezone.utc).isoformat(),
             'details_directory': str(DETAILS), 'jobs': {}, 'steps': []}

    def save(stage):
        state['steps'].append(stage)
        helper.atomic_replace(REPORT, json.dumps(state, indent=2).encode() + b'\n', 'updating')

    def retain(name, value):
        raw = value if isinstance(value, bytes) else json.dumps(value, indent=2).encode() + b'\n'
        helper.fresh_write(DETAILS / name, raw)

    try:
        DETAILS.mkdir(mode=0o700)
        original_unit = UNIT.read_bytes()
        _, _, environment = helper.inspect_unit(original_unit)
        os.environ.update({key: environment[key] for key in ('KD_CONTENT_URL', 'KD_INDEX_URL')})
        client = kd.KDClient(timeout=5)
        native_request = client.request

        def bounded_request(url, data=None, content_type='application/x-www-form-urlencoded'):
            # This diagnostic never uses the index endpoint.
            require(url == client.aci_url + '/', 'Diagnostic network access must remain on the existing Content ACI endpoint.')
            client.timeout = remaining()
            return native_request(url, data, content_type)

        client.request = bounded_request
        minimum_time = None
        for ident in (45, 46):
            raw = client.aci('IndexerGetStatus', Index=ident, MaxResults=1)
            retain('job-%d.xml' % ident, raw)
            tree = kd.parse_xml(raw)
            items = [node for node in tree.iter() if kd.local(node.tag) == 'item']
            require(len(items) == 1 and kd.value(items[0], 'id') == str(ident), 'The requested exact index job was not returned.')
            item = items[0]
            # Content can rewrite DREADDDATA to a cached-file DREADD command.
            # Retain that native evidence; do not reject or reinterpret its action.
            state['jobs'][str(ident)] = {name: clean(kd.value(item, name)) for name in
                ('id', 'status', 'description', 'received_time', 'start_time', 'end_time',
                 'documents_processed', 'documents_deleted', 'percentage_processed', 'docidrange', 'index_command')}
            stamp = timestamp_in(kd.value(item, 'received_time'))
            if stamp and (minimum_time is None or stamp < minimum_time):
                minimum_time = stamp
        save('exact-job45-and-job46-native-evidence-retained')
        raw = client.aci('Query', Text='*', DatabaseMatch=DATABASE, MaxResults=10, TotalResults='True',
                         Print='None', Summary='None', Combine='Simple', AnyLanguage='True', MinScore=0)
        retain('sensor-database-query.xml', raw)
        parsed = kd.parse_hits(raw)
        require(all(hit['database'].casefold() == DATABASE.casefold() for hit in parsed['hits']), 'Sensor query returned an out-of-scope hit.')
        state['sensor_database'] = {'database': DATABASE, 'total': parsed['total'],
                                    'references': [hit['reference'] for hit in parsed['hits']],
                                    'returned_hits': len(parsed['hits']), 'empty': parsed['total'] == 0}
        content_ids = run(['docker', 'ps', '--quiet', '--no-trunc', '--filter',
                          'label=com.docker.compose.project=basic-idol', '--filter',
                          'label=com.docker.compose.service=idol-content']).stdout.split()
        require(len(content_ids) == 1 and re.fullmatch(r'[0-9a-f]{64}', content_ids[0]), 'One existing Content container is required.')
        content_id = content_ids[0]
        state['content_container_id'] = content_id
        configuration = []
        for path in CONFIGS:
            exists = run(['docker', 'exec', content_id, 'test', '-f', path], allowed=(0, 1)).returncode == 0
            lines = []
            if exists:
                lines = run(['docker', 'exec', content_id, 'grep', '-n', '-i', '-E', CONFIG_PATTERN, path], allowed=(0, 1)).stdout.splitlines()
            configuration.append({'path': path, 'exists': exists, 'selected_lines': [clean(line) for line in lines]})
        retain('selected-indexing-configuration.json', configuration)
        state['configuration'] = configuration
        save('sensor-query-and-selected-configuration-retained')
        cached_bodies = []
        for ident in (45, 46):
            path = '/content/index/status/%d.data' % ident
            exists = run(['docker', 'exec', content_id, 'test', '-f', path], allowed=(0, 1)).returncode == 0
            detail = {'job_id': ident, 'path': path, 'exists': exists}
            if exists:
                digest_line = run(['docker', 'exec', content_id, 'sha256sum', path]).stdout.strip()
                count_line = run(['docker', 'exec', content_id, 'wc', '-c', path]).stdout.strip()
                detail.update(sha256=digest_line.split()[0], bytes=int(count_line.split()[0]),
                              first_120_bytes=clean(run(['docker', 'exec', content_id, 'head', '-c', '120', path]).stdout),
                              last_100_bytes=clean(run(['docker', 'exec', content_id, 'tail', '-c', '100', path]).stdout))
                detail['matches_authored_post_body'] = (detail['bytes'] == 13108 and detail['sha256'] ==
                    'ce882c5edfad0d7dfd3b1d97043df806a2862898cb58cc2e6453ea0b644f396f')
            cached_bodies.append(detail)
        retain('cached-request-bodies.json', cached_bodies)
        state['cached_request_bodies'] = cached_bodies
        save('cached-job45-and-job46-body-boundaries-retained')
        found = run(['docker', 'exec', content_id, 'find', '/content', '-maxdepth', '3', '-type', 'f',
                     '(', '-iname', '*index*.log', '-o', '-iname', '*index*.txt', ')']).stdout.splitlines()
        require(len(found) <= 40, 'Index log path list exceeds the bounded diagnostic inventory.')
        for path in found:
            parts = PurePosixPath(path)
            require(parts.is_absolute() and parts.parts[:2] == ('/', 'content') and '..' not in parts.parts,
                    'Index log discovery returned an unexpected path.')
        retain('index-log-paths.json', sorted(found))
        logs = []
        # Tail only a bounded set; record all discovered paths for any later targeted read.
        for path in sorted(found)[:8]:
            lines = run(['docker', 'exec', content_id, 'tail', '-n', '80', path]).stdout.splitlines()
            selected = []
            for line in lines:
                if not LOG_RELEVANT.search(line):
                    continue
                stamp = timestamp_in(line)
                if stamp and minimum_time and stamp < minimum_time:
                    continue
                selected.append({'line': clean(line), 'timestamp_recognized': stamp is not None})
            logs.append({'path': path, 'tail_lines_read': len(lines), 'filtered_lines': selected})
        retain('filtered-index-log-tails.json', logs)
        state['logs'] = {'discovered_paths': sorted(found), 'inspected_paths': [entry['path'] for entry in logs],
                         'earliest_job_received_time': minimum_time.isoformat() if minimum_time else None,
                         'filter_note': 'Last 80 lines per inspected log, filtered for job45/46, sensor, skip, discard, XML or error; recognized server timestamps older than job45/46 excluded. Unrecognized timestamps are flagged, not guessed.',
                         'matched_lines': sum(len(entry['filtered_lines']) for entry in logs)}
        require(UNIT.read_bytes() == original_unit, 'The app unit changed during the read-only diagnostic.')
        state.update(complete=True, app_unit_unchanged=True, elapsed_seconds=round(60 - (deadline - time.monotonic()), 2))
        save('read-only-index-diagnostic-complete')
        print(json.dumps({'complete': True, 'receipt': str(REPORT), 'details': str(DETAILS),
                          'sensor_records': parsed['total'], 'index_writes': 0, 'matched_log_lines': state['logs']['matched_lines']}))
        return 0
    except BaseException as error:
        state['error'] = {'type': type(error).__name__, 'message': clean(str(error))[:400]}
        save('diagnostic-stopped-partial-evidence-retained')
        print(json.dumps({'complete': False, 'receipt': str(REPORT), 'details': str(DETAILS), 'index_writes': 0}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
