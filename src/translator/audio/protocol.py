"""TRN1 protocol: 24-byte LE header + 16 kHz mono signed 16-bit PCM."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, replace
import struct
import time

SAMPLE_RATE = 16000
HEADER = struct.Struct("<4sBBHIQI")
MAX_FRAME_SAMPLES = 1600
MAX_MESSAGE_BYTES = 4096
DISCONTINUITY = 1


class AudioProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class AudioFrame:
    sequence: int
    sample_offset: int
    pcm: bytes
    flags: int = 0

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // 2

    @property
    def duration_ms(self) -> float:
        return self.sample_count / 16

    def encode(self) -> bytes:
        if not (0 <= self.sequence < 0xFFFFFFFF and 0 <= self.sample_offset < 2 ** 64):
            raise AudioProtocolError("Stream must restart before sequence wrap")
        if self.flags & ~DISCONTINUITY or self.flags < 0:
            raise AudioProtocolError("Unknown audio flags")
        if not isinstance(self.pcm, bytes) or len(self.pcm) % 2 or not 1 <= self.sample_count <= MAX_FRAME_SAMPLES:
            raise AudioProtocolError("Audio frame must contain 1–1600 PCM samples")
        return HEADER.pack(b"TRN1", 1, 1, self.flags, self.sequence, self.sample_offset, self.sample_count) + self.pcm

    @classmethod
    def parse(cls, data: bytes) -> AudioFrame:
        if not isinstance(data, bytes) or not HEADER.size < len(data) <= MAX_MESSAGE_BYTES:
            raise AudioProtocolError("Invalid audio message size")
        magic, version, kind, flags, sequence, offset, count = HEADER.unpack_from(data)
        if magic != b"TRN1" or version != 1 or kind != 1:
            raise AudioProtocolError("Invalid audio protocol")
        if flags & ~DISCONTINUITY or sequence == 0xFFFFFFFF:
            raise AudioProtocolError("Invalid audio flags or expired sequence")
        if not 1 <= count <= MAX_FRAME_SAMPLES or len(data) != HEADER.size + count * 2:
            raise AudioProtocolError("Audio sample count does not match payload")
        return cls(sequence, offset, data[HEADER.size:], flags)


def parse_frame(data: bytes) -> AudioFrame:
    return AudioFrame.parse(data)


class AudioStreamValidator:
    """Per-connection continuity and optional audio-time token-bucket limit."""

    def __init__(self, *, limit_rate: bool = True, clock=time.monotonic):
        self._sequence: int | None = None
        self._end = 0
        self.discontinuities = 0
        self._clock = clock
        self._time = clock()
        self._tokens = 3200.0  # 200 ms initial burst, never accumulated beyond this.
        self._limit_rate = limit_rate

    def accept(self, frame: AudioFrame) -> bool:
        if self._sequence is not None and (frame.sequence <= self._sequence or frame.sample_offset < self._end):
            raise AudioProtocolError("Repeated or stale audio frame")
        if self._limit_rate:
            now = self._clock()
            self._tokens = min(3200.0, self._tokens + max(0, now - self._time) * SAMPLE_RATE)
            self._time = now
            if frame.sample_count > self._tokens + 32:
                raise AudioProtocolError("Audio is arriving faster than real time")
            self._tokens -= frame.sample_count
        gap = bool(frame.flags & DISCONTINUITY) or (
            self._sequence is not None and (frame.sequence != self._sequence + 1 or frame.sample_offset != self._end)
        )
        if gap:
            self.discontinuities += 1
        self._sequence = frame.sequence
        self._end = frame.sample_offset + frame.sample_count
        return gap


class BoundedAudioQueue:
    """Single-event-loop queue measured in audio samples, with oldest-first loss."""

    def __init__(self, max_ms: int = 100):
        if max_ms <= 0:
            raise ValueError("Audio queue duration must be positive")
        self._max_samples = max_ms * 16
        self._samples = 0
        self._frames: deque[AudioFrame] = deque()
        self._ready = asyncio.Event()
        self._closed = False
        self.dropped_samples = 0

    @property
    def buffered_ms(self) -> float:
        return self._samples / 16

    def put(self, frame: AudioFrame) -> None:
        if self._closed:
            return
        if frame.sample_count > self._max_samples:
            raise AudioProtocolError("Audio frame exceeds the queue duration")
        dropped = False
        while self._frames and self._samples + frame.sample_count > self._max_samples:
            old = self._frames.popleft()
            self._samples -= old.sample_count
            self.dropped_samples += old.sample_count
            dropped = True
        if dropped:
            # Mark the next frame the consumer will actually receive.
            if self._frames:
                self._frames[0] = replace(self._frames[0], flags=self._frames[0].flags | DISCONTINUITY)
            else:
                frame = replace(frame, flags=frame.flags | DISCONTINUITY)
        self._frames.append(frame)
        self._samples += frame.sample_count
        self._ready.set()

    async def get(self) -> AudioFrame:
        while not self._frames:
            if self._closed:
                raise EOFError("Audio queue closed")
            self._ready.clear()
            await self._ready.wait()
        frame = self._frames.popleft()
        self._samples -= frame.sample_count
        return frame

    def clear(self) -> None:
        self._frames.clear()
        self._samples = 0

    def close(self) -> None:
        self.clear()
        self._closed = True
        self._ready.set()
