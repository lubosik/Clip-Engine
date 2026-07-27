#!/usr/bin/env python3
"""Render data-driven quote cards with Playwright.

The default is intentionally safe: an entry must be named explicitly. Use
--all only after the client has approved the sample design.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "quotes.json"
TEMPLATE_PATH = ROOT / "card.html"
OUTPUT_DIR = ROOT / "output"
FORMATS = {
    "feed": (1080, 1350),
    "story": (1080, 1920),
}
FOUR_K_LONG_EDGE = 3840


def load_entries() -> list[dict[str, str]]:
    with DATA_PATH.open(encoding="utf-8") as file:
        entries = json.load(file)
    if not isinstance(entries, list):
        raise ValueError("quotes.json must contain a top-level array")
    return entries


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def render(entry: dict[str, str], page) -> Path:
    format_name = entry.get("format", "feed")
    if format_name not in FORMATS:
        raise ValueError(f"Unsupported format {format_name!r} for {entry.get('id')!r}")

    width, height = FORMATS[format_name]
    image_path = (ROOT / entry["image"]).resolve()
    if not image_path.is_file():
        raise FileNotFoundError(f"Missing source image: {image_path}")
    panel_enabled = entry.get("panel", True)
    avatar_path = (ROOT / entry["avatar"]).resolve() if panel_enabled else None
    if avatar_path is not None and not avatar_path.is_file():
        raise FileNotFoundError(f"Missing profile image: {avatar_path}")

    is_four_k = entry.get("resolution") == "4k"
    render_scale = FOUR_K_LONG_EDGE / height / 2 if is_four_k else 1
    viewport_width = round(width * render_scale)
    viewport_height = round(height * render_scale)

    query = urlencode(
        {
            "image": file_url(image_path),
            "avatar": file_url(avatar_path) if avatar_path else "",
            "quote": entry.get("quote", ""),
            "handle": entry.get("handle", ""),
            "follow": entry.get("follow", "Follow for more"),
            "variant": entry.get("variant", "light"),
            "tone": entry.get("tone", "light"),
            "layout": entry.get("layout", "standard"),
            "tint": entry.get("tint", "rgba(255,255,255,0)"),
            "accent": entry.get("accent", "#d6b783"),
            "panel": "true" if panel_enabled else "false",
            "position": entry.get("position", "lower-left"),
            "photoPosition": entry.get("photo_position", "50% 50%"),
            "format": format_name,
            "renderScale": render_scale,
        }
    )

    page.set_viewport_size({"width": viewport_width, "height": viewport_height})
    page.goto(f"{file_url(TEMPLATE_PATH)}?{query}", wait_until="load")
    page.wait_for_function("window.__CARD_READY__ === true")

    output_path = OUTPUT_DIR / entry["output"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_options = dict(
        path=output_path,
        animations="disabled",
        caret="hide",
        scale="device" if is_four_k else "css",
    )
    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        screenshot_options.update(type="jpeg", quality=95)
    else:
        screenshot_options.update(type="png")
    page.locator("#canvas").screenshot(**screenshot_options)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render John quote-card entries")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--id", action="append", help="Entry ID to render; may be repeated")
    selection.add_argument(
        "--all",
        action="store_true",
        help="Render every entry. Use only after sample approval.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    entries = load_entries()
    selected = entries if args.all else [entry for entry in entries if entry.get("id") in set(args.id)]

    requested = set(args.id or [])
    found = {entry.get("id") for entry in selected}
    missing = requested - found
    if missing:
        raise ValueError(f"Unknown entry IDs: {', '.join(sorted(missing))}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--allow-file-access-from-files", "--font-render-hinting=none"],
        )
        context = browser.new_context(device_scale_factor=2)
        page = context.new_page()
        for entry in selected:
            output = render(entry, page)
            print(output)
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
