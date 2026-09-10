"""Finite, discoverable operations for agents; no arbitrary HTTP or shell escape hatch."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from translator.config import Preferences, data_directory
from translator.providers.languages import SUPPORTED_LANGUAGES

from .client import AgentClient, AgentError, SAFE_ID, public_result, unique_object


def _argument(name, kind="string", *, required=False, description="", **options):
    return {"name": name, "type": kind, "required": required, "description": description, **options}


SESSION_ID = _argument("--session-id", required=True, description="Use the exact session_id returned by session-create or status.")
REQUEST_ID = _argument("--request-id", description="Optional stable operation ID; generated when omitted. Never automatically retried.")
PROVIDER = _argument("--provider", required=True, choices=["gladia", "deepl", "ngrok"], description="Provider to configure or test.")
INPUT = _argument("--input", required=True, description="UTF-8 input file path, or - to read stdin. Never pass a key in command arguments.")


def _definition(name, summary, arguments=(), *, starts_runtime=False, starts_audio=False,
                external_network=False, writes_settings=False, returns_sensitive=False,
                examples=(), retry="Do not retry mutations automatically; inspect status after an uncertain response."):
    return {"name": name, "summary": summary, "arguments": list(arguments),
            "effects": {"starts_runtime": starts_runtime, "starts_audio": starts_audio,
                        "external_network": external_network, "writes_settings": writes_settings,
                        "returns_sensitive": returns_sensitive},
            "examples": list(examples or [f"translator agent {name}"]),
            "retry": {"automatic": False, "guidance": retry}}


COMMANDS = [
    _definition("catalog", "Return this offline command catalog; no runtime or files are required."),
    _definition("search", "Primary agent entry: retrieve command summaries and exact specifications for a Japanese or English goal, offline. Retrieval never executes a command.",
                [_argument("--query", required=True, minLength=1, maxLength=1024,
                           description="One natural-language goal or an exact command name; 1–1024 characters, no credentials."),
                 _argument("--limit", "integer", default=3, minimum=1, maximum=5,
                           description="Maximum retrieved command or limitation cards. Check retrieval status before choosing.")],
                examples=['translator agent search --query "アプリを起動したい"',
                          'translator agent search --query "read recent captions"'],
                retry="Read-only offline retrieval; rephrase an ambiguous or unmatched goal before any execution."),
    _definition("status", "Discover the runtime. Missing runtime succeeds with running:false. Running status includes settings, session metadata and caption_count, without transcript text."),
    _definition("runtime-start", "Start a detached runtime without opening a browser or starting a session/audio. Source startup may build missing assets.",
                [_argument("--timeout", "number", default=60, minimum=1, maximum=300, description="Maximum readiness wait in seconds.")],
                starts_runtime=True, external_network=True),
    _definition("runtime-stop", "Request authenticated shutdown and wait for that runtime to stop.",
                [_argument("--timeout", "number", default=30, minimum=1, maximum=300, description="Maximum shutdown wait in seconds."), REQUEST_ID]),
    _definition("settings-get", "Read current preferences and provider configuration metadata; never return API keys."),
    _definition("settings-set", "Update preferences from {expected_revision:integer,settings:object}; maximum UTF-8 input size is 32768 bytes.",
                [INPUT, REQUEST_ID], writes_settings=True, examples=["translator agent settings-set --input settings-change.json"]),
    _definition("session-create", "Create an idle conversation; an explicit demo/live mode is required. This does not start capture or providers.",
                [_argument("--mode", required=True, choices=["demo", "live"]),
                 _argument("--source-language", default="zh", choices=[item["code"] for item in SUPPORTED_LANGUAGES]),
                 _argument("--target-language", default="ja", choices=[item["code"] for item in SUPPORTED_LANGUAGES]),
                 _argument("--translation-enabled", "boolean", default=True, choices=["true", "false"],
                           description="Requires an explicit value: --translation-enabled true or --translation-enabled false."), REQUEST_ID],
                examples=["translator agent session-create --mode demo --source-language zh --target-language ja"]),
    _definition("session-start", "Prepare an existing conversation. Demo starts synthetic PCM; live mode waits for a participant and does not capture the PC.",
                [SESSION_ID, REQUEST_ID], examples=["translator agent session-start --session-id SESSION_ID"]),
    _definition("session-stop", "Stop a conversation, release audio/providers and discard its in-memory captions.",
                [SESSION_ID, REQUEST_ID], examples=["translator agent session-stop --session-id SESSION_ID"]),
    _definition("session-snapshot", "Read current conversation state and the latest captions (default10). caption_count is the total retained; captions_truncated reports omitted older entries.",
                [SESSION_ID, _argument("--limit", "integer", default=10, minimum=1, maximum=100,
                                       description="Maximum recent captions returned; use 1 for the newest utterance.")],
                examples=["translator agent session-snapshot --session-id SESSION_ID --limit 10"]),
    _definition("invite-create", "Return a sensitive, one-use, ten-minute participant invite URL. Share it only with the intended participant.",
                [SESSION_ID, REQUEST_ID], returns_sensitive=True, examples=["translator agent invite-create --session-id SESSION_ID"]),
    _definition("diagnostics", "Read runtime diagnostics without API keys or transcript text."),
    _definition("devices", "Enumerate native PC audio output devices; this does not start capture."),
    _definition("secret-set", "Read a non-empty plaintext UTF-8 key from stdin/file, maximum 4096 bytes. Memory-only by default; never echo the key.",
                [PROVIDER, INPUT, _argument("--persist", "boolean", default=False,
                                           description="Presence-only flag: add --persist without a value to opt in to the OS credential store.")],
                writes_settings=True, examples=["translator agent secret-set --provider gladia --input -",
                                               "translator agent secret-set --provider deepl --input private-key.txt --persist"]),
    _definition("provider-test", "Explicitly contact the configured provider; usage may be incurred. ngrok validation is performed by tunnel-start.",
                [PROVIDER, REQUEST_ID], external_network=True, examples=["translator agent provider-test --provider gladia"]),
    _definition("tunnel-start", "Explicitly publish the participant Hub with the configured ngrok account and domain.",
                [REQUEST_ID], external_network=True),
    _definition("tunnel-stop", "Stop the currently owned public tunnel.", [REQUEST_ID], external_network=True),
    _definition("audio-share", "Share the selected PC output for a bounded duration, keep the authenticated owner heartbeat alive, then stop owned capture.",
                [SESSION_ID, _argument("--seconds", "integer", required=True, minimum=1, maximum=3600),
                 _argument("--device-id", description="Output device ID from devices; omit for the current OS default.")],
                starts_audio=True, external_network=True,
                examples=["translator agent audio-share --session-id SESSION_ID --seconds 30"]),
]
COMMANDS[0]["arguments"] = [_argument("--command", choices=[item["name"] for item in COMMANDS],
                                     description="Return only one command definition to reduce agent context.")]


def catalog(command: str | None = None) -> dict:
    return {"catalog_version": 1, "executable": "translator agent", "output_schema_version": 1,
            "discovery": {"primary_command": "search", "query_argument": "--query",
                          "workflow": "Search by goal, check status and prerequisites, then execute the retrieved formal command with observed inputs.",
                          "search_result_schema": "contracts/agent-search.schema.json"},
            "global_arguments": [_argument("--data-dir", description="Use one isolated runtime profile; accepted before or after the command.")],
            "exit_codes": {"0": "success", "2": "invalid input", "3": "runtime discovery or authentication failed",
                           "4": "operation failed or outcome is uncertain"},
            "commands": [item for item in COMMANDS if command is None or item["name"] == command]}


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentError("INVALID_ARGUMENTS", "Arguments do not match the command contract.", 2,
                         next_action="Use translator agent search --query GOAL to discover an operation, or catalog --command NAME for its arguments.")


def _identifier(value: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("Invalid identifier")
    return value


def _duration(maximum):
    def validate(value):
        try:
            number = float(value)
            if not 1 <= number <= maximum:
                raise ValueError()
            return number
        except ValueError:
            raise argparse.ArgumentTypeError("Invalid duration") from None
    return validate


def _integer(maximum):
    def validate(value):
        if not value.isascii() or not value.isdigit() or not 1 <= int(value) <= maximum:
            raise argparse.ArgumentTypeError("Invalid integer")
        return int(value)
    return validate


def _query(value: str) -> str:
    if not value.strip() or len(value) > 1024 or any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise argparse.ArgumentTypeError("Invalid query")
    return value


def parser() -> JsonParser:
    result = JsonParser(prog="translator agent", add_help=False, allow_abbrev=False)
    subcommands = result.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    for definition in COMMANDS:
        child = subcommands.add_parser(definition["name"], add_help=False, allow_abbrev=False)
        for argument in definition["arguments"]:
            name = argument["name"]
            options = {"required": argument["required"]}
            if "default" in argument:
                options["default"] = argument["default"]
            if "choices" in argument:
                options["choices"] = argument["choices"]
            if name == "--persist":
                options["action"] = "store_true"
            elif name in {"--session-id", "--request-id"}:
                options["type"] = _identifier
            elif name in {"--seconds", "--limit"}:
                options["type"] = _integer(argument["maximum"])
            elif name == "--timeout":
                options["type"] = _duration(argument["maximum"])
            elif name == "--command":
                options["dest"] = "catalog_command"
            elif name == "--query":
                options["type"] = _query
            elif name in {"--source-language", "--target-language"}:
                options["choices"] = [language["code"] for language in SUPPORTED_LANGUAGES]
            elif name == "--translation-enabled":
                options["default"] = "true"
            child.add_argument(name, **options)
    return result


def _global_arguments(argv: list[str]) -> tuple[list[str], Path | None]:
    remaining, directory = [], None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--data-dir" or value.startswith("--data-dir="):
            if directory is not None:
                raise AgentError("INVALID_ARGUMENTS", "Specify the data directory only once.", 2)
            if "=" in value:
                raw = value.split("=", 1)[1]
            else:
                index += 1
                if index >= len(argv):
                    raise AgentError("INVALID_ARGUMENTS", "The data directory requires a path.", 2)
                raw = argv[index]
            if not raw or raw.startswith("--"):
                raise AgentError("INVALID_ARGUMENTS", "The data directory requires a path.", 2)
            try:
                directory = Path(raw).expanduser().resolve()
            except (OSError, ValueError):
                raise AgentError("INVALID_ARGUMENTS", "The data directory path is invalid.", 2) from None
        else:
            remaining.append(value)
        index += 1
    return remaining, directory


def read_input(location: str, maximum: int) -> str:
    try:
        if location == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            content = stream.read(maximum + 1)
            if isinstance(content, str):
                content = content.encode("utf-8")
        else:
            with Path(location).open("rb") as file:
                content = file.read(maximum + 1)
        if len(content) > maximum:
            raise AgentError("INPUT_TOO_LARGE", "Input exceeds the documented byte limit.", 2,
                             next_action="Use a smaller input file; consult catalog for this command's limit.")
        return content.decode("utf-8-sig")
    except (OSError, UnicodeError, ValueError, AttributeError):
        raise AgentError("INPUT_UNREADABLE", "Input could not be read as UTF-8.", 2,
                         next_action="Provide a readable UTF-8 file or pipe UTF-8 content to --input -.") from None


def settings_input(location: str) -> dict:
    text = read_input(location, 32768)
    try:
        value = json.loads(text, object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(value, dict) or set(value) != {"expected_revision", "settings"} or
                type(value["expected_revision"]) is not int or value["expected_revision"] < 0 or
                not isinstance(value["settings"], dict)):
            raise ValueError()
        if set(value["settings"]) - (set(Preferences.model_fields) - {"schema_version", "revision"}):
            raise ValueError()
        preferences = Preferences.model_validate(Preferences().model_dump() | value["settings"], strict=True)
        languages = {language["code"] for language in SUPPORTED_LANGUAGES}
        if preferences.source_language not in languages or preferences.target_language not in languages:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise AgentError("INVALID_SETTINGS_INPUT", "Expected a revision and strictly validated non-secret settings only.", 2,
                         next_action="Run settings-get and submit {expected_revision: current_revision, settings: {...}}.") from None


def _request_id(arguments) -> str:
    return getattr(arguments, "request_id", None) or uuid.uuid4().hex


def _running(directory: Path, timeout: float = 1) -> dict | None:
    with AgentClient(directory, timeout=timeout) as client:
        if not client.probe():
            return None
        return {"running": True, "instance_id": client.instance_id, "local_origin": client.origin}


def runtime_start(directory: Path, timeout: float) -> dict:
    current = _running(directory)
    if current:
        return current | {"already_running": True}
    executable = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "translator"]
    command = executable + ["start", "--no-browser", "--data-dir", str(directory)]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **options)
    except OSError:
        raise AgentError("RUNTIME_START_FAILED", "The runtime process could not be started.", 4,
                         next_action="Verify the Translator installation and its startup diagnostics.") from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _running(directory, timeout=min(.5, max(.05, deadline - time.monotonic())))
        if current:
            return current | {"already_running": False}
        if process.poll() is not None:
            raise AgentError("RUNTIME_START_FAILED", "The launcher exited before a runtime became ready.", 4,
                             next_action="Run status and inspect runtime.log or translator doctor; source startup may need Node.js 24.")
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
    raise AgentError("RUNTIME_START_TIMEOUT", "The runtime did not become ready before the deadline; startup may still be running.", 4,
                     next_action="Run status before retrying. No process was terminated.")


def runtime_stop(client: AgentClient, timeout: float, request_id: str) -> dict:
    identity = client.instance_id
    try:
        client.request("POST", "/api/local/shutdown", {"request_id": request_id})
    except AgentError as error:
        if error.code not in {"OPERATION_TIMEOUT", "CONNECTION_FAILED", "RUNTIME_NOT_RUNNING"}:
            raise
        # Shutdown can close the connection before its HTTP response arrives.
        # Resolve uncertainty through read-only discovery; never repeat shutdown.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = _running(client.directory, timeout=min(.5, max(.05, deadline - time.monotonic())))
        except AgentError as error:
            if error.code not in {"OPERATION_TIMEOUT", "CONNECTION_FAILED"}:
                raise
            # Uvicorn may accept a connection while draining but no longer service
            # it. Continue bounded observation without resending the mutation.
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
            continue
        if current is None:
            return {"running": False, "stopped_instance_id": identity}
        if current["instance_id"] != identity:
            return {"running": True, "stopped_instance_id": identity, "replacement_instance_id": current["instance_id"]}
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
    raise AgentError("RUNTIME_STOP_TIMEOUT", "The runtime has not finished shutting down; no force termination was attempted.", 4,
                     next_action="Inspect status and diagnostics before deciding whether to retry.")


def _recent_snapshot(snapshot: dict, limit: int = 10) -> dict:
    captions = snapshot.get("captions", {})
    if not isinstance(captions, dict):
        raise AgentError("INVALID_RESPONSE", "The runtime returned an invalid caption map.")
    recent = dict(list(captions.items())[-limit:])
    return snapshot | {"captions": recent, "caption_count": len(captions), "captions_truncated": len(captions) > limit}


def _snapshot(client: AgentClient, session_id: str, limit: int = 10) -> dict:
    snapshot = client.bootstrap().get("session")
    if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
        raise AgentError("SESSION_NOT_FOUND", "The requested conversation is not the runtime's current conversation.", 4,
                         next_action="Run status and use the returned session_id.")
    return _recent_snapshot(snapshot, limit)


def execute(arguments, directory: Path) -> dict:
    command = arguments.command
    prepared = None
    if command == "settings-set":
        prepared = settings_input(arguments.input)
    elif command == "secret-set":
        prepared = read_input(arguments.input, 4096).strip()
        if not prepared or "\x00" in prepared or "\n" in prepared or "\r" in prepared:
            raise AgentError("INVALID_SECRET_INPUT", "Supply one non-empty plaintext key without embedded line breaks.", 2,
                             next_action="Pipe the key as UTF-8 or use a private UTF-8 file with --input.")
    if command == "runtime-start":
        return runtime_start(directory, arguments.timeout)
    with AgentClient(directory) as client:
        if command == "status" and not client.probe():
            return {"running": False}
        client.connect()
        request_id = _request_id(arguments)
        if command == "status":
            bootstrap = client.bootstrap()
            snapshot = bootstrap.get("session")
            caption_count = 0
            if isinstance(snapshot, dict):
                caption_count = len(snapshot.get("captions", {}))
                snapshot = {key: value for key, value in snapshot.items() if key != "captions"}
            return {"running": True, "instance_id": client.instance_id, "local_origin": client.origin,
                    "settings": bootstrap.get("settings"), "session": snapshot, "caption_count": caption_count}
        if command == "runtime-stop":
            return runtime_stop(client, arguments.timeout, request_id)
        if command == "settings-get":
            return client.request("GET", "/api/local/settings")
        if command == "settings-set":
            return client.request("PATCH", "/api/local/settings", prepared | {"request_id": request_id})
        if command == "session-create":
            return _recent_snapshot(client.request("POST", "/api/local/sessions", {"request_id": request_id,
                "demo": arguments.mode == "demo", "source_language": arguments.source_language,
                "target_language": arguments.target_language,
                "translation_enabled": arguments.translation_enabled == "true"}))
        if command == "session-snapshot":
            return _snapshot(client, arguments.session_id, arguments.limit)
        if command in {"session-start", "session-stop"}:
            operation = command.removeprefix("session-")
            return _recent_snapshot(client.request("POST", f"/api/local/sessions/{arguments.session_id}/{operation}", {"request_id": request_id}))
        if command == "invite-create":
            return client.request("POST", f"/api/local/sessions/{arguments.session_id}/invites",
                                  {"request_id": request_id, "role": "speaker"})
        if command == "diagnostics":
            return client.request("GET", "/api/local/diagnostics")
        if command == "devices":
            return client.request("GET", "/api/local/audio/devices")
        if command == "secret-set":
            result = client.request("PUT", f"/api/local/secrets/{arguments.provider}",
                                    {"key": prepared, "persist": arguments.persist})
            state = result.get("secrets", {}).get(arguments.provider, {})
            return {"provider": arguments.provider, "configured": state.get("configured", True),
                    "storage": state.get("storage", "os_keyring" if arguments.persist else "memory")}
        if command == "provider-test":
            return client.request("POST", f"/api/local/providers/{arguments.provider}/test", {"request_id": request_id})
        if command in {"tunnel-start", "tunnel-stop"}:
            return client.request("POST", "/api/local/tunnel/" + command.removeprefix("tunnel-"), {"request_id": request_id})
        if command == "audio-share":
            from .audio import share_audio
            return asyncio.run(share_audio(client, arguments.session_id, arguments.seconds, device_id=arguments.device_id))
    raise AgentError("UNKNOWN_COMMAND", "This command is not supported.", 2,
                     next_action="Run translator agent catalog.")


def main(argv: list[str] | None = None) -> int:
    command = "unknown"
    try:
        remaining, directory = _global_arguments(list(argv or []))
        names = {item["name"] for item in COMMANDS}
        if remaining and remaining[0] in names:
            command = remaining[0]
        if not remaining or "--help" in remaining or "-h" in remaining:
            selection = command if command != "unknown" else "search"
            command, data = "catalog", catalog(selection)
        else:
            arguments = parser().parse_args(remaining)
            command = arguments.command
            if command == "catalog":
                data = catalog(arguments.catalog_command)
            elif command == "search":
                from .retrieval import search
                data = search(arguments.query, COMMANDS, limit=arguments.limit)
            else:
                data = execute(arguments, directory or data_directory())
        output = {"schema_version": 1, "ok": True, "command": command,
                  "data": public_result(data, allow_invite=command == "invite-create"), "error": None}
        code = 0
    except AgentError as error:
        output = {"schema_version": 1, "ok": False, "command": command, "data": None, "error": error.as_dict()}
        code = error.exit_code
    except KeyboardInterrupt:
        error = AgentError("INTERRUPTED", "The operation was interrupted; inspect current state before retrying.", 4)
        output = {"schema_version": 1, "ok": False, "command": command, "data": None, "error": error.as_dict()}
        code = 4
    except Exception:
        error = AgentError("AGENT_OPERATION_FAILED", "The agent operation failed without exposing internal input or credentials.", 4,
                           next_action="Run status and diagnostics, then check the command catalog.")
        output = {"schema_version": 1, "ok": False, "command": command, "data": None, "error": error.as_dict()}
        code = 4
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return code
