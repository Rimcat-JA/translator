"""One active conversation, bounded caption work, and explicit audio lifecycle."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
import inspect
import time
import uuid
from typing import Any, Callable


class SessionError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class SessionContext:
    session_id: str
    session_epoch: int
    source_language: str = "zh"
    target_language: str = "ja"
    translation_enabled: bool = True
    demo: bool = False
    status: str = "idle"
    revision: int = 1
    event_seq: int = 0
    stt_epoch: int = 0
    translation_epoch: int = 0
    captions: OrderedDict[str, dict] = field(default_factory=OrderedDict)
    components: dict = field(default_factory=lambda: {
        "stt_status": "idle", "translation_status": "idle",
        "system_audio_status": "idle", "mic_status": "not_requested",
        "playback_status": "idle",
    })
    lease_id: str | None = None
    lease_owner: str | None = None
    lease_expiry: float = 0
    audio_connection: str | None = None

    def snapshot(self) -> dict:
        return {"session_id": self.session_id, "session_epoch": self.session_epoch,
                "revision": self.revision, "status": self.status, "demo": self.demo,
                "source_language": self.source_language, "target_language": self.target_language,
                "translation_enabled": self.translation_enabled,
                "components": deepcopy(self.components), "captions": deepcopy(self.captions),
                "last_event_seq": self.event_seq}


@dataclass
class TranslationJob:
    utterance_id: str
    text: str
    original_revision: int
    translation_epoch: int
    stt_epoch: int
    source: str
    target: str
    is_final: bool
    due: float


class SessionService:
    def __init__(self, provider_factory: Callable | None = None) -> None:
        self.provider_factory = provider_factory
        self.current: SessionContext | None = None
        self._epoch = 0
        self._requests: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._subscribers: set[asyncio.Queue] = set()
        self._audio_subscribers: set = set()
        self._lock = asyncio.Lock()
        self._stt = self._translator = None
        self._events_task: asyncio.Task | None = None
        self._demo_task: asyncio.Task | None = None
        self._translation_tasks: list[asyncio.Task] = []
        self._jobs: OrderedDict[str, TranslationJob] = OrderedDict()
        self._job_ready = asyncio.Event()
        self._inflight = 0
        self.audio_dropped_frames = 0

    def require(self, session_id: str) -> SessionContext:
        if self.current is None or self.current.session_id != session_id:
            raise SessionError("SESSION_NOT_FOUND", "会話が見つかりません。", 404)
        return self.current

    def _remember(self, request_id: str, action: str, session_id: str) -> None:
        existing = self._requests.get(request_id)
        if existing and existing != (action, session_id):
            raise SessionError("REQUEST_ID_REUSED", "新しい操作IDで再試行してください。")
        self._requests[request_id] = (action, session_id)
        while len(self._requests) > 128:
            self._requests.popitem(last=False)

    def create(self, source_language: str = "zh", target_language: str = "ja",
               translation_enabled: bool = True, demo: bool = False,
               request_id: str = "") -> dict:
        if request_id in self._requests:
            previous = self._requests[request_id]
            if previous[0] == "create" and self.current and previous[1] == self.current.session_id:
                return self.current.snapshot()
            raise SessionError("REQUEST_ID_REUSED", "新しい操作IDで再試行してください。")
        if self.current and self.current.status != "ended":
            raise SessionError("SESSION_EXISTS", "通訳を終了してから新しい会話を開始してください。")
        self._epoch += 1
        self.current = SessionContext(uuid.uuid4().hex, self._epoch, source_language,
                                      target_language, translation_enabled, demo)
        if not translation_enabled or source_language == target_language:
            self.current.components["translation_status"] = "disabled"
        self._remember(request_id, "create", self.current.session_id)
        self.notify()
        return self.current.snapshot()

    def subscribe(self) -> tuple[asyncio.Queue, dict]:
        # No await between registration and the snapshot boundary.
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._subscribers.add(queue)
        return queue, self.snapshot_event()

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def snapshot_event(self) -> dict:
        return {"protocol_version": 1, "type": "session.snapshot",
                "data": self.current.snapshot() if self.current else None}

    def _broadcast(self, event: dict) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                # A slow display resynchronizes from a complete snapshot.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot_event())
            else:
                queue.put_nowait(deepcopy(event))

    def notify(self) -> None:
        self._broadcast(self.snapshot_event())

    def _caption_event(self, caption: dict) -> None:
        s = self.current
        if not s:
            return
        s.event_seq += 1
        self._broadcast({"protocol_version": 1, "type": "caption.upsert",
                         "session_id": s.session_id, "session_epoch": s.session_epoch,
                         "event_seq": s.event_seq, "data": caption})

    async def start(self, session_id: str, request_id: str) -> dict:
        async with self._lock:
            s = self.require(session_id)
            self._remember(request_id, "start", session_id)
            if s.status in {"waiting_for_peer", "running", "degraded"}:
                return s.snapshot()
            if s.status != "idle":
                raise SessionError("INVALID_STATE", "この会話は開始できません。")
            s.status = "waiting_for_peer"
            s.revision += 1
            if s.demo:
                try:
                    await self._prepare()
                except BaseException:
                    s.status = "idle"
                    self.notify()
                    raise
                s.status = "running"
                s.components["mic_status"] = "not_requested"
                self._demo_task = asyncio.create_task(self._demo(), name="translator-demo")
            self.notify()
            return s.snapshot()

    async def _prepare(self) -> None:
        if self._stt is not None:
            return
        from translator.providers import STTConfig, DummySTTProvider, DummyTranslationProvider
        s = self.current
        assert s is not None
        s.components["stt_status"] = "connecting"
        self.notify()
        try:
            if self.provider_factory:
                pair = self.provider_factory(s.demo)
                if inspect.isawaitable(pair):
                    pair = await pair
                self._stt, self._translator = pair
            elif s.demo:
                self._stt, self._translator = DummySTTProvider(), DummyTranslationProvider()
            else:
                raise SessionError("PROVIDERS_NOT_CONFIGURED", "設定画面でAPIキーを設定してください。", 503)
            await self._stt.start(STTConfig(language=s.source_language))
        except BaseException as exc:
            await self._close_providers()
            s.components["stt_status"] = "error"
            self.notify()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SessionError("STT_START_FAILED", "音声認識に接続できません。設定と回線を確認してください。", 503)
        s.stt_epoch += 1
        s.components["stt_status"] = "ready"
        self._events_task = asyncio.create_task(self._consume_events(s.stt_epoch), name="translator-stt-events")
        self._translation_tasks = [asyncio.create_task(self._translation_worker(),
                                                       name=f"translator-translation-{i}") for i in range(2)]

    async def _consume_events(self, epoch: int) -> None:
        try:
            async for event in self._stt.events():
                self.ingest(event, epoch)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.current:
                self.current.components["stt_status"] = "error"
                self.current.status = "degraded"
                self.notify()

    def ingest(self, event: Any, epoch: int | None = None) -> None:
        s = self.current
        if not s or s.status in {"idle", "ended"} or (epoch is not None and epoch != s.stt_epoch):
            return
        get = event.get if isinstance(event, dict) else lambda key, default=None: getattr(event, key, default)
        text = str(get("text", ""))[:12000].strip()
        if not text:
            return
        external_id = str(get("utterance_id", "") or "current")[:128]
        utterance_id = f"{s.stt_epoch}:{external_id}"
        existing = s.captions.get(utterance_id)
        is_final = bool(get("is_final", False))
        if existing and existing["original"]["is_final"]:
            if not is_final or existing["original"]["text"] == text:
                return
        revision = existing["original"]["revision"] + 1 if existing else 1
        status = "disabled" if not s.translation_enabled else "same_language" if s.source_language == s.target_language else "pending"
        caption = {"utterance_id": utterance_id, "stt_epoch": s.stt_epoch,
                   "source_language": s.source_language, "target_language": s.target_language,
                   "original": {"text": text, "revision": revision, "is_final": is_final},
                   "translation": {"text": "", "status": status,
                       "revision": (existing["translation"]["revision"] + 1) if existing else 1,
                       "based_on_original_revision": revision, "translation_epoch": s.translation_epoch,
                       "is_final": False, "error_code": None}}
        s.captions[utterance_id] = caption
        while len(s.captions) > 100:
            evicted, _ = s.captions.popitem(last=False)
            self._jobs.pop(evicted, None)
        self._caption_event(caption)
        if status == "pending":
            if len(self._jobs) >= 4 and utterance_id not in self._jobs:
                interim = next((k for k, job in self._jobs.items() if not job.is_final), None)
                if interim is not None:
                    dropped = self._jobs.pop(interim)
                    self._translation_error(dropped, "TRANSLATION_BUSY")
                else:
                    caption["translation"].update(status="error", error_code="TRANSLATION_BUSY")
                    self._caption_event(caption)
                    return
            self._jobs[utterance_id] = TranslationJob(utterance_id, text, revision,
                s.translation_epoch, s.stt_epoch, s.source_language, s.target_language, is_final,
                time.monotonic() + (0 if is_final else .3))
            self._job_ready.set()

    def _valid_job(self, job: TranslationJob) -> dict | None:
        s = self.current
        if not s or s.status == "ended" or s.translation_epoch != job.translation_epoch or not s.translation_enabled:
            return None
        caption = s.captions.get(job.utterance_id)
        if not caption or caption["original"]["revision"] != job.original_revision:
            return None
        if caption["source_language"] != job.source or caption["target_language"] != job.target:
            return None
        return caption

    def _translation_error(self, job: TranslationJob, code: str) -> None:
        caption = self._valid_job(job)
        if caption:
            caption["translation"].update(status="error", error_code=code, text="")
            caption["translation"]["revision"] += 1
            self._caption_event(caption)
            if self.current:
                self.current.components["translation_status"] = "error"
                if self.current.status != "stopping":
                    self.current.status = "degraded"
                self.notify()

    async def _translation_worker(self) -> None:
        while True:
            await self._job_ready.wait()
            ready = sorted((job for job in self._jobs.values() if job.due <= time.monotonic()),
                           key=lambda job: (not job.is_final, job.due))
            if not ready:
                if not self._jobs:
                    self._job_ready.clear()
                else:
                    await asyncio.sleep(min(.05, max(0, min(j.due for j in self._jobs.values()) - time.monotonic())))
                continue
            job = ready[0]
            self._jobs.pop(job.utterance_id, None)
            if not self._jobs:
                self._job_ready.clear()
            self._inflight += 1
            try:
                result = await asyncio.wait_for(self._translator.translate(job.text, job.source, job.target), 8)
                caption = self._valid_job(job)
                if caption:
                    get = result.get if isinstance(result, dict) else lambda key, default=None: getattr(result, key, default)
                    status = "ready" if isinstance(result, str) else get("status", "error")
                    text = result if isinstance(result, str) else get("text", "")
                    caption["translation"].update(text=text if status == "ready" else "", status=status,
                         is_final=job.is_final and status == "ready", error_code=get("error_code"))
                    caption["translation"]["revision"] += 1
                    self._caption_event(caption)
                    if self.current:
                        self.current.components["translation_status"] = "ready" if status == "ready" else "error"
                        if status == "error":
                            self.current.status = "degraded"
                        self.notify()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._translation_error(job, getattr(error, "code", "TRANSLATION_FAILED"))
            finally:
                self._inflight -= 1

    async def _demo(self) -> None:
        try:
            while True:
                await self._stt.send_audio(bytes(640))
                await asyncio.sleep(.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.current:
                self.current.status = "degraded"
                self.current.components["stt_status"] = "error"
                self.notify()

    def acquire_lease(self, session_id: str, owner: str, lease_id: str | None = None,
                      release: bool = False) -> dict:
        s = self.require(session_id)
        if release:
            if s.lease_owner == owner and (lease_id is None or lease_id == s.lease_id):
                s.lease_id = s.lease_owner = None
                s.lease_expiry = 0
            return {"lease_id": None, "expires_in": 0}
        if s.demo or s.status not in {"waiting_for_peer", "running", "degraded"}:
            raise SessionError("INVALID_STATE", "マイクを開始できる会話ではありません。")
        if s.lease_id and s.lease_expiry > time.monotonic():
            if s.lease_owner != owner or s.lease_id != lease_id:
                raise SessionError("PUBLISHER_BUSY", "他の画面でマイクを使用しています。")
        else:
            s.lease_id, s.lease_owner = uuid.uuid4().hex, owner
        s.lease_expiry = time.monotonic() + 15
        return {"lease_id": s.lease_id, "expires_in": 15}

    def valid_lease(self, owner: str, lease_id: str) -> bool:
        s = self.current
        return bool(s and s.lease_owner == owner and s.lease_id == lease_id and s.lease_expiry > time.monotonic())

    async def start_input(self, session_id: str, owner: str, lease_id: str, connection: str) -> None:
        async with self._lock:
            s = self.require(session_id)
            if not self.valid_lease(owner, lease_id):
                raise SessionError("LEASE_EXPIRED", "マイクの送信権を取り直してください。", 403)
            if s.audio_connection:
                raise SessionError("PUBLISHER_BUSY", "他の画面でマイクを使用しています。")
            await self._prepare()
            s.audio_connection = connection
            s.components["mic_status"], s.components["stt_status"] = "streaming", "active"
            s.status = "running"
            s.revision += 1
            self.notify()

    async def send_audio(self, pcm: bytes, owner: str, lease_id: str, connection: str) -> None:
        s = self.current
        if not self.valid_lease(owner, lease_id) or not s or s.audio_connection != connection:
            raise SessionError("LEASE_EXPIRED", "マイク送信権の有効期限が切れました。", 403)
        s.lease_expiry = time.monotonic() + 15
        try:
            await asyncio.wait_for(self._stt.send_audio(pcm), .2)
        except asyncio.TimeoutError:
            raise SessionError("STT_BACKPRESSURE", "音声認識への送信が混雑しています。再接続してください。", 503)

    async def finish_input(self, connection: str) -> None:
        async with self._lock:
            s = self.current
            if not s or s.audio_connection != connection:
                return
            s.audio_connection = None
            s.lease_id = s.lease_owner = None
            s.lease_expiry = 0
            s.components["mic_status"] = "not_requested"
            await self._drain()
            if s.status not in {"stopping", "ended"}:
                s.status = "waiting_for_peer"
            self.notify()

    async def _drain(self) -> None:
        if self._stt:
            deadline = time.monotonic() + 3.5
            with suppress(Exception):
                await asyncio.wait_for(self._stt.finish_input(), max(.01, deadline - time.monotonic()))
            if self._events_task:
                with suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(self._events_task), max(.01, deadline - time.monotonic()))
            deadline = time.monotonic() + 1
            while (self._jobs or self._inflight) and time.monotonic() < deadline:
                await asyncio.sleep(.02)
            await self._close_providers()

    async def _close_providers(self) -> None:
        tasks = ([self._events_task] if self._events_task else []) + self._translation_tasks
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._events_task = None
        self._translation_tasks = []
        self._jobs.clear()
        self._job_ready.clear()
        for provider in (self._stt, self._translator):
            if provider:
                with suppress(Exception):
                    await asyncio.wait_for(provider.close(), 2)
        self._stt = self._translator = None
        if self.current:
            self.current.components["stt_status"] = "idle"

    async def stop(self, session_id: str, request_id: str) -> dict:
        async with self._lock:
            s = self.require(session_id)
            self._remember(request_id, "stop", session_id)
            if s.status == "ended":
                return s.snapshot()
            s.status = "stopping"
            self.notify()
            if self._demo_task:
                self._demo_task.cancel()
                await asyncio.gather(self._demo_task, return_exceptions=True)
                self._demo_task = None
            s.audio_connection = None
            s.lease_id = s.lease_owner = None
            await self._drain()
            s.translation_epoch += 1
            s.captions.clear()
            s.components.update(stt_status="idle", translation_status="idle", mic_status="not_requested",
                                system_audio_status="idle")
            s.status = "ended"
            s.revision += 1
            self.notify()
            for queue in tuple(self._audio_subscribers):
                queue.close()
            return s.snapshot()

    async def configure(self, session_id: str, field_name: str, value: Any,
                        expected_revision: int | None = None) -> dict:
        async with self._lock:
            s = self.require(session_id)
            if expected_revision is not None and s.revision != expected_revision:
                raise SessionError("REVISION_CONFLICT", "別の画面で設定が変わりました。")
            if s.status in {"stopping", "ended"}:
                raise SessionError("INVALID_STATE", "終了した会話は変更できません。")
            if field_name == "source_language" and (s.audio_connection or s.demo):
                raise SessionError("STOP_MIC_FIRST", "言語を変える前にマイクを停止してください。")
            if field_name not in {"source_language", "target_language", "translation_enabled"}:
                raise SessionError("UNKNOWN_COMMAND", "この操作は対応していません。", 422)
            setattr(s, field_name, value)
            s.translation_epoch += 1
            self._jobs.clear()
            if not s.translation_enabled or s.source_language == s.target_language:
                s.components["translation_status"] = "disabled"
            else:
                s.components["translation_status"] = "idle"
            for caption in s.captions.values():
                if caption["translation"]["status"] == "pending":
                    caption["translation"].update(status="disabled" if not s.translation_enabled else "error",
                                                  error_code=None if not s.translation_enabled else "SETTINGS_CHANGED")
                    caption["translation"]["revision"] += 1
            s.revision += 1
            self.notify()
            return s.snapshot()

    def subscribe_audio(self):
        from translator.audio.protocol import BoundedAudioQueue
        queue = BoundedAudioQueue(max_ms=100)
        self._audio_subscribers.add(queue)
        if self.current and self.current.components["playback_status"] == "idle":
            self.current.components["playback_status"] = "ready"
            self.notify()
        return queue

    def unsubscribe_audio(self, queue) -> None:
        self._audio_subscribers.discard(queue)
        queue.close()
        if self.current and not self._audio_subscribers:
            self.current.components["playback_status"] = "idle"
            self.notify()

    def publish_system_audio(self, frame: bytes) -> None:
        s = self.current
        if not s or s.status in {"idle", "stopping", "ended"}:
            return
        from translator.audio.protocol import AudioFrame
        parsed = AudioFrame.parse(frame)
        if self._audio_subscribers and s.components["playback_status"] != "active":
            s.components["playback_status"] = "active"
            self.notify()
        for queue in tuple(self._audio_subscribers):
            dropped_before = queue.dropped_samples
            queue.put(parsed)
            if queue.dropped_samples > dropped_before:
                self.audio_dropped_frames += 1

    async def close(self) -> None:
        if self.current and self.current.status != "ended":
            await self.stop(self.current.session_id, uuid.uuid4().hex)
