import logging
from abc import ABC, abstractmethod
import httpx
from shared.languages import get_language

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
        source_lang = get_language(source)
        target_lang = get_language(target)
        if not source_lang or not target_lang:
            return text
        try:
            resp = await self._client.post(
                self._ENDPOINT,
                headers={"Authorization": f"DeepL-Auth-Key {self._api_key}"},
                json={
                    "text": [text],
                    "source_lang": source_lang["deepl_source"],
                    "target_lang": target_lang["deepl_target"],
                },
            )
            resp.raise_for_status()
            return resp.json()["translations"][0]["text"]
        except Exception as e:
            logger.error(f"DeepL error: {e}")
            return text

    async def close(self):
        await self._client.aclose()
