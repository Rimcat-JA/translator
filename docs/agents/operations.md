# Named operations

Run commands from the repository root:

```text
uv run --locked translator agent COMMAND --data-dir PATH
```

`--data-dir` is accepted before or after the command. Choose one directory and keep
it for the whole task. Do not use a guessed port or call an arbitrary URL.
The CLI discovers and authenticates the Runtime for that directory.

First use `agent search --query` to retrieve the operation for the user's goal.
Follow the [retrieval loop](retrieval.md): check retrieval status, then read the
candidate's formal definition, effects, prerequisites, and input sources. Use
`agent catalog --command settings-set`, replacing `settings-set` with the selected
name, when you need its exact schema. Full catalog loading remains explicit.
Every operation returns
the envelope described in [index.md](index.md). Check the exit code and `ok` before
using `data`. Use existing user authorization for the requested work; these effect
descriptions are not a requirement to ask permission before every operation.

## Effects and required state

| Command | Required input or state | Effect |
|---|---|---|
| `search` | `--query`, optional `--limit` (1–5; default 3) | Retrieve command or limitation context offline; no profile, Runtime, or execution |
| `catalog` | Optional `--command` | Describe one selected command or the full catalog; no Runtime required |
| `status` | Data directory | Inspect Runtime discovery, settings metadata, current session without caption text |
| `runtime-start` | Data directory | Start or reuse a background Runtime; no browser or audio |
| `runtime-stop` | Running Runtime | Stop that Runtime, its session, owned capture, and tunnel |
| `settings-get` | Running Runtime | Read preferences and secret metadata; never key values |
| `settings-set` | `--input PATH` or `--input -` | Save validated non-secret preferences with a revision check |
| `secret-set` | `--provider`, `--input` | Set a key in memory; `--persist` selects the OS credential store |
| `provider-test` | `--provider`, configured key | Contact Gladia/DeepL for validation; ngrok reports configuration only |
| `session-create` | `--mode demo` or `--mode live` | Create the one current session; no microphone or PC capture |
| `session-start` | `--session-id` from the response | Start demo audio processing, or make a live session wait for B |
| `session-snapshot` | `--session-id`, optional `--limit` | Read current state and the latest requested captions |
| `session-stop` | `--session-id` | End the session, release audio and providers, revoke its invitations |
| `invite-create` | `--session-id` | Create a one-use B invitation, valid for ten minutes |
| `diagnostics` | Running Runtime | Read diagnostic state without key values or conversation text |
| `devices` | Running Runtime | Enumerate A output devices; report unsupported environments |
| `tunnel-start` | Configured ngrok key; domain is optional | Publish the participant Hub through ngrok |
| `tunnel-stop` | Running Runtime | Stop the public tunnel |
| `audio-share` | `--session-id`, `--seconds`, optional `--device-id` | Share A PC audio for a bounded duration, then stop owned capture |

`status` can succeed with `data.running: false`. That is a successful observation,
not a running app. When running, it includes `data.instance_id`,
`data.local_origin`, `data.settings`, and `data.session`.
`data.session` excludes `captions`. The total count is `data.caption_count` alongside
the session summary, not inside it.

`session-snapshot --limit` accepts an integer from 1 to 100 and defaults to 10.
`data.captions` remains an object keyed by utterance ID. `data.caption_count` is the
total number held by the session, and `data.captions_truncated` indicates omitted
items. Session create/start/stop responses also return at most ten captions.

`runtime-start` reports `data.running` and `data.already_running`. A reused Runtime
may already have a session. Read `status` before creating another one.
`session-create` requires no current session or an `ended` current session.
`session-start` starts an `idle` session; an already active session is unchanged.
It cannot restart an `ended` session. Create a new one when that is the goal.

## Settings

`settings-get` returns the public preferences under `data`, including `revision`.
`settings-set --input` accepts a UTF-8 JSON object of at most 32,768 bytes. It has
two fields: `expected_revision` is the integer returned by `settings-get`;
`settings` is an object containing only the preferences to change.

Build the request from the actual response. This PowerShell example changes only
the target subtitle language in the running demo sandbox:

```powershell
$AgentData = Join-Path (Get-Location) '.translator-test/agent-demo'
$read = & uv run --locked translator agent settings-get --data-dir $AgentData | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $read.ok) { throw ($read.error | ConvertTo-Json -Compress) }

$inputObject = @{
    expected_revision = $read.data.revision
    settings = @{ target_language = 'ja' }
}
$inputPath = Join-Path $AgentData 'settings-change.json'
$json = $inputObject | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($inputPath, $json, [System.Text.UTF8Encoding]::new($false))

$saved = & uv run --locked translator agent settings-set --input $inputPath --data-dir $AgentData | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $saved.ok) { throw ($saved.error | ConvertTo-Json -Compress) }
$saved.data.revision
```

