"""Build source UI when inputs change; frozen packages never run developer tools."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def web_directory() -> Path:
    return Path(__file__).resolve().parent / "resources/web"


def _run(arguments: list[str], cwd: Path):
    subprocess.run(arguments, cwd=cwd, check=True, timeout=300,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def ensure_frontend(force: bool = False) -> Path:
    target = web_directory()
    if getattr(sys, "frozen", False):
        if not (target / "index.html").is_file():
            raise RuntimeError("FRONTEND_ASSET_MISSING: 配布版を再取得してください。")
        return target
    frontend = source_root() / "frontend"
    if not frontend.is_dir():
        if (target / "index.html").is_file():
            return target
        raise RuntimeError("FRONTEND_ASSET_MISSING: ソース一式を取得してください。")
    files = [path for path in frontend.rglob("*") if path.is_file()
             and not set(path.relative_to(frontend).parts) & {
                 "node_modules", "dist", "test-results", "playwright-report", ".vite"
             }]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(frontend).as_posix().encode())
        digest.update(path.read_bytes())
    fingerprint = digest.hexdigest()
    marker = target / "build-info.json"
    try:
        if not force and (target / "index.html").is_file() and json.loads(marker.read_text())["sha256"] == fingerprint:
            return target
    except (OSError, ValueError, KeyError):
        pass
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    node = shutil.which("node")
    if not npm or not node:
        raise RuntimeError("BUILD_TOOLS_MISSING: 配布版を使うか、Node.js 24 と npm を導入してください。")
    version = subprocess.check_output([node, "--version"], text=True, timeout=10).strip()
    if not version.startswith("v24."):
        raise RuntimeError("NODE_VERSION_UNSUPPORTED: ソースビルドには Node.js 24 を使用してください。")
    lock = frontend / "package-lock.json"
    if not lock.is_file():
        raise RuntimeError("FRONTEND_LOCK_MISSING: 完全なソースを再取得してください。")
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    install_marker = frontend / "node_modules/.translator-lock"
    if not install_marker.is_file() or install_marker.read_text() != lock_hash:
        _run([npm, "ci", "--no-audit", "--no-fund"], frontend)
        install_marker.write_text(lock_hash)
    _run([npm, "run", "build"], frontend)
    output = frontend / "dist"
    if not (output / "index.html").is_file():
        raise RuntimeError("FRONTEND_BUILD_FAILED")
    # This fixed package-owned directory contains only generated output.
    expected = (source_root() / "src/translator/resources/web").resolve()
    if target.resolve() != expected:
        raise RuntimeError("INVALID_ASSET_PATH")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(output, target)
    marker.write_text(json.dumps({"sha256": fingerprint}), encoding="utf-8")
    return target
