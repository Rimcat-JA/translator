"""The installed command catalog and committed agent navigation must agree."""
from pathlib import Path
import json
import subprocess
import sys


def test_agent_discovery_has_no_drift_and_links_resolve_from_another_cwd(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "tools/export_agent_docs.py"), "--check"],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_search_contract_and_navigation_point_to_the_installed_knowledge():
    from translator.agent.commands import COMMANDS
    from translator.agent.retrieval import knowledge_index, search

    root = Path(__file__).resolve().parents[1]
    def read(relative):
        return json.loads((root / relative).read_text(encoding="utf-8"))

    navigation = read("docs/agents/index.json")
    assert navigation["primary_discovery"]["command"] == "search"
    assert read(navigation["retrieval_index"]) == knowledge_index(COMMANDS)
    schema = read(navigation["search_result_schema"])
    assert schema["$defs"]["command"]["enum"] == COMMANDS
    for query in ("runtime-start", "開始", "astronomy nebula galaxy", "翻訳文を合成音声で読み上げたい"):
        result = search(query, COMMANDS)
        assert set(result) == set(schema["required"])
        assert result["status"] in schema["properties"]["status"]["enum"]
        for candidate in result["candidates"]:
            assert set(candidate) == set(schema["$defs"]["candidate"]["required"])
            assert (root / candidate["source"]["path"]).is_file()
            if candidate["kind"] == "command":
                assert candidate["definition"] in schema["$defs"]["command"]["enum"]
                assert candidate["definition"]["name"] == candidate["id"]
            else:
                assert candidate["definition"] is None
