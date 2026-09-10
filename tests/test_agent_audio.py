"""Headless host control with fake HTTP/WS; never touches devices or paid APIs."""
import asyncio
from copy import deepcopy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from translator.agent import audio
from translator.agent.client import AgentError


def snapshot(status="idle", session_id="session_test", demo=False):
    return {"session_id": session_id, "status": "waiting_for_peer", "demo": demo,
            "components": {"system_audio_status": status},
            "captions": {"private": {"original": {"text": "private conversation"}}}}


class FakeSocket:
    def __init__(self, initial=None, *, acknowledge=True):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.acknowledge = acknowledge
        self.order = []
        if initial is not None:
            self.queue.put_nowait(json.dumps({"type": "session.snapshot", "data": initial}))

    async def recv(self):
        value = await self.queue.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if self.acknowledge:
            self.queue.put_nowait(json.dumps({"type": "command.completed", "request_id": event["request_id"], "data": {"pong": True}}))

    async def close(self):
        self.order.append("close")
        self.closed = True


class FakeClient:
    origin = "http://127.0.0.1:8765"

    def __init__(self, socket):
        self.socket = socket
        self.calls = []
        self.start_error = None
        self.stop_error = None
        self.stop_response = None
        self.on_start = None

    def ws_headers(self):
        return {"Origin": self.origin, "Cookie": "translator_local=private-cookie"}

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        self.socket.order.append("start" if body["enabled"] else "stop")
        if body["enabled"]:
            if self.start_error:
                raise self.start_error
            if self.on_start:
                self.on_start()
            return snapshot("active")
        if self.stop_error:
            raise self.stop_error
        if self.stop_response is not None:
            return self.stop_response
        return snapshot()


@pytest.fixture
def connection(monkeypatch):
    captured = []

    def install(socket):
        async def connect(url, **options):
            captured.append((url, options))
            return socket
        monkeypatch.setattr(audio.websockets, "connect", connect)
        return captured

    return install


@pytest.mark.asyncio
async def test_bounded_share_authenticated_owner_heartbeat_and_stop_before_close(connection, monkeypatch):
    socket = FakeSocket(snapshot())
    captured = connection(socket)
    client = FakeClient(socket)
    monkeypatch.setattr(audio, "HEARTBEAT_INTERVAL", .1)
    result = await audio.share_audio(client, "session_test", 1, device_id="chosen-device")
    assert result["status"] == "stopped" and result["stopped"] is True
    assert 1 <= result["duration_seconds"] < 2
    assert "private" not in json.dumps(result)
    assert socket.order == ["start", "stop", "close"]
    assert [call[2]["enabled"] for call in client.calls] == [True, False]
    url, options = captured[0]
    owner = parse_qs(urlsplit(url).query)["audio_owner"][0]
    assert len(owner) == 32 and all(call[2]["owner_id"] == owner for call in client.calls)
    assert client.calls[0][2]["device_id"] == "chosen-device"
    assert options["origin"] == client.origin and options["proxy"] is None
    assert options["additional_headers"] == {"Cookie": "translator_local=private-cookie"}
    assert len(socket.sent) >= 2 and all(message["type"] == "command.ping" and message["request_id"] for message in socket.sent)


@pytest.mark.asyncio
async def test_preexisting_audio_is_rejected_without_enable_or_disable(connection):
    socket = FakeSocket(snapshot("active"))
    connection(socket)
    client = FakeClient(socket)
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 1)
    assert caught.value.code == "AUDIO_ALREADY_ACTIVE"
    assert client.calls == [] and socket.closed


@pytest.mark.asyncio
async def test_first_snapshot_required_before_capture(connection, monkeypatch):
    socket = FakeSocket()
    connection(socket)
    client = FakeClient(socket)
    monkeypatch.setattr(audio, "SNAPSHOT_TIMEOUT", .02)
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 1)
    assert caught.value.code == "AUDIO_SNAPSHOT_TIMEOUT"
    assert not client.calls and socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,code", [(snapshot(session_id="other"), "SESSION_MISMATCH"), (snapshot(demo=True), "DEMO_AUDIO_DISABLED")])
async def test_wrong_session_or_demo_never_starts_capture(connection, initial, code):
    socket = FakeSocket(initial)
    connection(socket)
    client = FakeClient(socket)
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 1)
    assert caught.value.code == code and not client.calls and socket.closed


@pytest.mark.asyncio
async def test_heartbeat_timeout_releases_matching_owner(connection, monkeypatch):
    socket = FakeSocket(snapshot(), acknowledge=False)
    connection(socket)
    client = FakeClient(socket)
    monkeypatch.setattr(audio, "HEARTBEAT_TIMEOUT", .03)
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 5)
    assert caught.value.code == "AUDIO_HEARTBEAT_TIMEOUT"
    assert socket.order == ["start", "stop", "close"]


@pytest.mark.asyncio
async def test_disconnect_errors_are_sanitized_and_capture_is_stopped(connection):
    socket = FakeSocket(snapshot())
    connection(socket)
    client = FakeClient(socket)
    loop = asyncio.get_running_loop()
    client.on_start = lambda: loop.call_soon_threadsafe(socket.queue.put_nowait, OSError("private-cookie private conversation"))
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 5)
    assert caught.value.code == "AUDIO_CONTROL_DISCONNECTED"
    assert "private" not in str(caught.value)
    assert socket.order == ["start", "stop", "close"]


