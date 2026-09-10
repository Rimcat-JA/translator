import io
import json
from pathlib import Path

import httpx
import pytest

from translator.agent import main
from translator.agent.client import AgentClient, AgentError
from translator.agent.commands import catalog, runtime_start, runtime_stop


HTTPX_CLIENT = httpx.Client
INSTANCE_ID = "a" * 32
SESSION_ID = "b" * 32
INSTANCE_SECRET = "private-instance-secret-012345"
BOOTSTRAP = "private-bootstrap-token-012345"
CSRF = "private-csrf-token-012345"
COOKIE = "private-cookie-token-012345"


def instance(directory: Path, **changes):
    directory.mkdir(parents=True, exist_ok=True)
    value = {"protocol_version": 1, "pid": 1234, "instance_id": INSTANCE_ID,
             "origin": "http://127.0.0.1:19876", "secret": INSTANCE_SECRET} | changes
    (directory / "instance.json").write_text(json.dumps(value), encoding="utf-8")
    return value


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    info = instance(tmp_path)
    requests = []
    state = {"session": None, "revision": 0, "source_language": "zh", "secret": None}
    options = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        requests.append((request, body))
        if path == "/health/live":
            return httpx.Response(200, json={"protocol_version": 1, "instance_id": INSTANCE_ID})
        if path == "/api/local/instance":
            assert request.headers["X-Translator-Instance"] == INSTANCE_SECRET
            assert request.headers["Origin"] == info["origin"]
            response = {"protocol_version": 1, "instance_id": INSTANCE_ID}
            if body["action"] == "open":
                response["url"] = info["origin"] + "/#bootstrap=" + BOOTSTRAP
            return httpx.Response(200, json=response)
        if path == "/api/local/bootstrap" and request.method == "POST":
            assert body == {"token": BOOTSTRAP}
            return httpx.Response(200, json={"csrf_token": CSRF},
                                  headers={"set-cookie": f"translator_local={COOKIE}; HttpOnly; SameSite=Strict; Path=/"})
        assert request.headers.get("cookie") == f"translator_local={COOKIE}"
        if request.method not in {"GET", "HEAD"}:
            assert request.headers["X-CSRF-Token"] == CSRF
        settings = {"revision": state["revision"], "source_language": state["source_language"],
                    "secrets": {"gladia": {"configured": state["secret"] is not None, "storage": "memory"}}}
        if path == "/api/local/logout":
            return httpx.Response(200, json={"status": "logged_out"})
        if path == "/api/local/bootstrap":
            return httpx.Response(200, json={"csrf_token": CSRF, "settings": settings, "session": state["session"]})
        if path == "/api/local/settings":
            if request.method == "PATCH":
                if body["expected_revision"] != state["revision"]:
                    return httpx.Response(409, json={"error": {"code": "SETTINGS_CONFLICT", "message": "not echoed", "retryable": False}})
                state["source_language"] = body["settings"].get("source_language", "zh")
                state["revision"] += 1
                settings.update(revision=state["revision"], source_language=state["source_language"])
            return httpx.Response(200, json=settings)
        if path == "/api/local/secrets/gladia":
            state["secret"] = body
            return httpx.Response(200, json={"secrets": {"gladia": {"configured": True, "storage": "memory", "key": body["key"]}}})
        if path == "/api/local/sessions":
            state["session"] = {"session_id": SESSION_ID, "status": "idle", "demo": body["demo"],
                                "source_language": body["source_language"], "target_language": body["target_language"],
                                "translation_enabled": body["translation_enabled"], "captions": {}}
            return httpx.Response(200, json=state["session"])
        if path.endswith("/start"):
            state["session"]["status"] = "running" if state["session"]["demo"] else "waiting_for_peer"
            return httpx.Response(200, json=state["session"])
        if path.endswith("/stop"):
            state["session"]["status"] = "ended"
            return httpx.Response(200, json=state["session"])
        if path.endswith("/invites"):
            return httpx.Response(200, json={"url": "http://127.0.0.1:8000/join#invite=sensitive-invite", "expires_in": 600})
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "private server details"}})

    def client_factory(**kwargs):
        options.append(kwargs)
        return HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("translator.agent.client.httpx.Client", client_factory)
    return {"directory": tmp_path, "requests": requests, "state": state, "options": options,
            "handler": handler, "info": info}


