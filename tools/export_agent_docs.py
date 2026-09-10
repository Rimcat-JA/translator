"""Generate agent discovery artifacts and check that local documentation links resolve."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from translator.agent.commands import catalog
from translator.agent.retrieval import knowledge_index

ROOT = Path(__file__).resolve().parents[1]
PAGES = [
    {"path": "AGENTS.md", "task": "Repository entry and operating rules"},
    {"path": "docs/agents/index.md", "task": "Choose one short document"},
    {"path": "docs/agents/retrieval.md", "task": "Search a goal and use the retrieved command specification"},
    {"path": "docs/agents/quickstart.md", "task": "Run an isolated offline demo from start to stop"},
    {"path": "docs/agents/operations.md", "task": "Settings, live sessions, invitations and headless audio"},
    {"path": "docs/agents/repository-map.md", "task": "Find implementation files and relevant checks"},
    {"path": "docs/agents/troubleshooting.md", "task": "Recover from a failed JSON command"},
    {"path": "contracts/implementation-api.md", "task": "Inspect authenticated HTTP and WebSocket protocols"},
    {"path": "docs/verification.md", "task": "Understand tested behavior and remaining hardware checks"},
]


def result_schema():
    error = {"type": "object", "additionalProperties": False,
             "required": ["code", "message", "retryable", "next_action"],
             "properties": {"code": {"type": "string"}, "message": {"type": "string"},
                            "retryable": {"type": "boolean"}, "next_action": {"type": "string"}}}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Translator agent command result v1", "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "ok", "command", "data", "error"],
            "properties": {"schema_version": {"const": 1}, "ok": {"type": "boolean"},
                           "command": {"type": "string"},
                           "data": {"type": ["object", "null"]},
                           "error": {"anyOf": [error, {"type": "null"}]}},
            "oneOf": [{"properties": {"ok": {"const": True}, "error": {"type": "null"}}},
                      {"properties": {"ok": {"const": False}, "data": {"type": "null"},
                                      "error": error}}]}


def search_schema(commands):
    """Schema for search data; executable definitions are the exact catalog values."""
    strings = {"type": "array", "items": {"type": "string"}}
    candidate = {"type": "object", "additionalProperties": False,
                 "required": ["id", "kind", "score", "summary", "when_to_use", "prerequisites",
                              "input_sources", "definition", "source"],
                 "properties": {
                     "id": {"type": "string"}, "kind": {"enum": ["command", "limitation"]},
                     "score": {"type": "number", "minimum": 0},
                     "summary": {"type": "object", "additionalProperties": False,
                                 "required": ["en", "ja"],
                                 "properties": {"en": {"type": "string"}, "ja": {"type": "string"}}},
                     "when_to_use": strings, "prerequisites": strings,
                     "input_sources": {"type": "object", "additionalProperties": {"type": "string"}},
                     "definition": {"anyOf": [{"$ref": "#/$defs/command"}, {"type": "null"}]},
                     "source": {"type": "object", "additionalProperties": False,
                                "required": ["path", "anchor"],
                                "properties": {"path": {"const": "src/translator/agent/knowledge.py"},
                                               "anchor": {"type": "string"}}}},
                 "allOf": [{"if": {"properties": {"kind": {"const": "command"}}},
                            "then": {"properties": {"definition": {"$ref": "#/$defs/command"}}},
                            "else": {"properties": {"definition": {"type": "null"}}}}]}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Translator search data v1 (inside the agent result envelope)",
            "type": "object", "additionalProperties": False,
            "$defs": {"command": {"enum": commands}, "candidate": candidate},
            "required": ["retrieval_version", "index_digest", "strategy", "status", "candidates", "next_action"],
            "properties": {"retrieval_version": {"const": 1},
                           "index_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                           "strategy": {"const": "local-bm25-cjk"},
                           "status": {"enum": ["matched", "ambiguous", "no_match", "unsupported"]},
                           "candidates": {"type": "array", "maxItems": 5,
                                          "items": {"$ref": "#/$defs/candidate"}},
                           "next_action": {"type": "string"}}}


def artifacts():
    commands = catalog()
    navigation = {"schema_version": 1, "entrypoint": "AGENTS.md",
                  "preferred_interface": "uv run --locked translator agent",
                  "primary_discovery": {"command": "search", "query_argument": "--query",
                                        "read": "docs/agents/retrieval.md"},
                  "command_catalog": "contracts/agent-interface.json",
                  "result_schema": "contracts/agent-result.schema.json",
                  "retrieval_index": "contracts/agent-knowledge.json",
                  "search_result_schema": "contracts/agent-search.schema.json",
                  "pages": PAGES,
                  "commands": [{"name": item["name"], "summary": item["summary"],
                                "read": "docs/agents/operations.md"}
                               for item in commands["commands"]]}
    def encode(value):
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    llms = "# Translator\n\n> Agent-operated translated captions and bounded PC audio sharing.\n\n"
    llms += 'Start with `uv run --locked translator agent search --query "your goal"`.\n'
    llms += "Check retrieval status, then read the candidate's exact definition, prerequisites and input sources.\n"
    llms += "Ambiguous, unsupported or unmatched retrieval is not permission to guess an operation.\n"
    llms += "Search is local BM25/CJK retrieval; it does not execute, call an LLM, or create a profile.\n"
    llms += "Use `catalog --command NAME` for a known operation; request full `catalog` only if needed.\n"
    llms += "Use an explicit data directory. Read `ok` before continuing. Session IDs come from results.\n\n"
    llms += "## Task documents\n\n"
    llms += "".join(f"- [{page['task']}]({page['path']})\n" for page in PAGES)
    llms += "\n## Machine-readable indexes\n\n"
    llms += "- [Document index](docs/agents/index.json)\n"
    llms += "- [Command catalog](contracts/agent-interface.json)\n"
    llms += "- [Retrieval knowledge index](contracts/agent-knowledge.json)\n"
    llms += "- [Search data JSON Schema](contracts/agent-search.schema.json)\n"
    llms += "- [Result JSON Schema](contracts/agent-result.schema.json)\n"
    return {"contracts/agent-interface.json": encode(commands),
            "contracts/agent-knowledge.json": encode(knowledge_index(commands["commands"])),
            "contracts/agent-search.schema.json": encode(search_schema(commands["commands"])),
            "contracts/agent-result.schema.json": encode(result_schema()),
            "docs/agents/index.json": encode(navigation), "llms.txt": llms}


def broken_links():
    failures = []
    paths = [ROOT / page["path"] for page in PAGES] + [ROOT / "README.md", ROOT / "llms.txt"]
    for path in paths:
        if not path.is_file():
            failures.append(f"Missing document: {path.relative_to(ROOT)}")
            continue
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            target = target.strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            destination = (path.parent / unquote(parsed.path)).resolve()
            if not destination.is_relative_to(ROOT) or not destination.exists():
                failures.append(f"Broken local link: {path.relative_to(ROOT)} -> {target}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failures = []
    for relative, expected in artifacts().items():
        path = ROOT / relative
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                failures.append(f"Stale artifact: {relative}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8", newline="\n")
    failures.extend(broken_links())
    if failures:
        print("\n".join(failures))
        print("Regenerate: uv run --locked python tools/export_agent_docs.py")
        return 1
    print("Agent catalog, result schema, indexes and document links match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
