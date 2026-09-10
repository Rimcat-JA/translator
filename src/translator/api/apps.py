"""Local administration and externally exposed participant applications."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hmac
import json
from pathlib import Path
import time
from typing import Any, Literal
from urllib.parse import urlsplit
import uuid

import anyio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from translator.sessions import SessionError
from translator.providers import ProviderError
from .auth import Principal


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TokenBody(Model):
    token: str = Field(min_length=16, max_length=200)


class Mutation(Model):
    request_id: str = Field(min_length=1, max_length=128)


class CreateSession(Mutation):
    source_language: str = Field(default="zh", min_length=2, max_length=12)
    target_language: str = Field(default="ja", min_length=2, max_length=12)
    translation_enabled: bool = True
    demo: bool = False


class SettingsPatch(Mutation):
    expected_revision: int | None = None
    settings: dict[str, Any]


class SecretBody(Model):
    key: str = Field(max_length=4096)
    persist: bool = True


class ImportBody(Mutation):
    persist: bool = False


class InviteBody(Mutation):
    role: Literal["speaker"] = "speaker"


class SystemAudioBody(Mutation):
    enabled: bool
    device_id: str | None = Field(default=None, max_length=512)


class LeaseBody(Mutation):
    lease_id: str | None = Field(default=None, max_length=128)
    release: bool = False


class StreamStart(Model):
    type: Literal["stream.start"]
    protocol_version: Literal[1]
    stream_id: str = Field(min_length=1, max_length=128)
    session_epoch: int
    publisher_lease_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["microphone"]
    format: Literal["pcm_s16le"]
    sample_rate: Literal[16000]
    channels: Literal[1]
    frame_samples: int = Field(ge=1, le=1600)


def error(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message,
                                    "retryable": status in {429, 503}}}, status_code=status)


def _languages() -> list[dict]:
    from translator.providers.languages import SUPPORTED_LANGUAGES
    return SUPPORTED_LANGUAGES


def _language(code: str) -> None:
    if code not in {language["code"] for language in _languages()}:
        raise SessionError("UNSUPPORTED_LANGUAGE", "対応する言語を選んでください。", 422)


def _origins(runtime: Any, local: bool) -> set[str]:
    base = runtime.local_origin if local else runtime.hub_origin
    origins = {base.rstrip("/")}
    if not local:
        for name in ("public_origin", "public_url", "tunnel_url"):
            value = getattr(runtime, name, None)
            if value:
                origins.add(value.rstrip("/"))
    # Explicit development origins remain separate for the two applications.
    extra = getattr(runtime, "local_dev_origin" if local else "hub_dev_origin", None)
    if extra:
        origins.add(extra.rstrip("/"))
    if getattr(runtime, "development", False):
        origins.add("http://127.0.0.1:5173" if local else "http://127.0.0.1:5174")
    return origins


def _valid_boundary(connection: Any, runtime: Any, local: bool, require_origin: bool) -> bool:
    origins = _origins(runtime, local)
    host = connection.headers.get("host", "").lower()
    if host not in {urlsplit(origin).netloc.lower() for origin in origins}:
        return False
    origin = connection.headers.get("origin")
    if origin is None:
        return not require_origin
    return origin in origins


def _principal(connection: Any, runtime: Any, local: bool, csrf: bool = False) -> Principal:
    token = connection.cookies.get("translator_local" if local else "translator_participant", "")
    principal = runtime.auth.local(token) if local else runtime.auth.participant(token)
    if principal is None:
        raise SessionError("AUTH_REQUIRED", "画面を開き直すか、新しい招待リンクから参加してください。", 401)
    if csrf and not hmac.compare_digest(connection.headers.get("x-csrf-token", ""), principal.csrf_token):
        raise SessionError("CSRF_INVALID", "画面を開き直してください。", 403)
    return principal


def _participant(connection: Any, runtime: Any, session_id: str, csrf: bool = False) -> Principal:
    principal = _principal(connection, runtime, False, csrf)
    s = runtime.sessions.require(session_id)
    if principal.session_id != s.session_id or s.status == "ended":
        raise SessionError("SESSION_FORBIDDEN", "この会話には参加できません。", 403)
    return principal


def _cookie(response: JSONResponse, name: str, value: str, origin: str, local: bool) -> None:
    response.set_cookie(name, value, max_age=8 * 3600, httponly=True,
                        secure=origin.startswith("https://"), samesite="strict" if local else "lax", path="/")


class BodyLimitMiddleware:
    """Bound actual request bytes, including chunked requests without Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > 32768:
                await error("REQUEST_TOO_LARGE", "送信内容が大きすぎます。", 413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


def _base_app(runtime: Any, local: bool) -> FastAPI:
    app = FastAPI(title="Translator local" if local else "Translator participant",
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        require_origin = request.method not in {"GET", "HEAD", "OPTIONS"}
        if local and request.url.path == "/api/local/instance":
            # CLI authenticates this narrowly scoped endpoint using a distinct IPC secret.
            require_origin = False
        if not _valid_boundary(request, runtime, local, require_origin):
            return error("ORIGIN_FORBIDDEN", "このアクセス元は許可されていません。", 403)
        length = request.headers.get("content-length", "0")
        try:
            if int(length) > 32768:
                return error("REQUEST_TOO_LARGE", "送信内容が大きすぎます。", 413)
        except ValueError:
            return error("INVALID_REQUEST", "送信内容を確認してください。", 400)
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else response.headers.get("cache-control", "no-cache")
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.exception_handler(SessionError)
    async def session_error(request: Request, exc: SessionError):
        return error(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # Validation input may contain credentials; never echo it.
        return error("INVALID_INPUT", "入力項目を確認してください。", 422)

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, exc: ValueError):
        return error("INVALID_SETTING", "設定値または保存方法を確認してください。", 422)

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, exc: ProviderError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message,
                                       "retryable": exc.retryable}}, status_code=503)

    @app.get("/health/live")
    async def health():
        return {"protocol_version": 1, "instance_id": runtime.instance_id}

    return app


