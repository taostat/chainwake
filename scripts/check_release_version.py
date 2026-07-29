"""Validate that a release tag matches every published version field."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_EXPECTED_ARG_COUNT = 2


def _fail(message: str) -> int:
    print(f"release version check failed: {message}", file=sys.stderr)
    return 1


def _matched_version(path: str, pattern: str, label: str) -> str:
    source = (ROOT / path).read_text()
    match = re.search(pattern, source, re.MULTILINE)
    if match is None:
        raise ValueError(f"{label} could not be read")
    return match.group(1)


def main(tag: str) -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project_version = str(project["project"]["version"])
    expected_tag = f"v{project_version}"
    if tag != expected_tag:
        return _fail(f"tag {tag!r} does not match package version {expected_tag!r}")

    server = json.loads((ROOT / "server.json").read_text())
    try:
        package_version = _matched_version(
            "chainwake/__init__.py",
            r'^__version__\s*=\s*"([^"]+)"$',
            "chainwake.__version__",
        )
        hermes_entrypoint_version = _matched_version(
            "__init__.py",
            r'^__version__\s*=\s*"([^"]+)"$',
            "Hermes entry-point version",
        )
        plugin_version = _matched_version(
            "plugin.yaml",
            r"^version:\s*([^\s]+)$",
            "Hermes plugin version",
        )
        hermes_version = _matched_version(
            "integrations/hermes/chainwake/SKILL.md",
            r"^version:\s*([^\s]+)$",
            "Hermes skill version",
        )
        openclaw_version = _matched_version(
            "integrations/openclaw/chainwake/SKILL.md",
            r"^\s*package:\s*chainwake==([^\s]+)$",
            "OpenClaw dependency version",
        )
    except ValueError as exc:
        return _fail(str(exc))

    versions = {
        "chainwake.__version__": package_version,
        "Hermes entry-point version": hermes_entrypoint_version,
        "Hermes plugin version": plugin_version,
        "Hermes skill version": hermes_version,
        "OpenClaw dependency version": openclaw_version,
        "server.json version": str(server.get("version")),
    }
    packages = server.get("packages")
    if not isinstance(packages, list) or len(packages) != 1:
        return _fail("server.json must declare exactly one package")
    versions["server.json package version"] = str(packages[0].get("version"))

    mismatches = {name: version for name, version in versions.items() if version != project_version}
    if mismatches:
        details = ", ".join(f"{name}={version!r}" for name, version in mismatches.items())
        return _fail(f"metadata does not match pyproject version {project_version!r}: {details}")

    print(f"release metadata matches {tag}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != _EXPECTED_ARG_COUNT:
        print("usage: check_release_version.py v<project-version>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
