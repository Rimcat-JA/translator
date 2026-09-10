"""Explicit ngrok lifecycle restricted to the runtime's participant Hub port."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import importlib
import inspect
import re
from urllib.parse import urlparse

from ..providers.base import ProviderError


class TunnelManager:
    def __init__(self, hub_port: int, local_port: int):
        if not 1 <= hub_port <= 65535 or not 1 <= local_port <= 65535 or hub_port == local_port:
            raise ValueError("Hub and management ports must be distinct valid ports")
        self._target = f"http://127.0.0.1:{hub_port}"
        self._listener = None
        self._lock = asyncio.Lock()
        self.status = "idle"
        self.url: str | None = None
        self.error_code: str | None = None

    async def start(self, authtoken: str, domain: str = "") -> str:
        async with self._lock:
            if self._listener:
                return self.url
            if not authtoken.strip():
                raise ProviderError("NGROK_NOT_CONFIGURED", "リモート接続の認証トークンを設定してください。")
            if domain and (len(domain) > 253 or not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", domain)):
                raise ProviderError("NGROK_INVALID_DOMAIN", "公開ドメインはhttps://やパスを含めず入力してください。")
            self.status = "connecting"
            self.error_code = None
            try:
                sdk = importlib.import_module("ngrok")
                options = {"authtoken": authtoken}
                if domain:
                    options["domain"] = domain
                # SDK returns an awaitable inside an active asyncio runtime.
                result = sdk.forward(self._target, **options)
                self._listener = await result if inspect.isawaitable(result) else result
                self.url = self._listener.url()
                parsed = urlparse(self.url)
                if parsed.scheme != "https" or not parsed.hostname:
                    raise ValueError("HTTPS public endpoint required")
                self.status = "ready"
                return self.url
            except asyncio.CancelledError:
                with suppress(Exception):
                    await self._close_listener()
                self.status = "error" if self._listener else "idle"
                raise
            except Exception:
                with suppress(Exception):
                    await self._close_listener()
                self.status = "error"
                self.error_code = "NGROK_START_FAILED"
                raise ProviderError(self.error_code, "リモート接続を開始できません。認証・ドメイン・ネットワークを確認してください。", True) from None

    async def _close_listener(self) -> None:
        listener = self._listener
        if listener:
            result = listener.close()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, 5.0)
        self._listener = None
        self.url = None

    async def stop(self) -> None:
        async with self._lock:
            try:
                await self._close_listener()
            except Exception:
                # Retain the listener for a retry; never claim that public
                # forwarding stopped when the SDK did not confirm closure.
                self.status = "error"
                self.error_code = "NGROK_STOP_FAILED"
                raise ProviderError(self.error_code, "公開停止を確認できませんでした。もう一度停止してください。", True) from None
            self.status = "idle"
            self.error_code = None
