"""换位采录模拟器：满载回路互换的本地规划与执行。

每次换位以签核负荷、旧确认分配、相位版本和独立复位状态为起点，
每一步都保证每个展台恰有一份可用分配、每个相位合计不超过有效容量。
模拟器定义两类可获签操作：一个展台的独立移位，以及由同一张设备
原件回执完整见证的成组移位。没有成组许可的现场，即使两张单独回执
时间相同也不按同时生效处理。只使用 SQLite、合成容量单位和模拟
设备回执，不接真实供电设备。
"""
import json
from collections import deque

from .source import record
from .store import log

MAX_DEPTH = 8
DEVICE = 'synthetic-controller'


def device_sign_group(permit, moves, at):
    """模拟设备为成组移位签发回执；成员与许可不完全一致时拒签。"""
    participants = sorted(permit.get('participants') or [])
    if not participants or sorted(moves) != participants:
        return None
    circuits = set(permit.get('circuit_pair') or permit.get('circuits') or [])
    if circuits and not set(moves.values()) <= circuits:
        return None
    return {'id': 'rcpt-' + permit['id'] + '-' + '-'.join(participants),
            'kind': 'group', 'permit': permit['id'], 'members': participants,
            'moves': {b: moves[b] for b in participants},
            'device': permit.get('issuer') or DEVICE, 'at': at}


def _positions(db, booths):
    """已见证的当前物理位置：每个展台恰一份确认分配，否则记入问题。"""
    pos, problems = {}, []
    for b in sorted(booths):
        rows = db.execute("SELECT circuit FROM power_allocation WHERE booth=? AND state='confirmed' ORDER BY id", (b,)).fetchall()
        if len(rows) == 1:
            pos[b] = rows[0]['circuit']
        else:
            pos[b] = None
            problems.append(f'participant {b} has {len(rows)} confirmed allocations')
    return pos, problems


def _spot_reason(db, booth, circuit_name, kw, formal, start, end):
    from .circuit import current
    from .safety import circuit_isolated
    cur, err = current(db, circuit_name)
    if err:
        return err
    if start < cur['start'] or end > cur['end']:
        return 'outside circuit service window'
    if formal:
        dec = db.execute('SELECT * FROM declaration_decision WHERE booth=?', (booth,)).fetchone()
        if not dec or dec['state'] in ('unsigned', 'disputed') or kw != dec['allowed_kw']:
            return 'declaration capacity unavailable'
    if circuit_isolated(db, circuit_name):
        return 'circuit isolated pending independent reset'
    return None


def _positions_reason(db, positions, kws, formals, start, end):
    """校验一组假想位置：窗口、签核负荷、复位状态与相位合计容量。"""
    from .circuit import current, phase_capacity
    for b, c in positions.items():
        reason = _spot_reason(db, b, c, kws[b], formals[b], start, end)
        if reason:
            return reason
    used = {}
    for b, c in positions.items():
        cur, err = current(db, c)
        if err:
            return err
        used[cur['phase']] = used.get(cur['phase'], 0.0) + kws[b]
    marks = ','.join('?' for _ in positions) or "''"
    rows = db.execute(f"SELECT * FROM power_allocation WHERE state='confirmed' AND booth NOT IN ({marks})",
                      tuple(positions)).fetchall()
    for row in rows:
        cur, err = current(db, row['circuit'])
        if err:
            return 'existing phase allocation has uncertain circuit version'
        if row['start'] < end and start < row['end']:
            used[cur['phase']] = used.get(cur['phase'], 0.0) + row['kw']
    for phase, total in used.items():
        cap, err = phase_capacity(db, phase)
        if err:
            return err
        if total > cap:
            return 'overlapping peak exceeds breaker capacity'
    return None


def _circuits(db):
    from .circuit import current
    names = [r['circuit'] for r in db.execute('SELECT DISTINCT circuit FROM circuit_version ORDER BY circuit')]
    return [n for n in names if current(db, n)[1] is None]


def _permit(db, participants):
    for row in db.execute('SELECT * FROM group_permit ORDER BY id'):
        if sorted(json.loads(row['participants_json'])) == sorted(participants):
            return row
    return None