def _static(app: FastAPI, runtime: Any, local: bool) -> None:
    web_dir = Path(runtime.web_dir)

    @app.get("/assets/{asset:path}")
    async def assets(asset: str):
        candidate = (web_dir / "assets" / asset).resolve()
        root = (web_dir / "assets").resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise HTTPException(404)
        return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/audio-worklet.js")
    async def worklet():
        candidate = web_dir / "audio-worklet.js"
        if not candidate.is_file():
            raise HTTPException(404)
        return FileResponse(candidate, media_type="text/javascript")

    async def index():
        if not (web_dir / "index.html").is_file():
            return error("FRONTEND_ASSET_MISSING", "画面ファイルがありません。起動コマンドでビルドしてください。", 503)
        return FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-cache"})

    if not local:
        @app.get("/")
        async def participant_home():
            return RedirectResponse("/join", status_code=307)

    for path in (["/", "/a", "/setup", "/settings", "/diagnostics"] if local else ["/join", "/b"]):
        app.add_api_route(path, index, methods=["GET"], include_in_schema=False)


def create_local_app(runtime: Any) -> FastAPI:
    app = _base_app(runtime, True)

    @app.post("/api/local/bootstrap")
    async def exchange(body: TokenBody):
        result = runtime.auth.exchange_bootstrap(body.token)
        if result is None:
            return error("BOOTSTRAP_EXPIRED", "アプリをもう一度起動して画面を開いてください。", 401)
        token, principal = result
        response = JSONResponse({"csrf_token": principal.csrf_token})
        _cookie(response, "translator_local", token, runtime.local_origin, True)
        return response

    @app.get("/api/local/bootstrap")
    async def bootstrap(request: Request):
        principal = _principal(request, runtime, True)
        source_root = getattr(runtime, "source_root", None)
        return {"protocol_version": 1, "surface": "local", "csrf_token": principal.csrf_token,
                "demo": bool(getattr(runtime, "demo", False)),
                "settings": runtime.settings.public(),
                "session": runtime.sessions.current.snapshot() if runtime.sessions.current else None,
                "languages": _languages(), "capabilities": ["manage", "captions", "system_audio", "invite"],
                "env_available": bool(source_root and (Path(source_root) / ".env").is_file())}

    @app.get("/api/local/settings")
    async def settings(request: Request):
        _principal(request, runtime, True)
        return runtime.settings.public()

    @app.patch("/api/local/settings")
    async def update_settings(request: Request, body: SettingsPatch):
        _principal(request, runtime, True, True)
        return runtime.settings.update(body.settings, body.expected_revision)

    @app.post("/api/local/settings/import-env")
    async def import_env(request: Request, body: ImportBody):
        _principal(request, runtime, True, True)
        return await runtime.import_env(persist=body.persist)

    @app.put("/api/local/secrets/{provider}")
    async def secrets(request: Request, provider: Literal["gladia", "deepl", "ngrok"], body: SecretBody):
        _principal(request, runtime, True, True)
        runtime.settings.set_secret(provider, body.key, body.persist)
        return runtime.settings.public()

    @app.post("/api/local/providers/{provider}/test")
    async def test_provider(request: Request, provider: Literal["gladia", "deepl", "ngrok"], body: Mutation):
        _principal(request, runtime, True, True)
        return await runtime.test_provider(provider)

    @app.get("/api/local/audio/devices")
    async def devices(request: Request):
        _principal(request, runtime, True)
        return await runtime.list_audio_devices()

    @app.post("/api/local/sessions")
    async def create(request: Request, body: CreateSession):
        _principal(request, runtime, True, True)
        _language(body.source_language)
        _language(body.target_language)
        return runtime.sessions.create(**body.model_dump())

    @app.post("/api/local/sessions/{session_id}/start")
    async def start(request: Request, session_id: str, body: Mutation):
        _principal(request, runtime, True, True)
        return await runtime.sessions.start(session_id, body.request_id)

    @app.post("/api/local/sessions/{session_id}/stop")
    async def stop(request: Request, session_id: str, body: Mutation):
        _principal(request, runtime, True, True)
        runtime.sessions.require(session_id)
        await runtime.set_system_audio(False, None)
        snapshot = await runtime.sessions.stop(session_id, body.request_id)
        runtime.auth.revoke_session(session_id)
        return snapshot

    @app.post("/api/local/sessions/{session_id}/system-audio")
    async def system_audio(request: Request, session_id: str, body: SystemAudioBody):
        _principal(request, runtime, True, True)
        s = runtime.sessions.require(session_id)
        if body.enabled and s.demo:
            raise SessionError("DEMO_AUDIO_DISABLED", "デモではPC音声を取得しません。実際の会話を開始してください。")
        if body.enabled and s.status not in {"waiting_for_peer", "running", "degraded"}:
            raise SessionError("INVALID_STATE", "通訳を開始してからPC音声を共有してください。")
        if body.enabled and time.monotonic() - getattr(runtime, "local_presence", 0) >= 10:
            raise SessionError("HOST_NOT_CONNECTED", "字幕画面の接続を待って再試行してください。")
        await runtime.set_system_audio(body.enabled, body.device_id)
        return s.snapshot()

    @app.post("/api/local/sessions/{session_id}/invites")
    async def invites(request: Request, session_id: str, body: InviteBody):
        _principal(request, runtime, True, True)
        s = runtime.sessions.require(session_id)
        if s.status in {"stopping", "ended"}:
            raise SessionError("SESSION_ENDED", "終了した会話への招待は作成できません。")
        token = runtime.auth.invite(session_id, body.role)
        origin = next((getattr(runtime, name, None) for name in ("public_origin", "public_url", "tunnel_url")
                       if getattr(runtime, name, None)), runtime.hub_origin)
        return {"url": origin.rstrip("/") + "/join#invite=" + token, "expires_in": 600}

    @app.post("/api/local/tunnel/start")
    async def tunnel_start(request: Request, body: Mutation):
        _principal(request, runtime, True, True)
        return await runtime.start_tunnel()

    @app.post("/api/local/tunnel/stop")
    async def tunnel_stop(request: Request, body: Mutation):
        _principal(request, runtime, True, True)
        return await runtime.stop_tunnel()

    @app.get("/api/local/diagnostics")
    async def diagnostics(request: Request):
        _principal(request, runtime, True)
        return runtime.diagnostics()

    @app.post("/api/local/shutdown")
    async def shutdown(request: Request, body: Mutation):
        _principal(request, runtime, True, True)
        await runtime.shutdown()
        return {"status": "stopping"}

    @app.websocket("/ws/local/events")
    async def local_events(websocket: WebSocket):
        await _events(websocket, runtime, True)

    _static(app, runtime, True)
    return app


