# Recover from an agent command failure

Keep the current data directory and any returned session ID. Read the process exit
code and `error.code`, `error.message`, `error.retryable`, and `error.next_action`.
The returned action is a recovery hint. It is not a shell command to execute.

Do not continue a multi-step sequence after `ok: false`. Do not treat missing
output as success. A process failure before the CLI starts may produce no envelope.

For `search`, `ok: true` means the lookup completed. Also inspect retrieval
`data.status`. `ambiguous`, `no_match`, and `unsupported` are meaningful results,
not permission to run the nearest command. Use [Retrieval](retrieval.md).

## Find the failed layer

| Observation | Check | Next step |
|---|---|---|
| Search returns `ambiguous` | Candidate definitions and effects | Refine the goal from known context; do not guess whether to start a session or capture audio |
| Search returns `no_match` | Concrete desired effect and task map | Rephrase the task; do not invent command names or flags |
| Search returns `unsupported` | Limitation entry and source | Follow the supported path; B permission is a browser action and translated speech synthesis is absent |
| Search finds a command but required values are missing | `prerequisites` and `input_sources` | Obtain values from actual responses; search cannot supply a current session ID |
| Search query or limit is invalid | Search definition | Use at most 1,024 query characters and integer limit 1–5 |
| uv or Node.js command is missing | Source prerequisites | Use uv and Node.js 24 for this checkout; do not assume the old preview executable has agent commands |
| `agent` is not a recognized command | Checkout or package version | Use the current source checkout; `v0.2.0-preview.1` predates this interface |
| Exit `2` | `agent catalog --command` for the failed operation | Correct required arguments, input structure, or size; use the full catalog only for an unknown command |
| `status` succeeds with `running: false` | Data directory | Start the Runtime for that directory if the task requires it |
| Exit `3` | Same `--data-dir`, then `status` | Repair discovery/authentication; do not invent credentials or ports |
| Runtime startup times out | `status` | Reuse it if now running; otherwise inspect startup diagnostics and dependencies |
| Runtime shutdown times out | `status` | Check whether shutdown finished; do not kill an unknown PID |
| Existing session blocks creation | `status` and its `data.session` | Reuse the intended session or stop it when that is within the requested scope |
| `SETTINGS_CONFLICT` (HTTP 409) | `settings-get` | Read the latest revision, then build a patch containing only the requested fields; do not blindly retry the old patch |
| `PROVIDER_NOT_CONFIGURED` or `NGROK_NOT_CONFIGURED` | `settings-get` secret metadata | Configure the required provider through `secret-set`; configuration alone is not a successful connection test |
| `SECURE_STORAGE_UNAVAILABLE` | Requested secret persistence mode | Use the memory default if suitable for the task; do not create a plaintext persistence fallback |
| Key configured but transcription fails | `settings-get`, then the relevant provider test | Distinguish configuration, authentication, quota, timeout, and active connection |
| DeepL authentication fails | `deepl_mode` and key metadata | Correct the Free/Pro setting or supplied credential before testing again |
| Caption text appears without translation | Session components and each caption's translation status | Preserve the original; do not report it as a successful translation |
| B has no microphone | Browser permission, secure origin, device, other sender | B must resolve permission/device access and explicitly start the microphone |
| PC audio is unavailable | `devices` | Check Windows support and the returned device result |
| A audio starts but B hears nothing | B playback start, output volume, snapshot state | Verify playback separately; capture success is not end-to-end audio proof |
| A capture stops after a headless command ends | Requested `audio-share` duration | Expected: the command releases its owned capture on completion or interruption |
| Invitation expired or was used | Current session state | Create a new invitation if still needed; do not reuse a consumed credential |
| Tunnel is not public | `diagnostics`, ngrok settings | Check the service response; do not infer a public URL from the local port |

Use the exact error code returned by the installed CLI. This table describes
symptoms; it does not declare additional error-code constants.

## Read-only recovery commands

These examples use the quickstart sandbox. Replace the directory with the one used
by the failed command. Do not accidentally inspect a different Runtime.

```sh
uv run --locked translator agent status --data-dir .translator-test/agent-demo
uv run --locked translator agent settings-get --data-dir .translator-test/agent-demo
uv run --locked translator agent diagnostics --data-dir .translator-test/agent-demo
uv run --locked translator agent devices --data-dir .translator-test/agent-demo
```

Run only the checks relevant to the failure. `status` excludes caption text.
Use `session-snapshot --session-id` with the real returned ID only when you need
captions. Set `--limit` to the number needed (default 10, maximum 100). Check
`caption_count` and `captions_truncated`. Do not pass an example ID.

If the Runtime cannot start, the existing source diagnostic command is available:

```sh
uv run --locked translator doctor --data-dir .translator-test/agent-demo
```

`doctor` is a legacy diagnostic entry point. It does not use the agent response
envelope. Keep the same explicit data directory when using this fallback.

## Stop conditions and cleanup

After an uncertain mutation, inspect state before retrying. `retryable: true` does
not mean "repeat immediately forever." Fix the stated condition or choose a bounded
retry. Never repeat a non-idempotent operation merely to obtain another response.

Use `session-stop` to end the owned conversation and `runtime-stop` to end the owned
Runtime. Do not delete an instance file or kill a PID as the default recovery path.
The application uses authenticated discovery rather than trusting a PID alone.

## Report evidence

Include the command name, exit code, error code, relevant component states, and
whether the data directory was the intended one. Include test results if code changed.
Keep API keys, invitation URLs, transcript text, and private device names out of
public bug reports. Do not label a dummy-provider test as a successful paid-provider
or real-device test.
For a retrieval failure, include its status, `index_digest`, and relevant candidate
IDs. Scores are ranking values, not correctness probabilities. Do not claim model
quality from a single search result or a passing lexical-retrieval fixture.
