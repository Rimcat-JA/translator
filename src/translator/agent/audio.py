"""Bounded, explicitly owned PC-audio sharing through the authenticated local API.

This client never captures or receives PCM. Its WebSocket maintains host presence;
the Runtime owns capture and atomically checks the matching audio owner token.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

import websockets

from .client import AgentError

CONNECT_TIMEOUT = 10.0
SNAPSHOT_TIMEOUT = 5.0
HEARTBEAT_INTERVAL = 3.0
HEARTBEAT_TIMEOUT = 4.0
STOP_TIMEOUT = 8.0
MAX_EVENT_BYTES = 8 * 1024 * 1024


def _failure(code: str, message: str, *, retryable: bool = False) -> AgentError:
    return AgentError(code, message, exit_code=4, retryable=retryable,
                      next_action="Run translator agent status to inspect the active session before retrying.")


def _snapshot(event: dict, session_id: str) -> dict:
    snapshot = event.get("data")
    if event.get("type") != "session.snapshot" or not isinstance(snapshot, dict):
        raise _failure("AUDIO_CONTROL_INVALID", "The host connection did not return a session snapshot.")
    if snapshot.get("session_id") != session_id:
        raise _failure("SESSION_MISMATCH", "The requested conversation is no longer the active session.")
    if snapshot.get("demo"):
        raise _failure("DEMO_AUDIO_DISABLED", "PC audio sharing requires an explicitly started non-demo conversation.")
    if snapshot.get("status") not in {"waiting_for_peer", "running", "degraded"}:
        raise _failure("SESSION_NOT_ACTIVE", "Start the requested conversation before sharing PC audio.")
    components = snapshot.get("components")
    if not isinstance(components, dict):
        raise _failure("AUDIO_CONTROL_INVALID", "The host did not return an audio status.")
    return components


def _event(raw) -> dict:
    if not isinstance(raw, str) or len(raw) > MAX_EVENT_BYTES:
        raise _failure("AUDIO_CONTROL_INVALID", "The host returned an invalid control message.")
    try:
        event = json.loads(raw)
    except (ValueError, TypeError):
        raise _failure("AUDIO_CONTROL_INVALID", "The host returned an invalid control message.") from None
    if not isinstance(event, dict):
        raise _failure("AUDIO_CONTROL_INVALID", "The host returned an invalid control message.")
    return event


async def _request(client, method: str, path: str, body: dict | None = None) -> dict:
    # AgentClient's HTTP transport has its own timeout and disables proxy lookup.
    # Keep synchronous requests off the loop so heartbeats continue during start.
    return await asyncio.to_thread(client.request, method, path, body)


async def _until(operation: asyncio.Task, monitors: list[asyncio.Task]):
    await asyncio.wait([operation, *monitors], return_when=asyncio.FIRST_COMPLETED)
    for monitor in monitors:
        if monitor.done():
            monitor.result()
            raise _failure("AUDIO_CONTROL_DISCONNECTED", "The host control connection ended.", retryable=True)
    return operation.result()


async def share_audio(client, session_id: str, seconds: int, *, device_id: str | None = None) -> dict:
    """Share the selected PC output for 1–3600 seconds, then release our owner.

    The client must already be connected. An existing or concurrent share is
    never adopted: the server's owner_id claim is atomic and releases are scoped.
    Cancellation still attempts a scoped stop before closing the presence socket.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 3600:
        raise _failure("INVALID_DURATION", "--seconds must be an integer between 1 and 3600.")
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or not all(
        char.isascii() and (char.isalnum() or char in "_-") for char in session_id
    ):
        raise _failure("INVALID_SESSION_ID", "Supply the session_id returned by translator agent status or translator agent session-start.")
    if device_id is not None and (not isinstance(device_id, str) or len(device_id) > 512):
        raise _failure("INVALID_DEVICE_ID", "Choose a device_id returned by translator agent devices.")
    try:
        parsed = urlsplit(client.origin)
        valid_origin = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                        and not (parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment))
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise _failure("UNSAFE_RUNTIME_ORIGIN", "The audio controller requires the authenticated local Runtime.")
    headers = client.ws_headers()
    if headers.get("Origin") != client.origin or not headers.get("Cookie"):
        raise _failure("AUTH_REQUIRED", "Connect to the local Runtime before sharing PC audio.")

    owner_id = uuid4().hex
    path = f"/api/local/sessions/{session_id}/system-audio"
    websocket_url = f"ws://{parsed.netloc}/ws/local/events?audio_owner={owner_id}"
    socket = None
    tasks: list[asyncio.Task] = []
    operation: asyncio.Task | None = None
    start_attempted = False
    stop_confirmed = False
    sharing = False
    started_at: float | None = None
    completed_seconds = 0.0
    pong = asyncio.Event()
    pending_ping: str | None = None

    async def receive():
        while True:
            event = _event(await socket.recv())
            if event.get("type") == "session.snapshot":
                components = _snapshot(event, session_id)
                if sharing and components.get("system_audio_status") != "active":
                    raise _failure("AUDIO_SHARE_INTERRUPTED", "PC audio sharing stopped before its requested duration.")
            elif event.get("request_id") == pending_ping:
                if event.get("type") == "command.completed" and event.get("data", {}).get("pong") is True:
                    pong.set()
                elif event.get("type") == "command.failed":
                    raise _failure("AUDIO_HEARTBEAT_FAILED", "The host rejected the audio controller heartbeat.")
            # Caption events are deliberately discarded and never included in output.

    async def heartbeat():
        nonlocal pending_ping
        while True:
            sent_at = time.monotonic()
            pending_ping = uuid4().hex
            pong.clear()
            await socket.send(json.dumps({"protocol_version": 1, "type": "command.ping", "request_id": pending_ping}))
            try:
                await asyncio.wait_for(pong.wait(), timeout=HEARTBEAT_TIMEOUT)
            except TimeoutError:
                raise _failure("AUDIO_HEARTBEAT_TIMEOUT", "The host did not acknowledge its control heartbeat.", retryable=True) from None
            # A slow acknowledgment must not add a second full interval beyond
            # the server's six-second presence deadline.
            await asyncio.sleep(max(0, HEARTBEAT_INTERVAL - (time.monotonic() - sent_at)))

    try:
        socket = await asyncio.wait_for(websockets.connect(
            websocket_url, origin=client.origin, additional_headers={"Cookie": headers["Cookie"]},
            proxy=None, open_timeout=CONNECT_TIMEOUT, close_timeout=2,
            ping_interval=None, max_size=MAX_EVENT_BYTES, max_queue=4,
        ), timeout=CONNECT_TIMEOUT)
        try:
            initial = _event(await asyncio.wait_for(socket.recv(), timeout=SNAPSHOT_TIMEOUT))
        except TimeoutError:
            raise _failure("AUDIO_SNAPSHOT_TIMEOUT", "The host did not confirm its audio control connection.", retryable=True) from None
        components = _snapshot(initial, session_id)
        if components.get("system_audio_status") in {"active", "connecting"}:
            raise _failure("AUDIO_ALREADY_ACTIVE", "Another controller is already sharing PC audio. Its share was left unchanged.")
        tasks = [asyncio.create_task(receive(), name="agent-audio-events"),
                 asyncio.create_task(heartbeat(), name="agent-audio-heartbeat")]
        body = {"request_id": uuid4().hex, "enabled": True, "owner_id": owner_id}
        if device_id is not None:
            body["device_id"] = device_id
        start_attempted = True
        operation = asyncio.create_task(_request(client, "POST", path, body))
        response = await _until(operation, tasks)
        if not isinstance(response, dict) or _snapshot({"type": "session.snapshot", "data": response}, session_id).get("system_audio_status") != "active":
            raise _failure("AUDIO_START_FAILED", "The host did not confirm that PC audio sharing started.")
        sharing = True
        started_at = time.monotonic()
        operation = asyncio.create_task(asyncio.sleep(seconds))
        await _until(operation, tasks)
        completed_seconds = round(time.monotonic() - started_at, 3)
    except AgentError:
        raise
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise _failure("AUDIO_CONTROL_TIMEOUT", "The local audio controller timed out.", retryable=True) from None
    except Exception:
        # Transport exceptions can contain cookies or response bodies. Return only
        # fixed errors; the diagnostics JSON never contains control-event text.
        raise _failure("AUDIO_CONTROL_DISCONNECTED", "The local audio control connection failed.", retryable=True) from None
    finally:
        sharing = False
        if operation is not None:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        if start_attempted:
            try:
                response = await asyncio.wait_for(_request(client, "POST", path, {
                    "request_id": uuid4().hex, "enabled": False, "owner_id": owner_id,
                }), timeout=STOP_TIMEOUT)
                stop_confirmed = isinstance(response, dict) and response.get("session_id") == session_id and (
                    isinstance(response.get("components"), dict)
                    and response["components"].get("system_audio_status") == "idle"
                )
            except Exception:
                # Closing this owner-bound socket triggers the server's scoped
                # release as a second path; do not claim its result was confirmed.
                stop_confirmed = False
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if socket is not None:
            with suppress(Exception):
                await asyncio.wait_for(socket.close(), timeout=3)
    if not stop_confirmed:
        raise _failure("AUDIO_STOP_UNCONFIRMED", "The host did not confirm that PC audio sharing stopped. Run translator agent status.", retryable=True)
    return {"session_id": session_id, "status": "stopped", "stopped": True,
            "requested_seconds": seconds, "duration_seconds": completed_seconds}
