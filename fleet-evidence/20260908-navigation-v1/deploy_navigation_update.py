#!/usr/bin/env python3
"""Reviewed Linux-root navigation update; only the app release path changes.

The operator supplies an already reviewed archive and its exact SHA-256. This
script preserves the old application unit, corpus and every KD database. If
post-switch acceptance fails, it restores only that application unit/service.
Case/feed state remains outside release directories and survives rollback. The
updater does not start a mission run, create cases, import files or flush KD caches.
All existing SQLite schemas and rows, including synthetic onboarding acceptance
records and BLOB originals, must remain unchanged. No new state database is
permitted. Started imports must be settled before this preservation-only update.
Runtime Python and data contracts must match the accepted equipment-case release.
Navigation HTML and all referenced scripts/styles are checked through GET against
the reviewed archive; interactive browser acceptance is a separate operator check.
"""
import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile

BASE = Path('/mnt/d/KDDeployment')
RELEASES = BASE / 'NavyDemo' / 'releases'
RECEIPTS = BASE / 'NavyDemo' / 'receipts'
UNIT = Path('/etc/systemd/system/fleet-evidence-demo.service')
SERVICE = 'fleet-evidence-demo.service'
SENSOR_DATABASE = 'FLEET_SENSOR_DEMO'
SOURCE = 'demo-pump-replay'
MISSION_SOURCE = 'mission-pump-feed'
APP_STATE = Path('/var/lib/fleet-evidence')
ORIGIN = 'http://127.0.0.1:8095'
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024
RELEASE_NAME = '20260908-navigation-v1'
PREVIOUS_RELEASE_NAME = '20260908-equipment-cases-v1'
IMMUTABLE_RUNTIME = (
    'server.py', 'kd_client.py', 'corpus_model.py', 'index_corpus.py',
    'sensor_kd.py', 'sensor_ingest.py', 'telemetry.py', 'sensor-contract.json',
    'case_store.py', 'mission_feed.py', 'registry_store.py', 'onboarding.py',
    'equipment_cases.py', 'evaluate_prognostics.py', 'evaluate_histories.py',
    'acceptance.json', 'model-evidence.json', 'verify_live.py',
)


def preserve_runtime(old, new):
    """A navigation release cannot silently change an existing backend contract."""
    hashes = {}
    for name in IMMUTABLE_RUNTIME:
        left, right = old / name, new / name
        no_symlinks(left)
        no_symlinks(right)
        require(left.is_file() and right.is_file(), 'An unchanged runtime file is missing.')
        require(left.read_bytes() == right.read_bytes(),
                'Navigation-only update contains a changed runtime file: ' + name)
        hashes[name] = digest(left.read_bytes())
    return hashes


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def no_symlinks(path):
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'A protected deployment path contains a symbolic link.')


def fresh_write(path, raw, mode=0o600):
    no_symlinks(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), mode)
    with os.fdopen(fd, 'wb') as out:
        if hasattr(os, 'fchmod'):
            os.fchmod(out.fileno(), mode)
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())


def atomic_replace(path, raw, suffix, mode=0o600):
    no_symlinks(path)
    temporary = path.with_name(path.name + '.' + suffix + '.tmp')
    fresh_write(temporary, raw, mode)
    os.replace(temporary, path)


def command(argv, timeout=60, env=None):
    # Always argv, never a shell; callers request only nonsecret status fields.
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    require(len(result.stdout) < 2 * 1024 * 1024 and len(result.stderr) < 1024 * 1024,
            'A deployment status command exceeded its output bound.')
    if result.returncode:
        raise RuntimeError(Path(argv[0]).name + ' failed with exit code ' + str(result.returncode) + '.')
    return result.stdout


def container_snapshot():
    ids = command(['docker', 'ps', '-a', '--quiet', '--no-trunc']).split()
    require(1 <= len(ids) <= 500 and len(ids) == len(set(ids))
            and all(re.fullmatch(r'[0-9a-f]{64}', ident) for ident in ids),
            'Cannot verify the bounded Docker container inventory.')
    # Never request the complete inspect object, Config, Environment or secrets.
    raw = command(['docker', 'inspect', '--format', '{{.Id}} {{.State.StartedAt}}', *sorted(ids)])
    result = {}
    for line in raw.splitlines():
        parts = line.split()
        require(len(parts) == 2 and parts[0] in ids and parts[0] not in result,
                'Docker lifecycle status has an unexpected shape.')
        require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\S+Z', parts[1]), 'Docker start timestamp is missing.')
        result[parts[0]] = parts[1]
    require(set(result) == set(ids), 'Docker lifecycle inventory is incomplete.')
    return result


def database_snapshot(client):
    result = {}
    for database in client.databases():
        rows = client.query('*', database=database, limit=10000, print_fields=False)
        require(type(rows.get('total')) is int and rows['total'] == len(rows.get('hits', [])),
                'Existing KD database exceeds the complete 10,000-record preservation bound.')
        references = [row.get('reference') for row in rows['hits']]
        require(all(isinstance(ref, str) and ref for ref in references), 'An existing KD reference is missing.')
        result[database] = {'count': rows['total'], 'references': sorted(set(references))}
    require('FLEET_EVIDENCE_DEMO' in result, 'The existing Fleet Evidence database was not found.')
    require(SENSOR_DATABASE in result, 'The already-seeded sensor database was not found; this updater never creates or seeds it.')
    return result


