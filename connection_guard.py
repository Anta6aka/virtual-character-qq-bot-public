"""Local QQ health, bounded recovery and private offline notices. No model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timedelta
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent


def last_ingress(path):
    """Read timestamps only from the existing native event log; never save message text."""
    path = Path(path)
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 131072))
        text = stream.read().decode('utf-8', errors='replace')
    modified = datetime.fromtimestamp(path.stat().st_mtime)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    times = re.findall(r'^\[(\d\d:\d\d:\d\d)(?:\.\d+)?\][^\n]*\(aiocqhttp\)', text, re.M)
    if not times:
        return None
    stamp = datetime.combine(modified.date(), datetime.strptime(times[-1], '%H:%M:%S').time())
    if stamp > modified + timedelta(seconds=5):
        stamp -= timedelta(days=1)
    return stamp.timestamp()


def missed_mention(messages, account, role_accounts, received_at, now):
    if received_at is None:
        return False
    for message in messages:
        stamp = float(message.get('time', 0))
        parts = message.get('message', [])
        if (not 90 <= now - stamp <= 600 or stamp <= received_at + 2
                or str(message.get('user_id')) in role_accounts or not isinstance(parts, list)):
            continue
        if any(p.get('type') == 'at' and str(p.get('data', {}).get('qq')) == account for p in parts):
            return True
    return False


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + '.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    pending.replace(path)


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    roles = cfg.get('roles', [])
    if (cfg.get('schema') != 1 or not str(cfg.get('recipient', '')).isdigit()
            or len(roles) != 2 or len({r.get('uin') for r in roles}) != 2
            or len({r.get('port') for r in roles}) != 2):
        raise ValueError('Invalid private connection guard configuration')
    for role in roles:
        if (not str(role.get('uin', '')).isdigit() or role.get('port') not in (6099, 6100)
                or role.get('onebot_port') not in (6199, 6200)
                or not isinstance(role.get('name'), str) or not role['name'].strip()):
            raise ValueError('Invalid role endpoint')
    return cfg


def transport_inventory(roles):
    """Map management and reverse-WS sockets to the same account's QQ worker."""
    script = r'''
$p=@(Get-CimInstance Win32_Process | Where-Object Name -eq 'QQ.exe')
$c=@(Get-NetTCPConnection -ErrorAction Stop)
@{processes=@($p | Select-Object ProcessId,ParentProcessId,CommandLine,ExecutablePath); sockets=@($c | Where-Object { ($_.LocalPort -ge 6099 -and $_.LocalPort -le 6119) -or $_.RemotePort -in @(6199,6200) } | Select-Object LocalPort,RemotePort,OwningProcess,State)} | ConvertTo-Json -Depth 5 -Compress
'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, timeout=18, check=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    data = json.loads(result.stdout.decode('utf-8-sig'))
    processes = {p['ProcessId']: p for p in data['processes']}
    sockets = data['sockets']
    reports = {}
    for role in roles:
        connections = [s['OwningProcess'] for s in sockets
                       if s['RemotePort'] == role['onebot_port'] and s['State'] == 5]
        def belongs(worker):
            parent = processes.get(processes.get(worker, {}).get('ParentProcessId'), {})
            return (str(role['uin']) in (parent.get('CommandLine') or '').split()
                    and 'NapCatQQ Desktop' in (parent.get('ExecutablePath') or ''))
        candidates = [s for s in sockets if s['State'] == 2 and 6099 <= s['LocalPort'] <= 6119
                      and belongs(s['OwningProcess'])]
        # NapCat automatically chooses the next free port. Prefer the actual reverse-WS worker,
        # never the stale login UI still occupying the preferred port.
        connected_candidates = [s for s in candidates if set(connections) == {s['OwningProcess']}]
        chosen = connected_candidates if connected_candidates else candidates
        selected = chosen[0] if len(chosen) == 1 else None
        worker = selected['OwningProcess'] if selected else None
        parent = processes.get(processes.get(worker, {}).get('ParentProcessId'), {})
        command = parent.get('CommandLine') or ''
        bound = (str(role['uin']) in command.split()
                 and 'NapCatQQ Desktop' in (parent.get('ExecutablePath') or ''))
        reports[role['uin']] = {'account_bound': bound,
                               'api_port': selected['LocalPort'] if selected else None,
                               'worker_matches': bool(worker and set(connections) == {worker}),
                               'transport_connected': bool(connections),
                               'account_processes': sum(str(role['uin']) in (p.get('CommandLine') or '').split()
                                   and 'NapCatQQ Desktop' in (p.get('ExecutablePath') or '') for p in processes.values())}
    return reports


class NapCatClient:
    def __init__(self, role, token_file):
        self.role = role
        self.port = role['port']
        self.token_file = Path(token_file)
        self.credential = None
        self.opener = build_opener(ProxyHandler({}))

    def post(self, path, payload=None, authenticate=True):
        if authenticate and not self.credential:
            token = json.loads(self.token_file.read_text(encoding='utf-8-sig'))['token']
            auth = self.post('/auth/login', {'hash': hashlib.sha256((token + '.napcat').encode()).hexdigest()}, False)
            self.credential = auth['Credential']
        headers = {'Content-Type': 'application/json'}
        if authenticate:
            headers['Authorization'] = 'Bearer ' + self.credential
        request = Request(f"http://127.0.0.1:{self.port}/api" + path,
                          json.dumps(payload or {}).encode(), headers)
        try:
            with self.opener.open(request, timeout=6) as response:
                result = json.load(response)
        except HTTPError as error:
            if error.code in (401, 403):
                self.credential = None  # A restarted QQ worker has a new WebUI session secret.
            raise
        if result.get('code') != 0:
            self.credential = None
            raise RuntimeError('Local NapCat API rejected operation')
        return result.get('data')

    def action(self, name, params=None):
        result = self.post('/Debug/call', {'action': name, 'params': params or {}})
        if not isinstance(result, dict) or result.get('retcode') != 0 or result.get('status') != 'ok':
            raise RuntimeError('OneBot operation unavailable')
        return result.get('data')

    def snapshot(self, transport):
        discovered = transport.get('api_port')
        if discovered is not None:
            if not isinstance(discovered, int) or not 6099 <= discovered <= 6119:
                raise ValueError('Untrusted management port')
            if discovered != self.port:
                self.port = discovered
                self.credential = None
        status = self.post('/QQLogin/CheckLoginStatus') or {}
        report = {'phase': status.get('loginPhase'), 'logged_in': status.get('isLogin') is True,
                  'ready': status.get('coreReady') is True, **transport}
        report['identity_matches'] = False
        report['online'] = False
        if report['logged_in'] and report['ready']:
            identity = self.action('get_login_info') or {}
            report['identity_matches'] = str(identity.get('user_id')) == self.role['uin']
            live = self.action('get_status') or {}
            report['online'] = live.get('online') is True and live.get('good') is True
        report['healthy'] = all(report.get(k) is True for k in
                                ('logged_in', 'ready', 'identity_matches', 'online',
                                 'account_bound', 'worker_matches'))
        report['healthy'] = report['healthy'] and transport.get('account_processes', 1) == 1
        return report

    def recover_absent(self):
        subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                        '-File', str(ROOT / 'start-qq.ps1'), '-Port', str(self.role['port'])],
                       check=True, capture_output=True, timeout=25,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def send_private(self, recipient, message):
        result = self.action('send_private_msg', {'user_id': int(recipient), 'message': message})
        if not isinstance(result, dict) or not result.get('message_id'):
            raise RuntimeError('Private message delivery not acknowledged')

    def try_quick_login(self):
        """One request for this account only; no password, device reset or QR automation."""
        for path in ('/QQLogin/GetQuickLoginListNew', '/QQLogin/GetQuickLoginList'):
            data = self.post(path)
            if isinstance(data, list) and any(isinstance(item, dict) and
                    str(item.get('uin') or item.get('qq') or item.get('account') or item.get('user_id')) == self.role['uin']
                    for item in data):
                self.post('/QQLogin/SetQuickLogin', {'uin': self.role['uin']})
                return True
        return False


class Guard:
    DEBOUNCE = 2
    ALERT_INTERVAL = 1800
    EVENT_FIELDS = ('healthy', 'phase', 'logged_in', 'ready', 'transport_connected',
                    'online', 'identity_matches', 'account_bound', 'worker_matches', 'api_port',
                    'ingress_stalled', 'ingress_check_error')

    def __init__(self, config, state_path, clients=None, inventory=transport_inventory, clock=time.time):
        self.config = config
        self.path = Path(state_path)
        self.events_path = self.path.with_name('connection_events.jsonl')
        self.clock = clock
        self.inventory = inventory
        self.clients = clients or {r['uin']: NapCatClient(r, config['token_file']) for r in config['roles']}
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.previous = {}

    @staticmethod
    def _event_state(status):
        return {key: status.get(key) for key in Guard.EVENT_FIELDS if key in status}

    def _record_transitions(self, statuses, now):
        """Append one JSON line per observed connection state change; no message bodies."""
        lines = []
        for role in self.config['roles']:
            key = role['uin']
            snapshot = self._event_state(statuses.get(key, {}))
            previous = self.previous.get(key)
            self.previous[key] = snapshot
            if previous is None:
                event = {'event': 'initial_observed'}
            elif previous != snapshot:
                changed = sorted(k for k in set(previous) | set(snapshot)
                                 if previous.get(k) != snapshot.get(k))
                event = {'event': 'state_changed', 'changed': changed}
            else:
                continue
            lines.append({'at': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now)),
                          'role': role.get('name', '?'), 'uin': str(key)[:3] + '***',
                          'state': snapshot, **event})
        if not lines:
            return
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open('a', encoding='utf-8') as stream:
                for line in lines:
                    stream.write(json.dumps(line, ensure_ascii=False) + '\n')
            if self.events_path.stat().st_size > 512 * 1024:
                kept = self.events_path.read_text(encoding='utf-8').splitlines()[-2000:]
                pending = self.events_path.with_suffix('.jsonl.pending')
                pending.write_text('\n'.join(kept) + '\n', encoding='utf-8')
                pending.replace(self.events_path)
        except OSError:
            # The timeline is diagnostic; a locked file must never block recovery.
            pass

    def tick(self):
        now = self.clock()
        try:
            inventory = self.inventory(self.config['roles'])
        except Exception:
            inventory = {}
        statuses = {}
        for role in self.config['roles']:
            try:
                statuses[role['uin']] = self.clients[role['uin']].snapshot(inventory.get(role['uin'], {}))
            except Exception as error:
                statuses[role['uin']] = {'healthy': False, 'error': type(error).__name__}
        # An independently online peer can witness a real @ that never reached this AstrBot.
        # Silence alone never triggers recovery. This checks ingress, not model response quality.
        for role in self.config['roles']:
            key = role['uin']
            state = self.state.setdefault(key, {})
            if not statuses[key].get('healthy') or not role.get('runtime') or not role.get('group_id'):
                continue
            if now - state.get('last_ingress_check', 0) < 90 and not state.get('failures'):
                continue
            state['last_ingress_check'] = now
            peer = next((r for r in self.config['roles'] if r['uin'] != key and statuses[r['uin']].get('healthy')), None)
            if peer is None:
                continue
            try:
                received = last_ingress(Path(role['runtime']) / 'run_out.log')
                if received is not None and now - received > 90:
                    history = self.clients[peer['uin']].action('get_group_msg_history', {
                        'group_id': role['group_id'], 'message_seq': 0, 'count': 20,
                        'disable_get_url': True, 'parse_mult_msg': False, 'quick_reply': True}) or {}
                    if missed_mention(history.get('messages', []), key, self.clients.keys(), received, now):
                        statuses[key].update(healthy=False, ingress_stalled=True)
            except Exception as error:
                statuses[key]['ingress_check_error'] = type(error).__name__
        # Snapshot after ingress detection so a "half-dead but online" stall is on the timeline.
        self._record_transitions(statuses, now)
        for role in self.config['roles']:
            key = role['uin']
            status = statuses[key]
            state = self.state.setdefault(key, {})
            if status.get('healthy'):
                preserved = {k: state.get(k, 0) for k in ('last_recovery', 'last_ingress_check')}
                state.clear()
                state.update(failures=0, healthy=True, **preserved)
                continue
            state['healthy'] = False
            state.setdefault('first_failure_at', now)
            state['failures'] = state.get('failures', 0) + 1
            if state['failures'] < self.DEBOUNCE:
                continue
            if status.get('ingress_stalled') and now - state.get('last_recovery', 0) >= 1800:
                state['last_recovery'] = now
                atomic_json(self.path, self.state)
                try:
                    self.clients[key].post('/QQLogin/RestartNapCat')
                    state['recovery'] = 'supported_restart_requested'
                except Exception as error:
                    state['last_error'] = type(error).__name__
                continue
            transport = inventory.get(key, {})
            if (transport.get('account_processes') == 0 and not transport.get('transport_connected')
                    and not transport.get('account_bound')
                    and now - state.get('last_recovery', 0) >= 1800):
                state['last_recovery'] = now
                atomic_json(self.path, self.state)
                try:
                    self.clients[key].recover_absent()
                    state['recovery'] = 'single_launcher_started'
                except Exception as error:
                    state['last_error'] = type(error).__name__
                continue
            if (status.get('account_bound') and not status.get('logged_in')
                    and not status.get('transport_connected') and status.get('phase') == 'waiting_qrcode'
                    and not state.get('quick_attempted')):
                state['quick_attempted'] = now
                atomic_json(self.path, self.state)
                try:
                    if self.clients[key].try_quick_login():
                        state['quick_requested'] = now
                        continue
                except Exception as error:
                    state['last_error'] = type(error).__name__
            if now - state.get('quick_requested', 0) < 90:
                continue
            if status.get('phase') == 'qrcode_scanned' and now - state['first_failure_at'] < 120:
                continue  # Give the owner time to confirm a live camera scan.
            sender = next((r for r in self.config['roles'] if r['uin'] != key and statuses[r['uin']].get('healthy')), None)
            if sender is None:
                continue
            try:
                if now - state.get('last_alert_attempt', 0) >= self.ALERT_INTERVAL:
                    # Reserve before I/O: ambiguous timeouts must not cause a resend storm.
                    state['last_alert_attempt'] = now
                    atomic_json(self.path, self.state)
                    if not self.clients[sender['uin']].snapshot(inventory.get(sender['uin'], {})).get('healthy'):
                        continue
                    self.clients[sender['uin']].send_private(self.config['recipient'], [
                        {'type': 'text', 'data': {'text': f"{role['name']}的连接检查未通过，需要在电脑打开NapCatQQ Desktop核对登录。若显示二维码，请用手机实时扫码；已停用保存图片扫码。程序没有反复重启账号。"}}])
                    state['notice'] = 'alert_acknowledged'
            except Exception as error:
                state['last_error'] = type(error).__name__
        atomic_json(self.path, self.state)
        return {role['name']: statuses[role['uin']] for role in self.config['roles']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'connection_guard.json')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--check', action='store_true', help='Read-only health; never send or save state')
    args = parser.parse_args()
    config = load_config(args.config)
    guard = Guard(config, ROOT / 'state/connection_guard.json')
    if args.check:
        inventory = transport_inventory(config['roles'])
        report = {}
        for role in config['roles']:
            try:
                report[role['name']] = guard.clients[role['uin']].snapshot(inventory[role['uin']])
            except Exception as error:
                report[role['name']] = {'healthy': False, 'error': type(error).__name__}
        print(json.dumps(report, ensure_ascii=False))
        raise SystemExit(0 if all(v.get('healthy') for v in report.values()) else 2)
    while True:
        try:
            report = guard.tick()
            # Only anonymous status; no credentials, IDs, QR URLs or message bodies.
            atomic_json(ROOT / 'state/connection_health.json', {'checked_at': time.time(), 'roles': report})
        except Exception as error:
            print('Connection guard iteration failed: ' + type(error).__name__, flush=True)
        if not args.watch:
            break
        time.sleep(30)


if __name__ == '__main__':
    main()
