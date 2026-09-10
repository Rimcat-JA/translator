# Repository map for code changes

Read the smallest set of files that owns the requested behavior.
Do not scan legacy clients first when changing the current app.

| Change | Implementation entry point | Relevant checks |
|---|---|---|
| Goal retrieval, bilingual knowledge, ranking, limitation entries | `src/translator/agent/`; start with the retrieved `source.path` | Retrieval tests and `tools/evaluate_agent_retrieval.py --check` |
| Agent command names, arguments, JSON, exit codes | `src/translator/agent/`, `src/translator/cli.py` | `tests/test_agent.py`, `tests/test_agent_docs.py` |
| Startup, background ownership, ports, shutdown | `src/translator/runtime.py`, `src/translator/instance.py`, `src/translator/cli.py` | `tests/test_runtime.py`, `tests/test_cli.py`, `tools/smoke_runtime.py` |
| Named management operations and REST authorization | `src/translator/api/apps.py`, `src/translator/api/auth.py` | `tests/test_api.py`, `tests/test_contracts.py` |
| Session state, captions, translation scheduling | `src/translator/sessions/service.py` | `tests/test_sessions.py`, `tests/test_api.py` |
| Gladia, DeepL, dummy providers and languages | `src/translator/providers/` | `tests/test_providers.py` |
| Native PC audio, headless ownership, TRN1 frames | `src/translator/audio/`, `src/translator/agent/audio.py` | `tests/test_audio.py`, `tests/test_agent_ownership.py` |
| Settings and secret storage | `src/translator/config.py`, `src/translator/secrets.py` | `tests/test_config.py` |
| Tunnel lifecycle | `src/translator/remote/ngrok.py`, `src/translator/runtime.py` | Runtime/API tests; explicit external test only when needed |
| Human screens and browser microphone/playback | `frontend/src/App.tsx`, `frontend/src/client.ts`, `frontend/src/model.ts`, `frontend/src/audio/` | Vitest, typecheck, Playwright |
| UI assets and Windows packaging | `src/translator/assets.py`, `tools/build_frontend.py`, `packaging/translator.spec`, `tools/package_release.py` | Build, source smoke, frozen smoke |
| CI and generated contracts | `.github/workflows/ci.yml`, `tools/export_contracts.py`, `tools/export_agent_docs.py`, `contracts/` | `tests/test_contracts.py`, `tests/test_agent_docs.py`, affected CI jobs |

The agent module and tests may be split as the CLI grows. Start with `agent search`
for the task. Its source references locate the indexed knowledge. Use
`agent catalog --command NAME` for the selected operation. This catalog and the
checked-in [agent contract](../../contracts/agent-interface.json) are the public
interface. Do not use a file name as an API guarantee.

## Working loop

1. Read `git status --short`. Preserve unrelated changes.
2. Search for the goal, then read the task's row above and the relevant contract.
3. Make the smallest complete change.
4. Run the targeted checks. Use fixtures and dummy providers by default.
5. If the CLI changed, regenerate its contract and check for unintended drift.
6. Update the corresponding agent document.
7. Report changed behavior, check results, and remaining validation limits.
8. Commit or publish when that action is in the user's authorized scope.

When the agent interface or document map changes, regenerate and verify it:

```sh
uv run --locked python tools/export_agent_docs.py
uv run --locked python tools/export_agent_docs.py --check
uv run --locked python tools/evaluate_agent_retrieval.py --check
```

The exporter updates `contracts/agent-interface.json`,
`contracts/agent-result.schema.json`, `contracts/agent-knowledge.json`,
`contracts/agent-search.schema.json`, `docs/agents/index.json`, and `llms.txt`.
Do not edit these generated files by hand.
For Pydantic HTTP-input changes, use `tools/export_contracts.py` and its `--check`
mode instead.

When changing retrieval knowledge or ranking, inspect the evaluator's actual
failures. Preserve the distinction between matched, ambiguous, unsupported, and
unmatched goals. Do not make every query return a runnable command to improve a
ranking metric. The evaluator checks retrieval cases, not a model performance claim.

## Commands from the repository root

The commands below work in PowerShell and a POSIX shell.

```sh
uv sync --locked
uv run --locked pytest tests/test_agent.py tests/test_agent_docs.py
uv run --locked pytest tests/test_sessions.py
uv run --locked pytest tests/test_api.py tests/test_config.py
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend test
uv run --locked python tools/build_frontend.py
```

For a broader change, run the full suite:

```sh
uv run --locked pytest
uv run --locked python tools/smoke_runtime.py
npm --prefix frontend exec playwright install chromium
npm --prefix frontend run test:e2e
```

Playwright starts its own isolated Runtime. Its injected providers and microphone
are synthetic. It does not call paid APIs. Installing the browser is a development
step, not part of ordinary app startup.

For Windows package changes, run on Windows:

```sh
uv run --locked pyinstaller --noconfirm packaging/translator.spec
uv run --locked python tools/smoke_runtime.py --exe dist/Translator-windows-x64/Translator.exe
uv run --locked python tools/package_release.py
```

## Ownership boundaries

- `src/translator/resources/web/` and `frontend/dist/` are generated. Edit
  `frontend/src/`, then build.
- `uv.lock` and `frontend/package-lock.json` are the dependency locks. Keep them
  consistent with their manifests.
- `client_a/`, `client_b/`, `web_a/`, `web_b/`, `server/main.py`, and `shared/`
  contain legacy code. Their old unauthenticated clients cannot operate the new
  Runtime. Do not restore unauthenticated compatibility paths.
- `docs/verification.md` describes evidence for releases. Passing a mock or
  synthetic test does not establish real API, real audio, or remote-device support.
