"""Emoji fallback must not instantiate every named face to read its cmap.

The bundled System Font has hundreds of named variable-font instances. With
its aliases visible, adding VS16 to a warning sign in Helvetica exercises the
expensive fallback path while retaining every instance. A supervisor bounds
anonymous memory and time independently of the browser, and can terminate only
processes descended from this guard.

Run through ci.run_patch_guards --group parity --only font-fallback-memory.
Requires Linux, psutil, fontconfig, and the staged macOS font bundle.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import subprocess
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path

import psutil

from helpers import resolve_binary

MIB = 1024 * 1024
MAX_GROWTH = 256 * MIB
MAX_TOTAL = 2048 * MIB
STARTUP_SECONDS = 60
PROBE_SECONDS = 15
OBSERVATION_SECONDS = 10
CLOSE_SECONDS = 5
FONTS = ["Helvetica", "Arial", "Apple Color Emoji", "System Font", ".SF NS", "Systemschrift"]


def named_instances(environment: dict[str, str]) -> int:
    result = subprocess.run(
        ["fc-list", ":family=System Font", "--format",
         "%{file}\t%{index}\t%{namedinstance}\n"],
        env=environment, capture_output=True, text=True, check=True, timeout=20,
    )
    instances = {
        (file, index)
        for file, index, named in (row.split("\t") for row in result.stdout.splitlines())
        if named == "True"
    }
    if len(instances) < 300:
        raise AssertionError(
            f"System Font exposes only {len(instances)} named instances; "
            "stage the complete font bundle without filtering named instances"
        )
    return len(instances)


async def exercise(binary: str, connection: Connection) -> None:
    from camoufox import DefaultAddons
    from camoufox.async_api import AsyncCamoufox
    from camoufox.utils import launch_options

    options = launch_options(
        executable_path=binary, headless=True, os="macos", geoip=False,
        config={"fonts": FONTS}, exclude_addons=[DefaultAddons.UBO],
        i_know_what_im_doing=True,
    )
    count = named_instances(options["env"])
    async with AsyncCamoufox(from_options=options) as browser:
        page = await browser.new_page(java_script_enabled=False)
        await page.route("**/*", lambda route: route.abort())
        await page.set_content(
            '<!doctype html><meta charset="utf-8">'
            '<p style="font:14px Helvetica;display:inline-block">Ready</p>',
        )
        await page.screenshot()
        connection.send({"phase": "ready", "named_instances": count})
        if connection.recv() != "continue":
            raise AssertionError("supervisor did not establish a memory baseline")

        await page.evaluate("""() => {
            const p = document.querySelector('p');
            p.style.fontFamily = 'Helvetica,Arial,sans-serif';
            p.textContent = '\u26a0\ufe0f';
        }""")
        await page.screenshot(timeout=PROBE_SECONDS * 1000)
        connection.send({"phase": "rendered"})
        if connection.recv() != "check":
            raise AssertionError("supervisor did not complete the memory observation")
        await page.screenshot(timeout=PROBE_SECONDS * 1000)
        result = await page.evaluate("""() => {
            const p = document.querySelector('p');
            const bounds = p.getBoundingClientRect();
            return {text: p.textContent, width: bounds.width, height: bounds.height};
        }""")
        if result["text"] != "⚠️" or result["width"] <= 0 or result["height"] <= 0:
            raise AssertionError(f"emoji layout did not complete: {result}")
        connection.send({"phase": "complete", "layout": result})


def worker(binary: str, connection: Connection) -> None:
    try:
        asyncio.run(exercise(binary, connection))
    except Exception as error:
        connection.send({"phase": "error", "message": f"{type(error).__name__}: {error}"})
        raise SystemExit(1) from None
    finally:
        connection.close()


def anonymous_bytes(process: psutil.Process) -> int:
    try:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return 0
        lines = Path(f"/proc/{process.pid}/status").read_text().splitlines()
        for line in lines:
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) * 1024
        # gVisor omits RssAnon, but exposes the same accounting per mapping.
        mappings = Path(f"/proc/{process.pid}/smaps").read_text().splitlines()
        if not mappings:
            # An exiting process can release its address space before its PID.
            return 0
        anonymous = [int(line.split()[1]) for line in mappings if line.startswith("Anonymous:")]
        if anonymous:
            return sum(anonymous) * 1024
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return 0
        raise RuntimeError(f"anonymous-memory counters unavailable for owned process {process.pid}")
    except (FileNotFoundError, ProcessLookupError, psutil.NoSuchProcess):
        return 0


def remember_descendants(root: psutil.Process, owned: set[psutil.Process]) -> None:
    try:
        owned.update(root.children(recursive=True))
    except psutil.NoSuchProcess:
        pass


def terminate_owned(owned: set[psutil.Process]) -> None:
    for process in owned:
        try:
            # psutil checks the process creation time before sending the signal.
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(owned, timeout=CLOSE_SECONDS)


def main() -> int:
    if sys.platform != "linux":
        raise SystemExit("font-fallback-memory requires Linux /proc anonymous-memory accounting")
    if anonymous_bytes(psutil.Process()) <= 0:
        raise SystemExit("anonymous-memory accounting is unavailable for the supervisor")
    context = multiprocessing.get_context("spawn")
    connection, child_connection = context.Pipe()
    child = context.Process(target=worker, args=(str(resolve_binary()), child_connection))
    child.start()
    child_connection.close()
    root = psutil.Process(child.pid)
    owned = {root}
    report: dict[str, object] = {}
    baseline = None
    peak_growth = 0
    deadline = time.monotonic() + STARTUP_SECONDS
    phase = "startup"
    probe_started = None
    rendered = False
    final_check_requested = False
    failure = None
    pipe_open = True
    try:
        while True:
            remember_descendants(root, owned)
            total = sum(anonymous_bytes(process) for process in owned)
            if baseline is not None:
                peak_growth = max(peak_growth, total - baseline)
            if total > MAX_TOTAL or peak_growth > MAX_GROWTH:
                failure = (
                    f"anonymous memory exceeded the guard: total={total / MIB:.1f} MiB, "
                    f"growth={peak_growth / MIB:.1f} MiB (limit {MAX_GROWTH / MIB:.0f})"
                )
                break
            if time.monotonic() > deadline:
                failure = f"{phase} exceeded its deadline"
                break
            while pipe_open and connection.poll():
                try:
                    message = connection.recv()
                except EOFError:
                    pipe_open = False
                    break
                if message["phase"] == "ready":
                    report["named_instances"] = message["named_instances"]
                    baseline = total
                    report["baseline_anon_mib"] = round(baseline / MIB, 1)
                    phase = "emoji fallback"
                    probe_started = time.monotonic()
                    deadline = probe_started + PROBE_SECONDS
                    connection.send("continue")
                elif message["phase"] == "rendered":
                    rendered = True
                elif message["phase"] == "complete":
                    report["layout"] = message["layout"]
                    phase = "browser teardown"
                    deadline = time.monotonic() + CLOSE_SECONDS
                elif message["phase"] == "error":
                    failure = message["message"]
            if (rendered and not final_check_requested and probe_started is not None
                    and time.monotonic() - probe_started >= OBSERVATION_SECONDS):
                connection.send("check")
                final_check_requested = True
            if failure or not child.is_alive():
                break
            child.join(timeout=0.05)
        if not failure and (child.exitcode != 0 or "layout" not in report):
            failure = f"worker exited {child.exitcode} without completing emoji layout"
    finally:
        remember_descendants(root, owned)
        terminate_owned(owned)
        child.join(timeout=CLOSE_SECONDS)
        connection.close()
    report["peak_growth_anon_mib"] = round(peak_growth / MIB, 1)
    report["observation_seconds"] = OBSERVATION_SECONDS
    report["status"] = "FAIL" if failure else "PASS"
    if failure:
        report["error"] = failure
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
