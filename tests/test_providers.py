import asyncio
import json

import httpx
import pytest

from translator.providers import (
    DeepLTranslationProvider, DummySTTProvider, GladiaSTTProvider, ProviderError, STTConfig,
)
from translator.providers.base import EventBuffer, STTEvent
from translator.providers.languages import stt_language


class FakeSocket:
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.messages.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    async def send(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed = True
        self.messages.put_nowait(None)


def transcript(text, final, start=0, external="changing-id"):
    return {"type": "transcript", "data": {"id": external, "is_final": final, "utterance": {"text": text, "start": start, "channel": 0}}}


async def collect(provider):
    return [event async for event in provider.events()]


@pytest.mark.asyncio
async def test_dummy_elapsed_samples_independent_of_frame_length():
    results = []
    for sample_count in (320, 640, 1600):
        provider = DummySTTProvider()
        await provider.start(STTConfig("en"))
        reader = asyncio.create_task(collect(provider))
        for _ in range(48000 // sample_count):
            await provider.send_audio(bytes(sample_count * 2))
            await asyncio.sleep(0)
        await provider.finish_input()
        results.append([(e.text, e.is_final) for e in await reader])
        await provider.close()
    assert results[0] == results[1] == results[2]
    assert [item[1] for item in results[0]] == [False, False, True]


@pytest.mark.asyncio
async def test_dummy_stop_emits_final_without_another_audio_frame():
    provider = DummySTTProvider()
    await provider.start(STTConfig())
    await provider.send_audio(bytes(640))
    await provider.finish_input()
    events = await collect(provider)
    assert len(events) == 1 and events[0].is_final


@pytest.mark.asyncio
async def test_gladia_drains_final_after_end_recording_and_before_end_session():
    socket = FakeSocket()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"url": "wss://api.gladia.io/v2/live?token=secret"})

    async def connect(url, **options):
        return socket

    provider = GladiaSTTProvider("private-key", transport=httpx.MockTransport(handler), connect=connect)
    assert not requests and not provider.connected
    await provider.start(STTConfig("nb"))
    assert json.loads(requests[0].content)["language_config"]["languages"] == ["no"]
    events_task = asyncio.create_task(collect(provider))
    await provider.send_audio(bytes(640))
    socket.messages.put_nowait(transcript("Hello", False, external="partial-1"))
    await asyncio.sleep(0)
    finishing = asyncio.create_task(provider.finish_input())
    await asyncio.sleep(0)
    socket.messages.put_nowait({"type": "end_recording"})
    socket.messages.put_nowait(transcript("Hello world", True, external="final-2"))
    socket.messages.put_nowait({"type": "end_session"})
    await finishing
    events = await events_task
    assert len(events) == 2
    assert events[0].utterance_id == events[1].utterance_id
    assert events[-1].text == "Hello world" and events[-1].is_final
    assert socket.sent[0] == bytes(640)
    assert any(isinstance(value, str) and json.loads(value)["type"] == "stop_recording" for value in socket.sent)
    await provider.close()
    assert socket.closed


@pytest.mark.asyncio
async def test_gladia_restart_discards_old_queue_and_session_identity():
    sockets = [FakeSocket(), FakeSocket()]
    connections = iter(sockets)

    async def connect(*args, **kwargs):
        return next(connections)

    provider = GladiaSTTProvider("key", transport=httpx.MockTransport(lambda req: httpx.Response(201, json={"url": "wss://api.gladia.io/live"})), connect=connect)
    await provider.start(STTConfig())
    sockets[0].messages.put_nowait(transcript("stale", True))
    await asyncio.sleep(0)
    await provider.start(STTConfig("en"))
    reader = asyncio.create_task(collect(provider))
    sockets[1].messages.put_nowait(transcript("new", True))
    stopping = asyncio.create_task(provider.finish_input())
    await asyncio.sleep(0)
    sockets[1].messages.put_nowait({"type": "end_session"})
    await stopping
    assert [e.text for e in await reader] == ["new"]
    await provider.close()


@pytest.mark.asyncio
async def test_gladia_auth_failure_safe_and_not_retried():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(401, json={"message": "private-key secret response"})

    provider = GladiaSTTProvider("private-key", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        await provider.start(STTConfig())
    assert caught.value.code == "GLADIA_AUTH_FAILED" and not caught.value.retryable
    assert "private-key" not in str(caught.value) and count == 1


@pytest.mark.asyncio
async def test_gladia_final_wait_has_deadline_and_reports_missing_final():
    socket = FakeSocket()

    async def connect(*args, **kwargs):
        return socket

    provider = GladiaSTTProvider("key", transport=httpx.MockTransport(lambda req: httpx.Response(201, json={"url": "wss://api.gladia.io/live"})), connect=connect, drain_timeout=0.01)
    await provider.start(STTConfig())
    await provider.finish_input()
    with pytest.raises(ProviderError, match="待機時間"):
        await collect(provider)
    await provider.close()


@pytest.mark.asyncio
async def test_event_buffer_finals_are_bounded_with_explicit_overflow():
    buffer = EventBuffer(limit=3)
    for i in range(1000):
        buffer.put(STTEvent(str(i), False, "current"))
    assert len(buffer.items) == 1 and buffer.items[0].text == "999"
    for i in range(4):
        buffer.put(STTEvent(str(i), True, str(i)))
    assert len(buffer.items) == 3 and buffer.done
    with pytest.raises(ProviderError):
        [item async for item in buffer.events()]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(403, "DEEPL_AUTH_FAILED"), (429, "DEEPL_RATE_LIMIT"), (456, "DEEPL_QUOTA"), (503, "DEEPL_UNAVAILABLE")])
async def test_deepl_failures_never_echo_source_as_success(status, code):
    provider = DeepLTranslationProvider("private-key", transport=httpx.MockTransport(lambda req: httpx.Response(status, text="private-key details")))
    result = await provider.translate("original", "en", "ja")
    assert result.status == "error" and result.text == "" and result.error_code == code
    assert "private-key" not in repr(result)
    await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("plan,host", [("free", "api-free.deepl.com"), ("pro", "api.deepl.com")])
async def test_deepl_plans_and_no_request_for_same_or_unknown_language(plan, host):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"translations": [{"text": "こんにちは"}]})

    provider = DeepLTranslationProvider("key", plan, transport=httpx.MockTransport(handler))
    assert provider._client is None and not requests
    assert (await provider.translate("Hello", "en", "en")).status == "same_language"
    assert (await provider.translate("Hello", "bad", "ja")).status == "error"
    assert not requests
    assert (await provider.translate("Hello", "en", "ja")).text == "こんにちは"
    assert requests[0].url.host == host
    await provider.close()


@pytest.mark.asyncio
async def test_deepl_timeout_and_invalid_payload():
    def timeout(request):
        raise httpx.ReadTimeout("secret endpoint and key")

    provider = DeepLTranslationProvider("key", transport=httpx.MockTransport(timeout))
    assert (await provider.translate("hello", "en", "ja")).error_code == "DEEPL_TIMEOUT"
    await provider.close()
    provider = DeepLTranslationProvider("key", transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"translations": []})))
    assert (await provider.translate("hello", "en", "ja")).error_code == "DEEPL_INVALID_RESPONSE"
    await provider.close()


def test_norwegian_provider_code_mapping():
    assert stt_language("nb") == "no"
    with pytest.raises(ValueError):
        stt_language("unknown")
