"""Keeps .github/workflows/ci.yml consistent with pyproject.toml.

The workflow is parsed with regular expressions (no YAML dependency), so these
checks are deliberately narrow: Python versions, the pandas/numpy pins, and the
absence of secrets or privileged triggers.
"""

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def declared_minimum(package: str) -> tuple[int, ...]:
    for dep in PYPROJECT["project"]["dependencies"]:
        m = re.fullmatch(rf"{package}\s*>=\s*([\d.]+)", dep.strip())
        if m:
            return version_tuple(m.group(1))
    raise AssertionError(f"pyproject.toml has no '{package}>=X' dependency")


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text()


def test_workflow_python_versions_match_requires_python(workflow):
    match = re.search(r'python-version:\s*\[([^\]]+)\]', workflow)
    assert match, "matrix python-version list not found"
    versions = re.findall(r'"(\d+\.\d+)"', match.group(1))
    floor = re.fullmatch(r">=\s*(\d+\.\d+)", PYPROJECT["project"]["requires-python"]).group(1)
    assert versions == ["3.11", "3.12", "3.13"]
    assert all(version_tuple(v) >= version_tuple(floor) for v in versions)
    assert floor in versions, "the declared minimum Python must be tested"


def test_lowest_pins_satisfy_declared_bounds_and_cover_every_python(workflow):
    pins = re.findall(
        r'python-version:\s*"(\d+\.\d+)"\s+deps:\s*lowest\s+pins:\s*"pandas==([\d.]+) numpy==([\d.]+)"', workflow
    )
    assert [p[0] for p in pins] == ["3.11", "3.12", "3.13"]
    for _, pandas_v, numpy_v in pins:
        assert version_tuple(pandas_v) >= declared_minimum("pandas")
        assert version_tuple(numpy_v) >= declared_minimum("numpy")
    # The declared floor itself must be exercised on at least one Python.
    def padded(v):
        return (version_tuple(v) + (0, 0, 0))[:3]

    floor = (padded(".".join(map(str, declared_minimum("pandas")))), padded(".".join(map(str, declared_minimum("numpy")))))
    assert any((padded(p), padded(n)) == floor for _, p, n in pins)


def test_workflow_installs_via_pyproject_and_runs_pytest(workflow):
    assert 'pip install -e ".[dev]"' in workflow
    assert "python -m pytest" in workflow
    assert "dev" in PYPROJECT["project"]["optional-dependencies"]


def test_workflow_triggers_and_safety(workflow):
    assert re.search(r"^on:\s*\n\s+push:\s*\n\s+pull_request:", workflow, re.M)
    assert "pull_request_target" not in workflow
    assert "secrets." not in workflow
    assert "contents: read" in workflow
    assert not re.search(r"\b(curl|wget)\b", workflow)
