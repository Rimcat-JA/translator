import asyncio
import time

import pytest

from translator.audio.protocol import AudioFrame
from translator.providers import STTEvent, TranslationResult
from translator.sessions import SessionError, SessionService


class QueueSTT:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.started = self.closed = 0
        self.final_on_finish = None

    async def start(self, config):
        self.started += 1

    async def send_audio(self, pcm):
        pass

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event

    async def finish_input(self):
        if self.final_on_finish:
            self.queue.put_nowait(self.final_on_finish)
        self.queue.put_nowait(None)

    async def close(self):
        self.closed += 1


class ControlledTranslation:
    def __init__(self, immediate=False):
        self.calls = []
        self.immediate = immediate

    async def translate(self, text, source, target):
        future = asyncio.get_running_loop().create_future()
        self.calls.append((text, future))
        if self.immediate:
            future.set_result(TranslationResult("translated " + text))
        return await future

    async def close(self):
        pass


async def ready_service(immediate=False):
    stt, translator = QueueSTT(), ControlledTranslation(immediate)
    service = SessionService(lambda demo: (stt, translator))
    s = service.create(request_id="create")
    await service.start(s["session_id"], "start")
    await service._prepare()
    return service, stt, translator


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


async def test_start_is_explicit_and_idempotent():
    stt, translator = QueueSTT(), ControlledTranslation(True)
    service = SessionService(lambda demo: (stt, translator))
    assert stt.started == 0
    snapshot = service.create(demo=True, request_id="create")
    assert snapshot["status"] == "idle"
    assert stt.started == 0
    assert service.create(demo=True, request_id="create") == snapshot
    with pytest.raises(SessionError, match="新しい会話"):
        service.create(request_id="second")
    await service.start(snapshot["session_id"], "start")
    await service.start(snapshot["session_id"], "start")
    assert stt.started == 1
    await service.close()
    assert service.current.status == "ended"
    assert stt.closed == 1
    assert not service._translation_tasks and service._demo_task is None


async def test_reversed_translation_completion_cannot_regress_final_or_other_utterance():
    service, _, translator = await ready_service()
    service.ingest(STTEvent("interim", False, "one"))
    await wait_until(lambda: len(translator.calls) == 1)
    service.ingest(STTEvent("final", True, "one"))
    await wait_until(lambda: len(translator.calls) == 2)
    translator.calls[1][1].set_result(TranslationResult("final translation"))
    await wait_until(lambda: service.current.captions["1:one"]["translation"]["is_final"])
    service.ingest(STTEvent("next", True, "two"))
    translator.calls[0][1].set_result(TranslationResult("stale interim"))
    await wait_until(lambda: len(translator.calls) == 3)
    translator.calls[2][1].set_result(TranslationResult("next translation"))
    service.ingest(STTEvent("late interim", False, "one"))
    await wait_until(lambda: service.current.captions["1:two"]["translation"]["is_final"])
    assert service.current.captions["1:one"]["translation"]["text"] == "final translation"
    assert service.current.captions["1:one"]["original"]["text"] == "final"
    assert service.current.captions["1:two"]["translation"]["text"] == "next translation"
    await service.close()


async def test_translation_off_and_old_stt_epoch_discard_late_work():
    service, _, translator = await ready_service()
    service.ingest(STTEvent("hello", True, "one"))
    await wait_until(lambda: len(translator.calls) == 1)
    await service.configure(service.current.session_id, "translation_enabled", False)
    translator.calls[0][1].set_result(TranslationResult("must disappear"))
    service.ingest(STTEvent("old stream", True, "late"), epoch=0)
    await asyncio.sleep(.02)
    assert list(service.current.captions) == ["1:one"]
    assert service.current.captions["1:one"]["translation"]["status"] == "disabled"
    assert service.current.captions["1:one"]["translation"]["text"] == ""
    await service.close()


async def test_final_after_input_stops_is_consumed_and_translated():
    service, stt, _ = await ready_service(True)
    s = service.current
    lease = service.acquire_lease(s.session_id, "speaker")
    await service.start_input(s.session_id, "speaker", lease["lease_id"], "connection")
    stt.final_on_finish = STTEvent("final after silence", True, "last")
    await service.finish_input("connection")
    caption = s.captions["1:last"]
    assert caption["original"]["is_final"]
    assert caption["translation"]["text"] == "translated final after silence"
    assert service._stt is None and stt.closed == 1
    assert s.components["mic_status"] == "not_requested"
    await service.close()
    assert not s.captions


