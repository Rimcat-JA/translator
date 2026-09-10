import asyncio
import multiprocessing
import struct
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from translator.audio import AudioFrame, AudioProtocolError, AudioStreamValidator, BoundedAudioQueue
from translator.audio.loopback import float_to_pcm, list_devices
from translator.audio.worker import NativeAudioWorker
from translator.providers import ProviderError
from translator.remote.ngrok import TunnelManager


def synthetic_capture(stop, output, status, device_id, frame_ms):
    """Spawnable fake driver: exercises IPC and process lifecycle without a mic."""
    status.put(("ready", None))
    output.put(AudioFrame(0, 0, bytes(frame_ms * 32), 1))
    stop.wait(30)
    output.cancel_join_thread()


def stuck_capture(stop, output, status, device_id, frame_ms):
    status.put(("ready", None))
    # Deliberately ignore the stop request, as a hung native driver can do.
    import time
    time.sleep(30)


def test_wire_header_little_endian_and_roundtrip():
    frame = AudioFrame(0x12345678, 0x123456789ABC, struct.pack("<hh", -32768, 32767), 1)
    wire = frame.encode()
    assert len(wire) == 28 and wire[:8] == b"TRN1\x01\x01\x01\x00"
    assert wire[8:12] == b"\x78\x56\x34\x12"
    assert AudioFrame.parse(wire) == frame


@pytest.mark.parametrize("offset,value", [(0, 0), (4, 2), (5, 0), (6, 2), (20, 0)])
def test_invalid_wire_protocol_rejected(offset, value):
    wire = bytearray(AudioFrame(0, 0, bytes(640)).encode())
    wire[offset] = value
    with pytest.raises(AudioProtocolError):
        AudioFrame.parse(bytes(wire))


def test_invalid_wire_sizes_and_raw_pcm_rejected():
    wire = AudioFrame(0, 0, bytes(640)).encode()
    for invalid in [wire[:-1], wire + b"\x00", bytes(640), bytes(5000), b""]:
        with pytest.raises(AudioProtocolError):
            AudioFrame.parse(invalid)
    for count in (0, 1601):
        with pytest.raises(AudioProtocolError):
            AudioFrame(0, 0, bytes(count * 2)).encode()


def test_stream_repeats_gaps_and_rate_limits():
    validator = AudioStreamValidator(limit_rate=False)
    assert not validator.accept(AudioFrame(0, 0, bytes(640)))
    assert validator.accept(AudioFrame(2, 640, bytes(640)))
    with pytest.raises(AudioProtocolError):
        validator.accept(AudioFrame(2, 640, bytes(640)))
    with pytest.raises(AudioProtocolError):
        validator.accept(AudioFrame(3, 0, bytes(640)))
    limited = AudioStreamValidator(clock=lambda: 0)
    for i in range(10):
        limited.accept(AudioFrame(i, i * 320, bytes(640)))
    with pytest.raises(AudioProtocolError, match="faster"):
        limited.accept(AudioFrame(10, 3200, bytes(640)))


@pytest.mark.asyncio
async def test_queue_is_bounded_by_duration_and_marks_first_survivor():
    queue = BoundedAudioQueue(max_ms=100)
    # Accelerated 60 minutes at 50 frames/s, without hardware or a wall-clock wait.
    for i in range(180000):
        queue.put(AudioFrame(i, i * 320, bytes(640)))
        assert queue.buffered_ms <= 100
    frame = await queue.get()
    assert frame.sequence == 179995 and frame.flags == 1
    assert queue.dropped_samples == 179995 * 320
    queue.close()
    with pytest.raises(EOFError):
        await queue.get()


@pytest.mark.asyncio
async def test_queue_mixed_frame_lengths_and_close_wakes_reader():
    queue = BoundedAudioQueue(max_ms=100)
    queue.put(AudioFrame(0, 0, bytes(1280)))  # 40 ms
    queue.put(AudioFrame(1, 640, bytes(3200)))  # 100 ms drops previous
    assert queue.buffered_ms == 100
    assert (await queue.get()).flags == 1
    reader = asyncio.create_task(queue.get())
    await asyncio.sleep(0)
    queue.close()
    with pytest.raises(EOFError):
        await reader


def test_pcm_clipping_silence_channel_mix_and_endianness():
    signal = np.array([-2, -1, -0.5, 0, 0.5, 1, 2, np.nan, np.inf])
    samples = np.frombuffer(float_to_pcm(signal), dtype="<i2")
    assert samples.tolist() == [-32768, -32768, -16384, 0, 16384, 32767, 32767, 0, 32767]
    stereo = np.array([[1.0, -1.0], [0.5, 0.5]])
    assert np.frombuffer(float_to_pcm(stereo), dtype="<i2").tolist() == [0, 16384]


@pytest.mark.asyncio
async def test_non_windows_audio_has_actionable_status_without_soundcard_import(monkeypatch):
    monkeypatch.setattr("translator.audio.loopback.sys.platform", "linux")
    result = await list_devices()
    assert not result["supported"] and result["devices"] == []
    worker = NativeAudioWorker()
    with pytest.raises(ProviderError) as caught:
        await worker.start()
    assert caught.value.code == "SYSTEM_AUDIO_WINDOWS_ONLY"
    assert not worker.active
    await worker.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows subprocess lifecycle")
