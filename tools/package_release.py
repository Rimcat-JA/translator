"""Assemble a preview ZIP, dependency notices and checksum after PyInstaller."""

import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist/Translator-windows-x64"


def notices():
    entries = []
    visited = set()
    # Include build-tool and vendored notices too: freezer hooks can pull in
    # modules (for example setuptools) outside the direct runtime dependency graph.
    pending = [dist.metadata["Name"] for dist in metadata.distributions()]
    while pending:
        name = pending.pop()
        dist = metadata.distribution(name)
        normalized = dist.metadata["Name"].lower().replace("_", "-")
        if normalized in visited:
            continue
        visited.add(normalized)
        for spec in dist.requires or []:
            requirement = Requirement(spec)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
        if normalized == "translator-desktop":
            continue
        texts = []
        for file in dist.files or []:
            if any(token in file.name.lower() for token in ("license", "copying", "notice")):
                path = Path(dist.locate_file(file))
                if path.is_file() and path.suffix.lower() not in (".py", ".pyc"):
                    texts.append(path.read_text(encoding="utf-8", errors="replace"))
        entries.append(f"{dist.metadata['Name']} {dist.version}\n" +
                       f"License: {dist.metadata.get('License-Expression') or dist.metadata.get('License', 'See upstream package')}\n" + "\n".join(texts))
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text())
    for relative, package in lock["packages"].items():
        if not relative or package.get("dev"):
            continue
        folder = ROOT / "frontend" / relative
        texts = [file.read_text(encoding="utf-8", errors="replace") for file in folder.iterdir()
                 if file.is_file() and file.name.lower().startswith(("license", "notice", "copying"))]
        entries.append(f"{relative} {package['version']}\nLicense: {package.get('license', 'See upstream package')}\n" + "\n".join(texts))
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        entries.append("Python\n" + python_license.read_text(encoding="utf-8", errors="replace"))
    return ("Third-party software and build-tool notices for the bundled preview.\n" +
            "ngrok service usage is also subject to your ngrok account terms.\n\n" +
            "\n\n".join(entries))


def main():
    if not (OUTPUT / "Translator.exe").is_file():
        raise SystemExit("Build packaging/translator.spec on Windows first.")
    source_marker = ROOT / "src/translator/resources/web/build-info.json"
    bundled_marker = OUTPUT / "_internal/translator/resources/web/build-info.json"
    if source_marker.read_bytes() != bundled_marker.read_bytes():
        raise SystemExit("Bundled UI is stale. Rebuild packaging/translator.spec before packaging.")
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src", "frontend", "pyproject.toml", "uv.lock", "packaging"], cwd=ROOT).returncode:
        raise SystemExit("Commit the application source before stamping the release manifest.")
    shutil.copy2(ROOT / "packaging/README-FIRST.txt", OUTPUT / "README-FIRST.txt")
    (OUTPUT / "THIRD-PARTY-NOTICES.txt").write_text(notices(), encoding="utf-8")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {"version": "0.2.0-preview.1", "commit": revision, "platform": "windows-x64",
                "python": sys.version.split()[0], "signed": False, "channel": "preview",
                "unverified": ["clean_windows_vm", "paid_apis", "two_device_remote_audio", "latency_targets"]}
    (OUTPUT / "release-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = Path(shutil.make_archive(str(ROOT / "dist/Translator-windows-x64-preview"), "zip", OUTPUT.parent, OUTPUT.name))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (ROOT / "dist/SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(archive)


if __name__ == "__main__":
    main()
