"""Безопасная render-проверка страницы публикации без запуска Avito-flow."""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT = PROJECT_ROOT / "debug" / "publish-ui-part2.png"
BASE_URL = os.environ.get("PUBLISH_UI_BASE_URL", "http://127.0.0.1:18767")


def main() -> None:
    console_errors: list[str] = []
    SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.goto(f"{BASE_URL}/#/draft")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(SCREENSHOT), full_page=True)

        assert page.get_by_role("heading", name="Публикация объявлений").is_visible()
        assert page.locator(".draft-location-card").count() == 1
        assert page.get_by_role("button", name="Опубликовать 1 объявление").is_visible()

        page.locator("select[name='drafts_count']").select_option("20")
        assert page.locator(".draft-location-card").count() == 20
        assert page.get_by_role("button", name="Подготовить варианты").is_visible()
        assert page.get_by_text("Стоимость просмотра, ₽", exact=True).is_visible()

        browser.close()

    assert not console_errors, console_errors
    print(f"publish UI render check passed; screenshot={SCREENSHOT}")


if __name__ == "__main__":
    main()
