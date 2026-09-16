import hashlib
import json
import os
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.codex_stats_agent import source_update as source
from agent.codex_stats_agent.instance import single_instance


class SourceTests(unittest.TestCase):
    def test_launcher_recovers_interrupted_switch_before_loading_new_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = Path(__file__).resolve().parents[1]
            shutil.copy2(repository/'agent'/'launch.py', root/'launch.py')
            for folder in ('agent', 'common'):
                shutil.copytree(repository/folder, root/'recovery'/folder, ignore=shutil.ignore_patterns('__pycache__'))
            selected = root/'releases'/'0.4.0'/'agent'/'codex_stats_agent'
            selected.mkdir(parents=True)
            (selected/'__main__.py').write_text('def main():\n    print("RECOVERED_OLD_CODE")\n    return 0\n')
            source.atomic(root/'current.json', {'version':'0.4.1'})
            source.atomic(root/'pending.json', {'previous':{'version':'0.4.0'}, 'version':'0.4.1'})
            env = dict(os.environ, APPDATA=str(root/'data'))
            env.pop('CODEX_STATS_UPDATE_CHILD', None)
            result = subprocess.run([sys.executable,'-B',str(root/'launch.py'),'--version'],
                                    env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('RECOVERED_OLD_CODE',result.stdout)
            self.assertFalse((root/'pending.json').exists())

    def test_lock_excludes_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'lock'
            code = ('from agent.codex_stats_agent.instance import single_instance; '
                    'from pathlib import Path; import sys; '
                    'with_lock = single_instance(Path(sys.argv[1])); with_lock.__enter__()')
            with single_instance(lock):
                result = subprocess.run([sys.executable, '-B', '-c', code, str(lock)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            result = subprocess.run([sys.executable, '-B', '-c', code, str(lock)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_archive_rejects_unsafe_entries_before_writing(self):
        for name in ('agent/../../evil.py', '/agent/evil.py', 'agent/a:evil.py',
                     'agent/a./evil.py', 'agent\\evil.py', 'other/evil.py'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with zipfile.ZipFile(root/'bad.zip', 'w') as bundle:
                    bundle.writestr(name, 'bad')
                with self.assertRaises(ValueError):
                    source.extract(root/'bad.zip', root/'output')
                self.assertFalse((root/'output').exists())

    def test_archive_accepts_source_and_rejects_duplicate_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root/'good.zip', 'w') as bundle:
                for name in ('agent/launch.py', 'agent/codex_stats_agent/__main__.py', 'common/releases.py'):
                    bundle.writestr(name, '# source')
            source.extract(root/'good.zip', root/'output')
            self.assertTrue((root/'output'/'agent'/'launch.py').exists())
            with zipfile.ZipFile(root/'good.zip', 'a') as bundle:
                bundle.writestr('agent/LAUNCH.py', '# source')
            with self.assertRaises(ValueError):
                source.extract(root/'good.zip', root/'second')

    def run_update(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); app = root/'data'; updates = app/'updates'; updates.mkdir(parents=True)
            stage = root/'stage'; stage.mkdir()
            source.atomic(root/'current.json', {'version':'0.4.0'})
            source.atomic(stage/'release.json', {})
            source.atomic(stage/'guard.json', {'pid':123, 'lease_until':time.time()+60, 'logs':{}})
            cfg = SimpleNamespace(codex_home=str(root/'codex'))
            calls = []
            class Process:
                def poll(self): return None if mode=='success' else 0
                def wait(self, timeout): return 0
            def start(*args, **kwargs):
                calls.append(args)
                if len(calls)==1 and mode=='blocked': raise OSError('blocked')
                if mode=='success':
                    source.atomic(updates/'health.json', {'version':'0.4.1', 'at':time.time()+1})
                return Process()
            with patch.dict(os.environ, CODEX_STATS_INSTALL_ROOT=str(root)), \
                 patch.object(source,'APP_DIR',app), patch.object(source.AgentConfig,'load',return_value=cfg), \
                 patch.object(source,'verify',return_value={'version':'0.4.1'}), patch.object(source,'prepare'), \
                 patch('agent.codex_stats_agent.updater.wait_for_exit',return_value=True), \
                 patch('agent.codex_stats_agent.updater.log_fingerprint',return_value={'new':(1,2)} if mode=='activity' else {}), \
                 patch.object(source.subprocess,'Popen',side_effect=start):
                result = source.apply_source(stage, installed_version='0.4.0')
            selected = json.loads((root/'current.json').read_text())['version']
            self.assertEqual(selected, '0.4.1' if mode=='success' else '0.4.0')
            self.assertFalse((root/'pending.json').exists())
            self.assertFalse((updates/'stop-request').exists())
            self.assertEqual(result, 0 if mode=='success' else 2 if mode=='activity' else 1)
            return json.loads((updates/'result.json').read_text())['update_result']

    def test_success(self): self.assertEqual(self.run_update('success'), 'success')
    def test_failed_health(self): self.assertEqual(self.run_update('failed'), 'rollback')
    def test_os_refusal_restores_old_pointer(self): self.assertEqual(self.run_update('blocked'), 'rollback')
    def test_activity_defers_without_switch(self): self.assertEqual(self.run_update('activity'), 'deferred')
