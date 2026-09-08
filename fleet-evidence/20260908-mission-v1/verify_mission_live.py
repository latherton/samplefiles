#!/usr/bin/env python3
"""Explicit, bounded acceptance of one synthetic maintenance episode via the app.

Requires --run-synthetic-episode. Uses only http://127.0.0.1:8095, never a native
KD index endpoint, service restart or cache sync. Retained output is private
acceptance evidence and must not be included in public release packages.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


ORIGIN = 'http://127.0.0.1:8095'
SOURCE = 'mission-pump-feed'
ASSETS = ('A-17', 'A-18')
EVIDENCE = ('TM-P200-C', 'WO-219')
ROLE = 'Acceptance reviewer'
POLL_SECONDS = 10
TIMEOUT_SECONDS = 1080
MAX_BODY_BYTES = 8 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fresh_write(path, raw):
    for part in (path, *path.parents):
        require(not part.is_symlink(), 'Acceptance output must not traverse symbolic links.')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def validate_snapshot(snapshot, expected_asset):
    """Independently reproduce the transport and captured-window identities."""
    require(isinstance(snapshot, dict) and snapshot.get('asset_id') == expected_asset
            and snapshot.get('synthetic') is True, 'The returned condition must identify the expected synthetic asset.')
    source = snapshot.get('source', {})
    provenance = source.get('provenance', {})
    require(source.get('through_kd') is True and source.get('synthetic') is True and source.get('label') == SOURCE,
            'The condition must retain its synthetic KD source provenance.')
    samples = snapshot.get('samples')
    require(isinstance(samples, list) and 1 <= len(samples) <= 181, 'Condition window length violates the bounded contract.')
    times = [stamp(row['timestamp']) for row in samples]
    require(all(left < right for left, right in zip(times, times[1:])) and snapshot.get('observed_at') == samples[-1]['timestamp'],
            'Condition timestamps are not strictly increasing or the final event time differs.')
    payload = {'schema': 'fleet.sensor.v1', 'source_id': SOURCE, 'asset_id': expected_asset,
               'synthetic': True, 'samples': samples}
    require(provenance.get('database') == 'FLEET_SENSOR_DEMO' and provenance.get('live') is True
            and provenance.get('reference') == 'urn:fleet-sensor:' + SOURCE + ':' + expected_asset
            and provenance.get('payload_sha256') == sha(canonical(payload)),
            'KD provenance or complete normalized payload hash did not match.')
    identity = {'asset_id': expected_asset, 'source': SOURCE, 'synthetic': True, 'samples': samples}
    require(snapshot.get('snapshot_id') == sha(canonical(identity)), 'Captured condition hash did not match its complete observations.')
    require(snapshot.get('analysis', {}).get('model_version') == 'fleet-demo-condition-v1', 'Unexpected analysis version.')
    return payload


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, message, headers, newurl):
        raise RuntimeError('Unexpected redirect; no alternate destination followed.')


class Acceptance:
    def __init__(self, output, resume_run=None, timeout_seconds=TIMEOUT_SECONDS):
        self.path = Path(output).resolve()
        require(not self.path.exists() and not self.path.with_suffix('.case.html').exists(), 'Choose fresh acceptance output paths; prior runs are retained.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh_write(self.path, b'{}\n')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.started = time.monotonic()
        self.deadline = self.started + timeout_seconds
        self.work_deadline = self.deadline - 150
        self.run_id = None
        self.resume_run = resume_run
        self.stage_name = 'initializing'
        self.last_delivery = None
        self.r = {'complete': False, 'passed': False, 'started_at': now(), 'origin': ORIGIN,
                  'synthetic_only': True, 'application_mutations_authorized_by_cli': True,
                  'timeout_seconds': timeout_seconds, 'poll_seconds': POLL_SECONDS,
                  'direct_native_index_calls': False, 'cache_sync_requested': False, 'service_restarts': False,
                  'browser_acceptance': False, 'runtime_preservation_acceptance': False,
                  'scope': 'One synthetic acquisition, KD readback and persistent review/outcome workflow via application HTTP; no operational findings or repair-causation claim.',
                  'mutations': [], 'stages': [], 'delivery_events': [], 'checks': []}
        self.save()

    def save(self):
        self.r['updated_at'] = now()
        temporary = self.path.with_name(self.path.name + '.updating')
        fresh_write(temporary, json.dumps(self.r, indent=2, allow_nan=False).encode() + b'\n')
        os.replace(temporary, self.path)

    def stage(self, name, **details):
        self.stage_name = name
        event = {'stage': name, 'at': now(), **details}
        self.r['stages'].append(event)
        self.save()
        print(json.dumps(event), flush=True)

    def check(self, name, condition, **details):
        self.r['checks'].append({'name': name, 'passed': bool(condition), **details})
        self.save()
        require(condition, name)

    def request(self, path, body=None, expected=200, html=False):
        require(path.startswith('/api/') and not path.startswith('//') and '\\' not in path,
                'Only fixed application API routes are supported.')
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'The bounded acceptance deadline has expired.')
        record = None
        if body is not None:
            record = {'path': path, 'intent_at': now(), 'body': body, 'result': 'not-yet-known'}
            self.r['mutations'].append(record)
            self.save()  # Never retry an uncertain write on a later transport error.
        request = urllib.request.Request(ORIGIN + path, data=canonical(body) if body is not None else None,
                                         headers={'Content-Type': 'application/json', 'Cache-Control': 'no-store'})
        try:
            response = self.opener.open(request, timeout=min(10, max(.1, remaining)))
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(MAX_BODY_BYTES + 1)
            status, content_type = response.code, response.headers.get('Content-Type', '')
        require(len(raw) <= MAX_BODY_BYTES, 'HTTP acceptance response exceeded its size bound.')
        if record is not None:
            record.update(result='response-received', status=status, received_at=now())
            self.save()
        require(status == expected, 'Unexpected HTTP status ' + str(status) + ' for ' + path + '; mutations are not replayed.')
        if html:
            require('text/html' in content_type, 'Expected the retained HTML case export.')
            return raw
        require('application/json' in content_type, 'Expected an application JSON response.')
        value = json.loads(raw)
        require(isinstance(value, dict), 'Expected a JSON object.')
        return value

    def mission(self):
        result = self.request('/api/mission')
        require(result.get('available') is True and result.get('run', {}).get('source_id') == SOURCE,
                'The mission acquisition interface is not ready for the expected source.')
        if self.run_id:
            require(result['run']['id'] == self.run_id, 'The active mission changed; do not take over another run.')
        compact = {'run': result['run']['id'], 'state': result['run']['state'],
                   'assets': [{'asset': row['asset_id'], 'publication': row['delivery'].get('publication_id'),
                               'verified_publication': row['delivery'].get('verified_publication_id'),
                               'delivery': row['delivery']['state'], 'pending_index_ids': row['delivery'].get('pending_index_ids', []),
                               'verified_at': row['delivery'].get('verified_at'), 'error': row['delivery'].get('error')}
                              for row in result['assets']]}
        signature = canonical(compact)
        if signature != self.last_delivery:
            self.last_delivery = signature
            self.r['delivery_events'].append({'at': now(), **compact})
            self.save()
            print(json.dumps({'stage': self.stage_name, 'at': now(), 'delivery': compact}), flush=True)
        self.r['latest_mission_summary'] = {'at': now(), 'run': result['run'],
                                          'total_history_samples': result.get('total_history_samples'),
                                          'case_counts': result.get('case_counts')}
        return result

    @staticmethod
    def asset(mission, asset='A-17'):
        rows = [row for row in mission['assets'] if row['asset_id'] == asset]
        require(len(rows) == 1, 'Mission did not return exactly one row for the expected asset.')
        return rows[0]

    def wait(self, name, predicate, deadline=None):
        self.stage(name)
        deadline = min(deadline or self.work_deadline, self.deadline)
        while time.monotonic() < deadline:
            result = predicate(self.mission())
            if result:
                return result
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(POLL_SECONDS, remaining))
        raise RuntimeError('Timed out during ' + name + '; retained native job IDs must be reconciled, never blindly resubmitted.')

    def pause_and_drain(self):
        mission = self.mission()
        if mission['run']['state'] == 'running':
            self.request('/api/mission/pause', {})
        self.stage('source-paused-draining-existing-deliveries')
        def settled(value):
            rows = value['assets']
            return value if value['run']['state'] == 'paused' and all(
                row['delivery']['state'] in ('verified', 'acquiring')
                and not row['delivery'].get('pending_index_ids') and not row['delivery'].get('error') for row in rows) else None
        result = self.wait('waiting-for-journaled-deliveries-to-finish', settled, self.deadline)
        self.r['paused_final_mission'] = result
        self.save()
        return result

    def history(self):
        after, all_rows = 0, []
        for _ in range(25):
            page = self.request('/api/mission/history?' + urllib.parse.urlencode({'after': after, 'limit': 1000}))
            rows = page.get('readings')
            require(isinstance(rows, list) and len(rows) <= 1000, 'Unexpected acquisition history page.')
            if not rows:
                return all_rows
            ids = [row['id'] for row in rows]
            require(all(type(ident) is int for ident in ids) and ids == sorted(set(ids))
                    and ids[0] > after and page.get('next_cursor') == ids[-1], 'History cursor or row ordering is inconsistent.')
            all_rows.extend(rows)
            after = ids[-1]
        raise RuntimeError('History exceeds the 25,000-reading acceptance bound.')

    def episode_cases(self):
        inventory = self.request('/api/cases')['cases']
        require(len(inventory) < 200, 'Case inventory may be truncated; episode uniqueness is unverified.')
        details = [self.request('/api/cases/' + row['id']) for row in inventory if row['asset_id'] == 'A-17' and row['source_id'] == SOURCE]
        return [case for case in details if case['episode'] == self.run_id + ':A-17']

    def run(self):
        self.stage('baseline-read-only-checks')
        health = self.request('/api/health')
        self.check('existing-evidence-health', health.get('ok') is True and health.get('indexed_records') == 48)
        before = self.mission()
        self.r['initial_mission'] = before
        self.r['initial_cases'] = self.request('/api/cases')
        self.r['original_seed_snapshots'] = {asset: self.request('/api/telemetry?' + urllib.parse.urlencode(
            {'asset': asset, 'mode': 'kd', 'source': 'demo-pump-replay'}))['snapshot_id'] for asset in ASSETS}
        if self.resume_run:
            require(before['run']['state'] == 'paused' and before['run']['id'] == self.resume_run,
                    '--resume-run must identify exactly the already-paused episode.')
            self.run_id = self.resume_run
            cases = self.episode_cases()
            require(not cases or len(cases) == 1 and cases[0]['status'] != 'closed'
                    and cases[0].get('reviewer_role') in ('', ROLE), 'The paused episode is not an unfinished acceptance review.')
            require(self.asset(before)['scenario'] == 'degradation' and self.asset(before).get('outcome') in (None, 'recovery'),
                    'The paused episode uses another presenter scenario or outcome.')
            started = self.request('/api/mission/resume', {})
        else:
            require(before['run']['state'] == 'idle' and before['run']['id'] is None,
                    'A fresh acceptance run requires idle state; use an explicit paused --resume-run only after inspection.')
            started = self.request('/api/mission/start', {'scenario': 'degradation'})
            self.run_id = started['run']['id']
        require(re.fullmatch(r'[0-9a-f]{32}', self.run_id or ''), 'No valid run identity was returned.')
        self.r['run_id'] = self.run_id
        self.r['source_started_at'] = now()
        self.stage('synthetic-source-started', run_id=self.run_id)

        def automatic_case(mission):
            row = self.asset(mission)
            if not row.get('open_case_id'):
                return None
            case = self.request('/api/cases/' + row['open_case_id'])
            if case['episode'] != self.run_id + ':A-17':
                return None
            return case if case['captured']['analysis']['status'] in ('watch', 'review') else None
        case = self.wait('waiting-for-automatic-condition-case', automatic_case)
        ident, captured = case['id'], case['captured']
        validate_snapshot(captured, 'A-17')
        self.r['automatic_case'] = case
        self.r['case_id'] = ident
        self.stage('automatic-condition-case-verified', case_id=ident, snapshot_id=captured['snapshot_id'])
        wrong = self.request('/api/cases', {'asset_id': 'A-17', 'source_id': SOURCE, 'snapshot_id': '0' * 64}, expected=400)
        self.check('wrong-snapshot-rejected', wrong.get('error') == 'invalid_request')
        review = self.request('/api/cases/' + ident + '/review', {
            'ids': list(EVIDENCE), 'reviewer_role': ROLE,
            'findings': 'Synthetic acceptance finding: the retained A-17 vibration window warranted review. No physical inspection was performed and the sensor trend does not establish a root cause.',
            'action': 'For this fictional exercise, compare the two captured KD references, select the authored recovery branch, and retain later measurements for human review. This is not a maintenance instruction.',
            'notes': 'Automated acceptance of the simulated maintenance episode; all role labels and outcomes are fictional.'})
        self.check('review-keeps-capture-and-verifies-two-sources', review['status'] == 'reviewed'
                   and review['captured'] == captured and set(EVIDENCE).issubset({record['id'] for record in review['evidence']}))
        expected_evidence = {record['id']: record for record in review['evidence']}
        self.r['recorded_review'] = review
        outcome = self.request('/api/mission/outcome', {'asset_id': 'A-17', 'outcome': 'recovery'})
        selected_at = now()
        self.r['recovery_selected_at'] = selected_at
        self.r['recovery_selection'] = outcome
        self.stage('authored-recovery-selected', boundary='Later readings do not establish repair causation.')
        initial_vibration = captured['analysis']['latest']['normalized_vibration_mm_s']
        def later_recovery(mission):
            snapshot = self.asset(mission).get('telemetry')
            if not snapshot or stamp(snapshot['observed_at']) <= stamp(selected_at):
                return None
            adjusted = snapshot['analysis']['latest'].get('normalized_vibration_mm_s')
            return snapshot if adjusted is not None and adjusted < initial_vibration - .2 else None
        recovery = self.wait('waiting-for-later-verified-lower-vibration', later_recovery)
        validate_snapshot(recovery, 'A-17')
        self.r['first_verified_recovery_window'] = recovery
        final_mission = self.pause_and_drain()
        self.stage('fresh-kd-readback-and-retained-history')
        history = self.history()
        run_rows = [row for row in history if row['run_id'] == self.run_id]
        self.check('history-count-matches-status', len(history) == final_mission['total_history_samples'] and len(run_rows) > 0)
        lookup = {(row['asset_id'], row['event_at']): row for row in run_rows}
        self.check('history-identities-and-acquisition-times', len(lookup) == len(run_rows) and all(
            row['sample']['timestamp'] == row['event_at'] == row['received_at']
            and stamp(row['event_at']) <= stamp(now()) for row in run_rows))
        if not self.resume_run:
            first_intent = next(row['intent_at'] for row in self.r['mutations'] if row['path'] == '/api/mission/start')
            self.check('fresh-run-does-not-backdate-readings', all(stamp(row['event_at']) >= stamp(first_intent) for row in run_rows))
        self.r['run_acquisition_history'] = run_rows
        final_snapshots = {}
        for asset in ASSETS:
            current = self.request('/api/telemetry?' + urllib.parse.urlencode({'asset': asset, 'mode': 'kd', 'source': SOURCE}))
            validate_snapshot(current, asset)
            retained = self.asset(final_mission, asset)['telemetry']
            self.check('fresh-kd-payload-matches-retained-readback-' + asset, retained is not None
                       and current['samples'] == retained['samples'] and current['snapshot_id'] == retained['snapshot_id']
                       and current['analysis'] == retained['analysis'])
            final_snapshots[asset] = current
        for label, snapshot in [('opening', captured), ('recovery', recovery), *final_snapshots.items()]:
            self.check('payload-matches-acquisition-history-' + label, all(
                lookup.get((snapshot['asset_id'], sample['timestamp']), {}).get('sample') == sample for sample in snapshot['samples']))
        self.r['final_fresh_kd_snapshots'] = final_snapshots
        follow = self.request('/api/cases/' + ident + '/follow-up', {
            'snapshot_id': final_snapshots['A-17']['snapshot_id'],
            'notes': 'Later measurements were captured after selecting an authored synthetic recovery branch. Their lower vibration is an observed simulation outcome, not proof of a physical repair or cause.'})
        self.check('later-follow-up-retains-original-capture', follow['captured'] == captured
                   and follow['follow_ups'][-1]['condition']['snapshot_id'] == final_snapshots['A-17']['snapshot_id'])
        closed = self.request('/api/cases/' + ident + '/close', {
            'disposition': 'monitor',
            'notes': 'Close this fictional review episode with continued monitoring. Later simulated vibration is lower; no actual inspection, equipment diagnosis, confirmed repair effectiveness or maintenance authorization is established.'})
        self.check('closed-case-preserves-capture-and-evidence', closed['status'] == 'closed' and closed['captured'] == captured
                   and {record['id']: record for record in closed['evidence']} == expected_evidence
                   and closed['closure']['disposition'] == 'monitor')
        duplicates = self.episode_cases()
        self.check('one-case-per-run-and-asset', len(duplicates) == 1 and duplicates[0]['id'] == ident)
        exported = self.request('/api/cases/' + ident + '/export', html=True)
        self.check('export-retains-capture-follow-up-and-evidence', all(text.encode() in exported for text in
                   (ident, captured['snapshot_id'], final_snapshots['A-17']['snapshot_id'], *EVIDENCE, ROLE, 'monitor')))
        export_path = self.path.with_suffix('.case.html')
        fresh_write(export_path, exported)
        self.r['closed_case'] = closed
        self.r['export'] = {'path': str(export_path), 'sha256': sha(exported), 'bytes': len(exported)}
        for asset in ASSETS:
            seed = self.request('/api/telemetry?' + urllib.parse.urlencode({'asset': asset, 'mode': 'kd', 'source': 'demo-pump-replay'}))
            self.check('original-seed-unchanged-' + asset, seed['snapshot_id'] == self.r['original_seed_snapshots'][asset])
        end = self.mission()
        self.check('source-left-paused-with-no-pending-delivery', end['run']['state'] == 'paused'
                   and all(row['delivery']['state'] == 'verified' and not row['delivery'].get('pending_index_ids')
                           and not row['delivery'].get('error') for row in end['assets']))
        self.check('paused-source-stopped-acquisition', end['total_history_samples'] == final_mission['total_history_samples'])
        self.r['final_mission'] = end
        self.r.update(complete=True, passed=True, completed_at=now())
        self.stage('synthetic-maintenance-episode-accepted', case_id=ident, checks=len(self.r['checks']))

    def execute(self):
        try:
            self.run()
            return 0
        except BaseException as error:
            self.r['failure'] = {'stage': self.stage_name, 'type': type(error).__name__, 'message': str(error)[:500]}
            self.r.update(complete=False, passed=False)
            self.save()
            # Never replay a failed mutation. Only pause the exact acknowledged
            # run and reconcile already-journaled deliveries within the deadline.
            if self.run_id and time.monotonic() < self.deadline:
                try:
                    self.pause_and_drain()
                    self.r['failure_cleanup'] = {'paused_and_drained': True}
                except BaseException as cleanup:
                    self.r['failure_cleanup'] = {'paused_and_drained': False, 'error': str(cleanup)[:300]}
            self.save()
            print(json.dumps({'stage': 'acceptance-failed', 'report': str(self.path), 'failure': self.r['failure'],
                              'cleanup': self.r.get('failure_cleanup')}), flush=True)
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-synthetic-episode', action='store_true', help='Explicitly authorize one app-managed synthetic feed/review/outcome episode.')
    parser.add_argument('--output', type=Path, required=True, help='Fresh private JSON receipt; companion HTML is also retained.')
    parser.add_argument('--resume-run', help='Only the exact paused, unfinished acceptance episode ID; no implicit takeover or new run.')
    parser.add_argument('--timeout-seconds', type=int, default=TIMEOUT_SECONDS,
                        help='Overall bounded run, including final pause/drain; default 1080, maximum 1140 seconds.')
    args = parser.parse_args()
    if not args.run_synthetic_episode:
        parser.error('--run-synthetic-episode is required; this verifier creates synthetic application state.')
    if args.resume_run and not re.fullmatch(r'[0-9a-f]{32}', args.resume_run):
        parser.error('--resume-run must be the exact lowercase 32-character run ID.')
    if not 300 <= args.timeout_seconds <= 1140:
        parser.error('--timeout-seconds must be 300–1140; leave headroom under the scheduled task lifetime.')
    return Acceptance(args.output, args.resume_run, args.timeout_seconds).execute()


if __name__ == '__main__':
    raise SystemExit(main())
