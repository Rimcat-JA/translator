# Agent entry point

Use this directory when an AI agent operates or changes Translator.
Start with one relevant command definition. Do not automate host UI clicks.

```sh
uv run --locked translator agent catalog --command runtime-start
```

`catalog --command` returns only the selected command. Replace `runtime-start`
with the operation you need, such as `session-create` or `settings-set`. Omit
`--command` only to discover the complete catalog. No running Runtime is required.
[agent-interface.json](../../contracts/agent-interface.json) is the
checked-in command contract. [index.json](index.json) is the document map.
The [result schema](../../contracts/agent-result.schema.json) defines the response
envelope. [llms.txt](../../llms.txt) provides a compact repository entry point.

## Choose one task

| Task | Read next |
|---|---|
| Start, run a local demo, and stop | [Quickstart](quickstart.md) |
| Configure providers, start a real conversation, create an invitation | [Operations](operations.md) |
| Change or test the code | [Repository map](repository-map.md) |
| Recover from a command error or unexpected state | [Troubleshooting](troubleshooting.md) |
| Help a person use the browser interface | [Human guide](../../README.md#人がブラウザで操作する場合) |
| Understand Runtime and audio ownership | [Architecture](../architecture.md) |
| Check which release gates were actually tested | [Verification record](../verification.md) |

## Read every response the same way

Each agent command writes one JSON object to stdout:

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "catalog",
  "data": {},
  "error": null
}
```

The empty `data` above illustrates the envelope only. Actual data is defined by
the command. A failed response has `ok: false`, `data: null`, and
`error: {code, message, retryable, next_action}`.

| Exit code | Meaning | Next step |
|---|---|---|
| `0` | Command succeeded | Read `data` |
| `2` | Input is invalid | Fix the input using `catalog` |
| `3` | Runtime discovery or authentication failed | Check `status` for the same data directory |
| `4` | Requested operation failed | Read the error and inspect relevant state |

Proceed only when the exit code is `0` and `ok` is `true`. A timeout does not prove
that a state change failed. Inspect state before repeating it. The CLI does not
automatically retry mutations. A request ID is not a general exactly-once guarantee.

Use `status` for routine checks. It excludes transcript text. When captions are
needed, use `session-snapshot --limit` with an integer from 1 to 100. The default
is 10. Its data includes `caption_count` and `captions_truncated` so you can detect
that only part of the in-memory history was returned.

## Scope

The host Runtime can be managed without opening its React interface. B microphone
capture and A-audio playback still run in B's browser after a user action.
The app provides B speech to translated captions and A PC audio to B playback.
It does not synthesize translated speech or provide two-way speech translation.

These instructions target the current source checkout. The Windows package tagged
`v0.2.0-preview.1` does not include the agent CLI. Its human interface remains
available through the existing release guide.

A newly built package includes `TranslatorAgent.exe` for console JSON output.
Its command form is `TranslatorAgent.exe agent COMMAND`. Keep the `agent` word.
Do not assume this executable exists in an older downloaded ZIP.
