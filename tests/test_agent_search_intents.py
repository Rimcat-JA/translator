"""Keep quoted command names and negated actions from becoming execution advice."""
import pytest

from translator.agent.commands import COMMANDS
from translator.agent.retrieval import search


@pytest.mark.parametrize("query, forbidden", [
    ("Show recent captions, not session-stop", "session-stop"),
    ("アプリを終了せず、字幕だけ読みたい", "runtime-stop"),
    ("キーは変えずにDeepLの接続を確かめたい", "secret-set"),
])
def test_a_negated_mutation_is_not_selected_as_a_match(query, forbidden):
    result = search(query, COMMANDS)
    assert not (result["status"] == "matched" and result["candidates"][0]["id"] == forbidden)


@pytest.mark.parametrize("query", [
    "Do not use audio-share; speak translated text aloud",
    "Use session-start to enable the remote participant's microphone from this CLI",
])
def test_an_embedded_command_name_does_not_override_a_capability_limit(query):
    result = search(query, COMMANDS)
    assert result["status"] == "unsupported"
    assert result["candidates"][0]["kind"] == "limitation"
    assert result["candidates"][0]["definition"] is None
