"""Offline process-level check of the agent workflow, also against the console EXE."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    executable = [str(args.exe.resolve())] if args.exe else [sys.executable, "-m", "translator"]
    with tempfile.TemporaryDirectory(prefix="translator-agent-") as temporary:
        root = Path(temporary)
        profile = root / "エージェント profile"
        unrelated = root / "different cwd"
        unrelated.mkdir()
        environment = os.environ.copy() | {"PYTHONIOENCODING": "ascii", "TRANSLATOR_NO_DIALOG": "1"}
        for key in ("GLADIA_API_KEY", "DEEPL_API_KEY", "NGROK_AUTHTOKEN"):
            environment.pop(key, None)
        if args.exe:
            environment["PATH"] = str(Path(environment.get("SystemRoot", r"C:\Windows")) / "System32")
        secret = "offline-smoke-fixture-value"

        def run(*arguments, check=True, input_text=None):
            result = subprocess.run(executable + ["agent", *arguments, "--data-dir", str(profile)],
                                    cwd=unrelated, env=environment, input=input_text,
                                    encoding="utf-8", capture_output=True, timeout=90,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            assert secret not in result.stdout + result.stderr, "Secret was echoed"
            assert len(result.stdout.splitlines()) == 1, "Expected exactly one JSON result"
            value = json.loads(result.stdout)
            assert set(value) == {"schema_version", "ok", "command", "data", "error"}
            assert value["schema_version"] == 1 and type(value["ok"]) is bool
            assert value["ok"] == (result.returncode == 0)
            if check:
                assert value["ok"], value["error"]
            return value

        catalog = run("catalog")["data"]
        assert "session-create" in {item["name"] for item in catalog["commands"]}
        assert not profile.exists(), "Catalog must not initialize a profile"
        invalid = run("session-create", "--mode", "not-a-mode", check=False)
        assert invalid["error"]["code"] == "INVALID_ARGUMENTS"
        assert run("status")["data"] == {"running": False}
        try:
            started = run("runtime-start", "--timeout", "60")["data"]
            assert started["running"] is True
            repeated = run("runtime-start")["data"]
            assert repeated["already_running"] and repeated["instance_id"] == started["instance_id"]
            status = run("status")["data"]
            assert status["session"] is None
            settings = run("settings-get")["data"]
            change = json.dumps({"expected_revision": settings["revision"], "settings": {"caption_font_size": 36}})
            assert run("settings-set", "--input", "-", input_text=change)["data"]["caption_font_size"] == 36
            configured = run("secret-set", "--provider", "gladia", "--input", "-", input_text=secret)["data"]
            assert configured["configured"] and configured["storage"] == "memory"
            sid = run("session-create", "--mode", "demo")["data"]["session_id"]
            assert run("session-start", "--session-id", sid)["data"]["status"] == "running"
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                snapshot = run("session-snapshot", "--session-id", sid)["data"]
                if any(row["translation"]["status"] == "ready" for row in snapshot["captions"].values()):
                    break
                time.sleep(.2)
            else:
                raise AssertionError("No translated demo caption arrived")
            invite = run("invite-create", "--session-id", sid)["data"]
            assert "#invite=" in invite["url"] and invite["expires_in"] == 600
            assert run("diagnostics")["data"]["privacy"]["audio_saved"] is False
            assert run("session-stop", "--session-id", sid)["data"]["status"] == "ended"
            assert run("runtime-stop")["data"]["running"] is False
            assert run("status")["data"] == {"running": False}
        finally:
            run("runtime-stop", check=False)
        print(f"PASS {'frozen' if args.exe else 'source'} agent JSON workflow: catalog/auth/settings/demo/invite/stop")


if __name__ == "__main__":
    main()
