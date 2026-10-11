"""Find a packaged Linux artifact built from the current sources and recipe.

A failed regression does not invalidate a successful compile: the current tests
always run again. Only same-repository push/dispatch builds are eligible.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from ._util import REPO_ROOT, log, set_output, summary
from .browser_inputs import BROWSER_DIRS, BROWSER_FILES, BUILD_ENTRY_POINTS

WORKFLOW = ".github/workflows/build.yml"
ARTIFACT = "CamoufoxBuilds-linux-x86_64"
BUILD_IF = "needs.resolve.outputs.artifact_id == ''"
INPUTS = tuple(sorted(set(BROWSER_DIRS + BROWSER_FILES + BUILD_ENTRY_POINTS))) + (
    "bundle",
    "ci/run_prepare.py",
    "ci/_util.py",
)
# These consume the browser or report results; they do not produce it.
TEST_STEPS = {
    "Verify input coordinates",
    "Verify mouse input on the packaged browser",
    "Upload artifacts",
    "Download compatible browser",
}


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def build_recipe(text: str) -> dict[str, Any]:
    workflow = yaml.safe_load(text)
    job = workflow["jobs"]["build"].copy()
    job.pop("needs", None)
    job.pop("permissions", None)
    steps = []
    for original in job.pop("steps"):
        if original.get("name") in TEST_STEPS:
            continue
        step = original.copy()
        if step.get("if") == BUILD_IF:
            step.pop("if")
        # Step IDs are references for the upload guard, not compiler inputs.
        step.pop("id", None)
        steps.append(step)
    job["steps"] = steps
    return {
        "job": job,
        "env": workflow.get("env"),
        "defaults": workflow.get("defaults"),
    }


def same_inputs(root: Path, sha: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected a full artifact source commit SHA")
    if git(root, "diff", "--name-only", sha, "--", *INPUTS).strip():
        return False
    previous = git(root, "show", f"{sha}:{WORKFLOW}")
    return build_recipe(previous) == build_recipe((root / WORKFLOW).read_text())


def trusted_run(run: dict[str, Any], repo: str) -> bool:
    return (
        run["path"] == WORKFLOW
        and run["status"] == "completed"
        and run["event"] in {"push", "workflow_dispatch"}
        and (run.get("head_repository") or {}).get("full_name") == repo
    )


def packaged_successfully(jobs: list[dict[str, Any]]) -> bool:
    return any(
        step["name"] in {"Build", "Download compatible browser"}
        and step["conclusion"] == "success"
        for job in jobs
        if job["name"] == "build (linux, x86_64)"
        for step in job["steps"]
    )


def api(repo: str, path: str) -> Any:
    response = subprocess.run(
        ["gh", "api", f"repos/{repo}/{path}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(response.stdout)


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    if os.environ.get("FORCE_BUILD") == "true":
        set_output("artifact_id", "")
        summary("Full browser rebuild requested.")
        return
    artifacts = api(repo, f"actions/artifacts?name={ARTIFACT}&per_page=100")[
        "artifacts"
    ]
    for artifact in artifacts:
        if artifact["expired"] or artifact["name"] != ARTIFACT:
            continue
        run_id = artifact["workflow_run"]["id"]
        run = api(repo, f"actions/runs/{run_id}")
        if not trusted_run(run, repo):
            continue
        sha = run["head_sha"]
        # Checkout is shallow; fetch only the candidate, without changing HEAD.
        git(REPO_ROOT, "fetch", "--no-tags", "--depth=1", "origin", sha)
        if not same_inputs(REPO_ROOT, sha):
            continue
        jobs = api(repo, f"actions/runs/{run_id}/jobs?per_page=100")["jobs"]
        if not packaged_successfully(jobs):
            continue
        set_output("artifact_id", str(artifact["id"]))
        set_output("run_id", str(run_id))
        summary(
            f"Reusing browser artifact `{artifact['id']}` from "
            f"[run {run_id}](https://github.com/{repo}/actions/runs/{run_id}) "
            f"(source `{sha}`). Browser inputs and build recipe match; "
            "the current tests will run against the unchanged package."
        )
        return
    log("No compatible artifact; compiling the browser")
    set_output("artifact_id", "")
    summary("No compatible retained artifact. A full browser build is required.")


if __name__ == "__main__":
    main()
