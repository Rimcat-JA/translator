"""The installed command catalog and committed agent navigation must agree."""
from pathlib import Path
import subprocess
import sys


def test_agent_discovery_has_no_drift_and_links_resolve_from_another_cwd(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "tools/export_agent_docs.py"), "--check"],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
