"""Windows SoundCard access isolated from the ASGI process and import path."""
from __future__ import annotations

import asyncio
import multiprocessing
from queue import Empty
import sys


def float_to_pcm(data) -> bytes:
    import numpy as np

    values = np.asarray(data, dtype=np.float64)
    if values.ndim == 2:
        values = values.mean(axis=1)
    elif values.ndim != 1:
        raise ValueError("Expected mono or multichannel audio samples")
    values = np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=-1.0)
    values = np.clip(values, -1.0, 1.0)
    return np.rint(values * np.where(values < 0, 32768, 32767)).astype("<i2").tobytes()


def _enumerate_devices(output) -> None:
    try:
        import soundcard as sc

        default = sc.default_speaker()
        devices = [{"id": speaker.id, "name": speaker.name, "channels": speaker.channels,
                    "is_default": bool(default and speaker.id == default.id)} for speaker in sc.all_speakers()]
        output.put({"supported": True, "devices": devices})
    except Exception:
        output.put({"supported": True, "devices": [], "error_code": "AUDIO_DEVICE_ENUMERATION_FAILED"})


async def list_devices() -> dict:
    if sys.platform != "win32":
        return {"supported": False, "devices": [], "error_code": "SYSTEM_AUDIO_WINDOWS_ONLY"}
    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=1)
    process = context.Process(target=_enumerate_devices, args=(output,), name="translator-devices")
    try:
        process.start()
        try:
            return await asyncio.to_thread(output.get, True, 8.0)
        except Empty:
            return {"supported": True, "devices": [], "error_code": "AUDIO_DEVICE_TIMEOUT"}
    finally:
        if process.pid:
            if process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 2.0)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 1.0)
            process.close()
        output.close()