def corpus_snapshot(root):
    no_symlinks(root)
    require(root.is_dir() and (root / 'manifest.json').is_file(), 'The source corpus is missing.')
    records, total = {}, 0
    for path in sorted(root.rglob('*')):
        no_symlinks(path)
        require(path.resolve().is_relative_to(root.resolve()), 'Corpus path leaves its release.')
        if path.is_dir():
            continue
        require(path.is_file() and stat.S_ISREG(path.stat().st_mode), 'Unsupported corpus file type.')
        total += path.stat().st_size
        require(len(records) < 249 and total < MAX_EXPANDED_BYTES, 'Corpus exceeds the bounded demo size.')
        records[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    manifest = json.loads((root / 'manifest.json').read_bytes())
    require(manifest.get('synthetic') is True and len(manifest.get('records', [])) == 48,
            'Expected the existing 48-record synthetic maintenance corpus.')
    return records


def extract_archive(archive, destination):
    """Validate every member before creating any release files."""
    no_symlinks(destination.parent)
    require(not destination.exists(), 'A release already occupies this name; prior attempts must remain intact.')
    with zipfile.ZipFile(archive) as source:
        items = source.infolist()
        require(0 < len(items) < 250 and sum(item.file_size for item in items) < MAX_EXPANDED_BYTES,
                'Archive must contain fewer than 250 entries and less than 8 MiB expanded data.')
        names = set()
        for item in items:
            name = item.filename
            relative = PurePosixPath(name)
            require(name == item.orig_filename and name and '\x00' not in name and '\\' not in name
                    and ':' not in name and not relative.is_absolute()
                    and all(part not in ('', '.', '..') for part in name.rstrip('/').split('/')),
                    'Archive contains an unsafe path.')
            require(name.rstrip('/').casefold() not in names, 'Archive contains duplicate or case-colliding paths.')
            names.add(name.rstrip('/').casefold())
            mode = stat.S_IFMT(item.external_attr >> 16)
            require(mode in ({0, stat.S_IFDIR} if item.is_dir() else {0, stat.S_IFREG}),
                    'Archive links and special files are prohibited.')
            require(not item.flag_bits & 1, 'Encrypted archive entries are unsupported.')
            require((destination / relative).resolve().is_relative_to(destination.resolve()),
                    'Archive path leaves the new release.')
        destination.mkdir(mode=0o755)
        for item in items:
            target = destination.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with source.open(item) as stream:
                raw = stream.read(MAX_EXPANDED_BYTES + 1)
            require(len(raw) == item.file_size and len(raw) < MAX_EXPANDED_BYTES,
                    'Archive member data differs from its declared bounded size.')
            fresh_write(target, raw, 0o644)
        # A restrictive caller umask must not make the new app unreadable to nobody.
        os.chmod(destination, 0o755)
        for directory in destination.rglob('*'):
            if directory.is_dir():
                os.chmod(directory, 0o755)


def inspect_unit(raw):
    text = raw.decode('utf-8')
    require('\x00' not in text and 'Description=Fleet Evidence synthetic demonstration' in text.splitlines(),
            'The existing app unit does not carry the expected Fleet Evidence marker.')
    require(not re.search(r'^\s*(EnvironmentFile|UnsetEnvironment)\s*=', text, re.M),
            'This updater requires explicit unit environment settings without files or unset overrides.')
    require(re.findall(r'^User=([^\r\n]+)', text, re.M) == ['nobody']
            and re.findall(r'^Group=([^\r\n]+)', text, re.M) == ['nogroup'],
            'Preserve the existing nobody/nogroup service identity; no privilege change is allowed.')
    require(re.findall(r'^ProtectSystem=([^\r\n]+)', text, re.M) == ['strict'],
            'Expected the existing strict read-only system protection.')
    require(not re.search(r'^\s*(RootDirectory|RootImage|DynamicUser|PrivateUsers)\s*=', text, re.M),
            'Unexpected root or identity namespace settings require separate review.')
    for setting, expected in (('StateDirectory', 'fleet-evidence'), ('StateDirectoryMode', '0700')):
        values = re.findall(r'^' + setting + r'=([^\r\n]+)', text, re.M)
        require(not values or values == [expected], 'An unexpected state directory configuration requires separate review.')
    working = re.findall(r'^WorkingDirectory=([^\r\n]+)', text, re.M)
    executables = re.findall(r'^ExecStart=([^\r\n]+)', text, re.M)
    require(len(working) == len(executables) == 1, 'Expected one explicit working directory and app command.')
    old = Path(working[0])
    require(old.is_absolute() and old.parent == RELEASES and old.is_dir(),
            'Existing working directory must be one retained NavyDemo release.')
    no_symlinks(old)
    expected = ['/usr/bin/python3', str(old / 'server.py'), '--host', '127.0.0.1', '--port', '8095',
                '--corpus', str(old / 'corpus'), '--static', str(old / 'static')]
    require(shlex.split(executables[0]) == expected, 'The app command differs from the reviewed loopback deployment contract.')
    environment = {}
    for line in text.splitlines():
        if not line.startswith('Environment='):
            continue
        for item in shlex.split(line.partition('=')[2]):
            name, separator, value = item.partition('=')
            if name in {'KD_CONTENT_URL', 'KD_INDEX_URL', 'KD_SENSOR_DATABASE', 'FLEET_STATE_DIR'}:
                require(separator and name not in environment, 'A KD environment setting is repeated or malformed.')
                environment[name] = value
    require(environment.get('KD_CONTENT_URL') and environment.get('KD_INDEX_URL'),
            'Existing unit must explicitly identify both KD endpoints.')
    require(environment.get('KD_SENSOR_DATABASE', SENSOR_DATABASE) == SENSOR_DATABASE,
            'An unexpected sensor database is already configured; no change was made.')
    require(environment.get('FLEET_STATE_DIR', str(APP_STATE)) == str(APP_STATE),
            'An unexpected application state path is already configured.')
    return text, old, environment


def updated_unit(text, old, new, environment):
    lines = text.splitlines(keepends=True)
    ending = '\r\n' if '\r\n' in text else '\n'
    result = []
    for line in lines:
        if line.startswith('WorkingDirectory='):
            result.append('WorkingDirectory=' + str(new) + ending)
        elif line.startswith('ExecStart='):
            if 'KD_SENSOR_DATABASE' not in environment:
                result.append('Environment=KD_SENSOR_DATABASE=' + SENSOR_DATABASE + ending)
            if 'FLEET_STATE_DIR' not in environment:
                result.append('Environment=FLEET_STATE_DIR=' + str(APP_STATE) + ending)
            if not re.search(r'^StateDirectory=', text, re.M):
                result.append('StateDirectory=fleet-evidence' + ending)
            if not re.search(r'^StateDirectoryMode=', text, re.M):
                result.append('StateDirectoryMode=0700' + ending)
            require(line.count(str(old)) == 3, 'The old app command contains unexpected release-path references.')
            result.append(line.replace(str(old), str(new)))
        else:
            result.append(line)
    return ''.join(result).encode('utf-8')


def logical_table_snapshot(db, table):
    """Hash logical values; BLOB originals never enter JSON receipts verbatim."""
    require(isinstance(table, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', table),
            'A durable state table has an unsupported identifier.')
    schema = db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()
    require(schema is not None and isinstance(schema[0], str), 'A state table definition is unavailable.')
    cursor = db.execute('SELECT * FROM "' + table + '"')
    hashes = []
    for row in cursor:
        require(len(hashes) < 100000, 'A state table exceeds the bounded preservation size.')
        safe = [{'blob_sha256': digest(value), 'bytes': len(value)} if isinstance(value, bytes) else value for value in row]
        hashes.append(digest(canonical(safe)))
    hashes.sort()
    return {'count': len(hashes), 'rows_sha256': digest(canonical(hashes)),
            'schema_sha256': digest(schema[0].encode('utf-8')), 'row_sha256': hashes}


def app_state_snapshot():
    """Read durable state without creating directories, database rows or markers."""
    no_symlinks(APP_STATE)
    if not APP_STATE.exists():
        return {'exists': False, 'databases': {}, 'table_rows': {}, 'case_rows': [], 'mission_states': []}
    require(APP_STATE.is_dir(), 'The durable application state path is not a directory.')
    # Systemd owns directory creation/chown. Never recursively chown unknown data.
    import pwd
    import grp
    expected_uid, expected_gid = pwd.getpwnam('nobody').pw_uid, grp.getgrnam('nogroup').gr_gid
    info = APP_STATE.stat()
    require((info.st_uid, info.st_gid) == (expected_uid, expected_gid),
            'Existing durable state has an unexpected owner; no ownership change was made.')
    require(stat.S_IMODE(info.st_mode) == 0o700, 'Existing durable state mode must be 0700.')
    files, total = [], 0
    for path in APP_STATE.rglob('*'):
        no_symlinks(path)
        require(path.resolve().is_relative_to(APP_STATE.resolve()), 'A state path leaves its protected directory.')
        if path.is_file():
            require(stat.S_ISREG(path.stat().st_mode), 'A state file is not regular.')
            files.append(path)
            total += path.stat().st_size
        else:
            require(path.is_dir(), 'Unsupported durable state file type.')
    require(len(files) <= 10000 and total <= 256 * 1024 * 1024, 'Durable state exceeds the bounded inspection size.')
    result = {'exists': True, 'path': str(APP_STATE), 'owner': 'nobody:nogroup', 'mode': '0700',
              'file_count': len(files), 'total_bytes': total, 'databases': {}, 'table_rows': {},
              'case_rows': [], 'mission_states': []}
    for path in sorted(path for path in files if path.suffix == '.sqlite3'):
        file_info = path.stat()
        require((file_info.st_uid, file_info.st_gid) == (expected_uid, expected_gid),
                'A durable SQLite database has an unexpected owner.')
        # URI read-only mode refuses absent databases. Do not use immutable=1:
        # that would ignore committed WAL data and could hide pending work.
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            db.execute('PRAGMA query_only=ON')
            require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'A retained state database failed its integrity check.')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            relative = path.relative_to(APP_STATE).as_posix()
            result['databases'][relative] = sorted(tables)
            result['table_rows'][relative] = {}
            for table in sorted(tables):
                result['table_rows'][relative][table] = logical_table_snapshot(db, table)
            if 'cases' in tables:
                rows = db.execute('SELECT id,body FROM cases ORDER BY id').fetchall()
                require(len(rows) <= 10000, 'Case inventory exceeds the bounded preservation size.')
                result['case_rows'].extend({'id': ident, 'body_sha256': digest(body.encode())} for ident, body in rows)
            if 'metadata' in tables:
                row = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
                if row:
                    value = json.loads(row[0])
                    require(value.get('state') in ('idle', 'paused'),
                            'Pause the existing mission through the app before deployment; no automatic feed start is allowed.')
                    result['mission_states'].append(value)
            if 'publications' in tables:
                pending = db.execute("SELECT COUNT(*) FROM publications WHERE state!='verified' OR notified=0").fetchone()[0]
                require(pending == 0, 'Resolve retained KD publications/case callbacks before deployment; no automatic reconciliation is allowed.')
            if 'onboarding_imports' in tables:
                pending = db.execute("""SELECT COUNT(*) FROM onboarding_imports WHERE connection_sha256 IS NOT NULL
                    AND status NOT IN ('verified','rejected','duplicates_only')""").fetchone()[0]
                require(pending == 0, 'Resolve started import receipts before deployment; no automatic indexing or reconciliation is allowed during acceptance.')
        finally:
            db.close()
    return result


def verify_mission_state(before, receipt_dir):
    require(command(['systemctl', 'show', SERVICE, '--property=User', '--value']).strip() == 'nobody'
            and command(['systemctl', 'show', SERVICE, '--property=Group', '--value']).strip() == 'nogroup',
            'The restarted app has an unexpected runtime identity.')
    require(command(['systemctl', 'show', SERVICE, '--property=StateDirectory', '--value']).strip() == 'fleet-evidence',
            'Systemd did not load the persistent application state directory.')
    mission = get_json('/api/mission')
    require(mission.get('available') is True and isinstance(mission.get('run'), dict),
            'Mission status is unavailable or has the wrong response schema.')
    run = mission['run']
    require(run.get('state') in ('idle', 'paused') and run.get('source_id') == MISSION_SOURCE,
            'The mission must remain idle or paused until the operator begins controlled acceptance.')
    if not before['mission_states']:
        require(run.get('state') == 'idle' and run.get('id') is None,
                'The newly installed mission must remain idle with no run before controlled acceptance.')
    require(isinstance(mission.get('assets'), list) and {row.get('asset_id') for row in mission['assets']} == {'A-17', 'A-18'},
            'Mission status must identify both synthetic demonstration assets.')
    cases = get_json('/api/cases')
    require(isinstance(cases.get('cases'), list), 'Case inventory route has the wrong response schema.')
    after = app_state_snapshot()
    require(after['exists'] and 'cases.sqlite3' in after['databases'],
            'The app did not initialize its case database in the durable state directory.')
    require(any('publications' in tables and 'readings' in tables for tables in after['databases'].values()),
            'The mission route did not initialize its durable acquisition/history database.')
    require(after['case_rows'] == before['case_rows'], 'Existing case rows changed during read-only deployment acceptance.')
    for database, tables in before['table_rows'].items():
        require(all(after['table_rows'].get(database, {}).get(table) == value for table, value in tables.items()),
                'Retained case/feed history rows changed during deployment acceptance.')
    require(not before['mission_states'] or after['mission_states'] == before['mission_states'],
            'A retained mission state changed during deployment acceptance.')
    fresh_write(receipt_dir / 'mission-status-http.json', canonical(mission) + b'\n')
    fresh_write(receipt_dir / 'cases-http.json', canonical(cases) + b'\n')
    fresh_write(receipt_dir / 'durable-state-after.json', canonical(after) + b'\n')
    return {'passed': True, 'state_directory': str(APP_STATE), 'owner': 'nobody:nogroup', 'mode': '0700',
            'case_count': len(cases['cases']), 'mission_state': run['state'], 'case_rows_preserved': True,
            'existing_history_rows_preserved': True,
            'state_write_proof': 'GET routes initialized/opened SQLite databases in the systemd-owned directory as the app user; no case or feed run was created.'}


def verify_onboarding_routes(receipt_dir):
    registry = get_json('/api/registry/equipment')
    equipment = registry.get('equipment')
    require(isinstance(equipment, list) and len(equipment) >= 6
            and {'A-17', 'A-18'}.issubset({row.get('id') for row in equipment})
            and len({row.get('id') for row in equipment}) == len(equipment),
            'The registry must include at least six distinct equipment records and both retained pumps.')
    fleet = get_json('/api/fleet')
    require(isinstance(fleet.get('equipment'), list) and fleet.get('summary', {}).get('equipment') == len(equipment)
            and {row.get('id') for row in fleet['equipment']} == {row['id'] for row in equipment},
            'The fleet overview does not cover the complete registered equipment inventory.')
    imports = get_json('/api/imports')
    sources = get_json('/api/source-configs')
    documents = get_json('/api/documents')
    require(isinstance(imports.get('imports'), list) and isinstance(sources.get('sources'), list)
            and isinstance(documents.get('documents'), list), 'The import/source/document routes have unexpected response shapes.')
    for source in sources['sources']:
        require(isinstance(source.get('health'), dict) and source.get('prediction_status') == 'observation_only'
                and all(key in source for key in ('last_received_at', 'last_verified_at', 'latest_reading_at')),
                'A saved source did not expose separate observation, reception and verification evidence.')
    model = get_json('/api/model-evidence')
    require(model.get('available') is True and model.get('status') == 'illustrative'
            and model.get('synthetic_only') is True and model.get('model_matches') is True
            and model.get('evaluation_matches') is True and model.get('external_histories', {}).get('available') is False,
            'Model evidence must match the frozen sources and disclose the absence of independent real histories.')
    for label, data in (('registry-equipment', registry), ('fleet-overview', fleet), ('imports', imports),
                        ('source-configs', sources), ('documents', documents), ('model-evidence', model)):
        fresh_write(receipt_dir / (label + '-http.json'), canonical(data) + b'\n')
    return {'passed': True, 'equipment_count': len(equipment), 'import_count_visible': len(imports['imports']),
            'source_count': len(sources['sources']), 'document_count': len(documents['documents']),
            'model_status': 'illustrative', 'external_histories_available': False,
            'application_post_requests': 0, 'import_submissions': 0, 'browser_acceptance': False}


def verify_equipment_case_routes(receipt_dir):
    registry = get_json('/api/registry/equipment')['equipment']
    require(registry and all(isinstance(row.get('id'), str) for row in registry),
            'Cannot select registered equipment for the case context check.')
    # Prefer an existing imported synthetic asset; no registry or source rows
    # are created to make this route pass.
    sources = get_json('/api/source-configs')['sources']
    imported = next((row for row in registry if row['id'].startswith('QA-')), registry[0])
    context = get_json('/api/equipment-case-context', {'asset': imported['id']})
    require(context.get('equipment', {}).get('id') == imported['id']
            and isinstance(context.get('sources'), list) and isinstance(context.get('documents'), list)
            and isinstance(context.get('replacement_events'), list),
            'Equipment case context does not expose the registered asset, imported observations, documents and replacement events.')
    for source in context['sources']:
        snapshot = source.get('snapshot')
        if snapshot:
            require(snapshot.get('source', {}).get('mode') == 'import'
                    and snapshot.get('asset_id') == imported['id']
                    and isinstance(snapshot.get('snapshot_id'), str),
                    'An imported case context returned the wrong equipment or snapshot type.')
    cases = get_json('/api/cases')
    require(isinstance(cases.get('cases'), list), 'Existing maintenance case inventory is unavailable.')
    fresh_write(receipt_dir / 'equipment-case-context-http.json', canonical(context) + b'\n')
    fresh_write(receipt_dir / 'equipment-case-inventory-http.json', canonical(cases) + b'\n')
    return {'passed': True, 'equipment_id': imported['id'], 'contexts_checked': 1,
            'saved_sources': len(sources), 'existing_cases': len(cases['cases']),
            'case_creations': 0, 'case_mutations': 0, 'import_submissions': 0, 'browser_acceptance': False}


def navigation_inventory():
    """Confirm this accepted demo baseline before and after the UI-only switch."""
    equipment = get_json('/api/registry/equipment')['equipment']
    imports = get_json('/api/imports')['imports']
    documents = get_json('/api/documents')['documents']
    sources = get_json('/api/source-configs')['sources']
    cases = get_json('/api/cases')['cases']
    mission = get_json('/api/mission')
    require((len(equipment), len(imports), len(documents), len(sources), len(cases)) == (8, 5, 2, 2, 2),
            'Expected the accepted eight-equipment, five-import, two-document/source/case baseline.')
    require(all(case.get('status') == 'closed' for case in cases), 'Both retained maintenance cases must remain closed.')
    run = mission.get('run', {})
    require(mission.get('available') is True and run.get('state') == 'paused'
            and run.get('source_id') == MISSION_SOURCE and run.get('id')
            and mission.get('total_history_samples') == 606,
            'Expected the accepted paused mission with 606 acquired readings; no source control is permitted.')
    return {'equipment_count': len(equipment), 'import_count': len(imports), 'document_count': len(documents),
            'source_count': len(sources), 'closed_case_count': len(cases),
            'mission': {**{key: run.get(key) for key in ('id', 'state', 'source_id', 'step')}, 'history_samples': 606}}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, message, headers, newurl):
        raise RuntimeError('Unexpected application redirect; no destination followed.')


