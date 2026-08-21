from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (ROOT / "agent" / "install.ps1", ROOT / "agent" / "uninstall.ps1")


class PowerShellScriptTests(unittest.TestCase):
    def test_scripts_have_utf8_bom_for_windows_powershell_51(self) -> None:
        for script in SCRIPTS:
            self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf"), script.name)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell доступен только на Windows")
    def test_scripts_parse_in_windows_powershell(self) -> None:
        for script in SCRIPTS:
            command = (
                "$errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                f"'{script}',[ref]$null,[ref]$errors); if($errors.Count){{exit 1}}"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