@pytest.mark.asyncio
async def test_worker_real_subprocess_start_frames_and_stop_without_device(monkeypatch):
    monkeypatch.setattr("translator.audio.worker._capture", synthetic_capture)
    worker = NativeAudioWorker()
    await worker.start()
    pid = worker._process.pid
    reader = worker.frames()
    frame = await anext(reader)
    assert frame.sample_count == 320 and worker.active
    await reader.aclose()
    await worker.stop()
    assert not worker.active and pid not in {p.pid for p in multiprocessing.active_children()}
    await worker.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows subprocess lifecycle")
@pytest.mark.asyncio
async def test_worker_forces_hung_driver_to_exit(monkeypatch):
    monkeypatch.setattr("translator.audio.worker._capture", stuck_capture)
    worker = NativeAudioWorker()
    await worker.start()
    pid = worker._process.pid
    await asyncio.wait_for(worker.stop(), 4)
    assert pid not in {p.pid for p in multiprocessing.active_children()}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows subprocess lifecycle")
@pytest.mark.asyncio
async def test_cancelled_stop_keeps_hung_driver_handle_and_shares_cleanup(monkeypatch):
    monkeypatch.setattr("translator.audio.worker._capture", stuck_capture)
    worker = NativeAudioWorker()
    await worker.start()
    process = worker._process
    pid = process.pid
    joined = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_join = process.join
    callers = []

    def observed_join(timeout=None):
        loop.call_soon_threadsafe(joined.set)
        return original_join(timeout)

    monkeypatch.setattr(process, "join", observed_join)
    try:
        first = asyncio.create_task(worker.stop())
        callers.append(first)
        await asyncio.wait_for(joined.wait(), 1)
        # Keep ownership until the process has actually exited, not merely
        # until another caller has requested stop.
        assert worker._process is process
        first.cancel()
        second = asyncio.create_task(worker.stop())
        callers.append(second)
        outcomes = await asyncio.wait_for(asyncio.gather(*callers, return_exceptions=True), 4)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert outcomes[1] is None
        assert worker._process is None and worker._output is None and worker._status is None
        assert pid not in {p.pid for p in multiprocessing.active_children()}
        await worker.stop()
    finally:
        for caller in callers:
            if not caller.done():
                caller.cancel()
        if callers:
            await asyncio.gather(*callers, return_exceptions=True)
        # Only the synthetic process created by this test can need emergency
        # cleanup if an assertion fails while exercising the old regression.
        try:
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(original_join, 2)
            process.close()
        except ValueError:
            pass  # Already closed by the successful worker cleanup.


@pytest.mark.asyncio
async def test_tunnel_explicit_only_and_targets_hub(monkeypatch):
    calls = []
    closed = []

    class Listener:
        def url(self):
            return "https://example.ngrok.app"

        async def close(self):
            closed.append(True)

    async def forward(target, **kwargs):
        calls.append((target, kwargs))
        return Listener()

    monkeypatch.setattr("translator.remote.ngrok.importlib.import_module", lambda name: SimpleNamespace(forward=forward))
    tunnel = TunnelManager(hub_port=8001, local_port=8766)
    assert not calls and tunnel.status == "idle"
    assert await tunnel.start("private-key", "example.ngrok.app") == "https://example.ngrok.app"
    assert calls == [("http://127.0.0.1:8001", {"authtoken": "private-key", "domain": "example.ngrok.app"})]
    await tunnel.start("private-key")
    assert len(calls) == 1
    await tunnel.stop()
    assert closed and tunnel.url is None


@pytest.mark.asyncio
async def test_tunnel_invalid_domain_and_safe_error(monkeypatch):
    with pytest.raises(ValueError):
        TunnelManager(8765, 8765)
    tunnel = TunnelManager(8000, 8765)
    with pytest.raises(ProviderError):
        await tunnel.start("key", "https://example.ngrok.app/path")

    def fail(name):
        raise RuntimeError("secret private-key")

    monkeypatch.setattr("translator.remote.ngrok.importlib.import_module", fail)
    with pytest.raises(ProviderError) as caught:
        await tunnel.start("private-key")
    assert "private-key" not in str(caught.value)
    assert tunnel.status == "error"


@pytest.mark.asyncio
async def test_tunnel_failed_close_is_reported_and_can_be_retried():
    class Listener:
        attempts = 0

        async def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("private-key details")

    tunnel = TunnelManager(8000, 8765)
    tunnel._listener = Listener()
    tunnel.url = "https://example.ngrok.app"
    with pytest.raises(ProviderError) as caught:
        await tunnel.stop()
    assert caught.value.code == "NGROK_STOP_FAILED"
    assert tunnel.status == "error" and tunnel._listener and tunnel.url
    await tunnel.stop()
    assert tunnel.status == "idle" and tunnel.url is None