class NavigationDocument(HTMLParser):
    """Read only navigation identities and asset links, not arbitrary page content."""
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.assets, self.buttons, self.groups, self.ids, self.brands = [], [], [], [], {}, []
        self.text, self.layouts, self.source_tablists = [], [], []

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        classes = attrs.get('class', '').split()
        if tag == 'script' and attrs.get('src'):
            self.assets.append(attrs['src'])
        if tag == 'link' and 'stylesheet' in attrs.get('rel', '').split():
            self.assets.append(attrs.get('href'))
        if attrs.get('id'):
            require(attrs['id'] not in self.ids, 'The navigation document has duplicate element IDs.')
            self.ids[attrs['id']] = attrs
        if tag == 'a' and 'brand' in classes:
            self.brands.append(attrs.get('href'))
        if attrs.get('data-evidence-layout'):
            self.layouts.append((attrs['data-evidence-layout'], attrs.get('data-view')))
        if attrs.get('role') == 'tablist' and any(node.get('id') == 'sourceTabs' for _, node in self.stack):
            self.source_tablists.append(attrs)
        in_primary = any('primary-nav' in node.get('class', '').split() for _, node in self.stack)
        if in_primary and 'nav-group' in classes:
            self.groups.append(attrs)
        if in_primary and tag == 'button' and attrs.get('data-view'):
            self.buttons.append({'view': attrs['data-view'], 'label': attrs.get('title')})
        if tag not in self.VOID:
            self.stack.append((tag, attrs))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.text.append(data)


