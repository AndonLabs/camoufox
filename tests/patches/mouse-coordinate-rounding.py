"""Regression for mouse input wedged by CSS-to-device-pixel rounding.

Run the packaged binary on Linux (headed cases need Xvfb):
    xvfb-run -a -s '-screen 0 3200x2400x24' python3 \
        tests/patches/mouse-coordinate-rounding.py /path/to/camoufox-bin

The original build hangs at the top edge in headed mode, even without
humanization. Each boundary move must reach the page AND leave the following
button click usable. A deliberately different fingerprint DPR catches using
spoofed display metrics for native input. No external pages are involved.
"""

import argparse
import asyncio
import itertools
import json
import os
import shutil
import tempfile
from pathlib import Path

from playwright.async_api import Page, Playwright, async_playwright


WIDTH = 960
HEIGHT = 640
TIMEOUT_SECONDS = 5
POINTS = [
    (0, 0),
    (448, 0),
    (0, 300),
    (WIDTH - 0.25, 300),
    (448, HEIGHT - 0.25),
    (WIDTH - 0.25, HEIGHT - 0.25),
    (448, 0.1),
    (0.1, 300),
]


async def exercise(page: Page, scale: float) -> None:
    await page.set_content("""
        <style>
          html, body { margin: 0; width: 100%; height: 100%; }
          button { position: fixed; left: 300px; top: 200px;
                   width: 120px; height: 60px; }
        </style>
        <button onclick="window.clicks++">Click after movement</button>
        <script>
          window.clicks = 0;
          window.lastMove = null;
          addEventListener('mousemove', event => {
            window.lastMove = [event.clientX, event.clientY];
          });
        </script>
    """)
    assert await page.evaluate("devicePixelRatio") == 7, (
        "Fingerprint DPR was not applied"
    )
    button = page.get_by_role("button")
    for count, (x, y) in enumerate(POINTS, 1):
        await page.evaluate("window.lastMove = null")
        await asyncio.wait_for(page.mouse.move(x, y), TIMEOUT_SECONDS)
        position = await page.evaluate("window.lastMove")
        assert position is not None, f"No renderer event at {(x, y)}"
        # Edge adjustment is limited to a device pixel, plus DOM's integer
        # client-coordinate rounding. Ordinary points must not be shifted away.
        tolerance = 1 / scale + 0.5
        assert abs(position[0] - x) <= tolerance, (x, y, position)
        assert abs(position[1] - y) <= tolerance, (x, y, position)
        await button.click(timeout=TIMEOUT_SECONDS * 1000)
        assert await page.evaluate("window.clicks") == count, (
            "Input queue stopped advancing"
        )

    # Keep the existing off-viewport semantics: it must return without waiting
    # for a renderer event, and the next real click must still work.
    await asyncio.wait_for(page.mouse.move(WIDTH, 300), TIMEOUT_SECONDS)
    await button.click(timeout=TIMEOUT_SECONDS * 1000)
    assert await page.evaluate("window.clicks") == len(POINTS) + 1


async def run_case(
    playwright: Playwright,
    binary: str,
    scale: float,
    headless: bool,
    humanize: bool,
) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CAMOU_CONFIG")
    }
    env["CAMOU_CONFIG_1"] = json.dumps(
        {
            "humanize": humanize,
            "humanize:maxTime": 0.3,
            "window.devicePixelRatio": 7,
        }
    )
    browser = await playwright.firefox.launch(
        executable_path=binary,
        headless=headless,
        env=env,
        firefox_user_prefs={
            "layout.css.devPixelsPerPx": str(scale),
            "toolkit.legacyUserProfileCustomizations.stylesheets": True,
        },
    )
    try:
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        await exercise(page, scale)
    finally:
        await asyncio.wait_for(browser.close(), TIMEOUT_SECONDS)


async def main(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="mouse-rounding-") as directory:
        # Camoufox loads chrome.css from its installation, not the profile.
        # Use a private copy and force a fractional chrome/content boundary;
        # host fonts and themes must not decide whether the regression runs.
        binary = args.binary.resolve()
        fixture = Path(directory) / "browser"
        shutil.copytree(binary.parent, fixture)
        (fixture / "chrome.css").write_text("""
            #navigator-toolbox {
              min-height: 51.4px !important;
              max-height: 51.4px !important;
              height: 51.4px !important;
              overflow: hidden !important;
            }
        """)
        async with async_playwright() as playwright:
            for scale, mode, humanize in itertools.product(
                args.scales, args.modes, args.humanize
            ):
                label = f"scale={scale} mode={mode} humanize={humanize}"
                print(f"RUN {label}", flush=True)
                await run_case(
                    playwright,
                    str(fixture / binary.name),
                    scale,
                    mode == "headless",
                    humanize == "on",
                )
                print(f"PASS {label}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--scales", nargs="+", type=float, default=[1, 1.25, 1.5, 2])
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["headed", "headless"],
        default=["headed", "headless"],
    )
    parser.add_argument(
        "--humanize", nargs="+", choices=["off", "on"], default=["off", "on"]
    )
    asyncio.run(main(parser.parse_args()))
