import asyncio
import os
import sounddevice as sd
import websockets
from dotenv import load_dotenv

load_dotenv()

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 100
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_MS / 1000)
SERVER_URL = os.getenv("SERVER_URL", "ws://localhost:8000") + "/audio/from_B"


async def run():
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def callback(indata, frames, time, status):
        loop.call_soon_threadsafe(queue.put_nowait, indata.copy().tobytes())

    async with websockets.connect(SERVER_URL) as ws:
        print(f"Connected to {SERVER_URL}")
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            callback=callback,
        ):
            print("Microphone active — press Ctrl+C to stop")
            while True:
                data = await queue.get()
                await ws.send(data)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Stopped")
