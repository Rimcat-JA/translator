"""Export request schemas directly from the API's validated Pydantic models."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from translator.api.apps import (
    CreateSession,
    ImportBody,
    InviteBody,
    LeaseBody,
    Mutation,
    SecretBody,
    SettingsPatch,
    StreamStart,
    SystemAudioBody,
    TokenBody,
)


MODELS = (TokenBody, Mutation, CreateSession, SettingsPatch, SecretBody, ImportBody,
          InviteBody, SystemAudioBody, LeaseBody, StreamStart)
DESTINATION = Path(__file__).resolve().parents[1] / "contracts" / "http-inputs.schema.json"


def export_schema() -> str:
    _, schema = models_json_schema([(model, "validation") for model in MODELS],
                                  title="Translator HTTP inputs and audio stream handshake")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the committed schema differs.")
    arguments = parser.parse_args()
    expected = export_schema()
    if arguments.check:
        if not DESTINATION.is_file() or DESTINATION.read_text(encoding="utf-8") != expected:
            print("API input schema differs. Run: uv run --locked python tools/export_contracts.py")
            return 1
        print("API input schema matches the Pydantic models.")
        return 0
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Exported {DESTINATION.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
