# Runtime architecture

The desktop entry point owns two separately bound loopback sockets. FastAPI Local exposes management and the host UI. FastAPI Hub exposes only participant routes. Both share one SessionService. Uvicorn runs one worker per app inside one process, using the same asyncio event loop. Assets are one React/Vite build; no production Node server is needed.

The launcher reserves real sockets before serving, falls back to an ephemeral port when occupied, and waits for both servers to start before opening the browser. An OS-held file lock prevents concurrent runtimes in one user data directory. Reopening/stopping uses a random local IPC secret and verified instance identifier; it never kills a process based on a PID file.

The local browser exchanges a 60-second one-use fragment token for an HttpOnly cookie and CSRF token. Participant invitations are separate random credentials bound to a session and speaker role. Host/Origin checks, cookies with distinct audiences, CSRF and WebSocket authorization are enforced server-side. ngrok receives the actual Hub port and can never be configured to forward the Local port through the UI.

SessionService owns captions, a separate STT result consumer and two translation workers. Caption updates carry session/STT/translation generations and source revisions. Translation completion is checked against the current utterance revision and generation. Pending work is bounded and final text is prioritized. Snapshot subscription establishes an event-loop boundary, and slow clients receive a replacement snapshot.

PC audio capture is an explicitly started Windows child process with bounded IPC and a parent-liveness watchdog. B uses AudioWorklets for microphone resampling and playback. The TRN1 transport is 24 bytes of header followed by signed 16-bit little-endian mono PCM at 16kHz. Audio-time buffers discard old frames; stale generations are not replayed after reconnection. Audio is never stored in React state or on disk.

The settings file contains only validated preferences. OS credentials or an explicitly selected in-memory secret store hold API tokens. Importing `.env` is an explicit user action. External service tests also require a UI action. Opening the app does not start external providers or capture devices. The demo uses synthetic PCM and the regular service pipeline with deterministic local providers.

The old unauthenticated server entry point intentionally fails with migration instructions. The old CLI and HTML files remain reference material, not compatible clients. Distributed-host compatibility credentials, automatic provider reconnection, every design-document scenario, and additional release gates must not be inferred from their presence in the original design.
