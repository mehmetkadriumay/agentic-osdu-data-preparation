from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "explore_source.ps1"


@unittest.skipUnless(POWERSHELL, "PowerShell is required for the exploration launcher")
class ExplorationLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "Survey with spaces"
        (self.workspace / "Data").mkdir(parents=True)

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        assert POWERSHELL is not None
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", *arguments],
            cwd=self.temporary.name,
            env={
                **os.environ,
                "OSDU_TEST_LAUNCHER": str(LAUNCHER),
                "OSDU_TEST_WORKSPACE": str(self.workspace),
                "OSDU_TEST_CWD": self.temporary.name,
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_preview_uses_the_readme_prompt_and_resolved_workspace(self) -> None:
        result = self.run_script(
            "-File", str(LAUNCHER), "-WorkspaceRoot", str(self.workspace), "-PrintPrompt"
        )
        assert result.returncode == 0, result.stderr
        assert f"metadata from {self.workspace / 'Data'}." in result.stdout
        assert r"C:\OSDU\NewSurvey" not in result.stdout
        assert "1. Register and confirm" in result.stdout
        assert "7. Build the review view" in result.stdout
        assert "Do not modify source files" in result.stdout

    def test_launch_passes_one_prompt_without_approval_bypass_flags(self) -> None:
        result = self.run_script(
            "-Command",
            """
function uv {
    @{ arguments = @($args); cwd = (Get-Location).ProviderPath } |
        ConvertTo-Json -Depth 4 -Compress
    $global:LASTEXITCODE = 0
}
& $env:OSDU_TEST_LAUNCHER -WorkspaceRoot $env:OSDU_TEST_WORKSPACE
if ((Get-Location).ProviderPath -ne $env:OSDU_TEST_CWD) {
    throw "The launcher changed the caller's working directory."
}
""",
        )
        assert result.returncode == 0, result.stderr
        call = json.loads(result.stdout)
        assert call["cwd"] == str(ROOT)
        assert call["arguments"][:4] == ["run", "--frozen", "copilot", "--interactive"]
        assert len(call["arguments"]) == 5
        assert str(self.workspace / "Data") in call["arguments"][4]

    def test_launch_reports_a_nonzero_copilot_exit(self) -> None:
        result = self.run_script(
            "-Command",
            """
$ErrorActionPreference = "Stop"
function uv { $global:LASTEXITCODE = 17 }
& $env:OSDU_TEST_LAUNCHER -WorkspaceRoot $env:OSDU_TEST_WORKSPACE
""",
        )
        assert result.returncode != 0
        assert "Copilot exploration exited with code 17" in result.stderr

    def test_missing_source_directory_fails_before_launch(self) -> None:
        result = self.run_script(
            "-File",
            str(LAUNCHER),
            "-WorkspaceRoot",
            self.temporary.name,
            "-PrintPrompt",
        )
        assert result.returncode != 0
        assert "existing Data directory" in result.stderr


if __name__ == "__main__":
    unittest.main()