async def test_publisher_lease_blocks_second_tab_and_expired_stream():
    service, _, _ = await ready_service(True)
    s = service.current
    lease = service.acquire_lease(s.session_id, "speaker")
    with pytest.raises(SessionError) as exc:
        service.acquire_lease(s.session_id, "speaker")
    assert exc.value.code == "PUBLISHER_BUSY"
    with pytest.raises(SessionError):
        service.acquire_lease(s.session_id, "other", lease["lease_id"])
    assert service.acquire_lease(s.session_id, "speaker", lease["lease_id"]) == lease
    await service.start_input(s.session_id, "speaker", lease["lease_id"], "socket")
    with pytest.raises(SessionError):
        await service.start_input(s.session_id, "speaker", lease["lease_id"], "second")
    s.lease_expiry = time.monotonic() - 1
    with pytest.raises(SessionError) as exc:
        await service.send_audio(bytes(640), "speaker", lease["lease_id"], "socket")
    assert exc.value.code == "LEASE_EXPIRED"
    await service.close()


async def test_language_revision_conflicts_leave_configuration_unchanged():
    service, _, _ = await ready_service(True)
    s = service.current
    lease = service.acquire_lease(s.session_id, "speaker")
    await service.start_input(s.session_id, "speaker", lease["lease_id"], "socket")
    revision = s.revision
    with pytest.raises(SessionError):
        await service.configure(s.session_id, "source_language", "en", revision)
    assert s.source_language == "zh" and s.revision == revision
    with pytest.raises(SessionError):
        await service.configure(s.session_id, "target_language", "en", revision - 1)
    assert s.target_language == "ja"
    await service.finish_input("socket")
    await service.configure(s.session_id, "source_language", "en", revision)
    assert s.source_language == "en" and s.revision == revision + 1
    await service.close()


async def test_caption_and_work_queues_remain_bounded_under_burst():
    service, _, translator = await ready_service()
    service.ingest(STTEvent("first", True, "one"))
    service.ingest(STTEvent("second", True, "two"))
    await wait_until(lambda: len(translator.calls) == 2)
    for i in range(200):
        service.ingest(STTEvent(str(i), True, str(i)))
    assert len(translator.calls) == 2
    assert len(service._jobs) <= 4
    assert len(service.current.captions) == 100
    assert any(c["translation"]["error_code"] == "TRANSLATION_BUSY" for c in service.current.captions.values())
    await service.close()
    assert service._inflight == 0 and not service._jobs


async def test_snapshot_boundary_and_slow_subscriber_recovery():
    service, _, _ = await ready_service(True)
    queue, initial = service.subscribe()
    assert initial["data"]["last_event_seq"] == 0
    for i in range(150):
        service.ingest(STTEvent(str(i), True, str(i)))
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(event["type"] == "session.snapshot" for event in events)
    assert len(events) <= 128
    service.unsubscribe(queue)
    await service.close()


async def test_audio_relay_is_bounded_in_milliseconds_for_all_frame_sizes():
    service = SessionService()
    s = service.create(request_id="create")
    await service.start(s["session_id"], "start")
    for samples in (320, 640, 1600):
        queue = service.subscribe_audio()
        for i in range(50):
            service.publish_system_audio(AudioFrame(i, samples * i, bytes(samples * 2)).encode())
        assert queue.buffered_ms <= 100
        assert (await queue.get()).flags & 1
        service.unsubscribe_audio(queue)
    await service.close()


async def test_cancelled_provider_start_is_closed_and_can_retry():
    class SlowSTT(QueueSTT):
        async def start(self, config):
            self.started += 1
            await asyncio.Event().wait()

    first, second, translator = SlowSTT(), QueueSTT(), ControlledTranslation(True)
    providers = iter([first, second])
    service = SessionService(lambda demo: (next(providers), translator))
    s = service.create(demo=True, request_id="create")
    task = asyncio.create_task(service.start(s["session_id"], "start"))
    await wait_until(lambda: first.started == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.closed == 1 and service._stt is None
    await service.start(s["session_id"], "retry")
    assert second.started == 1
    await service.close()
