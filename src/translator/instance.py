"""OS-held instance lock and authenticated local IPC discovery. PID is never authority."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from translator.config import atomic_json


class InstanceLock:
    def __init__(self, directory: Path):
        self.directory = directory
        self.file = None
        self.info_path = directory / "instance.json"

    def acquire(self) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (self.directory / "instance.lock").open("a+b")
        try:
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            return False
        return True

    def publish(self, origin: str, instance_id: str, secret: str):
        atomic_json(self.info_path, {
            "protocol_version": 1, "pid": os.getpid(), "origin": origin,
            "instance_id": instance_id, "secret": secret,
        })

    def release(self):
        if self.file is None:
            return
        self.info_path.unlink(missing_ok=True)
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()
        self.file = None


def contact_instance(directory: Path, action: str = "open") -> dict | None:
    try:
        info = json.loads((directory / "instance.json").read_text(encoding="utf-8"))
        origin = info["origin"]
        parsed = urlsplit(origin)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
                parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            return None
        if info["protocol_version"] != 1:
            return None
        with httpx.Client(timeout=2, trust_env=False, follow_redirects=False) as client:
            response = client.post(origin + "/api/local/instance", json={"action": action}, headers={
                "X-Translator-Instance": info["secret"], "Origin": origin,
            })
            response.raise_for_status()
            data = response.json()
            if data.get("instance_id") != info["instance_id"]:
                return None
            return data
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return None
