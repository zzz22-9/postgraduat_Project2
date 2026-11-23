"""One-off P1 acceptance, using an explicitly supplied temporary source DB and new output.

Run from minimal_factory with the isolated Python; this is a verification tool, not a
data connector. It creates simulation sandboxes and uses the existing factory HTTP API.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import socket
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import copytree
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from erp.app import create_app


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lines(path):
    raw = path.read_bytes()
    assert raw.endswith(b'\n'), path
    return [json.loads(line) for line in raw.splitlines()]


def verify(folder, run_id):
    snapshot = json.loads((folder / 'input.json').read_bytes())
    summary = json.loads((folder / 'summary.json').read_bytes())
    records = lines(folder / 'telemetry.jsonl')
    messages = lines(folder / 'protocol_messages.jsonl')
    sources = lines(folder / 'source_context.jsonl')
    events = lines(folder / 'events.jsonl')
    assert messages == summary['protocol_messages']
    assert {s['message_id'] for s in sources} == {m['message_id'] for m in messages}
    assert [e['detail'] for e in events if isinstance(e.get('detail'), dict)
            and 'message_id' in e['detail'] and 'payload' in e['detail']] == messages
    assert len({r['telemetry_id'] for r in records}) == len(records)
    assert all(a['virtual_minute'] <= b['virtual_minute'] for a, b in zip(records, records[1:]))
    orders = {w['work_order_id']: w for w in snapshot['orders']}
    by_message = {m['message_id']: m for m in messages}
    groups, file_cache = defaultdict(list), {}
    for r in records:
        wo = orders[r['work_order_id']]
        op = next(o for o in wo['operations'] if o['operation_id'] == r['operation_id'])
        assert r['run_id'] == run_id and r['order_id'] == wo['order_id']
        assert r['equipment_id'] == op['equipment_id']
        assert r['device_code'] == {'E-ASM': 'CNC_01', 'E-MOVE': 'Robot_01'}[op['equipment_id']]
        assert r['synthetic'] is True and r['transport_mode'] == 'local_derived'
        assert r['sample_period_s'] == 1 and all(math.isfinite(v) for v in r['metrics'].values())
        path = Path(r['source_file'])
        if path not in file_cache:
            raw = path.read_bytes()
            manifest = json.loads((path.parent / 'manifest.json').read_bytes())
            entry = next(e for e in manifest['field_library']['files'] if e['path'] == path.name)
            assert hashlib.sha256(raw).hexdigest() == entry['sha256']
            file_cache[path] = raw.splitlines()
        raw_sample = file_cache[path][r['reference_sample_index']]
        sample = json.loads(raw_sample)
        assert hashlib.sha256(raw_sample).hexdigest() == r['sample_hash']
        assert r['source_sample_time_s'] == sample['sample_time_s']
        assert r['source_time_s'] == sample['source_time_s']
        assert sha(path.parents[2] / sample['source_file']) == sample['source_sha256']
        if r['device_code'] == 'CNC_01':
            values = [n['value'] for n in r['payload']['nodes']]
            assert values == [r['metrics'][k] for k in
                              ('current_a', 'temperature_c', 'load_percent', 'alarm_code')]
        else:
            p = r['payload']
            frame = bytes.fromhex(p['response_frame_hex'])
            assert len(frame) == 17 and int.from_bytes(frame[4:6], 'big') == len(frame) - 6
            assert frame[6:9] == bytes([p['unit_id'], 3, 8])
            regs = [int.from_bytes(frame[i:i+2], 'big') for i in range(9, 17, 2)]
            assert regs == p['raw_registers'] and all(0 <= n <= 65535 for n in regs)
            assert r['metrics'] == dict(current_a=regs[0]/1000, temperature_c=regs[1]/10,
                                       load_percent=regs[2]/10, alarm_code=regs[3])
        if r['terminal_message_id']:
            m = by_message[r['terminal_message_id']]
            assert all(r[k] == m[k] for k in ('run_id', 'work_order_id', 'operation_id',
                       'equipment_id', 'device_code', 'virtual_minute', 'source_file', 'sample_hash', 'payload'))
            assert r['generation_method'] == 'existing_terminal_protocol_sample'
            assert r['is_anomaly'] == m['is_anomaly']
        else:
            assert r['generation_method'] == 'deterministic_template_variation_v1'
            assert r['is_anomaly'] is False
        groups[(r['work_order_id'], r['operation_id'], r['phase'], r['rework_id'])].append(r)
    group_results = []
    for key, group in groups.items():
        started = next(e for e in events if e['event_type'] == 'processing_started'
                       and tuple(e[k] for k in ('work_order_id', 'operation_id', 'phase', 'rework_id')) == key)
        wo = orders[key[0]]
        op = next(o for o in wo['operations'] if o['operation_id'] == key[1])
        seconds = op['minutes'] * started['quantity'] * 60
        assert len(group) == seconds + 1
        for sequence, r in enumerate(group):
            assert r['sample_sequence'] == sequence and r['elapsed_s'] == sequence
            assert abs(r['virtual_minute'] - (started['sim_time_min'] + sequence/60)) < 1e-8
        assert group[-1]['terminal_message_id']
        assert len({r['metrics']['current_a'] for r in group}) > 2
        group_results.append(dict(work_order_id=key[0], operation_id=key[1], phase=key[2],
            rework_id=key[3], device=group[0]['device_code'], count=len(group),
            start_min=group[0]['virtual_minute'], end_min=group[-1]['virtual_minute']))
    with closing(sqlite3.connect((folder / 'mes.db').as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert [json.loads(row[0]) for row in conn.execute('SELECT raw_json FROM protocol_messages ORDER BY rowid')] == messages
        assert all(row[0] == 'completed' for row in conn.execute('SELECT status FROM work_orders'))
        assert all(row[0] == 'available' for row in conn.execute('SELECT status FROM equipment'))
        for r in records:
            if r['phase'] != 'normal':
                task = conn.execute('SELECT * FROM reworks WHERE rework_id=?', (r['rework_id'],)).fetchone()
                assert task['work_order_id'] == r['work_order_id']
                assert task['target_operation_id'] == r['operation_id'] and r['phase'] == 'repair'
    return dict(run_id=run_id, folder=str(folder), makespan_min=summary['makespan_min'],
                protocol_count=len(messages), source_count=len(sources), event_count=len(events),
                telemetry_count=len(records), anomaly_count=sum(r['is_anomaly'] for r in records),
                rework_quantity=summary['rework_quantity'], groups=group_results,
                examples={device: next(r for r in records if r['device_code'] == device and r['sample_sequence'] == 1)
                          for device in ('CNC_01', 'Robot_01')}), records


def normalize(value, rw_ids):
    if isinstance(value, dict):
        return {k: normalize(v, rw_ids) for k, v in value.items() if k != 'recorded_at'}
    if isinstance(value, list):
        return [normalize(v, rw_ids) for v in value]
    return rw_ids.get(value, value) if isinstance(value, str) else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', required=True, type=Path, help='temporary copy only')
    parser.add_argument('--work-order-id', required=True)
    parser.add_argument('--output', required=True, type=Path, help='must not exist')
    parser.add_argument('--baseline-engine', required=True, type=Path, help='unmodified P0.4 engine source')
    parser.add_argument('--legacy-run', required=True, type=Path, help='temporary copy of an old run')
    args = parser.parse_args()
    args.source_db = args.source_db.resolve()
    args.output = args.output.resolve()
    factory = Path(__file__).resolve().parents[1]
    assert args.source_db.is_file() and not args.source_db.is_relative_to(factory / 'data')
    assert not args.output.exists() and not args.output.is_relative_to(factory / 'data')
    args.output.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location('simulation._p04_engine', args.baseline_engine)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    app = create_app(args.source_db, mes_base_url=url)
    service = app.state.simulation
    service.root = args.output / 'runs'
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    client = build_opener(ProxyHandler({}))
    requests, results, protected = [], [], {}

    def request(path, data=None, expected=200):
        req = Request(url + path, data=None if data is None else json.dumps(data).encode(),
                      headers={'Content-Type': 'application/json'})
        try:
            response = client.open(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            assert response.status == expected, (path, response.status, raw)
            body = json.loads(raw)
            requests.append(dict(path=path, method=req.get_method(), status=response.status,
                response_sha256=hashlib.sha256(raw).hexdigest(), total=body.get('total') if isinstance(body, dict) else None))
            return body

    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        request('/health')
        for name, mode, devices, minutes, count in (
                ('normal', 'normal', None, 12, 6),
                ('rework', 'anomaly', ['E-QC'], 17, 10),
                ('all_faults', 'anomaly', None, 28, 10)):
            scenario = dict(mode=mode)
            if devices is not None:
                scenario['anomaly_equipment_ids'] = devices
            payload = dict(source='work_orders', work_order_ids=[args.work_order_id],
                           overrides={args.work_order_id: dict(field_protocols=scenario)})
            rid = request('/simulation/runs', payload, 202)['run_id']
            deadline = time.monotonic() + 30
            while True:
                record = request(f'/simulation/runs/{rid}')
                if record['status'] != 'running':
                    break
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert record['status'] == 'completed', record.get('error')
            folder = service.root / rid
            result, records = verify(folder, rid)
            result['scenario'] = name
            assert (result['makespan_min'], result['protocol_count']) == (minutes, count)
            before = args.output / 'p04-baseline' / rid
            before.mkdir(parents=True)
            old = baseline.simulate(before, rid, record['input'], threading.Event(), lambda *_: None)
            old_events = lines(before / 'events.jsonl')
            new_events = lines(folder / 'events.jsonl')
            rw = lambda events: {k: f'REWORK-{i}' for i, k in enumerate(dict.fromkeys(
                e['rework_id'] for e in events if e.get('rework_id')))}
            assert normalize(old, rw(old_events)) == normalize(record['summary'], rw(new_events))
            assert normalize(old_events, rw(old_events)) == normalize(new_events, rw(new_events))
            for filename in ('protocol_messages.jsonl', 'source_context.jsonl'):
                assert (before / filename).read_bytes() == (folder / filename).read_bytes()
            result['p04_comparison'] = 'summary/events equal after mapping rework UUIDs and removing recorded_at; protocol/source byte-identical'
            received, offset, pages = [], 0, 0
            while True:
                page = request(f'/simulation/runs/{rid}/telemetry?offset={offset}&limit=100')
                assert page['total'] == len(records) and page['available']
                received.extend(page['items'])
                pages += 1
                if page['next_offset'] is None:
                    break
                offset = page['next_offset']
            assert received == records
            result['http_pages'] = pages
            assert request(f'/simulation/runs/{rid}/telemetry?offset=99999')['items'] == []
            for query in ('offset=-1', 'limit=0', 'limit=1001', 'offset=abc', 'limit=abc'):
                request(f'/simulation/runs/{rid}/telemetry?{query}', expected=422)
            assert request(f'/simulation/runs/{rid}/export')['summary'] == record['summary']
            for path, digest in protected.items():
                assert sha(path) == digest
            protected.update({p: sha(p) for p in folder.iterdir() if p.is_file()})
            results.append(result)
        legacy = service.root / args.legacy_run.name
        copytree(args.legacy_run, legacy)
        old_hashes = {p: sha(p) for p in legacy.rglob('*') if p.is_file()}
        assert request(f'/simulation/runs/{legacy.name}')['status'] == 'completed'
        assert request(f'/simulation/runs/{legacy.name}/telemetry')['available'] is False
        assert not (legacy / 'telemetry.jsonl').exists()
        assert all(sha(p) == digest for p, digest in old_hashes.items())
        request('/simulation/runs/bad/telemetry', expected=404)
        request('/simulation/runs/SIM-' + 'f'*32 + '/telemetry', expected=404)
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
    assert not thread.is_alive()
    evidence = dict(time=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    url=url, server_stopped=True, source_db=str(args.source_db),
                    baseline_engine_sha256=sha(args.baseline_engine), scenarios=results,
                    protected_new_run_files=len(protected), legacy_files_unchanged=len(old_hashes), http=requests)
    (args.output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in evidence.items() if k not in ('http', 'scenarios')}, ensure_ascii=False))
    print(json.dumps([{k: v for k, v in r.items() if k != 'examples'} for r in results], ensure_ascii=False))


if __name__ == '__main__':
    main()
