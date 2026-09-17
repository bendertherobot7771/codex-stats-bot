import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from server.codex_stats_server.database import StatsDatabase
from server.codex_stats_server.lifecycle import Lifecycle
from server.codex_stats_server.http_api import create_server


class PublicApiTests(unittest.TestCase):
    def test_proxy_auth_rejects_master_and_scopes_device(self):
        with tempfile.TemporaryDirectory() as directory:
            db = StatsDatabase(Path(directory)/'db.sqlite')
            life = Lifecycle(db,'',Path(directory))
            fixture = Path(__file__).parent/'fixtures'/'signed-zero-release.json'
            life.current_release = lambda: json.loads(fixture.read_text())
            token = life.enroll({'code':life.code(1),'machine_id':'pc','machine_name':'PC'})['api_key']
            server = create_server('127.0.0.1',0,db,'master',lifecycle=life)
            thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            base = f'http://127.0.0.1:{server.server_address[1]}'
            def request(path,key='',body=None,public=True):
                headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}
                if public: headers.update({'X-Codex-Public':'1','X-Real-IP':'203.0.113.4'})
                req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
                try:
                    with urllib.request.urlopen(req,timeout=3) as response: return response.status
                except urllib.error.HTTPError as error:
                    status=error.code; error.close(); return status
            try:
                self.assertEqual(request('/api/v1/public-auth','master'),401)
                self.assertEqual(request('/api/v1/public-auth','master',public=False),401)
                self.assertEqual(request('/api/v1/public-auth',token),200)
                self.assertEqual(request('/api/v1/public-auth','invalid'),401)
                self.assertEqual(request('/api/v1/control/updates','master',{'operation':'status'}),401)
                self.assertEqual(request('/api/v1/control/updates','master',{'operation':'status'},public=False),200)
                event={'event_id':'test','event_type':'quota_snapshot','task_id':'q','machine_id':'other','quota':{}}
                self.assertEqual(request('/api/v1/events','master',event),401)
                self.assertEqual(request('/api/v1/events',token,event),403)
                with patch.object(life,'enroll',return_value={}) as enroll:
                    self.assertEqual(request('/api/v1/enroll',body={'code':'x'}),200)
                    self.assertEqual(enroll.call_args.args[1],'203.0.113.4')
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3); db.close()