def get_static(path, timeout=10):
    require(path == '/' or isinstance(path, str) and re.fullmatch(r'/static/[a-z0-9][a-z0-9./_-]*\.(?:js|css)', path)
            and '..' not in PurePosixPath(path).parts, 'Only local reviewed HTML/script/style paths may be fetched.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(ORIGIN + path, timeout=timeout) as response:
        expected = ('text/html',) if path == '/' else ('text/css',) if path.endswith('.css') else ('application/javascript', 'text/javascript')
        require(response.status == 200 and response.headers.get_content_type() in expected,
                'A navigation asset did not return its successful expected content type.')
        raw = response.read(1024 * 1024 + 1)
    require(len(raw) <= 1024 * 1024, 'A navigation asset exceeded the one MiB response bound.')
    return raw


def verify_navigation_routes(release, receipt_dir):
    raw = get_static('/')
    require(raw == (release / 'static/index.html').read_bytes(), 'The served HTML differs from the reviewed release.')
    document = NavigationDocument()
    document.feed(raw.decode('utf-8'))
    expected = [
        ('fleet', 'Fleet overview'), ('cases', 'Maintenance cases'), ('registry', 'Equipment & parts'),
        ('evidence', 'Evidence library'), ('packet', 'Review packet'), ('imports', 'Import workspace'),
        ('connections', 'Sources & health'), ('mission', 'Synthetic feed control'),
        ('condition', 'Pump condition'), ('model-evidence', 'Model evidence'),
    ]
    require([(row['view'], row['label']) for row in document.buttons] == expected,
            'The served navigation does not match the reviewed ten-workspace hierarchy.')
    require(len(document.groups) == 4 and document.brands == ['#fleet']
            and all(label in ' '.join(document.text) for label in ('Daily work', 'Evidence', 'Data management', 'Demo & evaluation')),
            'The served navigation groups or fleet landing link are missing.')
    for ident, attribute, value in (
        ('savedSourcesTab', 'role', 'tab'), ('indexedSourcesTab', 'role', 'tab'),
        ('savedSourcesTab', 'data-view', 'connections'),
        ('indexedSourcesTab', 'data-view', 'sources'), ('trainingReferences', 'data-kind', 'training'),
    ):
        require(document.ids.get(ident, {}).get(attribute) == value, 'A shared source tab or training evidence filter is missing.')
    require('evidenceViewControls' in document.ids and document.layouts == [('cards', 'workspace'), ('list', 'evidence')]
            and len(document.source_tablists) == 1, 'The shared evidence-layout or source-tab controls are missing.')
    required_assets = {'/static/' + name + '.' + extension for name in ('app', 'sensor', 'mission', 'onboarding', 'equipment-cases')
                       for extension in ('js', 'css')}
    require(len(document.assets) == len(set(document.assets)) and required_assets.issubset(document.assets),
            'Required navigation scripts or styles are missing or duplicated.')
    records = [{'path': '/', 'bytes': len(raw), 'sha256': digest(raw)}]
    total, script = len(raw), None
    for path in document.assets:
        content = get_static(path)
        local = release / path.lstrip('/')
        no_symlinks(local)
        require(local.resolve().is_relative_to(release.resolve()) and local.is_file() and content == local.read_bytes(),
                'A served navigation script/style differs from the reviewed release: ' + path)
        total += len(content)
        require(total <= MAX_EXPANDED_BYTES, 'Navigation assets exceed the aggregate response bound.')
        records.append({'path': path, 'bytes': len(content), 'sha256': digest(content)})
        if path == '/static/app.js':
            script = content.decode('utf-8')
    require(script and re.search(r"view\s*:\s*['\"]fleet['\"]", script)
            and re.search(r"const\s+normalizeRoute\s*=\s*!\[[^\]]+\]\.includes\(name\);\s*if\s*\(normalizeRoute\)\s*\{\s*name\s*=\s*['\"]fleet['\"]", script),
            'The served app script does not declare fleet as its initial and unknown-route fallback.')
    result = {'passed': True, 'default_view': 'fleet', 'workspace_count': len(expected), 'group_count': 4,
              'asset_count': len(records), 'assets': records, 'served_bytes_match_reviewed_release': True,
              'default_route_static_check': True, 'application_post_requests': 0, 'browser_acceptance': False}
    fresh_write(receipt_dir / 'navigation-http-acceptance.json', canonical(result) + b'\n')
    return result


def get_json(path, parameters=None, timeout=10):
    require(path.startswith('/api/') and '?' not in path, 'Unexpected application route.')
    url = ORIGIN + path + ('?' + urllib.parse.urlencode(parameters) if parameters else '')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        require(response.status == 200 and 'application/json' in response.headers.get('Content-Type', ''),
                'Application did not return a successful JSON response.')
        raw = response.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, 'Application JSON exceeded its response bound.')
    value = json.loads(raw)
    require(isinstance(value, dict), 'Application response is not a JSON object.')
    return value


