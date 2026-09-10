"""Actual dual-Uvicorn integration; no frontend build, devices, API or tunnel."""
import asyncio
import signal
import socket
from urllib.parse import urlsplit

import httpx
import pytest

from translator.config import Settings
from translator.instance import InstanceLock, contact_instance
from translator.providers import ProviderError
from translator.runtime import Runtime, bind_loopback
from translator.secrets import SecretStore


@pytest.fixture
def runtime_factory(tmp_path, monkeypatch):
    web = tmp_path / "test-web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Runtime integration fixture</title>", encoding="utf-8")
    monkeypatch.setattr("translator.runtime.web_directory", lambda: web)
    for name in ("GLADIA_API_KEY", "DEEPL_API_KEY", "NGROK_AUTHTOKEN"):
        monkeypatch.delenv(name, raising=False)
    store = SecretStore()
    store.backend = None
    settings = Settings(tmp_path / "settings", secrets=store)

    def create(**options):
        return Runtime(settings, local_port=options.pop("local_port", 0), hub_port=options.pop("hub_port", 0), **options)

    return create


async def authenticate(client, runtime):
    response = await client.post("/api/local/bootstrap", headers={"Origin": runtime.local_origin}, json={"token": runtime.auth.issue_bootstrap()})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def assert_port_closed(origin):
    address = ("127.0.0.1", urlsplit(origin).port)
    with socket.socket() as probe:
        probe.settimeout(.3)
        assert probe.connect_ex(address) != 0


def test_bind_loopback_reserves_free_socket_and_leaves_conflicting_listener_alive():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        reserved = bind_loopback(port)
        try:
            assert reserved.getsockname()[0] == "127.0.0.1"
            assert reserved.getsockname()[1] != port
            assert occupied.fileno() >= 0
            with socket.create_connection(("127.0.0.1", port), timeout=.3):
                pass
        finally:
            reserved.close()


@pytest.mark.asyncio
async def test_runtime_actual_ports_auth_audiences_and_two_app_cleanup(runtime_factory):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        conflict = occupied.getsockname()[1]
        runtime = runtime_factory(local_port=conflict, hub_port=conflict)
        try:
            await runtime.start()
            assert runtime.local_origin != runtime.hub_origin
            assert urlsplit(runtime.local_origin).port != conflict
            assert urlsplit(runtime.hub_origin).port != conflict
            async with httpx.AsyncClient(base_url=runtime.local_origin, trust_env=False) as local, httpx.AsyncClient(base_url=runtime.hub_origin, trust_env=False) as hub:
                for client in (local, hub):
                    response = await client.get("/health/live")
                    assert response.status_code == 200
                    assert response.json()["instance_id"] == runtime.instance_id
                    page = await client.get("/", follow_redirects=True)
                    assert page.status_code == 200
                    assert page.url.path == ("/" if client is local else "/join")
                    assert (await client.get("/assets/missing.js")).status_code == 404
                assert (await local.get("/api/local/settings")).status_code == 401
                await authenticate(local, runtime)
                assert (await local.get("/api/local/settings")).status_code == 200
                # Port separation alone does not isolate cookies. The Hub must
                # reject admin APIs even when explicitly sent the admin cookie.
                hub.cookies.update(local.cookies)
                for path in ("/api/local/settings", "/api/local/diagnostics", "/api/local/instance"):
                    assert (await hub.get(path)).status_code == 404
                assert not (await hub.get("/api/v1/bootstrap")).json()["authenticated"]
                assert (await local.get("/api/v1/bootstrap")).status_code == 404
                assert (await local.get("/api/local/settings", headers={"Host": "attacker.example"})).status_code == 403
                assert (await local.get("/api/local/settings", headers={"Origin": "https://attacker.example"})).status_code == 403
                assert (await hub.post("/api/local/shutdown", json={"request_id": "attack"}, headers={"Origin": runtime.hub_origin})).status_code == 404
            assert occupied.fileno() >= 0
        finally:
            await runtime.close()
        assert all(task.done() for task in runtime._server_tasks)
        assert all(sock.fileno() == -1 for sock in runtime._sockets)
        assert_port_closed(runtime.local_origin)
        assert_port_closed(runtime.hub_origin)
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_start_does_not_start_providers_capture_or_tunnel(runtime_factory, monkeypatch):
    runtime = runtime_factory(demo=True)
    invoked = []

    def forbidden(*args, **kwargs):
        invoked.append(True)
        raise AssertionError("Explicit user start is required")

    runtime.sessions.provider_factory = forbidden
    monkeypatch.setattr("translator.audio.worker.NativeAudioWorker.start", forbidden)
    monkeypatch.setattr("translator.remote.ngrok.TunnelManager.start", forbidden)
    try:
        await runtime.start()
        async with httpx.AsyncClient(base_url=runtime.local_origin, trust_env=False) as client:
            await authenticate(client, runtime)
            response = await client.get("/api/local/bootstrap")
            assert response.status_code == 200
            assert response.json()["session"] is None
        assert not invoked and runtime.sessions._stt is None
        assert runtime._worker is None and runtime.tunnel.status == "idle"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_authenticated_ipc_status_stop_and_wrong_secret(runtime_factory, tmp_path):
    runtime = runtime_factory()
    lock = InstanceLock(tmp_path / "instance")
    assert lock.acquire()
    try:
        await runtime.start()
        lock.publish(runtime.local_origin, runtime.instance_id, runtime.instance_secret)
        async with httpx.AsyncClient(base_url=runtime.local_origin, trust_env=False) as client:
            headers = {"Origin": runtime.local_origin, "X-Translator-Instance": "incorrect"}
            denied = await client.post("/api/local/instance", headers=headers, json={"action": "status"})
            assert denied.status_code == 403
            assert runtime.instance_secret not in denied.text
            headers["X-Translator-Instance"] = runtime.instance_secret
            success = await client.post("/api/local/instance", headers=headers, json={"action": "status"})
            assert success.status_code == 200 and success.json()["instance_id"] == runtime.instance_id
        # Run blocking CLI discovery in another thread while real servers serve.
        status = await asyncio.to_thread(contact_instance, lock.directory, "status")
        assert status == {"instance_id": runtime.instance_id, "protocol_version": 1}
        await asyncio.to_thread(contact_instance, lock.directory, "stop")
        await asyncio.wait_for(runtime.wait(), timeout=1)
    finally:
        await runtime.close()
        lock.release()
    assert not lock.info_path.exists()
    assert_port_closed(runtime.local_origin)
    assert_port_closed(runtime.hub_origin)


