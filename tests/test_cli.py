"""CLI subprocess regressions independent of the machine's console locale."""
import os
import subprocess
import sys


def test_stop_prints_utf8_with_ascii_inherited_stdio(tmp_path):
    directory = tmp_path / "設定 with spaces"
    env = os.environ.copy() | {"PYTHONIOENCODING": "ascii", "TRANSLATOR_NO_DIALOG": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "translator", "stop", "--data-dir", str(directory)],
        cwd=tmp_path, env=env, capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "起動中のTranslatorはありません。" in result.stdout.decode("utf-8")
    assert not (directory / "instance.json").exists()


def test_help_prints_japanese_under_english_windows_codepage(tmp_path):
    env = os.environ.copy() | {"PYTHONIOENCODING": "cp1252"}
    result = subprocess.run(
        [sys.executable, "-m", "translator", "--help"], cwd=tmp_path,
        env=env, capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "字幕とPC音声共有" in result.stdout.decode("utf-8")