def _complete_receipt(db, permit_id, participants, targets):
    want = {b: targets[b] for b in targets}
    for row in db.execute("SELECT * FROM device_receipt WHERE permit_id=? AND kind='group' ORDER BY id", (permit_id,)):
        if sorted(json.loads(row['members_json'])) == sorted(participants) and json.loads(row['moves_json']) == want:
            return row
    return None


def _group_action(db, plan, positions, targets, kws, formals):
    """成组移位仅在许可与完整回执齐备、目标状态合法时可用。"""
    permit = _permit(db, targets)
    if not permit:
        return None
    circuits = set(json.loads(permit['circuits_json']))
    if circuits and not (set(positions.values()) | set(targets.values())) <= circuits:
        return None
    receipt = _complete_receipt(db, permit['id'], list(targets), targets)
    if not receipt:
        return None
    if _positions_reason(db, dict(targets), kws, formals, plan['start'], plan['end']):
        return None
    return {'kind': 'group', 'receipt': receipt['id'],
            'moves': [{'booth': b, 'from': positions[b], 'to': targets[b]}
                      for b in sorted(targets) if positions[b] != targets[b]]}


def _report(db, plan, positions, problems):
    """阻碍报告：形成循环的实际占用与缺少的凭据。"""
    from .circuit import current, phase_capacity
    targets = json.loads(plan['targets_json'])
    kws = json.loads(plan['kw_json'])
    formals = json.loads(plan['formal_json'])
    start, end = plan['start'], plan['end']
    cycle = []
    for b in sorted(targets):
        if positions.get(b) == targets[b]:
            continue
        holder = db.execute("SELECT id, booth FROM power_allocation WHERE state='confirmed' AND circuit=? AND start<? AND end>? ORDER BY id LIMIT 1",
                            (targets[b], end, start)).fetchone()
        cycle.append({'booth': b, 'holds': positions.get(b), 'target': targets[b],
                      'target_held_by': holder['booth'] if holder else None,
                      'target_allocation': holder['id'] if holder else None})
    missing = list(problems)
    circuits = _circuits(db)
    staging = False
    blocked_staging = []
    if all(positions.get(b) for b in targets):
        for c in circuits:
            if c in positions.values():
                continue
            holder = db.execute("SELECT booth, id FROM power_allocation WHERE state='confirmed' AND circuit=? AND start<? AND end>? ORDER BY id LIMIT 1",
                                (c, end, start)).fetchone()
            if holder:
                blocked_staging.append(f"{c} occupied by {holder['booth']} allocation {holder['id']}")
        for b in sorted(targets):
            for c in circuits:
                if c == positions[b]:
                    continue
                new = dict(positions)
                new[b] = c
                if _positions_reason(db, new, kws, formals, start, end) is None:
                    staging = True
                    break
            if staging:
                break
    if not staging:
        note = 'no staging circuit with free capacity for any participant'
        if blocked_staging:
            note += '; ' + '; '.join(blocked_staging)
        missing.append(note)
    for b in sorted(targets):
        cur, err = current(db, targets[b])
        if err:
            continue
        cap, cap_err = phase_capacity(db, cur['phase'])
        if not cap_err and cap is not None and cap < kws[b]:
            missing.append('circuit %s phase %s effective capacity %s below required %s (version %s)'
                           % (targets[b], cur['phase'], cap, kws[b], cur['event_id']))
    participants = sorted(targets)
    permit = _permit(db, participants)
    if not permit:
        missing.append('no group permit evidence covering participants ' + ','.join(participants))
    elif not _complete_receipt(db, permit['id'], participants, targets):
        seen = [sorted(json.loads(r['members_json'])) for r in db.execute(
            "SELECT members_json FROM device_receipt WHERE permit_id=? AND kind='group'", (permit['id'],))]
        missing.append('complete device receipt for permit %s covering %s absent; witnessed members: %s'
                       % (permit['id'], ','.join(participants), seen or 'none'))
    return {'positions': positions, 'targets': targets, 'window': [start, end],
            'cycle': cycle, 'missing': missing}