@pytest.mark.asyncio
async def test_runtime_shutdown_still_closes_session_tunnel_servers_after_driver_error(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    await runtime.start()
    cleanups = []

    async def bad_capture(enabled):
        cleanups.append("capture")
        raise RuntimeError("Simulated native driver cleanup error")

    async def close_session():
        cleanups.append("session")

    async def close_tunnel():
        cleanups.append("tunnel")

    monkeypatch.setattr(runtime, "set_system_audio", bad_capture)
    monkeypatch.setattr(runtime.sessions, "close", close_session)
    monkeypatch.setattr(runtime, "stop_tunnel", close_tunnel)
    try:
        await runtime.close()
        assert cleanups == ["capture", "session", "tunnel"]
        assert all(task.done() for task in runtime._server_tasks)
        assert_port_closed(runtime.local_origin)
        assert_port_closed(runtime.hub_origin)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_two_servers_do_not_leave_signal_handlers_installed(runtime_factory):
    before = signal.getsignal(signal.SIGINT)
    runtime = runtime_factory()
    try:
        await runtime.start()
    finally:
        await runtime.close()
    assert signal.getsignal(signal.SIGINT) == before


@pytest.mark.asyncio
async def test_provider_probe_gladia_consumes_errors_after_finish_and_closes(runtime_factory, monkeypatch):
    runtime = runtime_factory()
    runtime.settings.set_secret("gladia", "fake-test-key", persist=False)
    instances = []

    class FakeGladia:
        def __init__(self, key):
            self.closed = False
            self.finished = asyncio.Event()
            instances.append(self)

        async def start(self, config):
            pass

        async def finish_input(self):
            self.finished.set()

        async def events(self):
            await self.finished.wait()
            raise ProviderError("GLADIA_DRAIN_TIMEOUT", "最後の字幕を受信できませんでした。", True)
            yield  # Make this an async generator matching the adapter contract.

        async def close(self):
            self.closed = True

    monkeypatch.setattr("translator.providers.GladiaSTTProvider", FakeGladia)
    with pytest.raises(ProviderError) as caught:
        await runtime.test_provider("gladia")
    assert caught.value.code == "GLADIA_DRAIN_TIMEOUT"
    assert instances[0].closed and "gladia" not in runtime._provider_tests
    await runtime.close()
