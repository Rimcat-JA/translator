"""Check retrieval scoring independently, then exercise the frozen query fixture."""
from collections import Counter
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("evaluate_agent_retrieval", ROOT / "tools" / "evaluate_agent_retrieval.py")
EVALUATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATOR)


def test_fixture_has_bilingual_coverage_and_distinct_review_cases():
    from translator.agent.commands import COMMANDS

    rows = EVALUATOR.load_fixture(EVALUATOR.DEFAULT_FIXTURE)
    coverage = Counter(row.get("expected_command") for row in rows)
    assert set(coverage) - {None} == {item["name"] for item in COMMANDS}
    for command in COMMANDS:
        assert coverage[command["name"]] >= 2
        queries = [row["query"] for row in rows if row.get("expected_command") == command["name"]]
        assert any(query.isascii() for query in queries)
        assert any(not query.isascii() for query in queries)
    assert len([row for row in rows if row.get("partition") == "reviewer"]) >= 10
    assert {row["expected_status"] for row in rows} == EVALUATOR.STATUSES


def test_scoring_keeps_candidate_rank_and_status_separate():
    commands = [{"name": "first", "summary": "One"}, {"name": "second", "summary": "Two"}]
    def command(index):
        return {"id": commands[index]["name"], "kind": "command", "definition": commands[index]}
    responses = {
        "clear": {"status": "ambiguous", "candidates": [command(1), command(0)]},
        "unclear": {"status": "ambiguous", "candidates": [command(0), command(1)]},
        "unrelated": {"status": "no_match", "candidates": []},
        "not supported": {"status": "unsupported", "candidates": [
            {"id": "limitation", "kind": "limitation", "definition": None}]},
    }
    rows = [{"query": "clear", "expected_status": "matched", "expected_command": "first"},
            {"query": "unclear", "expected_status": "ambiguous"},
            {"query": "unrelated", "expected_status": "no_match"},
            {"query": "not supported", "expected_status": "unsupported"}]
    calls = []

    def search(query, definitions, *, limit):
        calls.append((definitions, limit))
        return responses[query]

    summary = EVALUATOR.evaluate(rows, commands, search)
    assert summary["metrics"]["top1"]["accuracy"] == 0
    assert summary["metrics"]["top3"]["accuracy"] == 1
    assert summary["metrics"]["status"]["accuracy"] == .75
    assert summary["gates"]["special_status"]["passed"]
    assert not summary["passed"]
    assert summary["failures"][0]["reasons"] == ["status", "top1"]
    assert calls == [(commands, 3)] * len(rows)


def test_correct_ranking_with_unusable_ambiguity_fails_status_gate():
    commands = [{"name": "first"}]
    rows = [{"query": "clear", "expected_status": "matched", "expected_command": "first"},
            {"query": "unrelated", "expected_status": "no_match"}]

    def search(query, *args, **kwargs):
        if query == "clear":
            return {"status": "ambiguous", "candidates": [
                {"id": "first", "kind": "command", "definition": commands[0]}]}
        return {"status": "no_match", "candidates": []}

    summary = EVALUATOR.evaluate(rows, commands, search)
    assert summary["gates"]["top1"]["passed"] and summary["gates"]["top3"]["passed"]
    assert summary["gates"]["special_status"]["passed"]
    assert not summary["gates"]["status"]["passed"] and not summary["passed"]


@pytest.mark.parametrize("status, kind", [
    ("matched", "limitation"), ("unsupported", "command"), ("no_match", "command"),
    ("matched", None), ("unsupported", None), ("ambiguous", None),
])
def test_status_and_candidate_contract_must_agree(status, kind):
    definitions = [{"name": "first"}]
    candidate = ({"id": "first", "kind": kind,
                  "definition": definitions[0] if kind == "command" else None} if kind else None)
    summary = EVALUATOR.evaluate([{"query": "query", "expected_status": "no_match"}], definitions,
                                 lambda *args, **kwargs: {"status": status,
                                                         "candidates": [candidate] if candidate else []})
    assert not summary["gates"]["response_contract"]["passed"]


@pytest.mark.parametrize("candidates", [
    [{"id": "first", "kind": "command", "definition": {"name": "first", "summary": "Invented"}}],
    [{"id": "unknown", "kind": "command", "definition": {"name": "unknown"}}],
    [{"id": "limitation", "kind": "limitation", "definition": {"name": "first"}}],
    [{"id": "first", "kind": "command", "definition": {"name": "first"}}] * 2,
])
def test_scoring_rejects_invented_definitions_and_invalid_candidates(candidates):
    rows = [{"query": "clear", "expected_status": "matched", "expected_command": "first"}]
    summary = EVALUATOR.evaluate(rows, [{"name": "first"}],
                                 lambda *args, **kwargs: {"status": "matched", "candidates": candidates})
    assert not summary["gates"]["response_contract"]["passed"]
    assert summary["metrics"]["top3"]["correct"] == 0


def test_engine_exception_is_a_failure_without_echoing_payload():
    def broken(*args, **kwargs):
        raise RuntimeError("secret payload that must not be copied")

    summary = EVALUATOR.evaluate([{"query": "unknown", "expected_status": "no_match"}], [], broken)
    assert not summary["passed"]
    assert "secret payload" not in json.dumps(summary)
    assert summary["failures"][0]["reasons"] == ["response_contract:RuntimeError", "status"]


def test_engine_cannot_redefine_command_authority_by_mutating_input():
    commands = [{"name": "first", "arguments": []}]

    def mutating_search(query, definitions, **kwargs):
        definitions[0]["arguments"].append({"name": "--invented"})
        return {"status": "matched", "candidates": [
            {"id": "first", "kind": "command", "definition": definitions[0]}]}

    summary = EVALUATOR.evaluate(
        [{"query": "clear", "expected_status": "matched", "expected_command": "first"}],
        commands, mutating_search)
    assert not summary["gates"]["response_contract"]["passed"]


def test_natural_language_retrieval_regression_gates_from_unrelated_directory(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    result = EVALUATOR.main(["--check"])
    summary = json.loads(capsys.readouterr().out)
    assert result == 0, json.dumps(summary, ensure_ascii=True, indent=2)
    assert summary["passed"] and summary["cases"] >= 60
    assert len(summary["fixture_sha256"]) == 64