def plan_remaining(db, plan):
    """从已见证的当前位置重算剩余路径；返回 (动作序列, None) 或 (None, 阻碍报告)。"""
    targets = json.loads(plan['targets_json'])
    kws = json.loads(plan['kw_json'])
    formals = json.loads(plan['formal_json'])
    positions, problems = _positions(db, targets)
    if problems:
        return None, _report(db, plan, positions, problems)
    if all(positions[b] == targets[b] for b in targets):
        return [], None
    group = _group_action(db, plan, positions, targets, kws, formals)
    if group:
        return [group], None
    circuits = _circuits(db)
    visited = {tuple(sorted(positions.items()))}
    queue = deque([(positions, [])])
    while queue:
        pos, path = queue.popleft()
        if len(path) >= MAX_DEPTH:
            continue
        for b in sorted(targets):
            for c in circuits:
                if c == pos[b]:
                    continue
                new = dict(pos)
                new[b] = c
                key = tuple(sorted(new.items()))
                if key in visited:
                    continue
                if _positions_reason(db, new, kws, formals, plan['start'], plan['end']):
                    continue
                visited.add(key)
                npath = path + [{'kind': 'shift', 'booth': b, 'from': pos[b], 'to': c}]
                if all(new[x] == targets[x] for x in targets):
                    return npath, None
                queue.append((new, npath))
    return None, _report(db, plan, positions, [])


def _apply_move(db, plan, action_id, booth, to, kw, formal, start, end):
    from .circuit import current
    old = db.execute("SELECT * FROM power_allocation WHERE booth=? AND state='confirmed'", (booth,)).fetchone()
    db.execute("UPDATE power_allocation SET state='released', reason=? WHERE id=?",
               (f'superseded by exchange action {action_id}', old['id']))
    log(db, 'circuit', old['id'], 'released', f'superseded by exchange action {action_id}')
    new_id = f'{action_id}-{booth}'
    dec = db.execute('SELECT * FROM declaration_decision WHERE booth=?', (booth,)).fetchone()
    cur, _ = current(db, to)
    db.execute('INSERT INTO power_allocation(id,booth,circuit,start,end,kw,formal,priority,state,reason,decl_version,circuit_event) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (new_id, booth, to, start, end, kw, formal, old['priority'], 'confirmed',
                f'exchange action {action_id}', dec['version'] if dec else None, cur['event_id']))
    log(db, 'circuit', new_id, 'confirmed', f'exchange action {action_id}', action_id)
    return {'booth': booth, 'from': old['circuit'], 'to': to,
            'old_allocation': old['id'], 'new_allocation': new_id}


def _execute(db, plan, action):
    """执行一个已规划动作；返回 (动作原件 id, None) 或 (None, 失败原因)。"""
    from .safety import reconcile_all
    targets = json.loads(plan['targets_json'])
    kws = json.loads(plan['kw_json'])
    formals = json.loads(plan['formal_json'])
    start, end = plan['start'], plan['end']
    seq = db.execute('SELECT count(*) FROM exchange_action WHERE exchange_id=?', (plan['id'],)).fetchone()[0] + 1
    action_id = f"{plan['id']}-a{seq}"
    positions, problems = _positions(db, targets)
    if problems:
        return None, problems[0]
    moves = []
    if action['kind'] == 'group':
        permit = _permit(db, targets)
        receipt = _complete_receipt(db, permit['id'], list(targets), targets) if permit else None
        if not receipt:
            return None, 'complete device receipt withdrawn before group execution'
        reason = _positions_reason(db, dict(targets), kws, formals, start, end)
        if reason:
            return None, reason
        receipt_id = receipt['id']
        for b in sorted(targets):
            if positions[b] != targets[b]:
                moves.append(_apply_move(db, plan, action_id, b, targets[b], kws[b], formals[b], start, end))
    else:
        booth, to = action['booth'], action['to']
        if positions.get(booth) != action['from']:
            return None, f"stale plan: {booth} no longer on {action['from']}"
        new = dict(positions)
        new[booth] = to
        reason = _positions_reason(db, new, kws, formals, start, end)
        if reason:
            return None, reason
        receipt_id = f'rcpt-{action_id}'
        receipt = {'id': receipt_id, 'kind': 'shift', 'permit': None, 'members': [booth],
                   'moves': {booth: to}, 'device': DEVICE, 'at': start}
        if record(db, receipt_id, 'device_receipt', receipt):
            db.execute('INSERT INTO device_receipt(id,kind,permit_id,members_json,moves_json,device,at) VALUES(?,?,?,?,?,?,?)',
                       (receipt_id, 'shift', None, json.dumps([booth]), json.dumps({booth: to}), DEVICE, start))
        moves.append(_apply_move(db, plan, action_id, booth, to, kws[booth], formals[booth], start, end))
    db.execute('INSERT INTO exchange_action(id,exchange_id,seq,kind,moves_json,receipt_id,state) VALUES(?,?,?,?,?,?,?)',
               (action_id, plan['id'], seq, action['kind'], json.dumps(moves, sort_keys=True), receipt_id, 'executed'))
    log(db, 'exchange', action_id, 'executed', f"{action['kind']} " + ','.join(m['booth'] for m in moves), receipt_id)
    reconcile_all(db)
    return action_id, None


