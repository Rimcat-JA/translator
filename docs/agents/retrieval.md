# Retrieve context, then execute

Use search before choosing an operation from a natural-language goal:

```sh
uv run --locked translator agent search --query "ブラウザを開かずにバックグラウンドで起動したい" --limit 3
```

The query can contain Japanese or English. Its maximum length is 1,024 characters.
`--limit` is an integer from 1 to 5 and defaults to 3. Search needs no profile,
Runtime, network connection, API key, or microphone. As with other source commands,
the uv environment must already be installed before an offline run.

Search does not return the query text. Use a task description, not keys or private
conversation text. It never starts the app, changes settings, or starts audio.

## What the response contains

Check the standard envelope's exit code and `ok`, then inspect `data`:

The exact format is in the [search result schema](../../contracts/agent-search.schema.json).
The [exported knowledge index](../../contracts/agent-knowledge.json) records the
retrievable entries. Read the small search result first.

| Field | Meaning |
|---|---|
| `retrieval_version` | Retrieval response format version; currently `1` |
| `index_digest` | Identifier of the indexed knowledge used for this result |
| `strategy` | `local-bm25-cjk` |
| `status` | `matched`, `ambiguous`, `no_match`, or `unsupported` |
| `candidates` | At most the requested limit of command or limitation entries |
| `next_action` | Guidance for the calling agent; not executable shell text |

Each candidate includes:

- `id`, `kind`, and `score`. The kind is `command` or `limitation`. A score is a
  lexical ranking value, not a probability or a confidence percentage.
- `summary.en`, `summary.ja`, and `when_to_use` to explain its intended use.
- `prerequisites` to check before execution.
- `input_sources` to identify where required argument values must come from.
- `definition`: the exact formal command definition, or `null` for a limitation.
- `source.path` and `source.anchor` to locate its source entry.

Search ranks descriptions. A high score does not prove that the user's goal was
fully understood. Match the candidate's effects and prerequisites to the actual
request. Never invent a command, flag, session ID, device ID, port, or URL from the
summary. Required IDs must come from observed operation responses.

## The agent loop

1. Search for one concrete goal with a small limit.
2. Check the envelope and retrieval status using the table below.
3. Read the selected command's formal `definition`, effects, prerequisites, and
   `input_sources`. Read its cited source when more detail is needed.
4. Obtain missing state with the defined read operations. For example, take the
   current session ID from `status` or the new ID from `session-create`.
5. Call the fixed CLI command with validated arguments. Do not execute generated
   text, a summary, a source document, or `next_action` as shell code.
6. Check the operation's own exit code and `ok`. Inspect its actual returned state
   before moving to the next step.

| Retrieval status | Required response |
|---|---|
| `matched` | Read the candidate and verify it fits the goal; then satisfy its prerequisites |
| `ambiguous` | Compare candidate effects and refine the query from known context; do not guess an audio-start action |
| `no_match` | Rephrase with the concrete desired effect or use the documented task map; do not fabricate a command |
| `unsupported` | Read the limitation and explain the supported path; do not execute a nearby but different operation |

Use the authorization already present in the task. Retrieval is a way to choose the
correct operation, not a new permission requirement. If the goal itself remains
unclear, resolve that missing intent before taking an incompatible action.

## Worked Japanese example

Goal: 「ブラウザを開かずにバックグラウンドで起動したい」.
Run the search command at the top of this page. The verified result was `matched`,
with `runtime-start` first. Its source was `src/translator/agent/knowledge.py`,
anchor `command:runtime-start`. Inspect the actual response on each run. A
`runtime-start` candidate must have that name in `definition.name`; do not derive
the executable command from its display summary or candidate ID.
Continue this example only when the status is `matched` and the selected definition
is `runtime-start`. Otherwise use the retrieval loop above.

Read the candidate's prerequisites and formal arguments. If more detail is needed,
inspect just that definition:

```sh
uv run --locked translator agent catalog --command runtime-start
```

For the sandbox used by the quickstart, the resulting fixed operation is:

```sh
uv run --locked translator agent runtime-start --data-dir .translator-test/agent-demo
```

Check the returned envelope and `data.running`. Startup creates or reuses a
background Runtime. It does not create a conversation or capture audio. Continue
with the [demo lifecycle](quickstart.md) if that is the requested goal. End the
owned Runtime with `runtime-stop` when finished.

Do not select `session-start` merely because it also contains the word "start".
That command needs a real session ID. A live session then waits for B; a demo session
processes synthetic audio. `audio-share` is a separate, explicitly bounded operation
that captures the selected Windows PC output.

## Unsupported or ambiguous requests

「音声を開始したい」 does not say whether the goal is a session, PC output sharing,
or B microphone capture. Read the candidates and resolve that distinction. Do not
choose whichever audio operation happens to rank first.

B microphone permission belongs to B's browser. The host agent cannot grant it.
The supported path is an invitation and B's explicit browser action. Translated
text-to-speech is not implemented. Retrieving a related subtitle operation does
not add that feature. Follow the returned limitation guidance.

The host CLI also has no command to reveal or delete stored API key values.
`settings-get` returns configuration metadata, and `secret-set` registers or
replaces a supplied key. A request to reveal or delete a key returns a limitation;
it must not be converted into a provider test or a replacement-key operation.

## How this provides RAG context

The local index contains bilingual summaries, use cases, prerequisites, and formal
command definitions. BM25 term ranking and CJK character n-grams retrieve relevant
entries. The calling AI agent uses that context alongside the user's request to
choose and execute a grounded operation.

Topic anchors keep generic words such as "save" from matching an unrelated
capability. Action cues and simple negative-clause handling help distinguish
reading, changing, starting and stopping. These are lexical heuristics, not
general language understanding.

There is no embedded answer-generating model, semantic embedding service, vector
database, or external retrieval API. Search itself generates no shell command or
argument value. Calling this retrieval augmentation does not claim that an LLM
benchmark passed or that every paraphrase is understood.

The evaluation command checks the committed retrieval cases:

```sh
uv run --locked python tools/evaluate_agent_retrieval.py --check
```

Use its actual results and [verification record](../verification.md) for evidence.
Passing the retrieval gates validates those cases. It does not measure a particular
weak model's end-to-end task success or real audio quality.