def create_hub_app(runtime: Any) -> FastAPI:
    app = _base_app(runtime, False)

    @app.get("/api/v1/bootstrap")
    async def bootstrap(request: Request):
        principal = runtime.auth.participant(request.cookies.get("translator_participant", ""))
        result = {"protocol_version": 1, "surface": "participant", "authenticated": principal is not None,
                  "capabilities": ["captions", "microphone", "playback"]}
        if principal:
            result.update(csrf_token=principal.csrf_token, session_id=principal.session_id, role=principal.role)
        return result

    @app.post("/api/v1/join")
    async def join(request: Request, body: TokenBody):
        result = runtime.auth.join(body.token)
        if result is None:
            return error("INVITE_EXPIRED", "招待の期限が切れたか、すでに使用されています。新しい招待を依頼してください。", 401)
        token, principal = result
        s = runtime.sessions.require(principal.session_id)
        if s.status in {"stopping", "ended"}:
            runtime.auth.leave(token)
            raise SessionError("SESSION_ENDED", "この会話は終了しました。", 403)
        response = JSONResponse({"csrf_token": principal.csrf_token, "session_id": principal.session_id,
                                 "role": principal.role, "snapshot": s.snapshot()})
        _cookie(response, "translator_participant", token, request.headers["origin"], False)
        return response

    @app.post("/api/v1/leave")
    async def leave(request: Request, body: Mutation):
        principal = _principal(request, runtime, False, True)
        s = runtime.sessions.current
        if s and s.session_id == principal.session_id and s.lease_owner == principal.identity:
            if s.audio_connection:
                await runtime.sessions.finish_input(s.audio_connection)
            runtime.sessions.acquire_lease(s.session_id, principal.identity, release=True)
        runtime.auth.leave(request.cookies.get("translator_participant", ""))
        response = JSONResponse({"status": "left"})
        response.delete_cookie("translator_participant", path="/")
        return response

    @app.get("/api/v1/languages")
    async def languages(request: Request):
        _principal(request, runtime, False)
        return {"languages": _languages()}

    @app.get("/api/v1/sessions/{session_id}/snapshot")
    async def snapshot(request: Request, session_id: str):
        _participant(request, runtime, session_id)
        return runtime.sessions.require(session_id).snapshot()

    @app.post("/api/v1/sessions/{session_id}/publisher-lease")
    async def lease(request: Request, session_id: str, body: LeaseBody):
        principal = _participant(request, runtime, session_id, True)
        if principal.role != "speaker":
            raise SessionError("ROLE_FORBIDDEN", "マイク送信は許可されていません。", 403)
        if body.release:
            s = runtime.sessions.require(session_id)
            if s.lease_owner == principal.identity and s.audio_connection:
                await runtime.sessions.finish_input(s.audio_connection)
        return runtime.sessions.acquire_lease(session_id, principal.identity, body.lease_id, body.release)

    @app.websocket("/ws/v1/sessions/{session_id}/events")
    async def participant_events(websocket: WebSocket, session_id: str):
        await _events(websocket, runtime, False, session_id)

    @app.websocket("/ws/v1/sessions/{session_id}/audio-input")
    async def audio_input(websocket: WebSocket, session_id: str):
        await _audio_input(websocket, runtime, session_id)

    @app.websocket("/ws/v1/sessions/{session_id}/audio-output")
    async def audio_output(websocket: WebSocket, session_id: str):
        await _audio_output(websocket, runtime, session_id)

    _static(app, runtime, False)
    return app