def _update_plan(db, plan, state, reason, report):
    db.execute('UPDATE exchange_plan SET state=?, reason=?, report_json=?, version=? WHERE id=?',
               (state, reason, json.dumps(report, sort_keys=True), plan['version'] + 1, plan['id']))


def _finalize(db, plan):
    for aid in json.loads(plan['apps_json']):
        row = db.execute("SELECT id FROM power_allocation WHERE id=? AND state='held'", (aid,)).fetchone()
        if row:
            db.execute("UPDATE power_allocation SET state='released', reason=? WHERE id=?",
                       (f"fulfilled by exchange {plan['id']}", aid))
            log(db, 'circuit', aid, 'released', f"fulfilled by exchange {plan['id']}")
    _update_plan(db, plan, 'completed', 'exchange complete', {'path': []})
    log(db, 'exchange', plan['id'], 'completed', 'exchange complete')


def _at_goal(positions, targets):
    return all(positions.get(b) == targets[b] for b in targets)


def _mark_blocked(db, plan, reason, report):
    if plan['state'] != 'blocked' or plan['reason'] != reason:
        _update_plan(db, plan, 'blocked', reason, report)
        log(db, 'exchange', plan['id'], 'blocked', reason)
    return {'id': plan['id'], 'exchange': True, 'state': 'blocked', 'reason': reason}


def _advance_one(db, plan):
    base = {'id': plan['id'], 'exchange': True}
    targets = json.loads(plan['targets_json'])
    if not targets:
        if plan['state'] == 'blocked':
            return {**base, 'state': 'blocked', 'reason': plan['reason']}
        return _mark_blocked(db, plan, 'exchange has no member moves', {'missing': ['exchange has no member moves']})
    positions, _ = _positions(db, targets)
    if _at_goal(positions, targets):
        _finalize(db, plan)
        return {**base, 'state': 'completed', 'reason': 'exchange complete'}
    path, report = plan_remaining(db, plan)
    if path is None:
        reason = report['missing'][0] if report['missing'] else 'no legal exchange path'
        return _mark_blocked(db, plan, reason, report)
    action_id, err = _execute(db, plan, path[0])
    if err:
        report = report or {'missing': []}
        report.setdefault('missing', []).append(err)
        return _mark_blocked(db, plan, err, report)
    positions, _ = _positions(db, targets)
    if _at_goal(positions, targets):
        _finalize(db, plan)
        return {**base, 'state': 'completed', 'reason': 'exchange complete'}
    remaining = path[1:]
    reason = f'{len(remaining)} exchange actions remain'
    _update_plan(db, plan, 'executing', reason, {'path': remaining})
    log(db, 'exchange', plan['id'], 'executing', reason)
    return {**base, 'state': 'executing', 'reason': f'executed {action_id}'}


def advance(db):
    """每个未完成的换位各推进一步；每步都从已见证位置重算剩余路径。"""
    out = []
    for plan in db.execute("SELECT * FROM exchange_plan WHERE state<>'completed' ORDER BY rowid").fetchall():
        out.append(_advance_one(db, dict(plan)))
    return out


