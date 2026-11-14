"""requirements.txt and pyproject.toml describe the same install."""

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _name(requirement: str) -> str:
    return re.split(r"[\[<>=!~ ;]", requirement, maxsplit=1)[0].lower()


def _pyproject() -> dict:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]


def _requirements() -> list[str]:
    lines = (_ROOT / "requirements.txt").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_requirements__core__exactly_the_pyproject_dependencies():
    core = _pyproject()["dependencies"]
    extras = {req for group in _pyproject()["optional-dependencies"].values() for req in group}

    listed = _requirements()

    assert set(core) <= set(listed)
    assert [req for req in listed if req not in core and req not in extras] == []


def test_requirements__optional_features__same_extra_same_version():
    extras = _pyproject()["optional-dependencies"]
    by_name = {_name(req): req for req in _requirements()}

    assert by_name["chromadb"] in extras["vector"]
    assert by_name["docling"] in extras["ingestion"]


def test_pytest__only_in_the_dev_extra():
    project = _pyproject()

    assert "pytest" not in {_name(req) for req in project["dependencies"]}
    assert "pytest" not in {_name(req) for req in _requirements()}
    assert "pytest" in {_name(req) for req in project["optional-dependencies"]["dev"]}
