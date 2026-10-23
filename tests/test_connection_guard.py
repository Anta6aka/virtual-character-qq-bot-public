import json
from pathlib import Path
import tempfile
import unittest

from connection_guard import Guard, NapCatClient, load_config, transport_inventory, missed_mention, last_ingress
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock
import io


class Client:
    def __init__(self, status):
        self.status = status
        self.sent = []
        self.refreshes = 0
        self.quick_attempts = 0
        self.recoveries = 0
        self.fail = False

    def snapshot(self, inventory):
        return dict(self.status)

    def qr_image(self):
        raise AssertionError('QR images must never be generated')

    def try_quick_login(self):
        self.quick_attempts += 1
        return False

    def recover_absent(self):
        self.recoveries += 1

    def post(self, path):
        self.refreshes += 1

    def send_private(self, recipient, message):
        self.sent.append((recipient, message))
        if self.fail:
            raise TimeoutError()


class ConnectionGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.config = {'schema': 1, 'recipient': '10003', 'roles': [
            {'uin': '10001', 'name': 'A', 'port': 6099, 'onebot_port': 6199},
            {'uin': '10002', 'name': 'B', 'port': 6100, 'onebot_port': 6200}]}
        self.waiting = {'healthy': False, 'logged_in': False, 'phase': 'waiting_qrcode',
                        'account_bound': True, 'transport_connected': False}
        self.clients = {'10001': Client(self.waiting), '10002': Client({'healthy': True})}
        self.now = 10000
        self.guard = Guard(self.config, self.path, self.clients, lambda roles: {}, lambda: self.now)

    def twice(self):
        self.guard.tick()
        self.guard.tick()

    def events(self):
        path = Path(self.temp.name) / 'connection_events.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_connection_timeline_records_initial_and_transitions_only(self):
        self.guard.tick()
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual({item['event'] for item in events}, {'initial_observed'})
        self.assertEqual({item['role'] for item in events}, {'A', 'B'})
        rendered = json.dumps(events, ensure_ascii=False)
        self.assertNotIn('10001', rendered)
        self.assertNotIn('10003', rendered)
        self.clients['10002'].status = {'healthy': False, 'phase': 'ready', 'transport_connected': False}
        self.guard.tick()
        changed = [item for item in self.events() if item['event'] == 'state_changed']
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]['role'], 'B')
        self.assertIn('healthy', changed[0]['changed'])
        self.guard.tick()
        self.assertEqual(len(self.events()), 3)

    def test_offline_debounced_and_text_only_to_fixed_private_recipient(self):
        self.guard.tick()
        self.assertFalse(self.clients['10002'].sent)
        self.guard.tick()
        recipient, parts = self.clients['10002'].sent[0]
        self.assertEqual(recipient, '10003')
        self.assertEqual([p['type'] for p in parts], ['text'])
        self.assertNotIn('NapCatQQ Desktop', self.path.read_text())
        self.assertEqual(self.clients['10001'].quick_attempts, 1)

    def test_stale_tcp_never_authorizes_wrong_account_qr(self):
        self.clients['10001'].status = {**self.waiting, 'transport_connected': True}
        self.twice()
        self.assertEqual(len(self.clients['10002'].sent), 1)
        self.assertTrue(all(p['type'] != 'image' for p in self.clients['10002'].sent[0][1]))

    def test_unknown_account_never_sends_qr(self):
        self.clients['10001'].status = {**self.waiting, 'account_bound': False}
        self.twice()
        self.assertTrue(all(p['type'] != 'image' for p in self.clients['10002'].sent[0][1]))

    def test_both_offline_cannot_send(self):
        self.clients['10002'].status = self.waiting
        self.twice()
        self.assertFalse(self.clients['10001'].sent or self.clients['10002'].sent)

    def test_recovery_clears_incident_and_prevents_pending_qr(self):
        self.twice()
        self.clients['10001'].status = {'healthy': True}
        self.now += 130
        self.guard.tick()
        self.assertEqual(len(self.clients['10002'].sent), 1)
        self.assertTrue(self.guard.state['10001']['healthy'])
        self.assertEqual(self.guard.state['10001']['failures'], 0)

    def test_durable_dedup_survives_restart_and_quick_login_only_once(self):
        self.twice()
        self.guard = Guard(self.config, self.path, self.clients, lambda roles: {}, lambda: self.now)
        self.guard.tick()
        self.assertEqual(len(self.clients['10002'].sent), 1)
        for _ in range(8):
            self.now += 121
            self.guard.tick()
        self.assertEqual(len(self.clients['10002'].sent), 1)
        self.assertEqual(self.clients['10001'].quick_attempts, 1)

    def test_missing_qq_recovers_once_without_duplicate_launch(self):
        self.guard.inventory = lambda roles: {'10001': {'account_processes': 0, 'transport_connected': False, 'account_bound': False}}
        self.clients['10001'].status = {'healthy': False, 'error': 'ConnectionRefusedError'}
        self.twice()
        self.guard.tick()
        self.assertEqual(self.clients['10001'].recoveries, 1)

    def test_present_account_or_unknown_inventory_never_launches_duplicate(self):
        self.guard.inventory = lambda roles: {'10001': {'account_processes': 1, 'transport_connected': False, 'account_bound': False}}
        self.twice()
        self.assertEqual(self.clients['10001'].recoveries, 0)

    def test_scanning_owner_is_not_interrupted(self):
        self.clients['10001'].status = {**self.waiting, 'phase': 'qrcode_scanned'}
        self.twice()
        self.assertFalse(self.clients['10002'].sent)
        self.assertEqual(self.clients['10001'].quick_attempts, 0)

    def test_ambiguous_send_timeout_has_no_immediate_retry(self):
        self.clients['10002'].fail = True
        self.twice()
        self.guard.tick()
        self.assertEqual(len(self.clients['10002'].sent), 1)
        self.assertEqual(self.guard.state['10001']['last_error'], 'TimeoutError')

    def test_snapshot_rejects_wrong_identity_and_false_online(self):
        role = self.config['roles'][0]
        client = NapCatClient(role, self.path)
        client.post = lambda *a: {'isLogin': True, 'coreReady': True, 'loginPhase': 'ready'}
        client.action = lambda name: {'user_id': '99999'} if name == 'get_login_info' else {'online': True, 'good': True}
        self.assertFalse(client.snapshot({'account_bound': True, 'worker_matches': True})['healthy'])
        client.action = lambda name: {'user_id': '10001'} if name == 'get_login_info' else {'online': False, 'good': True}
        self.assertFalse(client.snapshot({'account_bound': True, 'worker_matches': True})['healthy'])

    def test_snapshot_rejects_stale_worker_even_if_login_reports_true(self):
        client = NapCatClient(self.config['roles'][0], self.path)
        client.post = lambda *a: {'isLogin': True, 'coreReady': True}
        client.action = lambda name: {'user_id': '10001'} if name == 'get_login_info' else {'online': True, 'good': True}
        self.assertFalse(client.snapshot({'account_bound': True, 'worker_matches': False})['healthy'])
        self.assertTrue(client.snapshot({'account_bound': True, 'worker_matches': True})['healthy'])
        self.assertFalse(client.snapshot({'account_bound': True, 'worker_matches': True, 'account_processes': 2})['healthy'])

    def test_config_rejects_ambiguous_roles_or_recipient(self):
        self.path.write_text(json.dumps({**self.config, 'recipient': 'Antabaka'}))
        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_dynamic_port_follows_reverse_worker_instead_of_old_login_ui(self):
        processes = []
        sockets = []
        for root, worker, port in [(10, 11, 6099), (20, 21, 6101)]:
            processes.extend([{'ProcessId': root, 'ParentProcessId': 1,
                'CommandLine': 'QQ.exe -q 10001', 'ExecutablePath': 'C:/NapCatQQ Desktop/QQ.exe'},
                {'ProcessId': worker, 'ParentProcessId': root}])
            sockets.append({'LocalPort': port, 'RemotePort': 0, 'OwningProcess': worker, 'State': 2})
        sockets.append({'LocalPort': 50000, 'RemotePort': 6199, 'OwningProcess': 21, 'State': 5})
        result = SimpleNamespace(stdout=json.dumps({'processes': processes, 'sockets': sockets}).encode())
        with patch('connection_guard.subprocess.run', return_value=result):
            report = transport_inventory([self.config['roles'][0]])['10001']
        self.assertEqual(report['api_port'], 6101)
        self.assertTrue(report['worker_matches'])
        self.assertEqual(report['account_processes'], 2)

    def test_quick_login_cannot_switch_to_another_saved_account(self):
        client = NapCatClient(self.config['roles'][0], self.path)
        calls = []
        def post(path, value=None):
            calls.append(path)
            return [{'uin': '99999'}]
        client.post = post
        self.assertFalse(client.try_quick_login())
        self.assertNotIn('/QQLogin/SetQuickLogin', calls)

    def test_ingress_stall_requires_old_unreceived_human_mention(self):
        mention = {'time': 9800, 'user_id': '10003', 'message': [{'type': 'at', 'data': {'qq': '10001'}}]}
        self.assertTrue(missed_mention([mention], '10001', {'10001', '10002'}, 9700, 10000))
        self.assertFalse(missed_mention([mention], '10001', {'10001', '10002'}, 9900, 10000))
        self.assertFalse(missed_mention([mention], '10001', {'10001', '10002'}, 9700, 9850))
        self.assertFalse(missed_mention([mention], '10002', {'10001', '10002'}, 9700, 10000))
        self.assertFalse(missed_mention([], '10001', {'10001', '10002'}, 9700, 10000))
        mention['user_id'] = '10002'
        self.assertFalse(missed_mention([mention], '10001', {'10001', '10002'}, 9700, 10000))

    def test_native_ingress_reads_ansi_timestamp_without_saving_body(self):
        self.path.write_text('\x1b[32m[00:00:01.123] [INFO] (aiocqhttp)] secret-text\n', encoding='utf-8')
        self.assertIsInstance(last_ingress(self.path), float)

    def test_confirmed_missed_mention_recovers_after_two_polls(self):
        self.config['roles'][0].update(runtime=self.temp.name, group_id='10004')
        self.clients['10001'].status = {'healthy': True}
        self.clients['10002'].action = lambda *args: {'messages': [
            {'time': 9800, 'user_id': '10003', 'message': [{'type': 'at', 'data': {'qq': '10001'}}]}]}
        with patch('connection_guard.last_ingress', return_value=9700):
            self.guard.tick()
            self.now += 30
            self.guard.tick()
        self.assertEqual(self.clients['10001'].refreshes, 1)

    def test_ingress_stall_is_written_to_the_connection_timeline(self):
        self.config['roles'][0].update(runtime=self.temp.name, group_id='10004')
        self.clients['10001'].status = {'healthy': True, 'phase': 'ready'}
        self.clients['10002'].action = lambda *args: {'messages': [
            {'time': 9800, 'user_id': '10003', 'message': [{'type': 'at', 'data': {'qq': '10001'}}]}]}
        with patch('connection_guard.last_ingress', side_effect=[9950, 9700]):
            self.guard.tick()
            self.assertNotIn('ingress_stalled', self.events()[0]['state'])
            self.now += 100
            self.guard.tick()
        stalled = [item for item in self.events()
                   if item['event'] == 'state_changed' and item['role'] == 'A']
        self.assertEqual(len(stalled), 1, self.events())
        self.assertIn('ingress_stalled', stalled[0]['changed'])
        self.assertTrue(stalled[0]['state']['ingress_stalled'])
        self.assertFalse(stalled[0]['state']['healthy'])

    def test_restarted_webui_reauthenticates_after_expired_session(self):
        self.path.write_text('{"token":"isolated-secret"}')
        client = NapCatClient(self.config['roles'][0], self.path)
        client.credential = 'old-session'
        client.opener = SimpleNamespace(open=Mock(side_effect=[
            HTTPError('http://127.0.0.1', 401, 'expired', {}, None),
            io.BytesIO(b'{"code":0,"data":{"Credential":"new-session"}}'),
            io.BytesIO(b'{"code":0,"data":{"isLogin":true}}')]))
        with self.assertRaises(HTTPError):
            client.post('/QQLogin/CheckLoginStatus')
        self.assertIsNone(client.credential)
        self.assertTrue(client.post('/QQLogin/CheckLoginStatus')['isLogin'])
        self.assertEqual(client.credential, 'new-session')


if __name__ == '__main__':
    unittest.main()
