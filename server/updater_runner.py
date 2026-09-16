"""Small root-owned recovery anchor, outside the release being replaced."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.codex_stats_server.update_service import STATE, recover

transaction = STATE / "transaction.json"
if transaction.exists():
    journal = json.loads(transaction.read_text())
    if not journal.get("complete"):
        recover(journal)
result = subprocess.run(["/usr/bin/python3", "-m", "server.codex_stats_server.update_service"],
                        cwd="/opt/codex-stats-bot/current", timeout=270)
raise SystemExit(result.returncode)
