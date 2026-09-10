"""Generate agent discovery artifacts and check that local documentation links resolve."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from translator.agent.commands import catalog

ROOT = Path(__file__).resolve().parents[1]
PAGES = [
    {"path": "AGENTS.md", "task": "Repository entry and operating rules"},
    {"path": "docs/agents/index.md", "task": "Choose one short document"},
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


def artifacts():
    commands = catalog()
    navigation = {"schema_version": 1, "entrypoint": "AGENTS.md",
                  "preferred_interface": "uv run --locked translator agent",
                  "command_catalog": "contracts/agent-interface.json",
                  "result_schema": "contracts/agent-result.schema.json",
                  "pages": PAGES,
                  "commands": [{"name": item["name"], "summary": item["summary"],
                                "read": "docs/agents/operations.md"}
                               for item in commands["commands"]]}
    def encode(value):
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    llms = "# Translator\n\n> Agent-operated translated captions and bounded PC audio sharing.\n\n"
    llms += "Start with `uv run --locked translator agent catalog --command runtime-start`.\n"
    llms += "Read one command at a time; omit `--command` only when the full catalog is needed.\n"
    llms += "Use an explicit data directory. Read `ok` before continuing. Session IDs come from results.\n\n"
    llms += "## Task documents\n\n"
    llms += "".join(f"- [{page['task']}]({page['path']})\n" for page in PAGES)
    llms += "\n## Machine-readable indexes\n\n"
    llms += "- [Document index](docs/agents/index.json)\n"
    llms += "- [Command catalog](contracts/agent-interface.json)\n"
    llms += "- [Result JSON Schema](contracts/agent-result.schema.json)\n"
    return {"contracts/agent-interface.json": encode(commands),
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
