"""Exercise the rendered evidence report in Chromium at desktop and mobile widths."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import Browser, expect, sync_playwright


def check(browser: Browser, url: str, output: Path, report: Path | None) -> None:
    errors: list[str] = []
    desktop = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    page = desktop.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type in {"error", "warning"} else None
        ),
    )
    response = page.goto(url, wait_until="networkidle")
    assert response is not None and response.ok
    expect(page.get_by_role("heading", name="CSV export", exact=True)).to_be_visible()
    expect(page.get_by_text("Original error reproduced")).to_be_visible()
    expect(page.get_by_role("rowheader", name="stream_report_rows", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
    page.screenshot(path=output / "desktop.png", full_page=True)

    page.get_by_role("tab", name="Overview").focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_role("tab", name="Regression test")).to_be_focused()
    expect(page.get_by_text("pytest · Failed")).to_be_visible()
    expect(page.get_by_text("pytest · Passed")).to_be_visible()
    expect(page.get_by_text("Matched in both runs")).to_be_visible()
    page.screenshot(path=output / "regression.png", full_page=True)

    case = page.get_by_role("button", name="Shipping quote", exact=True)
    case.click()
    expect(case).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("heading", name="Shipping quote", exact=True)).to_be_visible()
    expect(page.get_by_text("Fixed application")).to_be_visible()
    page.get_by_role("tab", name="Capture details").click()
    expect(page.get_by_text("Mountain zone segment")).to_be_visible()
    expect(page.get_by_text("Exported test digest")).to_be_visible()
    page.get_by_role("tab", name="Overview").click()
    expect(page.get_by_role("rowheader", name="enable_express_quotes", exact=True)).to_be_visible()
    page.screenshot(path=output / "shipping.png", full_page=True)

    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download evidence").click()
    data = json.loads(downloaded.value.path().read_bytes())
    assert [incident["id"] for incident in data["cases"]] == ["csv-export", "shipping-quote"]

    for width in (390, 320):
        mobile = browser.new_context(
            viewport={"width": width, "height": 844},
            is_mobile=True,
            has_touch=True,
            device_scale_factor=1,
        )
        mobile_page = mobile.new_page()
        mobile_page.on("pageerror", lambda error: errors.append(str(error)))
        mobile_page.on(
            "console",
            lambda message: (
                errors.append(message.text) if message.type in {"error", "warning"} else None
            ),
        )
        mobile_page.goto(url + "#shipping-quote/regression", wait_until="networkidle")
        expect(
            mobile_page.get_by_role("heading", name="Shipping quote", exact=True)
        ).to_be_visible()
        expect(mobile_page.get_by_role("tab", name="Regression test")).to_have_attribute(
            "aria-selected", "true"
        )
        assert mobile_page.evaluate("document.documentElement.scrollWidth === innerWidth")
        mobile_page.get_by_role("button", name="CSV export", exact=True).tap()
        mobile_page.get_by_role("tab", name="Overview").tap()
        expect(mobile_page.get_by_text("Original error reproduced")).to_be_visible()
        assert mobile_page.evaluate("document.documentElement.scrollWidth === innerWidth")
        if width == 390:
            mobile_page.screenshot(path=output / "mobile.png", full_page=True)
        mobile.close()

    if report is not None:
        offline = browser.new_context(offline=True)
        file_page = offline.new_page()
        file_page.on("pageerror", lambda error: errors.append(str(error)))
        file_page.goto(report.resolve().as_uri())
        file_page.get_by_role("tab", name="Regression test").click()
        expect(file_page.get_by_text("pytest · Passed")).to_be_visible()
        offline.close()
    desktop.close()
    assert not errors, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:13811/")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".local/verification/ui"))
    parser.add_argument("--channel", default="chrome", help="set to chromium for bundled Chromium")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel)
        try:
            check(browser, args.url, args.output, args.report)
        finally:
            browser.close()
    print(
        "Browser checks passed: desktop, 390px and 320px mobile, tabs, download, offline file and console."
    )


if __name__ == "__main__":
    main()
