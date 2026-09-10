"""One owner for the local server, public hub, providers, capture worker, and tunnel."""

import asyncio
import contextlib
import hmac
import secrets
import socket
import time
import uuid

from fastapi import HTTPException, Request
import uvicorn

from translator import __version__
from translator.assets import source_root, web_directory
from translator.config import Settings


class RuntimeServer(uvicorn.Server):
    def capture_signals(self):
        # The CLI's asyncio.run owns interruption and closes the whole Runtime.
        # Two Uvicorn contexts would install handlers over one another, then
        # restore them out of order and leave an exited server's handler behind.
        return contextlib.nullcontext()


def bind_loopback(preferred: int) -> socket.socket:
    if preferred < 0 or preferred > 65535:
        raise ValueError("PORT_INVALID")
    for port in (preferred, 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Reserve the socket before starting Uvicorn; no probe/bind race.
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except OSError:
            sock.close()
    raise RuntimeError("PORT_BIND_FAILED")


class Runtime:
    def __init__(self, settings: Settings, *, demo=False, local_port=8765, hub_port=8000,
                 development=False):
        from translator.api.auth import AuthManager
        from translator.sessions.service import SessionService
        self.settings = settings
        self.demo = demo
        self.development = development
        self.instance_id = uuid.uuid4().hex
        self.instance_secret = secrets.token_urlsafe(32)
        self.web_dir = web_directory()
        self.source_root = source_root()
        self.auth = AuthManager()
        self.sessions = SessionService(provider_factory=self._providers)
        self._preferred_ports = local_port, hub_port
        self.local_origin = ""
        self.hub_origin = ""
        self.public_origin: str | None = None
        self.tunnel = None
        self._worker = None
        self._capture_task = None
        self._audio_lock = asyncio.Lock()
        self._servers: list[uvicorn.Server] = []
        self._server_tasks = []
        self._sockets: list[socket.socket] = []
        self._stopped = asyncio.Event()
        self._closed = False
        self._started_at = time.monotonic()
        self._provider_tests: dict = {}

    def _providers(self, demo: bool):
        from translator.providers import (DummySTTProvider, DummyTranslationProvider,
                                          GladiaSTTProvider, DeepLTranslationProvider)
        if demo:
            return DummySTTProvider(), DummyTranslationProvider()
        gladia = self.settings.get_secret("gladia")
        deepl = self.settings.get_secret("deepl")
        if not gladia:
            raise ValueError("STT_NOT_CONFIGURED")
        # Translation may be disabled; the adapter reports an explicit error if later enabled.
        return GladiaSTTProvider(gladia), DeepLTranslationProvider(deepl or "", plan=self.settings.value.deepl_mode)

    async def start(self):
        from translator.api.local_app import create_local_app
        from translator.api.hub_app import create_hub_app
        from translator.remote.ngrok import TunnelManager
        local = bind_loopback(self._preferred_ports[0])
        self._sockets.append(local)
        hub = bind_loopback(self._preferred_ports[1])
        self._sockets.append(hub)
        local_port, hub_port = local.getsockname()[1], hub.getsockname()[1]
        self.local_origin = f"http://127.0.0.1:{local_port}"
        self.hub_origin = f"http://127.0.0.1:{hub_port}"
        self.tunnel = TunnelManager(hub_port=hub_port, local_port=local_port)
        self.local_app = create_local_app(self)
        self.hub_app = create_hub_app(self)

        @self.local_app.post("/api/local/instance")
        async def instance(request: Request):
            supplied = request.headers.get("X-Translator-Instance", "")
            if (request.headers.get("origin") != self.local_origin or
                    not hmac.compare_digest(supplied, self.instance_secret)):
                raise HTTPException(403, "INSTANCE_AUTH_FAILED")
            data = await request.json()
            action = data.get("action")
            if action not in {"open", "stop", "status"}:
                raise HTTPException(422, "INVALID_ACTION")
            result = {"instance_id": self.instance_id, "protocol_version": 1}
            if action == "stop":
                asyncio.create_task(self.shutdown())
            elif action == "open":
                result["url"] = self.local_origin + "/#bootstrap=" + self.auth.issue_bootstrap()
            return result

        for app, sock in ((self.local_app, local), (self.hub_app, hub)):
            server = RuntimeServer(uvicorn.Config(app, host="127.0.0.1", port=sock.getsockname()[1],
                log_config=None, access_log=False, proxy_headers=False, ws_max_size=16_384,
                timeout_graceful_shutdown=5))
            self._servers.append(server)
            self._server_tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
        deadline = time.monotonic() + 15
        while not all(server.started for server in self._servers):
            if any(task.done() for task in self._server_tasks) or time.monotonic() > deadline:
                raise RuntimeError("HTTP_START_FAILED")
            await asyncio.sleep(0.025)

    async def wait(self):
        waiter = asyncio.create_task(self._stopped.wait())
        try:
            await asyncio.wait([waiter, *self._server_tasks], return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()

    async def shutdown(self):
        self._stopped.set()

    async def list_audio_devices(self):
        from translator.audio.loopback import list_devices
        return await list_devices()

    async def set_system_audio(self, enabled: bool, device_id: str | None = None):
        from translator.audio.worker import NativeAudioWorker
        async with self._audio_lock:
            if not enabled:
                task, worker = self._capture_task, self._worker
                self._capture_task = self._worker = None
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                if worker:
                    await worker.stop()
                if self.sessions.current:
                    self.sessions.current.components["system_audio_status"] = "idle"
                    self.sessions.notify()
                return {"status": "idle"}
            if self._worker:
                return {"status": "active"}
            if self.sessions.current is None or self.sessions.current.status in {"idle", "ended", "stopping"}:
                raise ValueError("SESSION_NOT_ACTIVE")
            worker = NativeAudioWorker(device_id=device_id, frame_ms=self.settings.value.frame_ms)
            await worker.start()
            self._worker = worker
            self.sessions.current.components["system_audio_status"] = "active"
            self.sessions.notify()

            async def relay():
                try:
                    async for frame in worker.frames():
                        self.sessions.publish_system_audio(frame.encode())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    current = self.sessions.current
                    if current:
                        current.components["system_audio_status"] = "error"
                        self.sessions.notify()
                    await worker.stop()
                    self._worker = None

            self._capture_task = asyncio.create_task(relay())
            return {"status": "active"}

    async def start_tunnel(self):
        key = self.settings.get_secret("ngrok")
        if not key:
            raise ValueError("NGROK_NOT_CONFIGURED")
        self.public_origin = await self.tunnel.start(key, self.settings.value.remote_domain or "")
        return {"status": self.tunnel.status, "url": self.public_origin}

    async def stop_tunnel(self):
        if self.tunnel:
            await self.tunnel.stop()
        self.public_origin = None
        return {"status": "idle", "url": None}

    async def test_provider(self, provider: str):
        from translator.providers import GladiaSTTProvider, DeepLTranslationProvider
        from translator.providers.base import STTConfig
        key = self.settings.get_secret(provider)
        if not key:
            raise ValueError("PROVIDER_NOT_CONFIGURED")
        adapter = None
        try:
            if provider == "deepl":
                adapter = DeepLTranslationProvider(key, plan=self.settings.value.deepl_mode)
                result = await adapter.test()
            elif provider == "gladia":
                adapter = GladiaSTTProvider(key)
                await adapter.start(STTConfig(language=self.settings.value.source_language))
                async def drain():
                    async for _event in adapter.events():
                        pass
                receiver = asyncio.create_task(drain())
                try:
                    await adapter.finish_input()
                    await asyncio.wait_for(receiver, timeout=4)
                finally:
                    receiver.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await receiver
                result = {"status": "ready"}
            else:
                return {"status": "configured", "message": "公開開始で接続を確認します。"}
            result = result | {"checked_at": time.time()}
            self._provider_tests[provider] = result
            return result
        finally:
            if adapter:
                await adapter.close()

    async def import_env(self, persist: bool = False):
        path = self.source_root / ".env"
        if not path.is_file():
            raise ValueError("ENV_NOT_FOUND")
        return self.settings.import_env(path, persist)

    def diagnostics(self):
        current = self.sessions.current
        return {"version": __version__, "instance_id": self.instance_id,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "local_origin": self.local_origin, "hub_origin": self.hub_origin,
            "tunnel_status": self.tunnel.status if self.tunnel else "idle",
            "provider_tests": self._provider_tests,
            "components": dict(current.components) if current else {},
            "settings_recovery": self.settings.recovery_warning,
            "privacy": {"audio_saved": False, "transcripts_saved": False},
            "env_available": (self.source_root / ".env").is_file(),
            "limits": {"caption_history": 100, "audio_message_bytes": 4096,
                       "sample_rate": 16000, "frame_ms": self.settings.value.frame_ms}}

    async def close(self):
        if self._closed:
            return
        self._closed = True
        # Each owned resource gets a cleanup attempt even if another driver fails.
        for cleanup in (lambda: self.set_system_audio(False), self.sessions.close, self.stop_tunnel):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(cleanup(), timeout=8)
        for server in self._servers:
            server.should_exit = True
        if self._server_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self._server_tasks, return_exceptions=True), 8)
            except TimeoutError:
                for task in self._server_tasks:
                    task.cancel()
        for sock in self._sockets:
            sock.close()