def register(db, ex):
    """登记换位清单并接管成员申请；证据同 ID 重放幂等。"""
    eid = ex['id']
    if not record(db, eid, 'exchange', ex):
        return None
    apps = [db.execute('SELECT * FROM power_allocation WHERE id=?', (aid,)).fetchone()
            for aid in ex.get('applications', [])]
    problem = None
    if not apps or any(a is None for a in apps):
        problem = 'member application absent'
    elif len({a['booth'] for a in apps}) != len(apps):
        problem = 'duplicate booth in exchange'
    elif len({(a['start'], a['end']) for a in apps}) != 1:
        problem = 'exchange member windows differ'
    elif any(a['state'] != 'waiting' for a in apps):
        problem = 'member application not waiting'
    else:
        for a in apps:
            n = db.execute("SELECT count(*) FROM power_allocation WHERE booth=? AND state='confirmed'", (a['booth'],)).fetchone()[0]
            if n != 1:
                problem = f"participant {a['booth']} has {n} confirmed allocations"
                break
    targets = {a['booth']: a['circuit'] for a in apps if a}
    kws = {a['booth']: a['kw'] for a in apps if a}
    formals = {a['booth']: a['formal'] for a in apps if a}
    start = apps[0]['start'] if apps and apps[0] else ''
    end = apps[0]['end'] if apps and apps[0] else ''
    state = 'blocked' if problem else 'executing'
    reason = problem or 'exchange registered'
    if not problem:
        for a in apps:
            db.execute("UPDATE power_allocation SET state='held', reason=? WHERE id=?",
                       (f'managed by exchange {eid}', a['id']))
            db.execute('DELETE FROM waiting_application WHERE id=?', (a['id'],))
            log(db, 'circuit', a['id'], 'held', f'managed by exchange {eid}')
    db.execute('INSERT INTO exchange_plan(id,state,reason,targets_json,kw_json,formal_json,apps_json,start,end,report_json,version) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (eid, state, reason, json.dumps(targets, sort_keys=True), json.dumps(kws, sort_keys=True),
                json.dumps(formals, sort_keys=True), json.dumps(sorted(a['id'] for a in apps if a)),
                start, end, '{}', 1))
    log(db, 'exchange', eid, state, reason, eid)
    return {'id': eid, 'state': state, 'reason': reason, 'exchange': True}


def status(db, exchange_id):
    """换位报告：状态路径或阻碍环、动作原件、旧分配与许可的版本归属。"""
    plan = db.execute('SELECT * FROM exchange_plan WHERE id=?', (exchange_id,)).fetchone()
    if not plan:
        return None
    targets = json.loads(plan['targets_json'])
    report = json.loads(plan['report_json']) if plan['report_json'] else {}
    positions, _ = _positions(db, targets)
    executed = []
    for row in db.execute('SELECT * FROM exchange_action WHERE exchange_id=? ORDER BY seq', (exchange_id,)):
        moves = json.loads(row['moves_json'])
        for m in moves:
            old = db.execute('SELECT circuit_event, decl_version, state FROM power_allocation WHERE id=?', (m['old_allocation'],)).fetchone()
            new = db.execute('SELECT circuit_event, decl_version, state FROM power_allocation WHERE id=?', (m['new_allocation'],)).fetchone()
            m['old_circuit_event'] = old['circuit_event'] if old else None
            m['old_decl_version'] = old['decl_version'] if old else None
            m['new_circuit_event'] = new['circuit_event'] if new else None
            m['new_decl_version'] = new['decl_version'] if new else None
            m['new_state'] = new['state'] if new else None
        executed.append({'id': row['id'], 'seq': row['seq'], 'kind': row['kind'],
                         'receipt': row['receipt_id'], 'moves': moves})
    permits = {}
    for b in sorted(targets):
        perm = db.execute('SELECT * FROM energize_permit WHERE booth=?', (b,)).fetchone()
        permits[b] = dict(perm) if perm else None
    return {'id': plan['id'], 'state': plan['state'], 'reason': plan['reason'],
            'version': plan['version'], 'targets': targets, 'positions': positions,
            'window': [plan['start'], plan['end']], 'executed': executed,
            'remaining': report.get('path'), 'cycle': report.get('cycle'),
            'missing': report.get('missing'), 'permits': permits}