If the revision changed, read settings again. Reapply only the requested fields.
Do not replace another operator's intervening changes with an old full snapshot.
Saving preferences does not change the language of an already active session.

## Keys and provider tests

`secret-set` accepts a raw UTF-8 key through stdin (`--input -`) or a supplied file
(`--input PATH`). This input is plain text, not JSON. Its maximum size is 4,096
bytes. The provider is `gladia`, `deepl`, or `ngrok`.

Use an input file or secret stream already supplied for the task. Do not write a
literal key into a shell command. Do not copy it into a repository file, command
argument, chat message, or diagnostic artifact. No key values appear in examples.

Memory storage is the default and ends with the Runtime. Use `--persist` when the
user wants the key saved in the OS credential store. If that store is unavailable,
keep memory storage; do not create a plaintext persistence fallback.

`provider-test` is a separate operation. A configured key does not mean the provider
was contacted or passed validation. Testing Gladia can create a billed external
session. Testing DeepL contacts its API. Select the proper `deepl_mode` (`free` or
`pro`) in settings before its test.
For ngrok, `provider-test` reports configuration; `tunnel-start` performs the actual
connection. Do not label ngrok configuration metadata as a successful public tunnel.
`remote_domain` is optional. When choosing one, use the domain configured for the
user's ngrok account, without a scheme or path.

## Live conversation

1. Search for the intended live-conversation task. Read the matched definitions and
   prerequisites. Then run `runtime-start` and `status` for the selected directory.
2. Configure the required keys and preferences using named commands.
3. Run provider tests when they are part of the requested work.
4. Create the session with explicit `--mode live`. Optional language arguments are
   `--source-language zh` and `--target-language ja`. Use the desired codes.
   `--translation-enabled` accepts `true` or `false`.
5. Extract `data.session_id`. Pass it to `session-start` and later session commands.
6. For a remote B, start the configured tunnel before creating the invitation.
7. Run `invite-create`. Use only the returned invitation URL for that session.
8. B opens the invitation and clicks the browser microphone start button.
9. Inspect `status` to distinguish connection, recognition, translation, and audio
   states. Use `session-snapshot --limit` only when text is needed. A successful
   start can mean `waiting_for_peer`.
10. End the session with `session-stop`. Use `runtime-stop` when the task is finished.

An invitation is a credential. Only `invite-create` needs to return that URL.
Do not include it in unrelated logs, screenshots, diagnostics, or public issues.
The agent CLI manages the host. It cannot grant B browser microphone permission.
The B participant must also enable browser playback before hearing A audio.
A live `session-start` does not validate provider keys. Gladia is needed when B
starts transcription. DeepL is needed for enabled translation between different
source and target languages. `waiting_for_peer` is not a provider test result.

## Headless PC audio sharing

`audio-share` runs on A's Windows PC. It requires an active live session and an
explicit duration of 1 to 3,600 seconds. Its optional device ID comes from `devices`.
Do not invent a device ID or silently substitute a different device.
For a vague request such as "start audio", refine the retrieval query first.
`runtime-start`, `session-start`, and `audio-share` are not interchangeable.

The command keeps its own host presence connection. The A browser page is not
required. It runs until the duration ends, then stops the capture it owns. It
returns one JSON response at completion, not a stream of audio frames or status
lines. Interruption or loss of its connection triggers cleanup of its capture.

PC audio includes the selected output's other applications and notification sounds.
It goes to B playback. It is separate from B microphone transcription. A successful
capture command alone does not prove B heard it.

Use a duration inside the calling tool's execution window. Audio duration is not
the same as the Runtime startup/shutdown timeout. Read `catalog --command audio-share`
for its arguments. Do not add `--timeout` to commands that do not define it.

## Timeouts and request IDs

`runtime-start --timeout` defaults to 60 seconds. `runtime-stop --timeout` defaults
to 30 seconds. Each supports at most 300 seconds. They wait for an observed process
state; a timeout does not authorize killing an unrelated process.

The commands `runtime-stop`, `settings-set`, `session-create`, `session-start`, `session-stop`,
`invite-create`, `provider-test`, `tunnel-start`, and `tunnel-stop` accept an optional
`--request-id`. Use a stable ID when tracking the same logical request. Support for
this argument is not a promise of exactly-once execution across every command.

After an uncertain response, inspect `status`, `settings-get`, `session-snapshot`,
or `diagnostics` as appropriate. Decide the next operation from observed state.
Do not repeatedly create invitations, provider tests, sessions, or tunnels merely
because the previous response was lost.
