#!/usr/bin/env python3
"""
fetch-enums-page.py — Fetch JS-rendered enum docs page using Playwright (headless Chromium).

Extracts <article class="content-container markdown"> from:
https://advertising.amazon.com/API/docs/en-us/reference/common-models/enums

Usage:
    python3 scripts/fetch-enums-page.py [output-file]

Requirements:
    pip3 install playwright
    python3 -m playwright install chromium
"""

import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[ERROR] playwright not installed. Run:")
    print("  pip3 install playwright")
    print("  python3 -m playwright install chromium")
    sys.exit(1)

URL = "https://advertising.amazon.com/API/docs/en-us/reference/common-models/enums"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "api-specs" / "enums-raw-manual.html"


def main():
    output_file = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    output_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Launching headless Chromium...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"[INFO] Navigating to: {URL}")
        page.goto(URL, wait_until="networkidle", timeout=30000)

        print('[INFO] Waiting for <article class="content-container markdown"> ...')
        page.wait_for_selector("article.content-container.markdown", timeout=15000)

        # Extract outerHTML of the article element
        article_html = page.evaluate("""
            () => {
                const el = document.querySelector('article.content-container.markdown');
                return el ? el.outerHTML : null;
            }
        """)

        browser.close()

    if not article_html:
        print("[ERROR] Could not find <article class='content-container markdown'>")
        sys.exit(1)

    output_file.write_text(article_html, encoding="utf-8")
    print(f"[OK] Extracted {len(article_html)} chars → {output_file}")


if __name__ == "__main__":
    main()
