"""Stable entry point, also usable with Python's isolated embedded distribution."""
import json
import os
import re
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    # Installed launcher lives alongside current.json; archive launcher lives in agent/.
    if (root / "current.json").exists():
        if (root / "pending.json").exists() and os.environ.get("CODEX_STATS_UPDATE_CHILD") != "1":
            # Independent recovery code from the previous trusted installation.
            sys.path.insert(0, str(root / "recovery"))
            from agent.codex_stats_agent.instance import single_instance
            from agent.codex_stats_agent.source_update import atomic
            try:
                with single_instance(root / "update.lock"):
                    from agent.codex_stats_agent.config import APP_DIR
                    with single_instance(APP_DIR / "agent.lock"):
                        pending = json.loads((root / "pending.json").read_text())
                        atomic(root / "current.json", pending["previous"])
                        (APP_DIR / "updates" / "stop-request").unlink(missing_ok=True)
                        atomic(APP_DIR / "updates" / "result.json", {"update_result": "rollback"})
                        (root / "pending.json").unlink()
            except RuntimeError:
                return 0  # A live updater/collector owns recovery.
            # Recovery imported old modules; restart before loading selected version.
            import subprocess
            return subprocess.call([sys.executable, "-B", str(__file__), *sys.argv[1:]])
        selection = json.loads((root / "current.json").read_text(encoding="utf-8"))
        if selection.get("legacy"):
            import subprocess
            return subprocess.call([str(root / "codex-stats-agent.exe"), *sys.argv[1:]],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        selected = selection["version"]
        if not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", selected):
            raise ValueError("Invalid installed version")
        source = root / "releases" / selected
        os.environ["CODEX_STATS_INSTALL_ROOT"] = str(root)
    else:
        source = root.parent
    sys.path.insert(0, str(source))
    from agent.codex_stats_agent.__main__ import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