def case_export_snapshot(expected_count=None):
    inventory = get_json('/api/cases')
    cases = inventory.get('cases')
    require(isinstance(cases, list) and len(cases) <= 200
            and (expected_count is None or len(cases) == expected_count),
            'The complete existing case inventory exceeds the bounded export-preservation check.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    result = {}
    for case in cases:
        ident = case.get('id')
        require(isinstance(ident, str) and re.fullmatch(r'FE-[A-F0-9]{12}', ident) and ident not in result,
                'The saved case inventory has an invalid or duplicate case identifier.')
        with opener.open(ORIGIN + '/api/cases/' + ident + '/export', timeout=15) as response:
            require(response.status == 200 and 'text/html' in response.headers.get('Content-Type', ''),
                    'An existing saved-case export is unavailable.')
            raw = response.read(16 * 1024 * 1024 + 1)
        require(len(raw) <= 16 * 1024 * 1024, 'A saved-case export exceeds its preservation bound.')
        result[ident] = {'sha256': digest(raw), 'bytes': len(raw)}
    return result


def wait_health(seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            health = get_json('/api/health', timeout=min(5, max(1, deadline - time.monotonic())))
            if health.get('ok') is True and health.get('database') == 'FLEET_EVIDENCE_DEMO' and health.get('indexed_records') == 48:
                return health
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('The app did not regain verified 48-record health within the bounded wait.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--release', required=True)
    args = parser.parse_args()
    require(sys.platform == 'linux' and os.geteuid() == 0, 'Run this updater as Linux root only after archive review.')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', args.release), 'Use a simple fresh lowercase release name.')
    require(args.release == RELEASE_NAME, 'This reviewed updater is limited to the navigation-v1 release.')
    require(re.fullmatch(r'[0-9a-f]{64}', args.sha256), 'Supply the exact lowercase archive SHA-256.')
    no_symlinks(BASE)
    no_symlinks(args.archive)
    archive = args.archive.resolve(strict=True)
    require(archive.parent == BASE.resolve() and archive.is_file() and archive.suffix == '.zip',
            'Archive must be a regular ZIP immediately under /mnt/d/KDDeployment.')
    require(archive.stat().st_size < MAX_ARCHIVE_BYTES, 'Compressed archive exceeds the bounded input size.')
    require(digest(archive.read_bytes()) == args.sha256, 'Archive SHA-256 does not match the reviewed file.')
    release = RELEASES / args.release
    state_dir = RECEIPTS / args.release
    report = BASE / ('navy-navigation-' + args.release + '.json')
    for path in (RELEASES, RECEIPTS, release, state_dir, report):
        no_symlinks(path)
    require(RELEASES.is_dir() and RECEIPTS.is_dir(), 'Expected existing NavyDemo release and receipt directories.')
    require(not release.exists() and not state_dir.exists() and not report.exists(),
            'Prior release or receipt exists; inspect it without rerunning this updater.')
    fresh_write(report, b'{}\n')
    state = {'release': args.release, 'archive_sha256': args.sha256, 'complete': False,
             'started_at': now(), 'steps': [], 'sensor_database': SENSOR_DATABASE,
             'native_index_writes': 0, 'automatic_feed_start': False, 'import_submissions': 0,
             'case_creations': 0, 'case_mutations': 0, 'browser_acceptance': False,
             'application_state_directory': str(APP_STATE),
             'rollback_policy': 'Restore only the previous app unit/service. Preserve durable case/feed state, staged release, every KD database and every receipt; no cleanup or reverse state migration.'}
    switched, old_raw, old_mode, client = False, None, None, None
    before_databases, before_containers = None, None

    def save(step):
        state['steps'].append({'step': step, 'at': now()})
        atomic_replace(report, json.dumps(state, indent=2).encode('utf-8') + b'\n', 'updating')

    try:
        state_dir.mkdir(mode=0o700)
        save('fresh-receipt-created')
        no_symlinks(UNIT)
        require(UNIT.is_file() and stat.S_ISREG(UNIT.stat().st_mode), 'Existing app unit must be a regular file.')
        old_raw, old_mode = UNIT.read_bytes(), stat.S_IMODE(UNIT.stat().st_mode)
        text, old, environment = inspect_unit(old_raw)
        require(old.name == PREVIOUS_RELEASE_NAME, 'Expected the accepted equipment-case-v1 service release.')
        require(command(['systemctl', 'show', SERVICE, '--property=FragmentPath', '--value']).strip() == str(UNIT),
                'The active service comes from another unit path.')
        require(not command(['systemctl', 'show', SERVICE, '--property=DropInPaths', '--value']).strip(),
                'Unexpected app service drop-ins require separate review.')
        require(command(['systemctl', 'show', SERVICE, '--property=WorkingDirectory', '--value']).strip() == str(old),
                'Loaded service and saved working directory disagree.')
        require(command(['systemctl', 'is-active', SERVICE]).strip() == 'active', 'The existing app service is not active.')
        fresh_write(state_dir / 'previous.service', old_raw)
        state.update(previous_release=str(old), previous_unit_sha256=digest(old_raw), receipt_directory=str(state_dir))
        previous_app_state = app_state_snapshot()
        require({'cases.sqlite3', 'mission.sqlite3', 'registry.sqlite3', 'onboarding.sqlite3'}.issubset(previous_app_state['databases']),
                'This updater requires the existing durable onboarding release and its case/history/registry/import databases.')
        fresh_write(state_dir / 'durable-state-before.json', canonical(previous_app_state) + b'\n')
        previous_case_exports = case_export_snapshot(len(previous_app_state['case_rows']))
        fresh_write(state_dir / 'case-export-hashes-before.json', canonical(previous_case_exports) + b'\n')
        previous_navigation_inventory = navigation_inventory()
        state['preserved_application_inventory'] = previous_navigation_inventory
        save('original-app-unit-preserved')
        extract_archive(archive, release)
        required = ['server.py', 'kd_client.py', 'corpus_model.py', 'index_corpus.py', 'sensor_kd.py',
                    'sensor_ingest.py', 'telemetry.py', 'sensor-contract.json', 'verify_live.py',
                    'case_store.py', 'mission_feed.py', 'acceptance.json',
                    'registry_store.py', 'onboarding.py', 'equipment_cases.py', 'model-evidence.json', 'evaluate_prognostics.py',
                    'deploy_onboarding_update.py', 'verify_onboarding_live.py',
                    'deploy_equipment_cases_update.py', 'verify_equipment_cases_live.py',
                    'deploy_navigation_update.py',
                    'static/index.html', 'static/app.js', 'static/app.css',
                    'static/sensor.js', 'static/sensor.css', 'static/mission.js', 'static/mission.css',
                    'static/onboarding.js', 'static/onboarding.css',
                    'static/equipment-cases.js', 'static/equipment-cases.css']
        require(all((release / path).is_file() for path in required), 'Archive is missing required application files.')
        unchanged_runtime = preserve_runtime(old, release)
        fresh_write(state_dir / 'preserved-runtime-hashes.json', canonical(unchanged_runtime) + b'\n')
        state['runtime_source_unchanged'] = True
        old_corpus, new_corpus = corpus_snapshot(old / 'corpus'), corpus_snapshot(release / 'corpus')
        require(old_corpus == new_corpus, 'Corpus manifest or source bytes changed; evidence reindexing is prohibited.')
        fresh_write(state_dir / 'preserved-corpus-hashes.json', canonical(old_corpus) + b'\n')
        state.update(release_path=str(release), corpus_files=len(old_corpus), corpus_unchanged=True,
                     corpus_manifest_sha256=old_corpus['manifest.json'])
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(release))
        for name in ('kd_client', 'sensor_kd', 'sensor_ingest', 'telemetry', 'index_corpus', 'corpus_model'):
            require(name not in sys.modules, 'A deployment module was loaded before the reviewed release was staged.')
        os.environ.update({key: value for key, value in environment.items() if key in {'KD_CONTENT_URL', 'KD_INDEX_URL'}})
        os.environ['KD_SENSOR_DATABASE'] = SENSOR_DATABASE
        kd_module = importlib.import_module('kd_client')
        sensor_module = importlib.import_module('sensor_kd')
        telemetry_module = importlib.import_module('telemetry')
        client = kd_module.KDClient()
        require(client.aci_url == environment['KD_CONTENT_URL'].rstrip('/')
                and client.index_url == environment['KD_INDEX_URL'].rstrip('/'),
                'Staged KD client resolved endpoints different from the existing app unit.')
        state['connection_sha256'] = digest((client.aci_url + '\n' + client.index_url).encode())
        before_containers = container_snapshot()
        require(len(before_containers) == 21, 'Expected the accepted 21-container baseline; no container changes are permitted.')
        before_databases = database_snapshot(client)
        fresh_write(state_dir / 'database-references-before.json', canonical(before_databases) + b'\n')
        fresh_write(state_dir / 'containers-before.json', canonical(before_containers) + b'\n')
        state['preserved_database_counts'] = {name: data['count'] for name, data in before_databases.items()}
        state['preserved_container_count'] = len(before_containers)
        save('staged-corpus-and-original-state-verified')
        expected = {}
        for asset, scenario in (('A-17', 'degradation'), ('A-18', 'normal')):
            simulation = telemetry_module.simulate(asset, scenario, 80)
            payload = sensor_module.validate_snapshot({'schema': sensor_module.SCHEMA, 'source_id': SOURCE,
                                                       'asset_id': asset, 'synthetic': True, 'samples': simulation['samples']})
            expected[asset] = {'scenario': scenario, 'simulation': simulation, 'payload': payload}
            fresh_write(state_dir / (asset + '-snapshot.json'), canonical(payload) + b'\n')
            existing = sensor_module.SensorKD(client, SENSOR_DATABASE).snapshot(SOURCE, asset)
            require(canonical({key: existing[key] for key in payload}) == canonical(payload),
                    'An existing seed snapshot changed or is missing; no indexing fallback is permitted.')
            fresh_write(state_dir / (asset + '-sensor-readback.json'), canonical(existing) + b'\n')
            state.setdefault('seed_readback', {})[asset] = {'reused': True, 'payload_sha256': sensor_module.payload_hash(payload), 'index_jobs': []}
            save('existing-sensor-snapshot-reused-' + asset)
        new_raw = updated_unit(text, old, release, environment)
        fresh_write(state_dir / 'installed.service', new_raw)
        current_app_state = app_state_snapshot()
        require(current_app_state['table_rows'] == previous_app_state['table_rows'],
                'Application state changed during preparation; no app switch was made.')
        require(UNIT.read_bytes() == old_raw, 'The app unit changed during preparation; no overwrite was made.')
        atomic_replace(UNIT, new_raw, args.release + '-install', old_mode)
        switched = True
        state['installed_unit_sha256'] = digest(new_raw)
        save('app-unit-paths-switched')
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'restart', SERVICE], timeout=90)
        state['health'] = wait_health()
        require(command(['systemctl', 'show', SERVICE, '--property=WorkingDirectory', '--value']).strip() == str(release),
                'The restarted app did not load the new release path.')
        state['onboarding_acceptance'] = verify_onboarding_routes(state_dir)
        state['equipment_case_acceptance'] = verify_equipment_case_routes(state_dir)
        state['mission_acceptance'] = verify_mission_state(previous_app_state, state_dir)
        state['navigation_acceptance'] = verify_navigation_routes(release, state_dir)
        save('durable-mission-and-case-routes-verified-without-starting-feed')
        sources = get_json('/api/telemetry/sources')
        require(sources.get('available') is True and sources.get('database') == SENSOR_DATABASE,
                'The KD sensor source registry is unavailable or uses the wrong database.')
        source_rows = [row for row in sources.get('sources', []) if row.get('source_id') == SOURCE]
        require(len(source_rows) == 1 and source_rows[0].get('synthetic') is True
                and set(source_rows[0].get('asset_ids', [])) == {'A-17', 'A-18'},
                'The source registry did not preserve both synthetic equipment snapshots.')
        fresh_write(state_dir / 'sensor-sources-http.json', canonical(sources) + b'\n')
        for asset, item in expected.items():
            simulation = get_json('/api/telemetry', {'asset': asset, 'mode': 'simulation', 'scenario': item['scenario'], 'step': '80'})
            require(all(simulation.get(key) == value for key, value in item['simulation'].items())
                    and simulation.get('source', {}).get('through_kd') is False
                    and simulation['source'].get('synthetic') is True, 'Simulation HTTP response differs from the authored fixture.')
            result = get_json('/api/telemetry', {'asset': asset, 'mode': 'kd', 'source': SOURCE})
            provenance = result.get('source', {}).get('provenance', {})
            require(result.get('asset_id') == asset and result.get('synthetic') is True
                    and result.get('samples') == item['payload']['samples']
                    and result.get('observed_at') == item['payload']['samples'][-1]['timestamp']
                    and result.get('source', {}).get('through_kd') is True
                    and result['source'].get('synthetic') is True
                    and provenance.get('live') is True and provenance.get('database') == SENSOR_DATABASE
                    and provenance.get('reference') == sensor_module.reference_for(SOURCE, asset)
                    and provenance.get('payload_sha256') == sensor_module.payload_hash(item['payload'])
                    and result.get('analysis') == telemetry_module.analyze_samples(item['payload']['samples'], synthetic=True),
                    'KD telemetry did not return the exact synthetic snapshot, analysis and live provenance.')
            fresh_write(state_dir / (asset + '-simulation-http.json'), canonical(simulation) + b'\n')
            fresh_write(state_dir / (asset + '-kd-telemetry-http.json'), canonical(result) + b'\n')
        state['sensor_http_acceptance'] = {'passed': True, 'assets': ['A-17', 'A-18'], 'samples_per_asset': 81,
                                           'through_kd': True, 'synthetic': True, 'full_payload_match': True}
        save('sensor-http-acceptance-passed')
        acceptance_path = state_dir / 'evidence-live-acceptance.json'
        require(not acceptance_path.exists(), 'Acceptance report must be fresh.')
        verification_env = os.environ.copy()
        verification_env['PYTHONDONTWRITEBYTECODE'] = '1'
        output = command([sys.executable, str(release / 'verify_live.py'), '--base', ORIGIN,
                          '--acceptance', str(release / 'acceptance.json'), '--output', str(acceptance_path)],
                         timeout=240, env=verification_env)
        fresh_write(state_dir / 'evidence-live-acceptance-stdout.txt', output.encode('utf-8'))
        acceptance = json.loads(acceptance_path.read_bytes())
        require(acceptance.get('complete') is True and acceptance.get('passed') is True,
                'Existing evidence HTTP acceptance did not pass.')
        state['evidence_acceptance'] = {'report': str(acceptance_path), 'passed': True,
                                        'checks': len(acceptance.get('checks', []))}
        after_databases, after_containers = database_snapshot(client), container_snapshot()
        fresh_write(state_dir / 'database-references-after.json', canonical(after_databases) + b'\n')
        fresh_write(state_dir / 'containers-after.json', canonical(after_containers) + b'\n')
        require(before_databases == after_databases, 'Original KD database reference sets or counts changed.')
        require(before_containers == after_containers, 'An existing Docker container identity or start time changed.')
        require(UNIT.read_bytes() == new_raw and corpus_snapshot(release / 'corpus') == old_corpus,
                'Installed app unit or corpus changed during verification.')
        require(preserve_runtime(old, release) == unchanged_runtime, 'An unchanged runtime file changed during acceptance.')
        require(navigation_inventory() == previous_navigation_inventory,
                'The accepted equipment/import/document/source/case/mission inventory changed during navigation acceptance.')
        state.update(original_databases_preserved=True, container_lifecycles_preserved=True, complete=True)
        final_app_state = app_state_snapshot()
        for database, tables in previous_app_state['table_rows'].items():
            require(all(final_app_state['table_rows'].get(database, {}).get(table) == value for table, value in tables.items()),
                    'An existing durable table changed during the complete read-only deployment acceptance.')
            require(final_app_state['databases'].get(database) == previous_app_state['databases'][database],
                    'The table inventory of an existing durable database changed during deployment.')
        require(set(final_app_state['databases']) == set(previous_app_state['databases']),
                'The durable database inventory changed during read-only equipment-case acceptance.')
        fresh_write(state_dir / 'durable-state-final.json', canonical(final_app_state) + b'\n')
        state['existing_durable_tables_preserved'] = True
        final_case_exports = case_export_snapshot(len(final_app_state['case_rows']))
        fresh_write(state_dir / 'case-export-hashes-after.json', canonical(final_case_exports) + b'\n')
        require(final_case_exports == previous_case_exports, 'An existing saved-case HTML export changed across the application update.')
        state['existing_case_exports_preserved'] = True
        save('navigation-update-accepted-without-case-mutations-imports-or-source-starts')
    except BaseException as error:
        state['complete'] = False
        state['error'] = {'type': type(error).__name__, 'message': str(error)[:600]}
        if switched and old_raw is not None:
            try:
                atomic_replace(UNIT, old_raw, args.release + '-rollback', old_mode)
                command(['systemctl', 'daemon-reload'])
                command(['systemctl', 'restart', SERVICE], timeout=90)
                restored = wait_health()
                state['rollback'] = {'attempted': True, 'unit_restored': UNIT.read_bytes() == old_raw,
                                     'health': restored, 'synthetic_sensor_database_retained': True,
                                     'durable_application_state_retained': True}
            except BaseException as rollback_error:
                state['rollback'] = {'attempted': True, 'failed': True, 'error_type': type(rollback_error).__name__,
                                     'message': str(rollback_error)[:300], 'previous_unit_path': str(state_dir / 'previous.service')}
        else:
            state['rollback'] = {'attempted': False, 'reason': 'The existing app unit was not switched.'}
        if client is not None and before_databases is not None:
            try:
                state['original_databases_preserved_after_failure'] = database_snapshot(client) == before_databases
                state['container_lifecycles_preserved_after_failure'] = container_snapshot() == before_containers
            except Exception as audit_error:
                state['failure_preservation_audit'] = type(audit_error).__name__
        save('update-failed-receipts-and-durable-state-retained')
        print(json.dumps({'complete': False, 'report': str(report), 'rollback': state.get('rollback', {}).get('unit_restored', False)}))
        return 1
    print(json.dumps({'complete': True, 'report': str(report), 'release': str(release), 'sensor_source': SOURCE}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
