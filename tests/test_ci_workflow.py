"""GitHub Actions CI workflow shape tests (S02).

Pure YAML-parse assertions against `.github/workflows/ci.yml`.

These prove:
  - the workflow runs ruff + mypy + pytest on Python 3.11 + 3.12 / ubuntu-latest,
  - it triggers on both push and pull_request,
  - `Install icm` precedes `Install package` so integration tests can resolve `icm` on PATH,
  - the coverage gate (--cov-fail-under=85) is invoked (either explicitly in the
    workflow or via pyproject.toml's pytest addopts).

No subprocess; no GitHub API; just `yaml.safe_load`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_workflow() -> dict[str, Any]:
    """Load and parse the CI workflow file."""
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict), "workflow root must be a mapping"
    return parsed


def _on_field(workflow: dict[str, Any]) -> Any:
    """Return the workflow's `on:` field, tolerating PyYAML's True-key footgun.

    PyYAML 1.1 parses unquoted `on` as the boolean True. GitHub Actions parses
    correctly either way, but our test must probe both keys to be robust.
    """
    if "on" in workflow:
        return workflow["on"]
    # Cast to a permissive mapping to avoid mypy complaining about a bool key
    # in a dict[str, Any]. PyYAML may use the True key when `on:` is unquoted.
    permissive: dict[Any, Any] = workflow
    return permissive.get(True)


def _step_names_and_runs(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """Flatten test job's steps into [(name, run-or-empty), ...]."""
    job = workflow["jobs"]["test"]
    steps = job["steps"]
    out: list[tuple[str, str]] = []
    for step in steps:
        assert isinstance(step, dict)
        name = str(step.get("name", ""))
        run = str(step.get("run", ""))
        out.append((name, run))
    return out


def test_workflow_yaml_shape() -> None:
    """Workflow parses, has the locked matrix + triggers + ruff/mypy/pytest steps."""
    workflow = _load_workflow()

    # name + on
    assert workflow.get("name") == "ci"
    on_field = _on_field(workflow)
    assert on_field is not None, "workflow missing `on:` trigger field"
    # `on:` may be a list (`[push, pull_request]`) or a mapping
    # (`{push: {}, pull_request: {}}`); both must contain push + pull_request.
    if isinstance(on_field, list):
        triggers = set(on_field)
    elif isinstance(on_field, dict):
        triggers = set(on_field.keys())
    else:
        raise AssertionError(f"unexpected `on:` shape: {type(on_field).__name__}")
    assert "push" in triggers, f"`on:` must include push, got {triggers}"
    assert "pull_request" in triggers, f"`on:` must include pull_request, got {triggers}"

    # job shape
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict) and "test" in jobs, "expected jobs.test"
    test_job = jobs["test"]
    assert test_job.get("runs-on") == "ubuntu-latest"

    matrix = test_job["strategy"]["matrix"]
    py_versions = matrix["python-version"]
    # Compare as strings to lock the matrix exactly.
    assert [str(v) for v in py_versions] == ["3.11", "3.12"], (
        f"python-version matrix must be exactly ['3.11', '3.12'], got {py_versions}"
    )

    # Steps include ruff + mypy + pytest somewhere (name OR run text).
    haystack = " | ".join(f"{n}::{r}" for n, r in _step_names_and_runs(workflow)).lower()
    assert "ruff check" in haystack, "no ruff check step found"
    assert "mypy" in haystack, "no mypy step found"
    assert "pytest" in haystack, "no pytest step found"


def test_workflow_installs_icm() -> None:
    """A step named exactly 'Install icm' exists and precedes 'Install package'."""
    workflow = _load_workflow()
    steps = _step_names_and_runs(workflow)
    names = [n for n, _ in steps]

    assert "Install icm" in names, (
        f"missing step named 'Install icm'; step names: {names}"
    )
    assert "Install package" in names, (
        f"missing step named 'Install package'; step names: {names}"
    )
    icm_idx = names.index("Install icm")
    pkg_idx = names.index("Install package")
    assert icm_idx < pkg_idx, (
        f"'Install icm' (index {icm_idx}) must precede 'Install package' "
        f"(index {pkg_idx})"
    )

    # Surface the version (manager directive).
    icm_run = steps[icm_idx][1].lower()
    assert "icm --version" in icm_run, (
        "Install icm step must surface `icm --version` per AC4"
    )


def test_workflow_runs_pytest_with_coverage_gate() -> None:
    """The coverage gate (--cov-fail-under=85) is invoked.

    Acceptable forms (per planner):
      A. The workflow's pytest step contains `--cov-fail-under=85` explicitly, OR
      B. pyproject.toml's [tool.pytest.ini_options].addopts contains the gate.

    Either form satisfies AC5.
    """
    # Form A: scan workflow steps.
    workflow = _load_workflow()
    pytest_steps = [
        run for name, run in _step_names_and_runs(workflow)
        if "pytest" in name.lower() or "pytest" in run.lower()
    ]
    workflow_has_gate = any("--cov-fail-under=85" in run for run in pytest_steps)

    # Form B: scan pyproject.toml.
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    addopts = (
        pyproject.get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("addopts", "")
    )
    pyproject_has_gate = "--cov-fail-under=85" in addopts

    assert workflow_has_gate or pyproject_has_gate, (
        "coverage gate --cov-fail-under=85 must be present either in the "
        "workflow's pytest step or in pyproject.toml's pytest addopts"
    )
