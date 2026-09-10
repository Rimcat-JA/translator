# 0.2.0 preview verification

Baseline: `6bfd8092f05b86160cd419782c980943c9d59c51`, repository main on 2026-09-10. The supplied design was used as a specification reference; its example commands were not assumed to exist in the baseline.

## Automated checks

Environment: Windows 11 x64, Python 3.13.13, Node 24.13.0, locked Python/npm dependencies. Ordinary checks do not contact paid APIs or capture a physical microphone/PC audio.

| Check | Result |
|---|---|
| Python API, session, config, providers, audio, runtime, agent CLI and contract tests | 135 passed |
| Frontend type checking and production build | Passed |
| Frontend reducer, audio DSP and UI tests | 23 unique tests passed |
| Actual source process startup/shutdown/restart | Passed twice |
| CLI Japanese output under inherited ASCII/cp1252 encodings | Passed; ASCII source process smoke also passed |
| PyInstaller Windows onedir build | Passed |
| Frozen executable startup/shutdown/restart with developer tools removed from PATH | Passed twice |
| Frozen SoundCard/ngrok native dependency imports | Passed |
| Python/Node/uv/Git absent from a clean Windows VM | Not tested |
| Real browser E2E | 3 scenarios passed: demo/settings/reconnect; independent B microphone + A playback + stop/leave; microphone denial |
| Visual inspection | Desktop home/captions/speaker and 320 px dark settings inspected |

## Agent interface additions

The agent interface is newer than the published `v0.2.0-preview.1` ZIP. Its entry point is `translator agent`, or `TranslatorAgent.exe agent` in newly built Windows artifacts. The human launcher remains available. No paid provider, physical microphone or PC capture was used for these checks.

- The source and frozen console process workflows passed: offline catalog, invalid-input JSON, discovery, background startup/reuse, settings through stdin, memory-only secret input without echo, demo captions, invitation creation, session stop and Runtime stop. Tests use an unrelated working directory, Japanese/space-containing profile paths and inherited ASCII stdout encoding. Frozen checks remove development tools from PATH.
- Agent responses use one versioned JSON envelope and stable exit codes. Tests cover credential/URL redaction, rejected redirects and foreign loopback identity, strict settings input, malformed/duplicate JSON keys, request failures and shutdown response loss. Mutations are not automatically repeated.
- Status omits transcript text. Snapshot responses default to ten recent captions; explicit limits up to 100 report total count and truncation. A single-command catalog limits the context needed by a smaller model. This is a deterministic interface design, not a benchmark of a particular model's task success.
- A real authenticated HTTP/WebSocket test with a synthetic driver exercises one-second headless sharing, owner acquisition/release and logout. Other agents and ordinary browser disconnections cannot release that capture. Physical capture and audible output remain unverified.
- A real synthetic hung child process verifies cancellation-safe and concurrent audio stop. Runtime retains a failed worker stop for retry. Generated command catalog, output schema, document index and local documentation links are checked for drift.
- The documented PowerShell demo sequence was executed verbatim through successful Runtime shutdown. Existing browser E2E scenarios still pass with scoped audio ownership.

Source and frozen process smoke tests cover an unrelated working directory, Japanese/space-containing data paths, an occupied preferred port, authenticated bootstrap, untrusted access rejection, missing-asset 404, sample-driven translated demo captions, second-launch reuse, conversation stop, authenticated application stop, and restart. A process ID alone is never trusted or killed. Runtime tests also check that startup invokes no provider/capture/tunnel, and that driver teardown failure does not prevent other owned resources from closing. Two concurrent Uvicorn signal handlers were found to restore stale callbacks; the Runtime now leaves signal ownership to the CLI/asyncio runner.

Backend tests cover one-use/expired/revoked credentials, cookie audiences, CSRF, invalid/null Origin and Host, management APIs absent on Hub, second-tab microphone rejection, lease expiry, framed microphone input through dummy STT/final drain/translation, A PCM output relay, host-presence stop, stale translation rejection, final-before-interim protection, bounded work/history and snapshot resynchronization.

## Native and long-duration synthetic checks

SoundCard 0.4.6 and ngrok 1.7.0 imported on Windows. Device enumeration ran in a separate process and returned seven output devices, including a default device. Device names are not included in this report. This did not capture audio or create a public tunnel.

The accelerated queue test inserts 180,000 frames × 320 samples = 57,600,000 samples, representing 60 minutes at 16 kHz. Every insertion is checked against a 100 ms / 1,600-sample / 3,200-byte PCM bound. Under an intentionally stalled consumer, 179,995 old frames are discarded and the first retained frame is marked discontinuous. Mixed 20/40/100 ms frames are also bounded by duration. This demonstrates bounded data structures, **not** 60 minutes of live-device/network stability or measured latency.

Synthetic child-process tests verify startup, frame delivery, normal stop and termination of a deliberately blocked fake driver. Physical loopback capture, audible playback quality and device loss during real use remain unverified.

Browser DSP tests cover 44.1/48 kHz input, exact sample counts over three seconds, chunk-boundary continuity, preserved 1 kHz input and at least 45 dB suppression of aliased 10/12 kHz tones. Playwright uses Chromium fake media and injected dummy providers plus synthetic A PCM; it does not substitute for physical microphone permission dialogs or audible hardware quality.

## Acceptance status and scope

| Design criteria | Current evidence |
|---|---|
| AC01 | Packaged startup with restricted PATH passed; clean VM gate remains open |
| AC02 | One-command source startup passed; Windows/Linux source tests run in CI; macOS is untested |
| AC03 | No 20-run browser-display p95 measurement; no five-second claim |
| AC04 | Dummy demo makes no external API calls and requires no microphone; browser/asset loading is local |
| AC05, AC17 | Browser pipeline is implemented and exercised with synthetic providers; real two-device HTTPS gate remains open |
| AC06 | No external provider or capture started on application startup; separate explicit controls |
| AC07 | Resource cleanup, host presence and synthetic worker stop tested; physical driver behavior unverified |
| AC08–AC12 | Final drain, stale updates, translation failures, permissions and snapshots tested with fixtures |
| AC13 | Accelerated bounded queues/history tested; live 60-minute soak remains open |
| AC14–AC16 | DOM and end-to-end latency / old-version comparison unmeasured |
| AC18 | API response validation redacts input, secrets excluded from settings/JS/logs; staged source scanned before submission |

This preview intentionally requires stopping the microphone before changing its recognition language. A dropped audio connection requires the user to restart audio; control/caption connections reconnect automatically. Stop/start also serves as manual STT reconnect. The demo covers the regular synthetic sequence; a selectable full failure-scenario library is not present. The old raw-PCM clients remain reference files; short-lived legacy bearer adapters and distributed-host GUI operation are not implemented. Input schemas are exported from Pydantic; a fully generated WebSocket-event type pipeline is not claimed.

Signed distribution, real API quota/authentication verification, ngrok account-specific publication, a clean Windows VM, two-device microphone permissions/audio and measured performance are pending release gates. The ZIP is an **unsigned preview**, not a claim that every P0–P5 production gate in the design has passed. No signing certificate, provider account, or ngrok contract was created or changed automatically.
