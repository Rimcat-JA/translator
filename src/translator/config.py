"""Validated, atomic, non-secret user preferences."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from translator.secrets import SecretStore, ENV_NAMES, valid_secret


def data_directory() -> Path:
    override = os.environ.get("TRANSLATOR_DATA_DIR")
    if override:
        path = Path(override).expanduser().resolve()
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Translator"
    elif sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/Translator"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "translator"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    source_language: str = Field(default="zh", pattern=r"^[a-z]{2}$")
    target_language: str = Field(default="ja", pattern=r"^[a-z]{2}$")
    translation_enabled: bool = True
    deepl_mode: Literal["free", "pro"] = "free"
    loopback_device_id: str | None = Field(default=None, max_length=1024)
    remote_domain: str | None = Field(default=None, max_length=253, pattern=r"^[a-zA-Z0-9.-]+$")
    theme: Literal["system", "light", "dark"] = "system"
    caption_font_size: int = Field(default=32, ge=24, le=64)
    show_original: bool = True
    frame_ms: Literal[20, 40, 100] = 20
    persist_transcripts: Literal[False] = False
    persist_audio: Literal[False] = False


def atomic_json(path: Path, value: dict):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


class Settings:
    def __init__(self, directory: Path | None = None, secrets: SecretStore | None = None):
        self.directory = directory or data_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "settings.json"
        self.secrets = secrets or SecretStore()
        self.recovery_warning: str | None = None
        self.value = Preferences()
        if self.path.exists():
            try:
                self.value = Preferences.model_validate_json(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                backup = self.path.with_name(f"settings.corrupt-{time.time_ns()}.json")
                os.replace(self.path, backup)
                self.recovery_warning = "設定を読み込めませんでした。元ファイルをバックアップしました。"

    def public(self) -> dict:
        return self.value.model_dump() | {
            "secrets": self.secrets.public(),
            "secure_storage_available": self.secrets.backend is not None,
            "recovery_warning": self.recovery_warning,
        }

    def update(self, changes: dict, expected_revision: int | None = None) -> dict:
        if expected_revision is not None and expected_revision != self.value.revision:
            raise ValueError("SETTINGS_CONFLICT")
        if set(changes) & {"schema_version", "revision"}:
            raise ValueError("READ_ONLY_SETTING")
        candidate = Preferences.model_validate(self.value.model_dump() | changes)
        from translator.providers.languages import SUPPORTED_LANGUAGES
        codes = {language["code"] for language in SUPPORTED_LANGUAGES}
        if candidate.source_language not in codes or candidate.target_language not in codes:
            raise ValueError("UNSUPPORTED_LANGUAGE")
        candidate.revision += 1
        atomic_json(self.path, candidate.model_dump())
        self.value = candidate
        return self.public()

    def set_secret(self, provider: str, key: str, persist: bool = True):
        self.secrets.set(provider, key, persist)
        return self.secrets.public()[provider]

    def get_secret(self, provider: str):
        return self.secrets.get(provider)

    def import_env(self, path: Path, persist: bool = False):
        from dotenv import dotenv_values
        values = dotenv_values(path)
        imported = []
        for provider, variable in ENV_NAMES.items():
            value = values.get(variable)
            if valid_secret(value):
                self.set_secret(provider, value, persist)
                imported.append(provider)
        return {"imported": imported, "original_preserved": True}
