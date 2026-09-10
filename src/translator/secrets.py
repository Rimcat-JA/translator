"""Secrets stay in the OS credential store or in this process, never in settings JSON."""

import os

PROVIDERS = ("gladia", "deepl", "ngrok")
ENV_NAMES = {"gladia": "GLADIA_API_KEY", "deepl": "DEEPL_API_KEY", "ngrok": "NGROK_AUTHTOKEN"}


def valid_secret(value: str | None) -> bool:
    return bool(value and value.strip() and not any(
        text in value.lower() for text in ("your_", "_here", "placeholder", "changeme")
    ))


class SecretStore:
    def __init__(self):
        self.memory: dict[str, str] = {}
        self.sources: dict[str, str] = {}
        self.backend = None
        try:
            import keyring
            backend = keyring.get_keyring()
            # A generic chain can silently select a plaintext backend. Only vetted OS stores.
            if type(backend).__module__ in {
                "keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService"
            }:
                self.backend = backend
        except Exception:
            pass

    def get(self, provider: str) -> str | None:
        if provider not in PROVIDERS:
            raise ValueError("UNKNOWN_PROVIDER")
        if provider in self.memory:
            return self.memory[provider] or None
        value = os.environ.get(ENV_NAMES[provider])
        if valid_secret(value):
            self.sources[provider] = "environment"
            return value
        if self.backend:
            try:
                value = self.backend.get_password("Translator", provider)
                if valid_secret(value):
                    self.sources[provider] = "os_keyring"
                    return value
            except Exception:
                pass
        return None

    def set(self, provider: str, value: str, persist: bool = True):
        if provider not in PROVIDERS:
            raise ValueError("UNKNOWN_PROVIDER")
        value = value.strip()
        if value and (not valid_secret(value) or len(value) > 4096):
            raise ValueError("INVALID_SECRET")
        if persist:
            if not self.backend:
                raise ValueError("SECURE_STORAGE_UNAVAILABLE: 今回のみ保存を選択してください。")
            try:
                if value:
                    self.backend.set_password("Translator", provider, value)
                else:
                    try:
                        self.backend.delete_password("Translator", provider)
                    except Exception:
                        pass
            except Exception:
                raise ValueError("SECURE_STORAGE_FAILED") from None
        self.memory[provider] = value
        self.sources[provider] = "os_keyring" if persist else "memory"

    def public(self):
        return {p: {"configured": bool(self.get(p)), "storage": self.sources.get(p, "none"),
                    "verified": False} for p in PROVIDERS}
