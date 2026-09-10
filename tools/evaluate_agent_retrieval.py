"""Offline command-retrieval regression, not a language-model capability benchmark.

The bilingual fixture was written from command contracts before inspecting the
retrieval knowledge examples. Reviewer cases are a separate paraphrase slice,
not an untouched holdout once failures have been inspected during development.
No command execution, runtime, provider connection, or model call is involved.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable


DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "agent_retrieval_eval.json"
STATUSES = {"matched", "ambiguous", "no_match", "unsupported"}


def load_fixture(path: Path) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("The fixture must be a non-empty array.")
    queries = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"query", "expected_status", "expected_command", "partition"}:
            raise ValueError("Invalid fixture row fields.")
        query = row.get("query")
        status = row.get("expected_status")
        if (not isinstance(query, str) or not query.strip() or query in queries
                or not isinstance(status, str) or status not in STATUSES):
            raise ValueError("Fixture queries must be unique, non-empty strings with a valid expected status.")
        if ((status == "matched" and not (isinstance(row.get("expected_command"), str) and row["expected_command"]))
                or (status != "matched" and "expected_command" in row)):
            raise ValueError("Only matched rows must specify an expected command.")
        if row.get("partition", "primary") not in {"primary", "reviewer"}:
            raise ValueError("Invalid fixture partition.")
        queries.add(query)
    return rows


def _metric(correct: int, total: int) -> dict:
    return {"correct": correct, "total": total, "accuracy": correct / total if total else None}


def evaluate(rows: list[dict], commands: list[dict], search: Callable) -> dict:
    # Freeze the authority before calling the engine: mutation of the supplied
    # command objects must not make an invented definition look authentic.
    definitions = {item["name"]: deepcopy(item) for item in commands}
    unknown = {row["expected_command"] for row in rows if row.get("expected_command")} - definitions.keys()
    if unknown:
        raise ValueError("Fixture contains commands absent from COMMANDS: " + ", ".join(sorted(unknown)))
    totals = Counter()
    partitions = {}
    failures = []
    for number, row in enumerate(rows, 1):
        reasons = []
        candidate_names = []
        actual_status = None
        contract_ok = True
        try:
            result = search(row["query"], commands, limit=3)
            if not isinstance(result, dict) or result.get("status") not in STATUSES:
                raise ValueError("invalid_status")
            actual_status = result["status"]
            candidates = result.get("candidates")
            if not isinstance(candidates, list) or len(candidates) > 3:
                raise ValueError("invalid_candidates")
            seen = set()
            for candidate in candidates:
                if (not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str)
                        or not candidate["id"] or candidate["id"] in seen):
                    raise ValueError("invalid_candidate_id")
                seen.add(candidate["id"])
                if candidate.get("kind") == "command":
                    name = candidate["id"]
                    if name not in definitions or candidate.get("definition") != definitions[name]:
                        raise ValueError("inexact_command_definition")
                    candidate_names.append(name)
                elif (candidate.get("kind") == "limitation" and "definition" in candidate
                      and candidate["definition"] is None):
                    candidate_names.append(None)
                else:
                    raise ValueError("invalid_candidate_kind")
            if actual_status == "no_match" and candidates:
                raise ValueError("no_match_has_candidates")
            if actual_status in {"matched", "unsupported", "ambiguous"} and not candidates:
                raise ValueError("status_requires_candidates")
            if actual_status == "matched" and candidates[0]["kind"] != "command":
                raise ValueError("matched_requires_command")
            if actual_status == "unsupported" and candidates[0]["kind"] != "limitation":
                raise ValueError("unsupported_requires_limitation")
        except Exception as error:
            contract_ok = False
            # Report the exception class, never arbitrary exception payloads.
            reasons.append("response_contract:" + type(error).__name__)
        status_ok = actual_status == row["expected_status"]
        if not status_ok:
            reasons.append("status")
        totals["all"] += 1
        totals["status"] += status_ok
        totals["contract"] += contract_ok
        if row["expected_status"] == "matched":
            expected = row["expected_command"]
            top1 = contract_ok and bool(candidate_names) and candidate_names[0] == expected
            top3 = contract_ok and expected in candidate_names[:3]
            totals["clear"] += 1
            totals["top1"] += bool(top1)
            totals["top3"] += top3
            partition = partitions.setdefault(row.get("partition", "primary"), Counter())
            partition["clear"] += 1
            partition["top1"] += bool(top1)
            partition["top3"] += top3
            if not top1:
                reasons.append("top1")
            if not top3:
                reasons.append("top3")
        else:
            totals["special"] += 1
            totals["special_status"] += status_ok
        if reasons:
            failures.append({"case": number, "query": row["query"],
                             "expected_status": row["expected_status"],
                             "expected_command": row.get("expected_command"),
                             "actual_status": actual_status, "candidate_commands": candidate_names,
                             "reasons": reasons})
    metrics = {"top1": _metric(totals["top1"], totals["clear"]),
               "top3": _metric(totals["top3"], totals["clear"]),
               "status": _metric(totals["status"], totals["all"]),
               "special_status": _metric(totals["special_status"], totals["special"]),
               "response_contract": _metric(totals["contract"], totals["all"])}
    thresholds = {"top1": .9, "top3": 1.0, "status": .95, "special_status": 1.0,
                  "response_contract": 1.0}
    gates = {name: {"minimum": threshold, "passed": metrics[name]["accuracy"] is not None
                   and metrics[name]["accuracy"] >= threshold} for name, threshold in thresholds.items()}
    return {"schema_version": 1, "evaluation": "offline_command_retrieval_regression",
            "cases": len(rows), "clear_cases": totals["clear"], "special_cases": totals["special"],
            "metrics": metrics,
            "partitions": {name: {"top1": _metric(count["top1"], count["clear"]),
                                   "top3": _metric(count["top3"], count["clear"])}
                           for name, count in partitions.items()},
            "gates": gates, "passed": all(gate["passed"] for gate in gates.values()),
            "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--check", action="store_true", help="Exit 1 if any documented regression gate fails.")
    arguments = parser.parse_args(argv)
    try:
        from translator.agent.commands import COMMANDS
        from translator.agent.retrieval import search

        rows = load_fixture(arguments.fixture)
        summary = evaluate(rows, COMMANDS, search)
        summary["fixture_sha256"] = hashlib.sha256(arguments.fixture.read_bytes()).hexdigest()
    except (ImportError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"schema_version": 1, "passed": False, "error": type(error).__name__}, sort_keys=True))
        return 2
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 1 if arguments.check and not summary["passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
