from pathlib import Path
import subprocess
import sys


def test_exported_api_input_contract_has_no_drift_from_other_working_directory(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools" / "export_contracts.py"
    result = subprocess.run([sys.executable, str(script), "--check"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
