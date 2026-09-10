import json

import pytest

from translator.config import Settings
from translator.instance import InstanceLock, contact_instance
from translator.secrets import SecretStore


@pytest.fixture
def store(monkeypatch):
    for name in ("GLADIA_API_KEY", "DEEPL_API_KEY", "NGROK_AUTHTOKEN"):
        monkeypatch.delenv(name, raising=False)
    value = SecretStore()
    value.backend = None
    return value


def test_memory_secret_never_persisted_or_returned(tmp_path, store):
    settings = Settings(tmp_path, store)
    settings.set_secret("gladia", "private-example-value", persist=False)
    settings.update({"theme": "dark"}, expected_revision=0)
    assert "private-example-value" not in json.dumps(settings.public())
    assert "private-example-value" not in settings.path.read_text()
    assert settings.public()["secrets"]["gladia"]["storage"] == "memory"
    with pytest.raises(ValueError, match="SECURE_STORAGE_UNAVAILABLE"):
        settings.set_secret("deepl", "private-example-value", persist=True)


def test_settings_revision_validation_and_recovery(tmp_path, store):
    settings = Settings(tmp_path, store)
    settings.update({"caption_font_size": 48})
    with pytest.raises(ValueError, match="CONFLICT"):
        settings.update({"theme": "dark"}, expected_revision=0)
    for changes in ({"caption_font_size": 100}, {"persist_audio": True}, {"api_key": "x"}, {"source_language": "xx"}):
        with pytest.raises(ValueError):
            settings.update(changes)
    assert Settings(tmp_path, store).value.caption_font_size == 48
    settings.path.write_text("{broken", encoding="utf-8")
    recovered = Settings(tmp_path, store)
    assert recovered.recovery_warning
    assert len(list(tmp_path.glob("settings.corrupt-*.json"))) == 1


def test_env_import_is_explicit_and_ignores_placeholders(tmp_path, store):
    env = tmp_path / ".env"
    env.write_text("GLADIA_API_KEY=your_gladia_api_key_here\nDEEPL_API_KEY=example-secret\n")
    settings = Settings(tmp_path, store)
    assert not settings.get_secret("deepl")
    result = settings.import_env(env, persist=False)
    assert result["imported"] == ["deepl"]
    assert env.exists()
    assert settings.get_secret("deepl") == "example-secret"


def test_instance_lock_and_stale_pid_do_not_kill_process(tmp_path):
    first, second = InstanceLock(tmp_path), InstanceLock(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.publish("http://127.0.0.1:1", "instance", "secret")
    assert contact_instance(tmp_path) is None
    first.release()
    assert second.acquire()
    second.release()


def test_instance_does_not_send_secret_to_external_host(tmp_path, monkeypatch):
    (tmp_path / "instance.json").write_text(json.dumps({"origin": "http://example.org", "protocol_version": 1, "secret": "private"}))
    def forbidden(*args, **kwargs):
        pytest.fail("External IPC request attempted")
    monkeypatch.setattr("httpx.Client", forbidden)
    assert contact_instance(tmp_path) is None
