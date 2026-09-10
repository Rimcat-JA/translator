"""Real-process offline smoke: isolated paths, port conflicts, auth, restart and shutdown."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx

from translator.instance import contact_instance


def wait_instance(directory: Path, process: subprocess.Popen):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Runtime exited early ({process.returncode})")
        value = contact_instance(directory, "open")
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Runtime did not become ready")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    executable = [str(args.exe.resolve())] if args.exe else [sys.executable, "-m", "translator"]
    with tempfile.TemporaryDirectory(prefix="translator-smoke-") as temporary:
        base = Path(temporary)
        directory = base / "設定 日本語 with spaces"
        directory.mkdir()
        work = base / "unrelated cwd"
        work.mkdir()
        env = os.environ.copy()
        for variable in ("GLADIA_API_KEY", "DEEPL_API_KEY", "NGROK_AUTHTOKEN"):
            env.pop(variable, None)
        env["TRANSLATOR_DATA_DIR"] = str(directory)
        if args.exe:
            # Exercise the frozen package with developer runtimes unavailable through PATH.
            env["PATH"] = os.path.join(env.get("SystemRoot", "C:\\Windows"), "System32")
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        command = executable + ["start", "--demo", "--no-browser", "--local-port", str(port), "--hub-port", "0"]
        process = None
        try:
            for iteration in range(2):
                process = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                instance = wait_instance(directory, process)
                parsed = urlsplit(instance["url"])
                origin = f"{parsed.scheme}://{parsed.netloc}"
                assert parsed.port != port, "Port conflict must select another port"
                with httpx.Client(base_url=origin, trust_env=False, timeout=15) as client:
                    assert client.get("/health/live").status_code == 200
                    assert client.get("/api/local/settings").status_code == 401
                    token = parse_qs(parsed.fragment)["bootstrap"][0]
                    response = client.post("/api/local/bootstrap", json={"token": token}, headers={"Origin": origin})
                    assert response.status_code == 200, response.text
                    csrf = response.json()["csrf_token"]
                    client.headers.update({"Origin": origin, "X-CSRF-Token": csrf})
                    bootstrap = client.get("/api/local/bootstrap").json()
                    assert bootstrap["session"] is None, "Starting app must not start a conversation"
                    assert client.get("/assets/missing.js").status_code == 404
                    response = client.post("/api/local/sessions", json={"request_id": uuid.uuid4().hex, "demo": True})
                    assert response.status_code == 200, response.text
                    session = response.json()["session_id"]
                    response = client.post(f"/api/local/sessions/{session}/start", json={"request_id": uuid.uuid4().hex})
                    assert response.status_code == 200, response.text
                    deadline = time.monotonic() + 12
                    captions = {}
                    while time.monotonic() < deadline:
                        snapshot = client.get("/api/local/bootstrap").json()["session"]
                        captions = snapshot["captions"]
                        if any(row["translation"]["status"] == "ready" for row in captions.values()):
                            break
                        time.sleep(0.1)
                    assert captions and any(row["translation"]["status"] == "ready" for row in captions.values())
                    repeated = subprocess.run(command, cwd=work, env=env, capture_output=True, timeout=15)
                    repeated_error = repeated.stderr.decode("utf-8", errors="replace")
                    for credential in (token, csrf, instance["url"]):
                        repeated_error = repeated_error.replace(credential, "[redacted]")
                    assert repeated.returncode == 0, (
                        f"Duplicate launcher exited {repeated.returncode}: {repeated_error[-4000:]}"
                    )
                    assert contact_instance(directory, "status")["instance_id"] == instance["instance_id"]
                    response = client.post(f"/api/local/sessions/{session}/stop", json={"request_id": uuid.uuid4().hex})
                    assert response.status_code == 200, response.text
                    assert response.json()["status"] == "ended"
                assert contact_instance(directory, "stop")
                process.wait(timeout=15)
                assert process.returncode == 0, process.returncode
                assert not (directory / "instance.json").exists()
                process = None
                print(f"PASS {'frozen' if args.exe else 'source'} launch {iteration + 1}: demo/auth/port conflict/duplicate/stop")
            if args.exe:
                subprocess.run(executable + ["doctor", "--no-browser"], cwd=work, env=env,
                               check=True, timeout=20)
                diagnostics = json.loads((directory / "diagnostics.json").read_text(encoding="utf-8"))
                assert all(value == "available" for value in diagnostics["native_dependencies"].values())
                print("PASS frozen native dependencies: SoundCard and ngrok imports")
        finally:
            occupied.close()
            if process and process.poll() is None:
                contact_instance(directory, "stop")
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
