"""Authenticated local HTTP client. Credentials never leave the validated loopback origin."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import httpx


class AgentError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 4, *,
                 retryable: bool = False, next_action: str = "Run translator agent status."):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
        self.next_action = next_action

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable,
                "next_action": self.next_action}


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def read_instance(directory: Path) -> dict | None:
    try:
        with (directory / "instance.json").open("rb") as file:
            raw = file.read(16385)
    except FileNotFoundError:
        return None
    except OSError:
        raise AgentError("INSTANCE_UNREADABLE", "Runtime discovery metadata could not be read.", 3,
                         next_action="Check the data directory permissions, then run status.") from None
    try:
        if len(raw) > 16384:
            raise ValueError()
        info = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(info, dict) or type(info.get("protocol_version")) is not int or info["protocol_version"] != 1:
            raise ValueError()
        origin = info["origin"]
        parsed = urlsplit(origin)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or
                parsed.password or parsed.path or parsed.query or parsed.fragment or
                not parsed.port or parsed.netloc != f"127.0.0.1:{parsed.port}"):
            raise ValueError()
        if not isinstance(info["instance_id"], str) or not SAFE_ID.fullmatch(info["instance_id"]):
            raise ValueError()
        if not isinstance(info["secret"], str) or not 16 <= len(info["secret"]) <= 200:
            raise ValueError()
        return info
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AgentError("INSTANCE_INVALID", "Runtime discovery metadata is invalid; no credential was sent.", 3,
                         next_action="Inspect the data directory and restart Translator through its normal launcher.") from None


def _public(value):
    """Defense in depth for credentials accidentally added to a future API response."""
    forbidden = {"csrf_token", "bootstrap_token", "bootstrap", "access_token", "authorization", "cookie", "key", "secret"}
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items() if key.lower() not in forbidden}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def public_result(value: dict, *, allow_invite: bool = False) -> dict:
    result = _public(value)

    def scrub(item):
        if isinstance(item, str) and ("#bootstrap=" in item or (not allow_invite and "#invite=" in item)):
            return "[redacted credential URL]"
        if isinstance(item, dict):
            return {key: scrub(value) for key, value in item.items()}
        if isinstance(item, list):
            return [scrub(value) for value in item]
        return item

    return scrub(result)


class AgentClient:
    def __init__(self, directory: Path, timeout: float = 15):
        self.directory = directory
        self.timeout = timeout
        self.origin = ""
        self.instance_id = ""
        self._info: dict | None = None
        self._http: httpx.Client | None = None
        self._authenticated = False

    def _setup(self) -> bool:
        info = read_instance(self.directory)
        if info is None:
            return False
        self._info = info
        self.origin, self.instance_id = info["origin"], info["instance_id"]
        if self._http:
            self._http.close()
        self._http = httpx.Client(base_url=self.origin, timeout=self.timeout, trust_env=False,
                                  follow_redirects=False, headers={"Origin": self.origin})
        return True

    def _decode(self, response: httpx.Response) -> dict:
        if 300 <= response.status_code < 400:
            raise AgentError("REDIRECT_REJECTED", "The local runtime attempted to redirect the request.", 3)
        if not response.is_success:
            try:
                detail = response.json().get("error", {})
            except (ValueError, AttributeError):
                detail = {}
            candidate = detail.get("code") if isinstance(detail, dict) else None
            code = candidate if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", candidate) else "HTTP_OPERATION_FAILED"
            auth = response.status_code in {401, 403}
            if auth:
                next_action = "Run status, then reopen the runtime using runtime-start if needed."
            elif response.status_code == 409:
                next_action = "Run status or session-snapshot; reconcile current state before submitting a new request."
            elif response.status_code == 422:
                next_action = "Run catalog and settings-get; correct input and its expected_revision."
            else:
                next_action = "Run diagnostics and inspect provider settings before retrying explicitly."
            guidance = {
                "SETTINGS_CONFLICT": "Run settings-get and submit the new expected_revision with the intended settings changes.",
                "STT_NOT_CONFIGURED": "Run secret-set --provider gladia --input - before starting live interpretation.",
                "NGROK_NOT_CONFIGURED": "Run secret-set --provider ngrok --input - before tunnel-start.",
                "PROVIDER_NOT_CONFIGURED": "Use secret-set --provider PROVIDER --input - for the intended provider, then explicitly run provider-test.",
                "SECURE_STORAGE_UNAVAILABLE": "Run secret-set without --persist to hold the credential only in this runtime's memory.",
                "SECURE_STORAGE_FAILED": "Inspect the OS credential store or run secret-set without --persist for memory-only storage.",
                "SESSION_NOT_FOUND": "Run status and use its current session_id; create a session only if none is active.",
            }
            next_action = guidance.get(code, next_action)
            raise AgentError(code, "The runtime rejected the operation; inspect the error code and current state.",
                             3 if auth else 4, retryable=detail.get("retryable") is True if isinstance(detail, dict) else False,
                             next_action=next_action)
        try:
            if len(response.content) > 16 * 1024 * 1024:
                raise ValueError()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise AgentError("INVALID_RESPONSE", "The runtime returned an invalid response.", 4) from None

    def _send(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        if not self._http or not path.startswith("/") or path.startswith("//"):
            raise AgentError("CLIENT_NOT_CONNECTED", "An authenticated runtime connection is required.", 3)
        try:
            response = self._http.request(method, path, json=body, headers=headers)
        except httpx.ConnectError:
            raise AgentError("RUNTIME_NOT_RUNNING", "No runtime is reachable in this data directory.", 3,
                             next_action="Run translator agent runtime-start.") from None
        except httpx.TimeoutException:
            raise AgentError("OPERATION_TIMEOUT", "The operation timed out; its outcome may be unknown.", 4,
                             next_action="Inspect status before deciding whether to repeat the operation. Do not retry blindly.") from None
        except httpx.HTTPError:
            raise AgentError("CONNECTION_FAILED", "The runtime connection failed; its outcome may be unknown.", 4,
                             next_action="Run status and inspect current state before retrying explicitly.") from None
        return self._decode(response)

    def _check_identity(self, data: dict) -> None:
        if (data.get("instance_id") != self.instance_id or type(data.get("protocol_version")) is not int
                or data["protocol_version"] != 1):
            raise AgentError("INSTANCE_MISMATCH", "The listener does not match the recorded runtime instance.", 3,
                             next_action="Do not stop this listener. Inspect the data directory and normal launcher.")

    def probe(self) -> bool:
        if not self._setup():
            return False
        try:
            # Check the listener identity before transmitting the IPC secret.
            self._check_identity(self._send("GET", "/health/live"))
            result = self._send("POST", "/api/local/instance", {"action": "status"},
                                {"X-Translator-Instance": self._info["secret"]})
            self._check_identity(result)
            return True
        except AgentError as error:
            if error.code == "RUNTIME_NOT_RUNNING":
                return False
            raise

    def connect(self):
        if not self.probe():
            raise AgentError("RUNTIME_NOT_RUNNING", "No runtime is reachable in this data directory.", 3,
                             next_action="Run translator agent runtime-start.")
        opened = self._send("POST", "/api/local/instance", {"action": "open"},
                            {"X-Translator-Instance": self._info["secret"]})
        self._check_identity(opened)
        try:
            url = urlsplit(opened["url"])
            if (f"{url.scheme}://{url.netloc}" != self.origin or url.username or url.password or
                    url.path != "/" or url.query):
                raise ValueError()
            fragment = parse_qs(url.fragment, strict_parsing=True)
            if set(fragment) != {"bootstrap"} or len(fragment["bootstrap"]) != 1:
                raise ValueError()
            token = fragment["bootstrap"][0]
            if not 16 <= len(token) <= 200:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise AgentError("BOOTSTRAP_REJECTED", "The runtime supplied an invalid bootstrap URL; no token was exchanged.", 3) from None
        result = self._send("POST", "/api/local/bootstrap", {"token": token})
        csrf = result.get("csrf_token")
        if not isinstance(csrf, str) or not 16 <= len(csrf) <= 200 or not self._http.cookies.get("translator_local"):
            raise AgentError("BOOTSTRAP_REJECTED", "The runtime authentication response was incomplete.", 3)
        self._http.headers["X-CSRF-Token"] = csrf
        self._authenticated = True
        return self

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self._authenticated:
            raise AgentError("AUTH_REQUIRED", "Authenticate with the runtime before making this request.", 3)
        if not path.startswith("/api/local/"):
            raise AgentError("PATH_FORBIDDEN", "The agent interface can only call named local operations.", 2)
        return self._send(method, path, body)

    def bootstrap(self) -> dict:
        return self.request("GET", "/api/local/bootstrap")

    def ws_headers(self) -> dict:
        if not self._authenticated:
            raise AgentError("AUTH_REQUIRED", "Authenticate before opening an event stream.", 3)
        cookie = self._http.cookies.get("translator_local")
        return {"Origin": self.origin, "Cookie": f"translator_local={cookie}"}

    def close(self) -> None:
        if self._http:
            try:
                if self._authenticated:
                    self._send("POST", "/api/local/logout", {})
            except AgentError:
                # Shutdown and older runtimes may no longer expose logout.
                pass
            finally:
                self._http.close()
                self._http = None
                self._authenticated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