async def _ws_authorize(websocket: WebSocket, runtime: Any, local: bool, session_id: str | None = None) -> Principal | None:
    try:
        if not _valid_boundary(websocket, runtime, local, True):
            raise SessionError("ORIGIN_FORBIDDEN", "許可されていない接続元です。", 403)
        return _principal(websocket, runtime, True) if local else _participant(websocket, runtime, session_id)
    except SessionError:
        await websocket.close(code=4403)
        return None


async def _events(websocket: WebSocket, runtime: Any, local: bool, session_id: str | None = None) -> None:
    principal = await _ws_authorize(websocket, runtime, local, session_id)
    if principal is None:
        return
    await websocket.accept()
    queue, snapshot = runtime.sessions.subscribe()
    if local:
        runtime.local_presence = time.monotonic()
    try:
        await websocket.send_json(snapshot)
    except BaseException:
        runtime.sessions.unsubscribe(queue)
        if local:
            await runtime.set_system_audio(False, None)
        raise

    async def sender():
        while True:
            event = await queue.get()
            if not local:
                # Re-check cookie audience and session on every emission after revocation/stop.
                _participant(websocket, runtime, session_id)
            await websocket.send_json(event)

    async def receiver():
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 8192:
                raise SessionError("REQUEST_TOO_LARGE", "操作データが大きすぎます。", 413)
            if local:
                _principal(websocket, runtime, True)
                runtime.local_presence = time.monotonic()
            else:
                _participant(websocket, runtime, session_id)
            request_id = ""
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError()
                request_id = message.get("request_id", "")
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                    raise ValueError()
                command = message.get("type")
                if command in {"command.ping", "ping"}:
                    data = {"pong": True}
                elif command in {"command.get_snapshot", "get_snapshot"}:
                    await websocket.send_json(runtime.sessions.snapshot_event())
                    continue
                else:
                    allowed = {"command.set_source_language": "source_language"}
                    if local:
                        allowed.update({"command.set_target_language": "target_language",
                                        "command.set_translation_enabled": "translation_enabled"})
                    if command not in allowed:
                        raise SessionError("ROLE_FORBIDDEN", "この操作は許可されていません。", 403)
                    field_name = allowed[command]
                    value = message.get("data", {}).get("enabled" if field_name == "translation_enabled" else "language")
                    if field_name == "translation_enabled":
                        if not isinstance(value, bool):
                            raise ValueError()
                    else:
                        if not isinstance(value, str):
                            raise ValueError()
                        _language(value)
                    s = runtime.sessions.current
                    if not s:
                        raise SessionError("SESSION_NOT_FOUND", "会話を開始してください。", 404)
                    expected = message.get("expected_revision")
                    if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)):
                        raise ValueError()
                    data = await runtime.sessions.configure(s.session_id, field_name, value, expected)
                await websocket.send_json({"protocol_version": 1, "type": "command.completed",
                                           "request_id": request_id, "data": data})
            except (ValueError, TypeError, AttributeError):
                await websocket.send_json({"type": "command.failed", "request_id": request_id,
                    "error": {"code": "INVALID_INPUT", "message": "操作内容を確認してください。", "retryable": False}})
            except SessionError as exc:
                await websocket.send_json({"type": "command.failed", "request_id": request_id,
                    "error": {"code": exc.code, "message": exc.message, "retryable": exc.status == 503}})

    async def presence_watchdog():
        while True:
            await asyncio.sleep(1)
            if local and time.monotonic() - runtime.local_presence >= 6:
                await runtime.set_system_audio(False, None)
                return

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    if local:
        tasks.append(asyncio.create_task(presence_watchdog()))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, SessionError, RuntimeError):
        pass
    finally:
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            runtime.sessions.unsubscribe(queue)
            if local:
                # A surviving host view can explicitly restart sharing after another view closes.
                await runtime.set_system_audio(False, None)
            with suppress(RuntimeError, WebSocketDisconnect, OSError):
                await websocket.close(code=1000 if local else 4401)


