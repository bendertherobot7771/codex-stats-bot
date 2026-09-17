from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.releases import check_file, verify, version
from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.lifecycle import Lifecycle
from server.codex_stats_server.telegram_bot import TelegramBot
from server.codex_stats_server.http_api import create_server
from agent.codex_stats_agent import updater

FIXTURE = Path(__file__).parent / 'fixtures' / 'signed-zero-release.json'


class SignatureTests(unittest.TestCase):
    def test_real_rsa_signature(self):
        self.assertEqual(verify(json.loads(FIXTURE.read_text()))['version'], '0.0.0')

    def test_payload_tampering_rejected(self):
        envelope = json.loads(FIXTURE.read_text())
        raw = base64.b64decode(envelope['payload']).replace(b'0.0.0', b'9.9.9')
        envelope['payload'] = base64.b64encode(raw).decode()
        with self.assertRaises(ValueError):
            verify(envelope)

    def test_signature_tampering_and_truncation_rejected(self):
        for value in (b'x' * 384, b'x', b'\xff' * 384):
            envelope = json.loads(FIXTURE.read_text())
            envelope['signature'] = base64.b64encode(value).decode()
            with self.assertRaises(ValueError):
                verify(envelope)

    def test_stable_versions_only(self):
        for value in ('../x', 'v1.0.0', '1.0.0-beta', '1.0', '-1.0.0'):
            with self.assertRaises(ValueError):
                version(value)
        self.assertGreater(version('1.10.0'), version('1.9.0'))

    def test_size_and_hash_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'package'
            path.write_bytes(b'x')
            check_file(path, {'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()})
            with self.assertRaises(ValueError):
                check_file(path, {'size': 2, 'sha256': hashlib.sha256(b'x').hexdigest()})

    @unittest.skipUnless(os.name == 'nt','Windows RSA provider')
    def test_bootstrap_dotnet_verifies_same_signature(self):
        import subprocess
        source = (Path(__file__).parents[1]/'agent'/'bootstrap.ps1').read_text(encoding='utf-8-sig')
        crypto = source[source.index('$modulusHex ='):source.index('$rsa.Dispose()')+len('$rsa.Dispose()')]
        envelope = json.loads(FIXTURE.read_text())
        prefix = f"$ErrorActionPreference='Stop'; $payloadBytes=[Convert]::FromBase64String('{envelope['payload']}'); $signature=[Convert]::FromBase64String('{envelope['signature']}');\n"
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory)/'verify.ps1'
            script.write_text(prefix+crypto,encoding='utf-8-sig')
            result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = StatsDatabase(Path(self.temp.name) / 'db.sqlite')
        self.notices = []
        self.life = Lifecycle(self.db, 'http://192.168.1.2:8765', Path(self.temp.name), self.notices.append)
        self.life.current_release = lambda: json.loads(FIXTURE.read_text())
        self.clock = patch('server.codex_stats_server.lifecycle.time.time', return_value=1000)
        self.now = self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.db.close()
        self.temp.cleanup()

    def checkin(self, busy=False, queued=0, **extra):
        return self.life.checkin(dict(machine_name='PC', version='0.4.0', protocol=1, busy=busy, queued=queued, **extra), 'pc')

    def offer(self):
        self.life.save({'version':'0.4.1','envelope':{'fixture':True},'phase':'waiting'})

    def test_enrollment_single_use_expiry_and_device_token(self):
        code = self.life.code(1)
        result = self.life.enroll({'code':code,'machine_id':'pc','machine_name':'PC'})
        self.assertEqual(self.life.authenticate(result['api_key']), 'pc')
        self.assertIsNone(self.life.authenticate('wrong-token'))
        with self.assertRaises(ValueError):
            self.life.enroll({'code':code,'machine_id':'pc2','machine_name':'PC2'})
        code = self.life.code(1)
        self.now.return_value = 1901
        with self.assertRaises(ValueError):
            self.life.enroll({'code':code,'machine_id':'pc3','machine_name':'PC3'})

    def test_concurrent_enrollment_uses_code_once(self):
        code = self.life.code(1)
        results = []
        def call(i):
            try:
                self.life.enroll({'code':code,'machine_id':str(i),'machine_name':'PC'})
                results.append(True)
            except ValueError:
                results.append(False)
        threads = [threading.Thread(target=call,args=(i,)) for i in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(results.count(True), 1)

    def test_server_requires_idle_and_warning_and_rechecks_activity(self):
        self.offer()
        self.checkin(busy=True)
        self.assertEqual(self.life.control({'operation':'claim'})['phase'], 'waiting')
        self.checkin()
        self.now.return_value = 1059
        self.checkin()
        self.assertEqual(self.life.control({'operation':'claim'})['phase'], 'waiting')
        self.now.return_value = 1060
        self.checkin()
        self.assertEqual(self.life.control({'operation':'claim'})['phase'], 'warning')
        self.assertEqual(len(self.notices),1)
        self.now.return_value = 1090
        self.checkin(busy=True)
        self.assertEqual(self.life.state()['phase'], 'waiting')
        self.checkin()
        self.now.return_value = 1150
        self.checkin()
        self.life.control({'operation':'claim'})
        self.now.return_value = 1211
        self.checkin()
        self.assertEqual(self.life.control({'operation':'claim'})['phase'], 'server')
        self.assertTrue(self.life.blocked())
        self.life.control({'operation':'finish'})
        self.assertFalse(self.life.blocked())

    def test_notification_failure_blocks_update(self):
        self.offer()
        self.checkin()
        self.now.return_value = 1121
        self.checkin()
        self.life.announce = lambda text: (_ for _ in ()).throw(RuntimeError('Telegram unavailable'))
        with self.assertRaises(RuntimeError):
            self.life.control({'operation':'claim'})
        self.assertEqual(self.life.state()['phase'], 'waiting')

    def test_client_gets_lease_only_after_warning(self):
        self.offer()
        state = self.life.state(); state['phase']='clients'; self.life.save(state)
        self.checkin()
        self.now.return_value = 1121
        reply = self.checkin(begin_update='0.4.1')
        self.assertNotIn('lease_until',reply)
        self.now.return_value = 1180
        self.assertNotIn('lease_until',self.checkin(begin_update='0.4.1'))
        self.now.return_value = 1182
        self.assertGreater(self.checkin(begin_update='0.4.1')['lease_until'],1182)
        self.checkin(busy=True)
        self.assertNotIn('lease_until',self.checkin(begin_update='0.4.1'))

    def test_queued_data_prevents_idle(self):
        self.offer(); self.checkin(queued=1)
        self.now.return_value = 2000
        self.checkin(queued=1)
        self.assertFalse(self.life.ready()[0])

    def test_client_idle_boundary_is_60_seconds_plus_60_second_warning(self):
        self.offer()
        state = self.life.state(); state['phase'] = 'clients'; self.life.save(state)
        self.checkin()
        self.now.return_value = 1059
        self.assertNotIn('lease_until', self.checkin(begin_update='0.4.1'))
        self.assertEqual(self.notices, [])
        self.now.return_value = 1060
        self.assertNotIn('lease_until', self.checkin(begin_update='0.4.1'))
        self.assertEqual(len(self.notices), 1)
        self.now.return_value = 1119
        self.assertNotIn('lease_until', self.checkin(begin_update='0.4.1'))
        self.now.return_value = 1120
        self.assertGreater(self.checkin(begin_update='0.4.1')['lease_until'], 1120)

    def test_new_task_event_revokes_idle_without_waiting_for_checkin(self):
        self.offer(); self.checkin()
        self.now.return_value=1121; self.checkin()
        self.life.control({'operation':'claim'})
        self.life.on_event({'event_type':'task_started','machine_id':'pc','sent_at':1121})
        self.assertEqual(self.life.state()['phase'],'waiting')
        self.assertFalse(self.life.ready()[0])

    def test_addpc_remains_admin_alias_and_instructions_have_url(self):
        bot = TelegramBot('test',{1},set(),self.db,self.life)
        self.db.ensure_bot_user(2,'viewer')
        bot._handle_update({'message':{'chat':{'id':2},'text':'/addpc'}})
        text = bot.outgoing.get_nowait()[1]['text']
        self.assertNotIn(' -Code ',text)
        bot._handle_update({'message':{'chat':{'id':1},'text':'/addpc'}})
        text = bot.outgoing.get_nowait()[1]['text']
        self.assertIn('/agent/bootstrap.ps1',text)
        self.assertIn(' -Code ',text)
        self.assertIn('15 минут',text)


    def test_install_for_existing_admin_and_viewers(self):
        bot = TelegramBot('test', {1}, {2, 3}, self.db, self.life)
        codes = set()
        for chat_id in (1, 2, 3):
            bot._handle_update({'message': {'chat': {'id': chat_id, 'type': 'private'}, 'text': '/install'}})
            text = bot.outgoing.get_nowait()[1]['text']
            command = next(line for line in text.splitlines() if line.startswith('$p ='))
            self.assertIn("-ServerUrl 'http://192.168.1.2:8765'", command)
            self.assertIn('-ErrorAction Stop;', command)
            self.assertIn('/v0.4.8/agent/bootstrap.ps1', command)
            code = command.split(" -Code '")[1].split("'")[0]
            self.assertNotIn(code, codes)
            codes.add(code)
            result = self.life.enroll({'code': code, 'machine_id': str(chat_id), 'machine_name': 'PC'})
            self.assertEqual(self.life.authenticate(result['api_key']), str(chat_id))
        self.assertEqual(self.db.bot_user(2)['role'], 'viewer')
        bot._handle_update({'message': {'chat': {'id': 2}, 'text': '/adduser 4'}})
        self.assertIsNone(self.db.bot_user(4))

    def test_install_denies_unknown_disabled_and_group_without_codes(self):
        bot = TelegramBot('test', {1}, {2, -123}, self.db, self.life)
        self.db.disable_bot_user(2)
        for chat_id, kind in ((99, 'private'), (2, 'private'), (-123, 'group')):
            bot._handle_update({'message': {'chat': {'id': chat_id, 'type': kind}, 'text': '/install'}})
            self.assertNotIn(' -Code ', bot.outgoing.get_nowait()[1]['text'])
        self.assertEqual(self.db._connection.execute('SELECT count(*) FROM enrollment_codes').fetchone()[0], 0)

    def test_install_local_configuration_and_invalid_arguments(self):
        bot = TelegramBot('test', {1}, set(), self.db, self.life)
        for command in ('/install local', '/install typo', '/install local extra'):
            bot._handle_update({'message': {'chat': {'id': 1}, 'text': command}})
            self.assertNotIn(' -Code ', bot.outgoing.get_nowait()[1]['text'])
        self.life.local_url = 'http://192.168.32.125:8765'
        bot._handle_update({'message': {'chat': {'id': 1}, 'text': '/install@codex_stats_bot local'}})
        self.assertIn("-ServerUrl 'http://192.168.32.125:8765'", bot.outgoing.get_nowait()[1]['text'])

    def test_install_in_help_and_menu(self):
        from server.codex_stats_server.telegram_bot import _help, _telegram_commands
        for role in ('admin', 'viewer'):
            self.assertIn('/install local', _help(role))
        self.assertIn('install', [item['command'] for item in _telegram_commands()])
        self.assertIn('install_local', [item['command'] for item in _telegram_commands()])

    def test_local_menu_command_for_viewer(self):
        bot = TelegramBot('test', {1}, {2}, self.db, self.life)
        self.life.local_url = 'http://192.168.32.125:8765'
        self.life.public_url = 'https://example.com:28443'
        for command, address, heading in (
            ('/install_local', self.life.local_url, 'локальная сеть сервера'),
            ('/install_local@codex_stats_bot', self.life.local_url, 'локальная сеть сервера'),
            ('/install', self.life.public_url, 'интернет (глобальная сеть)'),
        ):
            bot._handle_update({'message': {'chat': {'id': 2, 'type': 'private'}, 'text': command}})
            text = bot.outgoing.get_nowait()[1]['text']
            self.assertIn(heading, text.splitlines()[0])
            self.assertIn("-ServerUrl '" + address + "'", text)


class WindowsRollbackTests(unittest.TestCase):
    def execute_case(self, healthy):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root/'app'; stage = app/'updates'/'0.4.1'; stage.mkdir(parents=True)
            target = root/'local'/'CodexStatsAgent'/'codex-stats-agent.exe'; target.parent.mkdir(parents=True)
            target.write_bytes(b'old')
            (stage/'codex-stats-agent.exe').write_bytes(b'new')
            (stage/'release.json').write_text('{}')
            (stage/'guard.json').write_text(json.dumps({'logs':{},'lease_until':time.time()+60}))
            cfg = SimpleNamespace(codex_home=str(root/'codex'))
            manifest = {'version':'0.4.1','assets':{'codex-stats-agent.exe':{'size':3,'sha256':hashlib.sha256(b'new').hexdigest()}}}
            class Process:
                def __init__(self,*args,**kwargs):
                    if healthy:
                        (app/'updates'/'health.json').write_text(json.dumps({'version':'0.4.1','at':time.time()+1}))
                def poll(self): return 0 if not healthy else None
                def wait(self,timeout=None): return 0
            with patch.object(updater,'APP_DIR',app), patch.object(updater.AgentConfig,'load',return_value=cfg), \
                 patch.object(updater,'verify',return_value=manifest), patch.dict(os.environ,LOCALAPPDATA=str(root/'local')), \
                 patch.object(updater.subprocess,'CREATE_NO_WINDOW',0,create=True), patch.object(updater.subprocess,'Popen',Process):
                result = updater.apply_update(stage, installed_version='0.4.0')
            self.assertEqual(result, 0 if healthy else 1)
            self.assertEqual(target.read_bytes(), b'new' if healthy else b'old')
            return json.loads((app/'updates'/'result.json').read_text())

    def test_successful_update(self):
        self.assertEqual(self.execute_case(True)['update_result'],'success')

    def test_failed_start_rolls_back(self):
        self.assertEqual(self.execute_case(False)['update_result'],'rollback')


class ScopedHttpTests(unittest.TestCase):
    def test_enrolled_client_is_scoped_and_maintenance_queues_events(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StatsDatabase(Path(directory)/'db.sqlite')
            life = Lifecycle(db,'',Path(directory))
            life.current_release = lambda: json.loads(FIXTURE.read_text())
            code = life.code(1)
            server = create_server('127.0.0.1',0,db,'master',lifecycle=life)
            thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            base = f'http://127.0.0.1:{server.server_address[1]}'
            def request(path,data,key=''):
                req = urllib.request.Request(base+path,data=json.dumps(data).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=2) as response: return json.load(response)
            try:
                token = request('/api/v1/enroll',{'code':code,'machine_id':'pc','machine_name':'PC'})['api_key']
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/v1/control/updates',{'operation':'status'},token)
                self.assertEqual(error.exception.code,401); error.exception.close()
                event = {'event_id':'x','event_type':'quota_snapshot','task_id':'q','account_fingerprint':'a','machine_id':'another-pc','quota':{}}
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/v1/events',event,token)
                self.assertEqual(error.exception.code,403); error.exception.close()
                event['machine_id']='pc'
                life.save({'phase':'server'})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/api/v1/events',event,token)
                self.assertEqual(error.exception.code,503); error.exception.close()
                self.assertEqual(len(db.accounting_data()[1]),0)
                life.save({'phase':'clients'})
                self.assertFalse(request('/api/v1/events',event,token)['duplicate'])
                self.assertTrue(request('/api/v1/events',event,token)['duplicate'])
                self.assertEqual(len(db.accounting_data()[1]),1)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2); db.close()


@unittest.skipUnless(os.name == 'posix','Linux updater')
class ServerRecoveryTests(unittest.TestCase):
    def test_legacy_migration_requires_fresh_idle_proof_and_no_later_work(self):
        from server.codex_stats_server.activate_legacy import idle_proofs
        events = [{'machine_id':'pc','sent_at':999,'maintenance_probe':{'busy':False,'queued':0,'idle_since':600}}]
        self.assertTrue(idle_proofs(events,1000))
        events.append({'machine_id':'pc','sent_at':1000,'event_type':'task_started'})
        self.assertFalse(idle_proofs(events,1000))
        self.assertFalse(idle_proofs([],1000))
    def test_archive_traversal_is_rejected(self):
        import io
        import tarfile
        from server.codex_stats_server.update_service import extract_verified
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root/'bad.tar'
            with tarfile.open(archive,'w') as bundle:
                entry = tarfile.TarInfo('server/../../escape'); entry.size=1
                bundle.addfile(entry,io.BytesIO(b'x'))
            with self.assertRaises(ValueError): extract_verified(archive,root/'stage')
            self.assertFalse((root/'escape').exists())

    def test_failed_health_restores_previous_code_not_database(self):
        from server.codex_stats_server import update_service as service
        journal = {'previous':'/old','new_version':'0.4.1','old_version':'0.4.0'}
        with tempfile.TemporaryDirectory() as directory, patch.object(service,'STATE',Path(directory)), \
             patch.object(service,'healthy',side_effect=lambda value: value=='0.4.0'), \
             patch.object(service,'run_service') as systemd, patch.object(service,'switch_release') as switch, \
             patch.object(service,'control',return_value={}) as control:
            service.recover(journal)
            switch.assert_called_once_with(Path('/old'))
            self.assertEqual([call.args[0] for call in systemd.call_args_list],['stop','start'])
            self.assertEqual(control.call_args.args[0],'failure')
            self.assertTrue(json.loads((Path(directory)/'transaction.json').read_text())['complete'])

    def test_healthy_recovery_reopens_admission_without_rollback(self):
        from server.codex_stats_server import update_service as service
        with tempfile.TemporaryDirectory() as directory, patch.object(service,'STATE',Path(directory)), \
             patch.object(service,'healthy',return_value=True), patch.object(service,'switch_release') as switch, \
             patch.object(service,'control',return_value={}) as control:
            service.recover({'previous':'/old','new_version':'0.4.1','old_version':'0.4.0'})
            switch.assert_not_called(); control.assert_called_once_with('finish')
