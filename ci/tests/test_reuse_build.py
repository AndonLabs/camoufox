"""An artifact may survive a test failure, but never a browser input change."""

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from ci import reuse_build


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=CI", "-c", "user.email=ci@example.test", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> tuple[Path, str, dict[str, Any]]:
    workflow: dict[str, Any] = {
        "jobs": {
            "build": {
                "runs-on": "ubuntu-latest-16-cores",
                "env": {"BUILD_TARGET": "linux,x86_64"},
                "steps": [
                    {"name": "Build", "run": "python3 multibuild.py"},
                    {
                        "name": "Verify mouse input on the packaged browser",
                        "run": "old test",
                    },
                ],
            }
        }
    }
    files = {
        ".github/workflows/build.yml": yaml.safe_dump(workflow),
        "patches/example.patch": "native",
        "additions/juggler/input/MouseDispatch.js": "resource",
        "bundle/fonts.json": "fonts",
        "upstream.sh": "version=156.0.1\nrelease=beta.34.1\n",
        "multibuild.py": "packaging",
        "ci/run_prepare.py": "prepare",
        "tests/mouse.py": "test",
    }
    for name, contents in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "build inputs")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD"), workflow


def test_test_changes_reuse_but_browser_changes_rebuild(
    source_repo: tuple[Path, str, dict[str, Any]],
) -> None:
    root, sha, workflow = source_repo
    (root / "tests/mouse.py").write_text("fixed test")
    workflow["jobs"]["build"]["steps"][1]["run"] = "corrected test"
    workflow["jobs"]["build"]["steps"][0]["if"] = reuse_build.BUILD_IF
    workflow["jobs"]["build"]["needs"] = "resolve"
    (root / reuse_build.WORKFLOW).write_text(yaml.safe_dump(workflow))
    assert reuse_build.same_inputs(root, sha)


@pytest.mark.parametrize(
    "path",
    [
        "patches/example.patch",
        "additions/juggler/input/MouseDispatch.js",
        "bundle/fonts.json",
        "upstream.sh",
        "multibuild.py",
        "ci/run_prepare.py",
        "settings/new-property.json",
    ],
)
def test_changed_or_new_build_inputs_cannot_reuse(
    source_repo: tuple[Path, str, dict[str, Any]],
    path: str,
) -> None:
    root, sha, _ = source_repo
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed")
    git(root, "add", path)
    assert not reuse_build.same_inputs(root, sha)


def test_deleted_input_cannot_reuse(
    source_repo: tuple[Path, str, dict[str, Any]],
) -> None:
    root, sha, _ = source_repo
    (root / "patches/example.patch").unlink()
    assert not reuse_build.same_inputs(root, sha)


@pytest.mark.parametrize("change", ["command", "environment", "new_step"])
def test_build_recipe_changes_cannot_reuse(
    source_repo: tuple[Path, str, dict[str, Any]],
    change: str,
) -> None:
    root, sha, workflow = source_repo
    job = workflow["jobs"]["build"]
    if change == "command":
        job["steps"][0]["run"] += " --different-flags"
    elif change == "environment":
        job["env"]["BUILD_TARGET"] = "linux,arm64"
    else:
        job["steps"].append({"name": "New build step", "run": "change-source"})
    (root / reuse_build.WORKFLOW).write_text(yaml.safe_dump(workflow))
    assert not reuse_build.same_inputs(root, sha)


def test_failed_test_can_reuse_successful_compile_but_not_failed_compile() -> None:
    jobs: list[dict[str, Any]] = [
        {
            "name": "build (linux, x86_64)",
            "steps": [
                {"name": "Build", "conclusion": "success"},
                {
                    "name": "Verify mouse input on the packaged browser",
                    "conclusion": "failure",
                },
            ],
        }
    ]
    assert reuse_build.packaged_successfully(jobs)
    failed = copy.deepcopy(jobs)
    failed[0]["steps"][0]["conclusion"] = "failure"
    assert not reuse_build.packaged_successfully(failed)


def test_pull_request_or_foreign_repository_artifacts_are_rejected() -> None:
    run = {
        "path": reuse_build.WORKFLOW,
        "status": "completed",
        "event": "push",
        "head_repository": {"full_name": "AndonLabs/camoufox"},
    }
    assert reuse_build.trusted_run(run, "AndonLabs/camoufox")
    assert not reuse_build.trusted_run(
        {**run, "event": "pull_request"}, "AndonLabs/camoufox"
    )
    assert not reuse_build.trusted_run(run, "another/repository")
    assert not reuse_build.trusted_run(
        {**run, "path": "other.yml"}, "AndonLabs/camoufox"
    )
