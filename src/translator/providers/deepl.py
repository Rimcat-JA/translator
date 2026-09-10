"""DeepL adapter. API failure is never returned as a successful source-text echo."""
from __future__ import annotations

import httpx

from .base import ProviderError, TranslationResult, http_error
from .languages import get_language


class DeepLTranslationProvider:
    def __init__(self, api_key: str, plan: str = "free", *, transport=None):
        if plan not in {"free", "pro"}:
            raise ValueError("DeepL plan must be free or pro")
        self._api_key = api_key
        self.endpoint = "https://api-free.deepl.com" if plan == "free" else "https://api.deepl.com"
        self._client: httpx.AsyncClient | None = None
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=5.0, transport=self._transport)
        return self._client

    async def translate(self, text: str, source: str, target: str) -> TranslationResult:
        source_lang, target_lang = get_language(source), get_language(target)
        if not source_lang or not target_lang:
            return TranslationResult(status="error", error_code="UNSUPPORTED_LANGUAGE")
        if source == target:
            return TranslationResult(status="same_language")
        if not self._api_key:
            return TranslationResult(status="error", error_code="DEEPL_NOT_CONFIGURED")
        try:
            resp = await self._http().post(
                self.endpoint + "/v2/translate",
                headers={"Authorization": f"DeepL-Auth-Key {self._api_key}"},
                json={"text": [text], "source_lang": source_lang["deepl_source"], "target_lang": target_lang["deepl_target"]},
            )
            if not resp.is_success:
                return TranslationResult(status="error", error_code=http_error("DEEPL", resp.status_code).code)
            translated = resp.json()["translations"][0]["text"]
            if not isinstance(translated, str) or not translated.strip():
                raise ValueError("missing translation")
            return TranslationResult(translated)
        except httpx.TimeoutException:
            return TranslationResult(status="error", error_code="DEEPL_TIMEOUT")
        except httpx.HTTPError:
            return TranslationResult(status="error", error_code="DEEPL_UNAVAILABLE")
        except (ValueError, KeyError, IndexError, TypeError):
            return TranslationResult(status="error", error_code="DEEPL_INVALID_RESPONSE")

    async def test(self) -> dict:
        """Explicit authenticated usage request; sends no conversation text."""
        try:
            resp = await self._http().get(self.endpoint + "/v2/usage", headers={"Authorization": f"DeepL-Auth-Key {self._api_key}"})
            if not resp.is_success:
                raise http_error("DEEPL", resp.status_code)
            return {"status": "ready"}
        except httpx.TimeoutException:
            raise ProviderError("DEEPL_TIMEOUT", "翻訳サービスへの接続がタイムアウトしました。", True) from None
        except httpx.HTTPError:
            raise ProviderError("DEEPL_UNAVAILABLE", "翻訳サービスに接続できません。", True) from None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
