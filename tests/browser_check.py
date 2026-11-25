"""Verify the public evidence viewer with pointer, keyboard, downloads and offline use."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from playwright.sync_api import Browser, Locator, Page, expect, sync_playwright


def observed(locator: Locator, outcome: dict) -> None:
    text = locator.text_content()
    if outcome["status"] == "error":
        assert text == f"{outcome['type']}: {outcome['message']}"
    elif isinstance(outcome["result"], str):
        assert text == outcome["result"]
    else:
        assert json.loads(text) == outcome["result"]


def watch(page: Page, errors: list[str]) -> None:
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console", lambda message: errors.append(message.text) if message.type == "error" else None
    )


def download(page: Page, button: Locator) -> bytes:
    with page.expect_download() as result:
        button.click()
    return result.value.path().read_bytes()


def fits(page: Page, width: int) -> None:
    assert page.evaluate(
        "[document.documentElement.clientWidth, document.documentElement.scrollWidth]"
    ) == [width, width]


def check(browser: Browser, url: str, output: Path, report: Path | None) -> None:
    errors: list[str] = []
    desktop = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    page = desktop.new_page()
    watch(page, errors)
    response = page.goto(url, wait_until="networkidle")
    assert response and response.ok
    cases = json.loads(page.locator("#evidence").text_content())["cases"]
    assert [c["id"] for c in cases] == ["csv-export", "shipping-quote"]
    for case in cases:
        page.locator("#case").select_option(case["id"])
        expect(page.locator("h1")).to_have_text(case["headline"])
        stages = page.locator("button[data-stage]:visible")
        expect(stages).to_have_count(4)
        for i, run in enumerate(
            [
                case["live_requests"]["original"],
                case["live_requests"]["retry"],
                case["comparison"]["historical"],
                case["regression"]["after"],
            ]
        ):
            stages.nth(i).click()
            expect(stages.nth(i)).to_have_attribute("aria-pressed", "true")
            observed(page.locator("#output"), run["outcome"])
            if i < 2:
                expect(page.locator("#proof")).to_contain_text(
                    f"POST {run['path']} · {run['http_status']}"
                )
        expect(page.locator("#proof")).to_contain_text("pytest exit 1 · Failed")
        expect(page.locator("#proof")).to_contain_text("pytest exit 0 · Passed")
        stages.last.press("Home")
        expect(stages.first).to_be_focused()
        stages.first.press("ArrowRight")
        expect(stages.nth(1)).to_be_focused()
        stages.nth(1).press("End")
        expect(stages.last).to_be_focused()
        stages.nth(2).click()
        page.evaluate("scrollTo(0,0)")
        page.screenshot(path=output / ("desktop.png" if case == cases[0] else "shipping.png"))
        page.locator("#jump").click()
        expect(page.locator("#explorer")).to_be_focused()
        cells = case["exploration"]["cells"]
        assert len(cells) == 4
        for i, cell in enumerate(cells):
            choice = page.locator(f"#grid button[data-index='{i}']")
            choice.click()
            expect(choice).to_have_attribute("aria-pressed", "true")
            for n, key in enumerate(("original", "candidate")):
                observed(page.locator("#cell-output > pre").nth(n), cell[key]["outcome"])
                reads = page.locator("#cell-output details").nth(n)
                reads.locator("summary").click()
                assert json.loads(reads.locator("pre").text_content()) == cell[key]["reads"]
            if cell["settings"] == case["exploration"]["baseline_settings"]:
                expect(page.locator("#cell-title")).to_contain_text("Captured settings")
                if case == cases[0]:
                    page.locator("#explorer").screenshot(path=output / "matrix.png")
        page.locator("#regression summary").click()
        patch = page.locator("#regression-body pre").last
        patch.focus()
        expect(patch).to_be_focused()
        expect(patch).to_have_attribute("tabindex", "0")
        archive = download(
            page, page.get_by_role("button", name="Download runnable synthetic pytest")
        )
        assert hashlib.sha256(archive).hexdigest() == case["artifacts"]["zip_sha256"]
        (output / case["artifacts"]["filename"]).write_bytes(archive)
        page.locator("#provenance summary").click()
        for reason in set(case["comparison"]["reasons"].values()):
            expect(page.locator("#origins")).to_contain_text(reason)
        expect(page.locator("#command")).to_contain_text(case["comparison"]["capture_id"])
        fits(page, 1440)

    full = json.loads(download(page, page.locator("#save")))
    assert full["cases"] == cases
    summary = json.loads(download(page, page.locator("#summary")))
    assert summary == full["share_summary"]
    assert all(
        set(x) == {"replay_matched", "changed_flags", "before_passed", "after_passed"}
        for x in summary["incidents"]
    )

    for width, height in ((1366, 768), (768, 1024), (390, 844), (320, 640)):
        context = browser.new_context(
            viewport={"width": width, "height": height},
            is_mobile=width < 768,
            has_touch=width < 768,
        )
        screen = context.new_page()
        watch(screen, errors)
        screen.goto(url + "#shipping-quote/regression", wait_until="networkidle")
        expect(screen.locator("#regression")).to_have_attribute("open", "")
        expect(screen.locator("#case")).to_have_value("shipping-quote")
        screen.locator(".skip").focus()
        screen.keyboard.press("Enter")
        expect(screen.locator("#investigation")).to_be_focused()
        expect(screen.locator("#case")).to_have_value("shipping-quote")
        assert screen.url.endswith("#shipping-quote/regression")
        screen.locator("#case").select_option("csv-export")
        if width <= 390:
            expect(screen.locator(".m-stage").first).to_have_accessible_name("Capture App failed")
        stage = screen.locator("button[data-stage='3']:visible")
        stage.click()
        expect(screen.locator("#output-title")).to_have_text("Test passed")
        screen.go_back()
        expect(screen.locator("#output-title")).to_have_text("Failure matched")
        fits(screen, width)
        screen.evaluate("scrollTo(0, 0)")
        change = screen.locator(
            ".delta:visible, #matrix th[scope=row]:visible"
        ).first.bounding_box()
        assert change and change["y"] + change["height"] < height
        if width == 390:
            screen.screenshot(path=output / "mobile.png")
        context.close()

    if report is not None:
        offline = browser.new_context(offline=True)
        file_page = offline.new_page()
        watch(file_page, errors)
        file_page.goto(report.resolve().as_uri() + "#shipping-quote/combinations")
        expect(file_page.locator("#case")).to_have_value("shipping-quote")
        expect(file_page.locator("#explorer")).to_be_focused()
        expect(file_page.locator("#grid button")).to_have_count(4)
        assert json.loads(download(file_page, file_page.locator("#summary"))) == summary
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
        "Both incidents passed browser checks at five widths, including keyboard, downloads and offline use."
    )


if __name__ == "__main__":
    main()
