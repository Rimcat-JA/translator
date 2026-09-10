# Implemented application contract

All URLs are relative to the current origin. Local and participant surfaces use separate ASGI apps. `GET /health/live` is public. JSON errors are `{error:{code,message,retryable}}`.

`http-inputs.schema.json` is generated from the actual Pydantic HTTP request models and the `StreamStart` audio handshake. Regenerate with `uv run --locked python tools/export_contracts.py`; CI uses `--check` to detect drift. This artifact covers validated inputs only. The snapshot and outbound WebSocket envelopes below are documented contracts, and are not claimed to have generated Python event schemas. Frontend response validation remains in its Zod schemas.

## Authentication

Launcher URL: `/#bootstrap=TOKEN`. POST `/api/local/bootstrap` `{token}` exchanges it once and returns `{csrf_token}`; erase the fragment immediately. Local cookie is `translator_local`. GET `/api/local/bootstrap` requires it and returns `{protocol_version:1,surface:"local",csrf_token,settings,session,languages,capabilities}`. `session` is a snapshot or null. `/api/v1/bootstrap` is public and returns `{protocol_version:1,surface:"participant",authenticated,csrf_token?,session_id?,role?,capabilities}`.

Invitation URL: Hub `/join#invite=TOKEN`. POST `/api/v1/join` `{token}` returns `{csrf_token,session_id,role:"speaker",snapshot}` and sets `translator_participant`. All mutation requests after bootstrap/join send `X-CSRF-Token: csrf_token`. Requests and WS must have same approved Origin. Credentials remain HttpOnly cookies.

## Local management

GET/PATCH `/api/local/settings`: flat settings (`source_language`,`target_language`,`translation_enabled`,`deepl_mode`,`loopback_device_id`,`remote_domain`,`theme`,`caption_font_size`,`show_original`). PATCH body `{request_id,expected_revision,settings:{...}}`; return public settings. Secrets are metadata only `{secrets:{gladia:{configured,storage},deepl:...,ngrok:...}}`. PUT `/api/local/secrets/{gladia|deepl|ngrok}` `{key,persist}`. POST `/api/local/providers/{provider}/test` `{request_id}`. GET `/api/local/audio/devices`; GET `/api/local/diagnostics`.

POST `/api/local/sessions` `{request_id,source_language:"zh",target_language:"ja",translation_enabled:true,demo:false}` creates idle session and returns snapshot directly. POST `/api/local/sessions/{id}/start` / `stop` `{request_id}` returns snapshot directly. POST `/api/local/sessions/{id}/system-audio` `{request_id,enabled,device_id?}` returns snapshot. POST `/api/local/sessions/{id}/invites` `{request_id,role:"speaker"}` returns `{url,expires_in:600}`. POST `/api/local/tunnel/start` / `stop` `{request_id}`. POST `/api/local/shutdown` `{request_id}`.

## Participant

GET `/api/v1/sessions/{id}/snapshot` returns snapshot. GET `/api/v1/languages` returns `{languages:[{code,name_ja,name_en,...}]}`. POST `/api/v1/leave` `{request_id}`. POST `/api/v1/sessions/{id}/publisher-lease` `{request_id,lease_id?:string,release?:boolean}` returns `{lease_id,expires_in:15}`; renew each 5 s. Another holder yields HTTP 409. Audio stream closure releases lease.

## Snapshot and events

Snapshot: `{session_id,session_epoch,revision,status,demo,source_language,target_language,translation_enabled,components:{stt_status,translation_status,system_audio_status,mic_status,playback_status},captions:{[utterance_id]:caption},last_event_seq}`. Idle demo creation does not start audio; start begins local synthetic PCM demo with dummy providers.

WS `/ws/local/events` and `/ws/v1/sessions/{id}/events` sends initial `{protocol_version:1,type:"session.snapshot",data:snapshot|null}`. Subsequent `session.snapshot` uses same shape. `caption.upsert` = `{protocol_version:1,type,session_id,session_epoch,event_seq,data:caption}`. Caption matches design section 9.5. Event subscription and initial snapshot occur at one event-loop boundary.

WS accepts `{type:"command.ping",request_id}` (renew local presence) and `{type:"command.get_snapshot",request_id}`; set commands `{type:"command.set_source_language"|"command.set_target_language"|"command.set_translation_enabled",request_id,expected_revision,data:{language}|{enabled}}`. Server sends `{type:"command.completed",request_id,data}` or `{type:"command.failed",request_id,error:{code,message,retryable}}`. Send local ping every 3 s; PC audio capture stops after 10 s without authenticated local presence. Source language change requires stopped mic; target/translation commands local only.

## Audio

WS `/ws/v1/sessions/{id}/audio-input`: send design's `stream.start` JSON with `publisher_lease_id`, `session_epoch`, `source_kind:"microphone"`, pcm_s16le/16000/1, frame_samples 320 (max 1600). Wait for `stream.accepted` `{stream_id,stream_generation}` before binary TRN1 frames. Stream input is lease-bound and must remain paced; server strips header for STT. `stream.stop` closes gracefully/drains final events. WS `/ws/v1/sessions/{id}/audio-output` immediately sends `stream.accepted`, followed by TRN1 frames from explicitly enabled A capture. Disconnect flushes generation and queues.

## Python ownership

`AuthManager()`; `.issue_bootstrap() -> token`; `.revoke_session(session_id)`. `SessionService(provider_factory=None)` with sync provider_factory(demo:bool)->(STT,TranslationProvider). Root injects factory reading current settings. `.create(source_language,target_language,translation_enabled,demo,request_id)` is sync, `.start(id,request_id)` / `.stop(id,request_id)` async, `.current` SessionContext or None. `.close()` async; `.publish_system_audio(frame:bytes)` sync bounded relay. Runtime owns lifecycle; app factories never start providers on construction.
