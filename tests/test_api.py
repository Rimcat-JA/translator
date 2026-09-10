from types import SimpleNamespace
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from translator.api import AuthManager, create_hub_app, create_local_app
from translator.audio.protocol import AudioFrame
from translator.providers import DummySTTProvider, DummyTranslationProvider
from translator.sessions import SessionService


class FakeSettings:
    def public(self):
        return {"revision": 1, "secrets": {"gladia": {"configured": False, "storage": "memory"}}}

    def update(self, value, expected_revision):
        return self.public()

    def set_secret(self, provider, key, persist):
        pass


class OriginClient(TestClient):
    def websocket_connect(self, url, **kwargs):
        if url.startswith("/"):
            url = str(self.base_url).rstrip("/").replace("http", "ws", 1) + url
        return super().websocket_connect(url, **kwargs)


@pytest.fixture
def runtime(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Translator</title>", encoding="utf-8")
    stopped = []

    async def set_audio(enabled, device_id):
        stopped.append(enabled)

    return SimpleNamespace(auth=AuthManager(), sessions=SessionService(lambda demo: (DummySTTProvider(), DummyTranslationProvider())),
        settings=FakeSettings(), web_dir=tmp_path, source_root=tmp_path, instance_id="test-instance", demo=False,
        local_origin="http://127.0.0.1:8765", hub_origin="http://127.0.0.1:8000", public_origin=None,
        set_system_audio=set_audio, stopped=stopped)


def login_local(client, runtime):
    token = runtime.auth.issue_bootstrap()
    result = client.post("/api/local/bootstrap", json={"token": token})
    assert result.status_code == 200
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    return token


def local_client(runtime):
    return OriginClient(create_local_app(runtime), base_url=runtime.local_origin,
                      headers={"Origin": runtime.local_origin})


def hub_client(runtime):
    return OriginClient(create_hub_app(runtime), base_url=runtime.hub_origin,
                      headers={"Origin": runtime.hub_origin})


def create_waiting(runtime, client):
    login_local(client, runtime)
    response = client.post("/api/local/sessions", json={"request_id": "create"})
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    assert client.post(f"/api/local/sessions/{session_id}/start", json={"request_id": "start"}).status_code == 200
    return session_id


def join(hub, runtime, session_id):
    response = hub.post("/api/v1/join", json={"token": runtime.auth.invite(session_id)})
    assert response.status_code == 200
    hub.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def test_local_bootstrap_is_one_time_and_cookie_is_scoped(runtime):
    with local_client(runtime) as client:
        assert client.get("/api/local/bootstrap").status_code == 401
        token = login_local(client, runtime)
        assert client.get("/api/local/bootstrap").json()["surface"] == "local"
        assert client.post("/api/local/bootstrap", json={"token": token}).status_code == 401
        assert runtime.sessions.current is None
        assert "translator_local" in client.cookies
        token = runtime.auth.issue_bootstrap()
        result = client.post("/api/local/bootstrap", json={"token": token})
        cookie = result.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "domain=" not in cookie


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:8765"])
def test_local_origin_and_host_are_strict(runtime, origin):
    with local_client(runtime) as client:
        token = runtime.auth.issue_bootstrap()
        assert client.post("/api/local/bootstrap", json={"token": token}, headers={"Origin": origin}).status_code == 403
        assert client.get("/health/live", headers={"Host": "evil.example"}).status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/local/events", headers={"Origin": origin}):
                pass


def test_csrf_and_schema_validation_do_not_echo_secret_inputs(runtime):
    with local_client(runtime) as client:
        login_local(client, runtime)
        result = client.post("/api/local/sessions", json={"request_id": "a"}, headers={"X-CSRF-Token": "wrong"})
        assert result.status_code == 403
        result = client.put("/api/local/secrets/gladia", json={"key": "private-key", "persist": "private-key"})
        assert result.status_code == 422 and "private-key" not in result.text
        result = client.post("/api/local/sessions", json={"request_id": "a", "source_language": "bad-code"})
        assert result.status_code == 422


def test_hub_never_exposes_admin_or_legacy_routes_or_accepts_admin_cookie(runtime):
    with local_client(runtime) as local, hub_client(runtime) as hub:
        login_local(local, runtime)
        hub.cookies.update(local.cookies)
        for path in ("/api/local/settings", "/api/local/diagnostics", "/audio/from_A", "/captions/to_A"):
            assert hub.get(path).status_code == 404
        assert hub.get("/api/v1/languages").status_code == 401
        for path in ("/audio/from_A", "/audio/from_B", "/captions/to_A", "/control/A", "/control/B"):
            with pytest.raises(WebSocketDisconnect):
                with hub.websocket_connect(path):
                    pass


def test_invite_is_one_use_role_is_server_assigned_and_session_bound(runtime):
    with local_client(runtime) as local, hub_client(runtime) as hub:
        session_id = create_waiting(runtime, local)
        token = runtime.auth.invite(session_id)
        assert hub.post("/api/v1/join", json={"token": token, "role": "host"}).status_code == 422
        response = hub.post("/api/v1/join", json={"token": token})
        assert response.status_code == 200 and response.json()["role"] == "speaker"
        assert hub.post("/api/v1/join", json={"token": token}).status_code == 401
        assert hub.get(f"/api/v1/sessions/{session_id}/snapshot").status_code == 200
        assert hub.get("/api/v1/sessions/other/snapshot").status_code in {403, 404}
        runtime.auth.revoke_session(session_id)
        assert hub.get(f"/api/v1/sessions/{session_id}/snapshot").status_code == 401


def test_participant_ws_snapshot_permissions_and_cookie_revocation(runtime):
    with local_client(runtime) as local, hub_client(runtime) as hub:
        session_id = create_waiting(runtime, local)
        with pytest.raises(WebSocketDisconnect):
            with hub.websocket_connect(f"/ws/v1/sessions/{session_id}/events"):
                pass
        join(hub, runtime, session_id)
        with hub.websocket_connect(f"/ws/v1/sessions/{session_id}/events") as ws:
            initial = ws.receive_json()
            assert initial["type"] == "session.snapshot" and initial["data"]["session_id"] == session_id
            ws.send_json({"type": "command.set_target_language", "request_id": "fake-role", "role": "host", "data": {"language": "en"}})
            assert ws.receive_json()["error"]["code"] == "ROLE_FORBIDDEN"
            runtime.auth.revoke_session(session_id)
            ws.send_json({"type": "command.ping", "request_id": "after-revoke"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_publisher_lease_second_tab_and_invalid_audio_role(runtime):
    with local_client(runtime) as local, hub_client(runtime) as hub:
        session_id = create_waiting(runtime, local)
        join(hub, runtime, session_id)
        path = f"/api/v1/sessions/{session_id}/publisher-lease"
        result = hub.post(path, json={"request_id": "lease"})
        assert result.status_code == 200
        lease = result.json()["lease_id"]
        assert hub.post(path, json={"request_id": "second-tab"}).status_code == 409
        assert hub.post(path, json={"request_id": "renew", "lease_id": lease}).status_code == 200
        with hub.websocket_connect(f"/ws/v1/sessions/{session_id}/audio-input") as ws:
            ws.send_json({"type": "stream.start", "protocol_version": 1, "stream_id": "one", "session_epoch": 1,
                          "publisher_lease_id": lease, "source_kind": "system_loopback", "format": "pcm_s16le",
                          "sample_rate": 16000, "channels": 1, "frame_samples": 320})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_valid_audio_pipeline_strips_headers_and_drains_after_stream_stop(runtime):
    with local_client(runtime) as local, hub_client(runtime) as hub:
        session_id = create_waiting(runtime, local)
        join(hub, runtime, session_id)
        lease = hub.post(f"/api/v1/sessions/{session_id}/publisher-lease", json={"request_id": "lease"}).json()["lease_id"]
        with hub.websocket_connect(f"/ws/v1/sessions/{session_id}/audio-input") as ws:
            ws.send_json({"type": "stream.start", "protocol_version": 1, "stream_id": "one", "session_epoch": 1,
                          "publisher_lease_id": lease, "source_kind": "microphone", "format": "pcm_s16le",
                          "sample_rate": 16000, "channels": 1, "frame_samples": 320})
            assert ws.receive_json()["type"] == "stream.accepted"
            ws.send_bytes(AudioFrame(0, 0, bytes(640)).encode())
            ws.send_json({"type": "stream.stop"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        snapshot = hub.get(f"/api/v1/sessions/{session_id}/snapshot").json()
        caption = next(iter(snapshot["captions"].values()))
        assert caption["original"]["is_final"]
        assert caption["translation"]["status"] == "ready"
        assert runtime.sessions._stt is None
        assert runtime.sessions.current.lease_id is None


def test_audio_stream_rechecks_expired_lease_and_output_forwards_framed_pcm(runtime):
    with local_client(runtime) as local, hub_client(runtime) as hub:
        session_id = create_waiting(runtime, local)
        join(hub, runtime, session_id)
        lease = hub.post(f"/api/v1/sessions/{session_id}/publisher-lease", json={"request_id": "lease"}).json()["lease_id"]
        with hub.websocket_connect(f"/ws/v1/sessions/{session_id}/audio-input") as ws:
            ws.send_json({"type": "stream.start", "protocol_version": 1, "stream_id": "one", "session_epoch": 1,
                          "publisher_lease_id": lease, "source_kind": "microphone", "format": "pcm_s16le",
                          "sample_rate": 16000, "channels": 1, "frame_samples": 320})
            assert ws.receive_json()["type"] == "stream.accepted"
            runtime.sessions.current.lease_expiry = time.monotonic() - 1
            ws.send_bytes(AudioFrame(0, 0, bytes(640)).encode())
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        with hub.websocket_connect(f"/ws/v1/sessions/{session_id}/audio-output") as output:
            assert output.receive_json()["type"] == "stream.accepted"
            frame = AudioFrame(0, 0, bytes(640)).encode()
            hub.portal.call(runtime.sessions.publish_system_audio, frame)
            assert output.receive_bytes() == frame
        assert not runtime.sessions._audio_subscribers


def test_static_routes_do_not_return_html_for_missing_api_or_assets(runtime):
    with local_client(runtime) as client:
        assert client.get("/a").status_code == 200
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/no-such-route").status_code == 404
        assert client.get("/unknown").status_code == 404


def test_actual_chunked_body_is_bounded(runtime):
    with hub_client(runtime) as client:
        response = client.post("/api/v1/join", content=iter([b"x" * 20000, b"y" * 20000]), headers={"Content-Type": "application/json"})
        assert response.status_code == 413


def test_host_view_disconnect_stops_pc_capture(runtime):
    with local_client(runtime) as client:
        login_local(client, runtime)
        with client.websocket_connect("/ws/local/events") as ws:
            assert ws.receive_json()["type"] == "session.snapshot"
            ws.send_json({"type": "command.ping", "request_id": "presence"})
            assert ws.receive_json()["type"] == "command.completed"
        assert runtime.stopped == [False]


def test_host_presence_expiry_stops_capture(runtime):
    with local_client(runtime) as client:
        login_local(client, runtime)
        with client.websocket_connect("/ws/local/events") as ws:
            ws.receive_json()
            runtime.local_presence = time.monotonic() - 12
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        assert runtime.stopped and runtime.stopped[-1] is False


def test_development_origins_are_distinct_and_public_hub_origin_is_dynamic(runtime):
    runtime.development = True
    with local_client(runtime) as local, hub_client(runtime) as hub:
        assert local.get("/health/live", headers={"Origin": "http://127.0.0.1:5173"}).status_code == 200
        assert hub.get("/health/live", headers={"Origin": "http://127.0.0.1:5173"}).status_code == 403
        assert hub.get("/health/live", headers={"Origin": "http://127.0.0.1:5174"}).status_code == 200
        runtime.public_origin = "https://example.ngrok.app"
        assert hub.get("/api/v1/bootstrap", headers={"Host": "example.ngrok.app", "Origin": runtime.public_origin}).status_code == 200
        assert local.get("/health/live", headers={"Host": "example.ngrok.app", "Origin": runtime.public_origin}).status_code == 403
