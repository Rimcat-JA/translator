"""Isolated browser-test runtime. Dummy services are injected only in this fixture.

Uses normal authenticated endpoints and IPC; no test-only HTTP bypass exists.
Synthetic A audio is opt-in through the normal system-audio button.
"""
import asyncio
import contextlib
import math
import struct
from pathlib import Path

from translator.audio.protocol import AudioFrame
from translator.assets import ensure_frontend
from translator.config import Preferences, Settings
from translator.instance import InstanceLock
from translator.providers import DummySTTProvider, DummyTranslationProvider
from translator.runtime import Runtime
from translator.secrets import SecretStore


class SyntheticRuntime(Runtime):
    async def list_audio_devices(self):
        return {"supported": True, "devices": [{"id": "synthetic", "name": "Test sine output"}]}

    async def set_system_audio(self, enabled, device_id=None, **ownership):
        if self._capture_task:
            self._capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._capture_task
            self._capture_task = None
        if self.sessions.current:
            self.sessions.current.components["system_audio_status"] = "active" if enabled else "idle"
            self.sessions.notify()
        if enabled:
            async def feed():
                sequence = 0
                while True:
                    offset = sequence * 320
                    pcm = struct.pack("<320h", *(int(math.sin(2 * math.pi * 440 * (offset + i) / 16000) * 5000) for i in range(320)))
                    frame = AudioFrame(sequence=sequence, sample_offset=offset, pcm=pcm, flags=1 if sequence == 0 else 0)
                    self.sessions.publish_system_audio(frame.encode())
                    sequence += 1
                    await asyncio.sleep(.02)
            self._capture_task = asyncio.create_task(feed())
        return {"status": "active" if enabled else "idle"}


async def main():
    ensure_frontend()
    directory = Path(".translator-test/frontend-e2e").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    secrets = SecretStore()
    secrets.backend = None
    # These fixture strings never leave this process. Provider factory always uses dummies.
    secrets.set("gladia", "fixture-gladia", persist=False)
    secrets.set("deepl", "fixture-deepl", persist=False)
    settings = Settings(directory, secrets)
    settings.value = Preferences()  # Repeatable fixture defaults without deleting user files.
    runtime = SyntheticRuntime(settings, local_port=18765, hub_port=18000)
    runtime.sessions.provider_factory = lambda _demo: (DummySTTProvider(), DummyTranslationProvider())
    lock = InstanceLock(directory)
    if not lock.acquire():
        raise RuntimeError("Another browser fixture owns this directory")
    try:
        await runtime.start()
        if runtime.local_origin != "http://127.0.0.1:18765" or runtime.hub_origin != "http://127.0.0.1:18000":
            raise RuntimeError("Browser test ports are occupied")
        lock.publish(runtime.local_origin, runtime.instance_id, runtime.instance_secret)
        await runtime.wait()
    finally:
        await runtime.close()
        lock.release()


if __name__ == "__main__":
    asyncio.run(main())
