"""Contracts for the artifacts uploaded to PyPI."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_sdist_contains_only_release_inputs(tmp_path: Path) -> None:
    """The source archive must not depend on untracked checkout files."""
    uv = shutil.which("uv")
    assert uv is not None

    result = subprocess.run(
        [
            uv,
            "build",
            "--clear",
            "--no-build-logs",
            "--no-create-gitignore",
            "--out-dir",
            str(tmp_path),
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    sdist = next(tmp_path.glob("chainwake-*.tar.gz"))
    with tarfile.open(sdist, mode="r:gz") as archive:
        names = set(archive.getnames())

    root = next(name for name in names if name.endswith("/PKG-INFO")).rsplit("/", 1)[0]
    relative_names = {name.removeprefix(f"{root}/") for name in names if name != root}
    # Hatchling adds its generated ignore file to source archives.  Everything
    # else must be an intentional install/release input.
    allowed_roots = {
        ".gitignore",
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "PKG-INFO",
        "chainwake",
    }
    assert {name.split("/", 1)[0] for name in relative_names} <= allowed_roots


def test_clean_release_artifacts_include_license_and_typed_marker(tmp_path: Path) -> None:
    """Inspect a clean build so stale files under dist/ cannot satisfy the contract."""
    uv = shutil.which("uv")
    assert uv is not None

    result = subprocess.run(
        [
            uv,
            "build",
            "--clear",
            "--no-build-logs",
            "--no-create-gitignore",
            "--out-dir",
            str(tmp_path),
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    wheel = next(tmp_path.glob("chainwake-*.whl"))
    sdist = next(tmp_path.glob("chainwake-*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
        wheel_metadata_name = next(
            name for name in wheel_names if name.endswith(".dist-info/METADATA")
        )
        wheel_metadata = Parser().parsestr(archive.read(wheel_metadata_name).decode())

    assert "chainwake/py.typed" in wheel_names
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names)
    assert wheel_metadata.get_all("License-File") == ["LICENSE"]

    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_names = set(archive.getnames())
        root = next(name for name in sdist_names if name.endswith("/PKG-INFO")).rsplit("/", 1)[0]
        package_info = archive.extractfile(f"{root}/PKG-INFO")
        assert package_info is not None
        sdist_metadata = Parser().parsestr(package_info.read().decode())

    assert f"{root}/LICENSE" in sdist_names
    assert f"{root}/chainwake/py.typed" in sdist_names
    assert sdist_metadata.get_all("License-File") == ["LICENSE"]
