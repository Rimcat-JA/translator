"""Explicitly started, bounded, stoppable native audio subprocess."""
from __future__ import annotations

import asyncio
import multiprocessing
import os
from queue import Empty, Full
import sys
import threading
import time

from .loopback import float_to_pcm
from .protocol import AudioFrame
from ..providers.base import ProviderError


def _watch_parent(stop) -> None:
    # A blocked native driver cannot prevent exit after the runtime has died.
    parent = multiprocessing.parent_process()
    while True:
        time.sleep(0.5)
        if parent is not None and not parent.is_alive():
            os._exit(0)


def _capture(stop, output, status, device_id: str | None, frame_ms: int) -> None:
    threading.Thread(target=_watch_parent, args=(stop,), daemon=True).start()
    try:
        import soundcard as sc

        speakers = sc.all_speakers()
        speaker = next((s for s in speakers if s.id == device_id), None) if device_id else sc.default_speaker()
        if speaker is None:
            status.put(("error", "AUDIO_DEVICE_MISSING"))
            return
        loopback = sc.get_microphone(speaker.id, include_loopback=True)
        count = 16 * frame_ms
        # SoundCard/WASAPI negotiates the requested rate through the OS mixer.
        # Multichannel acquisition avoids the documented Windows single-channel
        # bug; include all surround channels so center-channel speech is retained.
        # The returned samples are downmixed explicitly before PCM conversion.
        channels = max(2, int(speaker.channels))
        with loopback.recorder(samplerate=16000, channels=channels, blocksize=count) as recorder:
            sequence, offset = 0, 0
            discontinuity = True
            status.put(("ready", None))
            while not stop.is_set():
                data = recorder.record(numframes=count)
                if len(data) != count:
                    raise ValueError("Unexpected sample count")
                frame = AudioFrame(sequence, offset, float_to_pcm(data), int(discontinuity))
                try:
                    output.put_nowait(frame)
                    discontinuity = False
                except Full:
                    # The queue is fixed to 60 ms. Never let a feeder or callback
                    # accumulate an unbounded copy of captured system audio.
                    try:
                        output.get_nowait()
                    except Empty:
                        pass
                    try:
                        output.put_nowait(AudioFrame(sequence, offset, frame.pcm, 1))
                    except Full:
                        pass
                    discontinuity = True
                sequence += 1
                offset += count
                if sequence >= 0xFFFFFFFE:
                    raise ValueError("Stream lifetime exceeded")
    except Exception:
        try:
            status.put_nowait(("error", "AUDIO_CAPTURE_FAILED"))
        except Full:
            pass
    finally:
        # Avoid an exit hang when the runtime stopped reading captured frames.
        output.cancel_join_thread()


class NativeAudioWorker:
    def __init__(self, device_id: str | None = None, frame_ms: int = 20):
        if frame_ms not in (20, 40, 100):
            raise ValueError("Audio profile must be 20, 40 or 100 ms")
        self.device_id = device_id
        self.frame_ms = frame_ms
        self._process = None
        self._stop = None
        self._output = None
        self._status = None
        self._stop_task: asyncio.Task | None = None
        self.active = False

    async def start(self) -> None:
        if self.active:
            return
        if sys.platform != "win32":
            raise ProviderError("SYSTEM_AUDIO_WINDOWS_ONLY", "PC音声共有はWindows版で利用できます。")
        await self.stop()
        context = multiprocessing.get_context("spawn")
        self._stop = context.Event()
        self._output = context.Queue(maxsize=max(1, 60 // self.frame_ms))
        self._status = context.Queue(maxsize=4)
        self._process = context.Process(target=_capture, args=(self._stop, self._output, self._status, self.device_id, self.frame_ms), name="translator-loopback")
        process, stop_requested = self._process, self._stop
        try:
            self._process.start()
            kind, code = await asyncio.to_thread(self._status.get, True, 10.0)
            if kind != "ready":
                raise ProviderError(code, "PC音声を取得できません。出力デバイスを確認してください。")
            if self._process is not process or stop_requested.is_set():
                raise ProviderError("AUDIO_CAPTURE_START_CANCELLED", "PC音声の開始は停止要求により取り消されました。")
            self.active = True
        except ProviderError:
            await self.stop()
            raise
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception:
            await self.stop()
            raise ProviderError("AUDIO_CAPTURE_START_FAILED", "PC音声の開始に失敗しました。出力デバイスを確認してください。") from None

    async def frames(self):
        while self.active:
            try:
                kind, code = self._status.get_nowait()
                if kind == "error":
                    raise ProviderError(code, "PC音声の取得が停止しました。出力デバイスを確認してください。")
            except Empty:
                pass
            try:
                frame = await asyncio.to_thread(self._output.get, True, 0.25)
            except Empty:
                if self.active and not self._process.is_alive():
                    raise ProviderError("AUDIO_WORKER_EXITED", "PC音声の取得プロセスが終了しました。")
                continue
            except (OSError, ValueError):
                if not self.active:
                    return
                raise ProviderError("AUDIO_WORKER_EXITED", "PC音声の取得プロセスが終了しました。") from None
            if self.active:
                yield frame

    async def stop(self) -> None:
        """All callers share bounded cleanup, even when one caller is cancelled.

        Runtime can cancel its relay while that relay is already stopping a
        failed driver. Keep the child handle until cleanup has actually finished,
        and do not let cancelling the relay cancel the process cleanup itself.
        """
        self.active = False
        cleanup = self._stop_task
        if cleanup is None or cleanup.done():
            if self._process is None and self._output is None and self._status is None:
                return
            cleanup = asyncio.create_task(self._stop_process(), name="translator-audio-stop")
            self._stop_task = cleanup
        cancelled = False
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Propagate cancellation after the owned process has had its
                    # bounded join/terminate/kill attempts. Repeated cancellation
                    # also leaves the same cleanup task running.
                    cancelled = True
                except Exception:
                    break  # Retrieve and propagate the shared cleanup failure below.
            if cancelled:
                if not cleanup.cancelled():
                    cleanup.exception()
                raise asyncio.CancelledError
            cleanup.result()
        finally:
            if cleanup.done() and self._stop_task is cleanup:
                self._stop_task = None

    async def _stop_process(self) -> None:
        process = self._process
        try:
            if process is not None:
                if self._stop is not None:
                    self._stop.set()
                if process.pid:
                    await asyncio.to_thread(process.join, 1.0)
                    if process.is_alive():
                        process.terminate()
                        await asyncio.to_thread(process.join, 1.0)
                    if process.is_alive():
                        process.kill()
                        await asyncio.to_thread(process.join, 1.0)
                    if process.is_alive():
                        raise ProviderError("AUDIO_WORKER_STOP_FAILED", "PC音声の取得プロセスの終了を確認できませんでした。")
                process.close()
                # Retain the live handle if any stop stage fails, so a later
                # explicit stop can retry instead of silently losing the child.
                self._process = None
            for channel in (self._output, self._status):
                if channel is not None:
                    channel.close()
                    channel.cancel_join_thread()
            self._output = self._status = self._stop = None
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("AUDIO_WORKER_STOP_FAILED", "PC音声の取得プロセスの終了を確認できませんでした。") from None