async def _audio_input(websocket: WebSocket, runtime: Any, session_id: str) -> None:
    principal = await _ws_authorize(websocket, runtime, False, session_id)
    if principal is None:
        return
    if principal.role != "speaker":
        await websocket.close(code=4403)
        return
    await websocket.accept()
    connection = uuid.uuid4().hex
    started = False
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), 10)
        if len(raw) > 4096:
            raise ValueError()
        setup = StreamStart.model_validate_json(raw)
        s = runtime.sessions.require(session_id)
        if setup.session_epoch != s.session_epoch:
            raise SessionError("SESSION_EXPIRED", "会話が更新されました。", 403)
        await runtime.sessions.start_input(session_id, principal.identity, setup.publisher_lease_id, connection)
        started = True
        await websocket.send_json({"type": "stream.accepted", "protocol_version": 1,
                                   "stream_id": setup.stream_id, "stream_generation": connection})
        from translator.audio.protocol import AudioFrame, AudioStreamValidator
        validator = AudioStreamValidator()
        samples = 0
        began = time.monotonic()
        while True:
            message = await asyncio.wait_for(websocket.receive(), 15)
            _participant(websocket, runtime, session_id)
            if message["type"] == "websocket.disconnect":
                break
            if message.get("text"):
                if len(message["text"]) > 256:
                    raise ValueError()
                control = json.loads(message["text"])
                if isinstance(control, dict) and control.get("type") == "stream.stop":
                    break
                raise ValueError()
            payload = message.get("bytes")
            if not payload or len(payload) > 4096:
                raise ValueError()
            frame = AudioFrame.parse(payload)
            if frame.sample_count > setup.frame_samples:
                raise ValueError()
            validator.accept(frame)
            samples += frame.sample_count
            # Small browser scheduling bursts are allowed; unbounded fast uploads are rejected.
            if samples > (time.monotonic() - began + .5) * 16000:
                raise SessionError("AUDIO_RATE_LIMIT", "音声の送信速度が上限を超えました。", 429)
            await runtime.sessions.send_audio(frame.pcm, principal.identity, setup.publisher_lease_id, connection)
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, ValidationError, SessionError, ProviderError):
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=4400)
    finally:
        with anyio.CancelScope(shield=True):
            if started:
                await runtime.sessions.finish_input(connection)
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()


async def _audio_output(websocket: WebSocket, runtime: Any, session_id: str) -> None:
    principal = await _ws_authorize(websocket, runtime, False, session_id)
    if principal is None:
        return
    if principal.role != "speaker":
        await websocket.close(code=4403)
        return
    await websocket.accept()
    queue = runtime.sessions.subscribe_audio()
    try:
        await websocket.send_json({"type": "stream.accepted", "protocol_version": 1,
                                   "stream_generation": uuid.uuid4().hex, "sample_rate": 16000, "channels": 1})
    except BaseException:
        runtime.sessions.unsubscribe_audio(queue)
        raise

    async def send():
        while True:
            frame = await queue.get()
            _participant(websocket, runtime, session_id)
            await asyncio.wait_for(websocket.send_bytes(frame.encode()), .2)

    async def receive():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            _participant(websocket, runtime, session_id)

    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, SessionError, asyncio.TimeoutError, RuntimeError, EOFError):
        pass
    finally:
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            runtime.sessions.unsubscribe_audio(queue)
            with suppress(RuntimeError, WebSocketDisconnect, OSError):
                await websocket.close()
