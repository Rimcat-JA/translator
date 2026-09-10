"""Short-lived, in-memory credentials with separate local/participant audiences."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import secrets
import time


@dataclass(frozen=True)
class Principal:
    identity: str
    csrf_token: str
    expires: float
    session_id: str | None = None
    role: str = "host"


class AuthManager:
    def __init__(self) -> None:
        self._bootstrap: dict[str, float] = {}
        self._local: dict[str, Principal] = {}
        self._participants: dict[str, Principal] = {}
        self._invites: dict[str, tuple[str, str, float]] = {}

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _prune(self) -> None:
        now = time.monotonic()
        self._bootstrap = {k: v for k, v in self._bootstrap.items() if v > now}
        self._invites = {k: v for k, v in self._invites.items() if v[2] > now}
        self._local = {k: v for k, v in self._local.items() if v.expires > now}
        self._participants = {k: v for k, v in self._participants.items() if v.expires > now}

    def issue_bootstrap(self) -> str:
        self._prune()
        token = secrets.token_urlsafe(32)
        if len(self._bootstrap) >= 32:
            self._bootstrap.pop(next(iter(self._bootstrap)))
        self._bootstrap[self._hash(token)] = time.monotonic() + 60
        return token

    def exchange_bootstrap(self, token: str) -> tuple[str, Principal] | None:
        self._prune()
        expiry = self._bootstrap.pop(self._hash(token), 0)
        if expiry <= time.monotonic():
            return None
        return self._issue(self._local)

    def _issue(self, audience: dict[str, Principal], session_id: str | None = None,
               role: str = "host") -> tuple[str, Principal]:
        token = secrets.token_urlsafe(32)
        principal = Principal(secrets.token_hex(16), secrets.token_urlsafe(32),
                              time.monotonic() + 8 * 3600, session_id, role)
        if len(audience) >= 128:
            audience.pop(next(iter(audience)))
        audience[self._hash(token)] = principal
        return token, principal

    def local(self, token: str) -> Principal | None:
        self._prune()
        return self._local.get(self._hash(token))

    def participant(self, token: str) -> Principal | None:
        self._prune()
        return self._participants.get(self._hash(token))

    def invite(self, session_id: str, role: str = "speaker") -> str:
        self._prune()
        token = secrets.token_urlsafe(32)
        if len(self._invites) >= 64:
            self._invites.pop(next(iter(self._invites)))
        self._invites[self._hash(token)] = (session_id, role, time.monotonic() + 600)
        return token

    def join(self, token: str) -> tuple[str, Principal] | None:
        self._prune()
        invite = self._invites.pop(self._hash(token), None)
        if invite is None:
            return None
        return self._issue(self._participants, invite[0], invite[1])

    def leave(self, token: str) -> None:
        self._participants.pop(self._hash(token), None)

    def logout_local(self, token: str) -> None:
        self._local.pop(self._hash(token), None)

    def revoke_session(self, session_id: str) -> None:
        self._participants = {k: v for k, v in self._participants.items()
                              if v.session_id != session_id}
        self._invites = {k: v for k, v in self._invites.items() if v[0] != session_id}