def invoke(capsys, directory, *arguments):
    code = main([*arguments, "--data-dir", str(directory)])
    captured = capsys.readouterr()
    assert not captured.err
    assert len(captured.out.splitlines()) == 1
    value = json.loads(captured.out)
    assert set(value) == {"schema_version", "ok", "command", "data", "error"}
    assert value["schema_version"] == 1
    return code, value, captured.out


def test_catalog_is_offline_finite_and_help_is_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("translator.agent.commands.data_directory", lambda: pytest.fail("Catalog must not resolve a profile"))
    monkeypatch.setattr("translator.agent.client.httpx.Client", lambda **kwargs: pytest.fail("Catalog must not use HTTP"))
    assert len(catalog()["commands"]) == 18
    code, value, _ = invoke(capsys, tmp_path, "catalog")
    assert code == 0 and value["data"] == catalog()
    assert not (tmp_path / "instance.json").exists()
    assert main(["--help"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "catalog"


def test_status_authenticates_without_exposing_bootstrap_cookie_or_csrf(fake_runtime, capsys):
    code, value, output = invoke(capsys, fake_runtime["directory"], "status")
    assert code == 0 and value["data"]["running"]
    assert value["data"]["settings"]["revision"] == 0
    for credential in (INSTANCE_SECRET, BOOTSTRAP, CSRF, COOKIE):
        assert credential not in output
    assert fake_runtime["requests"][-1][0].url.path == "/api/local/logout"
    assert all(options["trust_env"] is False and options["follow_redirects"] is False for options in fake_runtime["options"])


def test_missing_runtime_status_succeeds_but_actions_require_runtime(tmp_path, capsys):
    code, value, _ = invoke(capsys, tmp_path, "status")
    assert code == 0 and value["data"] == {"running": False}
    code, value, _ = invoke(capsys, tmp_path, "settings-get")
    assert code == 3 and value["error"]["code"] == "RUNTIME_NOT_RUNNING"


@pytest.mark.parametrize("origin", ["https://example.com", "http://example.com:8000", "http://127.0.0.1:8000@evil.example",
                                     "http://127.0.0.1:8000/path", "http://127.0.0.1:8000?x=y", "http://127.0.0.1:08000"])
def test_invalid_discovery_never_transmits_credentials(tmp_path, monkeypatch, capsys, origin):
    instance(tmp_path, origin=origin)
    monkeypatch.setattr("translator.agent.client.httpx.Client", lambda **kwargs: pytest.fail("Unsafe origin reached HTTP"))
    code, value, output = invoke(capsys, tmp_path, "status")
    assert code == 3 and value["error"]["code"] == "INSTANCE_INVALID"
    assert origin not in output and INSTANCE_SECRET not in output


def test_mismatched_listener_receives_no_instance_secret(tmp_path, monkeypatch, capsys):
    instance(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"protocol_version": 1, "instance_id": "wrong-runtime"})

    monkeypatch.setattr("translator.agent.client.httpx.Client", lambda **kwargs: HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    code, value, _ = invoke(capsys, tmp_path, "settings-get")
    assert code == 3 and value["error"]["code"] == "INSTANCE_MISMATCH"
    assert len(requests) == 1 and "x-translator-instance" not in requests[0].headers


@pytest.mark.parametrize("url", ["https://evil.example/#bootstrap=" + BOOTSTRAP,
                                  "http://127.0.0.1:19876@evil.example/#bootstrap=" + BOOTSTRAP,
                                  "http://127.0.0.1:19876/?secret=value#bootstrap=" + BOOTSTRAP])
def test_bootstrap_exchange_rejects_other_origin_or_query(fake_runtime, monkeypatch, capsys, url):
    original = fake_runtime["handler"]

    def handler(request):
        if request.url.path == "/api/local/instance" and json.loads(request.content)["action"] == "open":
            return httpx.Response(200, json={"protocol_version": 1, "instance_id": INSTANCE_ID, "url": url})
        return original(request)

    monkeypatch.setattr("translator.agent.client.httpx.Client", lambda **kwargs: HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    code, value, output = invoke(capsys, fake_runtime["directory"], "settings-get")
    assert code == 3 and value["error"]["code"] == "BOOTSTRAP_REJECTED"
    assert not any(request.url.path == "/api/local/bootstrap" for request, _ in fake_runtime["requests"])
    assert BOOTSTRAP not in output and url not in output


def test_http_redirect_is_never_followed(tmp_path, monkeypatch, capsys):
    instance(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.example/collect"})

    monkeypatch.setattr("translator.agent.client.httpx.Client", lambda **kwargs: HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    code, value, _ = invoke(capsys, tmp_path, "settings-get")
    assert code == 3 and value["error"]["code"] == "REDIRECT_REJECTED"
    assert len(requests) == 1


@pytest.mark.parametrize("arguments", [["session-create"], ["session-start"], ["session-start", "--session-id", "../secret"],
                                       ["secret-set", "--provider", "gladia", "--key", "DO_NOT_ECHO"],
                                       ["DO_NOT_ECHO"], ["runtime-start", "--timeout", "nan"]])
def test_invalid_arguments_always_emit_one_redacted_json_error(tmp_path, capsys, arguments):
    code, value, output = invoke(capsys, tmp_path, *arguments)
    assert code == 2 and not value["ok"]
    assert "DO_NOT_ECHO" not in output and "../secret" not in output
    assert set(value["error"]) == {"code", "message", "retryable", "next_action"}


def test_data_directory_is_accepted_before_the_command(fake_runtime, capsys):
    assert main(["--data-dir", str(fake_runtime["directory"]), "settings-get"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["revision"] == 0


def test_settings_revision_conflict_is_not_retried_and_unknown_input_never_sent(fake_runtime, capsys):
    path = fake_runtime["directory"] / "changes.json"
    path.write_text(json.dumps({"expected_revision": 0, "settings": {"source_language": "en"}}), encoding="utf-8")
    code, value, _ = invoke(capsys, fake_runtime["directory"], "settings-set", "--input", str(path), "--request-id", "stable-request")
    assert code == 0 and value["data"]["revision"] == 1
    code, value, _ = invoke(capsys, fake_runtime["directory"], "settings-set", "--input", str(path), "--request-id", "same-body-no-retry")
    assert code == 4 and value["error"]["code"] == "SETTINGS_CONFLICT"
    sent = [body for request, body in fake_runtime["requests"] if request.method == "PATCH"]
    assert len(sent) == 2 and sent[0]["request_id"] == "stable-request"
    path.write_text('{"expected_revision":1,"settings":{"api_key":"DO_NOT_ECHO"}}', encoding="utf-8")
    code, value, output = invoke(capsys, fake_runtime["directory"], "settings-set", "--input", str(path))
    assert code == 2 and "DO_NOT_ECHO" not in output
    assert len([request for request, _ in fake_runtime["requests"] if request.method == "PATCH"]) == 2


@pytest.mark.parametrize("payload", ["{\"expected_revision\":0,\"expected_revision\":1,\"settings\":{}}",
                                    '{"expected_revision":true,"settings":{}}',
                                    '{"expected_revision":0,"settings":{"translation_enabled":"false"}}',
                                    '{"expected_revision":0,"settings":{"persist_audio":true}}',
                                    "x" * 32769], ids=["duplicate", "boolean-revision", "coerced-boolean", "audio-persistence", "oversize"])
def test_settings_input_is_bounded_strict_and_rejects_duplicate_keys(tmp_path, monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code, value, output = invoke(capsys, tmp_path, "settings-set", "--input", "-")
    assert code == 2 and value["error"]["code"] in {"INVALID_SETTINGS_INPUT", "INPUT_TOO_LARGE"}
    assert payload not in output


def test_secret_stdin_is_memory_only_and_never_echoed(fake_runtime, monkeypatch, capsys):
    secret = "PRIVATE-API-KEY-DO-NOT-PRINT"
    monkeypatch.setattr("sys.stdin", io.StringIO(secret + "\n"))
    code, value, output = invoke(capsys, fake_runtime["directory"], "secret-set", "--provider", "gladia", "--input", "-")
    assert code == 0 and value["data"] == {"provider": "gladia", "configured": True, "storage": "memory"}
    assert fake_runtime["state"]["secret"] == {"key": secret, "persist": False}
    assert secret not in output


def test_secret_http_error_body_is_not_echoed_and_not_retried(fake_runtime, monkeypatch, capsys):
    secret = "PRIVATE-API-KEY-DO-NOT-PRINT"
    original = fake_runtime["handler"]
    writes = []

    def handler(request):
        if request.method == "PUT":
            writes.append(request)
            return httpx.Response(503, json={"error": {"code": "PROVIDER_FAILED", "message": secret, "retryable": False}})
        return original(request)

    monkeypatch.setattr("translator.agent.client.httpx.Client", lambda **kwargs: HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("sys.stdin", io.StringIO(secret))
    code, value, output = invoke(capsys, fake_runtime["directory"], "secret-set", "--provider", "gladia", "--input", "-")
    assert code == 4 and value["error"]["retryable"] is False
    assert secret not in output and len(writes) == 1


def test_explicit_demo_session_controls_return_exact_ids(fake_runtime, capsys):
    code, created, _ = invoke(capsys, fake_runtime["directory"], "session-create", "--mode", "demo", "--translation-enabled", "false")
    assert code == 0 and created["data"]["status"] == "idle" and created["data"]["demo"] is True
    assert created["data"]["translation_enabled"] is False
    code, started, _ = invoke(capsys, fake_runtime["directory"], "session-start", "--session-id", SESSION_ID)
    assert code == 0 and started["data"]["status"] == "running"
    code, snapshot, _ = invoke(capsys, fake_runtime["directory"], "session-snapshot", "--session-id", SESSION_ID)
    assert code == 0 and snapshot["data"]["session_id"] == SESSION_ID
    code, mismatch, _ = invoke(capsys, fake_runtime["directory"], "session-snapshot", "--session-id", "other")
    assert code == 4 and mismatch["error"]["code"] == "SESSION_NOT_FOUND"
    code, invite, _ = invoke(capsys, fake_runtime["directory"], "invite-create", "--session-id", SESSION_ID)
    assert code == 0 and "#invite=" in invite["data"]["url"]
    code, ended, _ = invoke(capsys, fake_runtime["directory"], "session-stop", "--session-id", SESSION_ID)
    assert code == 0 and ended["data"]["status"] == "ended"


def test_runtime_start_background_arguments_no_demo_browser_or_pipes(tmp_path, monkeypatch):
    probes = iter([None, {"running": True, "instance_id": INSTANCE_ID, "local_origin": "http://127.0.0.1:19876"}])
    monkeypatch.setattr("translator.agent.commands._running", lambda *args, **kwargs: next(probes))
    launches = []

    def launch(command, **options):
        launches.append((command, options))
        return object()

    monkeypatch.setattr("translator.agent.commands.subprocess.Popen", launch)
    result = runtime_start(tmp_path, 2)
    assert result["running"] and result["already_running"] is False
    command, options = launches[0]
    assert "--no-browser" in command and "--demo" not in command
    assert command[-2:] == ["--data-dir", str(tmp_path)]
    assert options["stdout"] == options["stderr"] == options["stdin"] == -3


def test_runtime_start_does_not_spawn_or_kill_on_unknown_listener(fake_runtime, monkeypatch, capsys):
    monkeypatch.setattr("translator.agent.commands._running", lambda *args, **kwargs: (_ for _ in ()).throw(
        AgentError("INSTANCE_MISMATCH", "Listener differs.", 3)))
    monkeypatch.setattr("translator.agent.commands.subprocess.Popen", lambda *args, **kwargs: pytest.fail("Must preserve unknown listener"))
    code, value, _ = invoke(capsys, fake_runtime["directory"], "runtime-start")
    assert code == 3 and value["error"]["code"] == "INSTANCE_MISMATCH"


def test_runtime_start_timeout_preserves_its_background_process(tmp_path, monkeypatch):
    monkeypatch.setattr("translator.agent.commands._running", lambda *args, **kwargs: None)
    ticks = iter([0, 0, 2])
    monkeypatch.setattr("translator.agent.commands.time.monotonic", lambda: next(ticks, 2))
    monkeypatch.setattr("translator.agent.commands.time.sleep", lambda _: None)

    class Process:
        def poll(self):
            return None

        def terminate(self):
            pytest.fail("A timed-out startup must not terminate a potentially live runtime")

    monkeypatch.setattr("translator.agent.commands.subprocess.Popen", lambda *args, **kwargs: Process())
    with pytest.raises(AgentError) as exc:
        runtime_start(tmp_path, 1)
    assert exc.value.code == "RUNTIME_START_TIMEOUT"


def test_client_logout_releases_each_short_lived_principal(fake_runtime):
    for _ in range(3):
        with AgentClient(fake_runtime["directory"]) as client:
            client.connect()
            assert client.ws_headers()["Origin"] == fake_runtime["info"]["origin"]
    logouts = [request for request, _ in fake_runtime["requests"] if request.url.path == "/api/local/logout"]
    assert len(logouts) == 3


def test_catalog_can_select_one_command_for_small_context(tmp_path, capsys):
    code, value, _ = invoke(capsys, tmp_path, "catalog", "--command", "session-create")
    assert code == 0
    assert [item["name"] for item in value["data"]["commands"]] == ["session-create"]
    language = next(item for item in value["data"]["commands"][0]["arguments"] if item["name"] == "--source-language")
    assert "zh" in language["choices"] and "en" in language["choices"]


def test_audio_share_actual_main_dispatches_integer_seconds(fake_runtime, monkeypatch, capsys):
    calls = []

    async def share(client, session_id, seconds, *, device_id):
        assert type(seconds) is int
        calls.append((session_id, seconds, device_id))
        return {"status": "stopped", "seconds": seconds}

    monkeypatch.setattr("translator.agent.audio.share_audio", share)
    code, value, _ = invoke(capsys, fake_runtime["directory"], "audio-share", "--session-id", SESSION_ID, "--seconds", "1")
    assert code == 0 and calls == [(SESSION_ID, 1, None)]
    assert value["data"]["status"] == "stopped"
    code, _, _ = invoke(capsys, fake_runtime["directory"], "audio-share", "--session-id", SESSION_ID, "--seconds", "1.5")
    assert code == 2 and len(calls) == 1


def test_runtime_stop_resolves_drain_timeout_without_retrying_shutdown(fake_runtime, monkeypatch):
    calls = []

    class Client:
        directory = fake_runtime["directory"]
        instance_id = INSTANCE_ID

        def request(self, method, path, body):
            calls.append((method, path, body))
            raise AgentError("OPERATION_TIMEOUT", "Shutdown response was lost.")

    probes = iter([AgentError("OPERATION_TIMEOUT", "Server draining."), None])

    def probe(*args, **kwargs):
        result = next(probes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("translator.agent.commands._running", probe)
    monkeypatch.setattr("translator.agent.commands.time.sleep", lambda _: None)
    assert runtime_stop(Client(), 2, "stop-request") == {"running": False, "stopped_instance_id": INSTANCE_ID}
    assert len(calls) == 1


def test_runtime_stop_does_not_hide_identity_mismatch(fake_runtime, monkeypatch):
    class Client:
        directory = fake_runtime["directory"]
        instance_id = INSTANCE_ID

        def request(self, *args):
            return {"status": "stopping"}

    def probe(*args, **kwargs):
        raise AgentError("INSTANCE_MISMATCH", "Listener does not match.", 3)

    monkeypatch.setattr("translator.agent.commands._running", probe)
    with pytest.raises(AgentError) as exc:
        runtime_stop(Client(), 2, "request")
    assert exc.value.code == "INSTANCE_MISMATCH"


def test_status_excludes_transcripts_and_snapshots_bound_recent_history(fake_runtime, capsys):
    fake_runtime["state"]["session"] = {"session_id": SESSION_ID, "status": "running", "demo": True,
        "captions": {f"utterance-{i}": {"original": {"text": f"private-utterance-{i}"}} for i in range(100)}}
    code, value, output = invoke(capsys, fake_runtime["directory"], "status")
    assert code == 0 and value["data"]["caption_count"] == 100
    assert "captions" not in value["data"]["session"] and "private-utterance" not in output
    code, value, _ = invoke(capsys, fake_runtime["directory"], "session-snapshot", "--session-id", SESSION_ID)
    assert code == 0 and value["data"]["caption_count"] == 100 and value["data"]["captions_truncated"]
    assert list(value["data"]["captions"]) == [f"utterance-{i}" for i in range(90, 100)]
    code, value, _ = invoke(capsys, fake_runtime["directory"], "session-snapshot", "--session-id", SESSION_ID, "--limit", "1")
    assert code == 0 and list(value["data"]["captions"]) == ["utterance-99"]
    code, _, _ = invoke(capsys, fake_runtime["directory"], "session-snapshot", "--session-id", SESSION_ID, "--limit", "101")
    assert code == 2
