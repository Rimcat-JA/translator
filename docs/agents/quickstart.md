# First run: a local demo

Run from the repository root. Use an explicit data directory under
`.translator-test/` so this walkthrough does not reuse normal application settings.
Keep the same directory in every command.

Prerequisites: uv, Node.js 24, and npm. uv manages the repository's Python version.
The first dependency installation and UI build need network access. After setup,
the demo's recognition and translation pipeline runs locally.

```sh
uv sync --locked
uv run --locked translator agent catalog --command runtime-start
```

The published `v0.2.0-preview.1` executable cannot run this workflow. Use source.

## PowerShell

The helper below rejects a failed envelope before another operation starts.
It never constructs a URL or assumes a port. The Runtime chooses its own ports.

```powershell
$AgentData = Join-Path (Get-Location) '.translator-test/agent-demo'

function Invoke-TranslatorAgent {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $raw = & uv run --locked translator agent @Arguments --data-dir $AgentData
    $exitCode = $LASTEXITCODE
    $result = $raw | ConvertFrom-Json
    if ($exitCode -ne 0 -or -not $result.ok) {
        throw ($result.error | ConvertTo-Json -Compress)
    }
    return $result.data
}

Invoke-TranslatorAgent runtime-start | Out-Null
$created = Invoke-TranslatorAgent session-create --mode demo --source-language zh --target-language ja
$SessionId = $created.session_id
if (-not $SessionId) { throw 'session-create returned no session_id' }

Invoke-TranslatorAgent session-start --session-id $SessionId | Out-Null
Start-Sleep -Seconds 4
$snapshot = Invoke-TranslatorAgent session-snapshot --session-id $SessionId --limit 3
$snapshot | ConvertTo-Json -Depth 12

Invoke-TranslatorAgent session-stop --session-id $SessionId | Out-Null
Invoke-TranslatorAgent runtime-stop | ConvertTo-Json
```

`runtime-start` starts a background process and waits for it to become ready.
It opens no browser and starts no session, microphone, or PC audio capture.
`session-create` creates the selected mode. `session-start` starts the local demo.
The four-second wait lets synthetic audio produce captions. Inspect actual
`data.captions`; do not assume a successful start already means captions exist.
This example requests at most three captions. `caption_count` reports the total
held in memory; `captions_truncated` reports whether some were omitted. Use `status`
instead when you need session state without any caption text.

If any step fails, stop following the sequence. Read
[troubleshooting.md](troubleshooting.md). Use the same data directory to inspect
state. If the Runtime was already running in this directory, `runtime-start`
reuses it. It does not promise an empty session.

## POSIX shell

This uses Python to parse JSON and preserve returned IDs. It calls the same CLI.

```sh
uv run --locked python - <<'PY'
import json
from pathlib import Path
import subprocess
import time

directory = str(Path('.translator-test/agent-demo').resolve())

def agent(*args):
    process = subprocess.run(
        ['uv', 'run', '--locked', 'translator', 'agent', *args,
         '--data-dir', directory],
        capture_output=True, text=True, encoding='utf-8', check=False,
    )
    result = json.loads(process.stdout)
    if process.returncode != 0 or not result['ok']:
        raise RuntimeError(result.get('error'))
    return result['data']

agent('runtime-start')
created = agent('session-create', '--mode', 'demo',
                '--source-language', 'zh', '--target-language', 'ja')
session_id = created['session_id']
agent('session-start', '--session-id', session_id)
time.sleep(4)
snapshot = agent('session-snapshot', '--session-id', session_id, '--limit', '3')
print(json.dumps({'status': snapshot['status'], 'captions': snapshot['captions']},
                 ensure_ascii=False))
agent('session-stop', '--session-id', session_id)
agent('runtime-stop')
PY
```

The script prints demo captions to the terminal. Do not adapt it to persist real
conversation text unless the user requested that recording.

## Continue

For an actual conversation, use [Operations](operations.md). Select `--mode live`
explicitly and configure the required providers. Starting a live session does not
grant B browser microphone permission or start A PC audio sharing.
