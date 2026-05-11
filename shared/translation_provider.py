import logging
from abc import ABC, abstractmethod
import httpx

logger = logging.getLogger(__name__)


class TranslationProvider(ABC):
    async def close(self):
        pass

    @abstractmethod
    async def translate(self, text: str, source: str, target: str) -> str:
        pass


class DummyTranslationProvider(TranslationProvider):
    async def translate(self, text: str, source: str, target: str) -> str:
        return f"[訳]{text}"


class DeepLTranslationProvider(TranslationProvider):
    _ENDPOINT = "https://api-free.deepl.com/v2/translate"

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=5.0)

    async def translate(self, text: str, source: str, target: str) -> str:
        try:
            resp = await self._client.post(
                self._ENDPOINT,
                headers={"Authorization": f"DeepL-Auth-Key {self._api_key}"},
                json={
                    "text": [text],
                    "source_lang": source.upper(),
                    "target_lang": target.upper(),
                },
            )
            resp.raise_for_status()
            return resp.json()["translations"][0]["text"]
        except Exception as e:
            logger.error(f"DeepL error: {e}")
            return text

    async def close(self):
        await self._client.aclose()
