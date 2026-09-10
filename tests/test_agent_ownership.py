"""Real authenticated HTTP/WS ownership; physical audio is replaced at the driver boundary."""
import asyncio
import json
import uuid

import httpx
import pytest
from websockets.asyncio.client import connect

from translator.config import Settings
from translator.runtime import Runtime
from translator.secrets import SecretStore


@pytest.mark.asyncio
async def test_headless_owner_isolated_from_other_agents_and_browser_disconnect(tmp_path, monkeypatch):
    class Worker:
        starts = 0
        stops = 0

        def __init__(self, **options):
            pass

        async def start(self):
            Worker.starts += 1

        async def stop(self):
            Worker.stops += 1

        async def frames(self):
            await asyncio.Event().wait()
            yield  # pragma: no cover - the fake driver deliberately produces no real audio

    monkeypatch.setattr("translator.audio.worker.NativeAudioWorker", Worker)
    store = SecretStore()
    store.backend = None
    runtime = Runtime(Settings(tmp_path, secrets=store), local_port=0, hub_port=0)
    try:
        await runtime.start()
        session = runtime.sessions.create(request_id="create")
        sid = session["session_id"]
        await runtime.sessions.start(sid, "start")
        async with httpx.AsyncClient(base_url=runtime.local_origin, trust_env=False) as a, \
                httpx.AsyncClient(base_url=runtime.local_origin, trust_env=False) as b:
            for client in (a, b):
                client.headers["Origin"] = runtime.local_origin
                auth = await client.post("/api/local/bootstrap", json={"token": runtime.auth.issue_bootstrap()})
                client.headers["X-CSRF-Token"] = auth.json()["csrf_token"]
            owner_a, owner_b = uuid.uuid4().hex, uuid.uuid4().hex
            path = f"/api/local/sessions/{sid}/system-audio"

            async def change(client, owner, enabled):
                return await client.post(path, json={"enabled": enabled, "owner_id": owner,
                                                     "request_id": uuid.uuid4().hex})

            assert (await change(a, owner_a, True)).status_code == 409
            ws_origin = runtime.local_origin.replace("http://", "ws://") + "/ws/local/events"

            def socket(client, owner=None):
                return connect(ws_origin + (f"?audio_owner={owner}" if owner else ""),
                               origin=runtime.local_origin, proxy=None,
                               additional_headers={"Cookie": f"translator_local={client.cookies.get('translator_local')}"})

            async with socket(a, owner_a) as first:
                assert json.loads(await first.recv())["type"] == "session.snapshot"
                assert (await change(a, owner_a, True)).status_code == 200
                assert Worker.starts == 1
                async with socket(b, owner_b) as second:
                    await second.recv()
                    busy = await change(b, owner_b, True)
                    assert busy.status_code == 409 and busy.json()["error"]["code"] == "AUDIO_BUSY"
                    assert (await change(b, owner_b, False)).status_code == 200
                    # Copying another owner's public ID still cannot acquire its identity.
                    assert (await change(b, owner_a, True)).status_code == 409
                async with socket(b) as browser:
                    await browser.recv()
                await asyncio.sleep(.05)
                assert Worker.stops == 0 and runtime._worker is not None
            for _ in range(100):
                if Worker.stops == 1 and not runtime.audio_listeners:
                    break
                await asyncio.sleep(.01)
            assert runtime._worker is None and Worker.stops == 1
            assert not runtime.audio_listeners
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_failed_driver_stop_retains_runtime_owner_for_retry(tmp_path):
    class Worker:
        failures = 1

        async def stop(self):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("synthetic stop failure")

    store = SecretStore()
    store.backend = None
    runtime = Runtime(Settings(tmp_path, secrets=store), local_port=0, hub_port=0)
    worker = Worker()
    runtime._worker, runtime._audio_owner = worker, "agent-owner"
    with pytest.raises(RuntimeError):
        await runtime.set_system_audio(False, owner_id="agent-owner", only_if_owner=True)
    assert runtime._worker is worker and runtime._audio_owner == "agent-owner"
    await runtime.set_system_audio(False, owner_id="agent-owner", only_if_owner=True)
    assert runtime._worker is None and runtime._audio_owner is None