@pytest.mark.asyncio
async def test_cancelled_share_attempts_stop_before_socket_close(connection):
    socket = FakeSocket(snapshot())
    connection(socket)
    client = FakeClient(socket)
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    client.on_start = lambda: loop.call_soon_threadsafe(started.set)
    task = asyncio.create_task(audio.share_audio(client, "session_test", 60))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.order == ["start", "stop", "close"]
    assert all(task.done() for task in asyncio.all_tasks() if task.get_name().startswith("agent-audio-"))


@pytest.mark.asyncio
async def test_concurrent_owner_conflict_uses_only_scoped_release(connection):
    socket = FakeSocket(snapshot())
    captured = connection(socket)
    client = FakeClient(socket)
    client.start_error = AgentError("AUDIO_BUSY", "Another share won the audio claim.", exit_code=4)
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 1)
    assert caught.value.code == "AUDIO_BUSY"
    owner = parse_qs(urlsplit(captured[0][0]).query)["audio_owner"][0]
    assert client.calls[1][2] == {"request_id": client.calls[1][2]["request_id"], "enabled": False, "owner_id": owner}
    assert socket.closed


@pytest.mark.asyncio
async def test_unconfirmed_stop_is_never_reported_as_success(connection):
    socket = FakeSocket(snapshot())
    connection(socket)
    client = FakeClient(socket)
    client.stop_error = OSError("private-cookie")
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 1)
    assert caught.value.code == "AUDIO_STOP_UNCONFIRMED"
    assert "private" not in str(caught.value) and socket.closed


@pytest.mark.asyncio
async def test_missing_stop_status_is_not_treated_as_idle(connection):
    socket = FakeSocket(snapshot())
    connection(socket)
    client = FakeClient(socket)
    client.stop_response = {"session_id": "session_test"}
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", 1)
    assert caught.value.code == "AUDIO_STOP_UNCONFIRMED" and socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [0, 3601, True, 1.5, "1"])
async def test_duration_bounds_are_checked_before_any_connection(connection, seconds):
    socket = FakeSocket(snapshot())
    captured = connection(socket)
    client = FakeClient(socket)
    with pytest.raises(AgentError) as caught:
        await audio.share_audio(client, "session_test", seconds)
    assert caught.value.code == "INVALID_DURATION"
    assert not client.calls and not captured


@pytest.mark.asyncio
async def test_real_agent_http_websocket_owner_lifecycle_and_logout(tmp_path, monkeypatch):
    from translator.agent.client import AgentClient
    from translator.config import Settings
    from translator.instance import InstanceLock
    from translator.runtime import Runtime
    from translator.secrets import SecretStore

    class SyntheticWorker:
        starts = 0
        stops = 0

        def __init__(self, **options):
            pass

        async def start(self):
            SyntheticWorker.starts += 1

        async def stop(self):
            SyntheticWorker.stops += 1

        async def frames(self):
            await asyncio.Event().wait()
            yield  # The fake driver never captures or produces physical audio.

    monkeypatch.setattr("translator.audio.worker.NativeAudioWorker", SyntheticWorker)
    for variable in ("GLADIA_API_KEY", "DEEPL_API_KEY", "NGROK_AUTHTOKEN"):
        monkeypatch.delenv(variable, raising=False)
    store = SecretStore()
    store.backend = None
    directory = tmp_path / "isolated-runtime"
    settings = Settings(directory, secrets=store)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Agent audio fixture</title>", encoding="utf-8")
    runtime = Runtime(settings, local_port=0, hub_port=0)
    runtime.web_dir = web
    lock = InstanceLock(directory)
    assert lock.acquire()
    client = AgentClient(directory)
    try:
        await runtime.start()
        lock.publish(runtime.local_origin, runtime.instance_id, runtime.instance_secret)
        await asyncio.to_thread(client.connect)
        session = await asyncio.to_thread(client.request, "POST", "/api/local/sessions", {
            "request_id": "agent-audio-create", "demo": False, "translation_enabled": False,
        })
        session_id = session["session_id"]
        await asyncio.to_thread(client.request, "POST", f"/api/local/sessions/{session_id}/start", {
            "request_id": "agent-audio-start",
        })
        cookie = client._http.cookies.get("translator_local")
        assert runtime.auth.local(cookie) is not None
        result = await audio.share_audio(client, session_id, 1)
        assert result["status"] == "stopped" and result["stopped"] is True
        assert SyntheticWorker.starts == 1 and SyntheticWorker.stops == 1
        assert runtime._worker is None and runtime._audio_owner is None
        # The WebSocket close handshake may complete before the ASGI endpoint's
        # finally block unregisters its listener, especially on the Linux runner.
        async def wait_for_listener_cleanup():
            while runtime.audio_listeners:
                await asyncio.sleep(.01)

        await asyncio.wait_for(wait_for_listener_cleanup(), timeout=2)
        assert not runtime.audio_listeners
        assert runtime.sessions._stt is None
        assert runtime.sessions.current.components["system_audio_status"] == "idle"
        assert cookie not in json.dumps(result)
        await asyncio.to_thread(client.close)
        assert runtime.auth.local(cookie) is None
    finally:
        await asyncio.to_thread(client.close)
        await runtime.close()
        lock.release()
    assert not lock.info_path.exists()
