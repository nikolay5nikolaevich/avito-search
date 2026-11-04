"""Регрессии поиска фото на синтетическом DOM с подтверждёнными маркерами."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import publish_test_isolation  # noqa: E402, F401
import publisher  # noqa: E402
from playwright.async_api import Page, Route, async_playwright  # noqa: E402


def _photo_dom(count: int, source: str = "blob:") -> str:
    """Минимальная структура: вложенные images и динамические preview-ID."""
    images = "".join(
        f'<img data-marker="preview({910000 + index})/image" '
        f'src="{source}{index}">'
        for index in range(count)
    )
    return (
        '<div data-marker="images"><div data-marker="images">'
        f'<div data-marker="images/drop-zone">{images}</div>'
        '</div></div>'
    )


class PhotoPreviewDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        chromium = self.playwright.chromium
        options = {"headless": True}
        if not Path(chromium.executable_path).is_file():
            options["channel"] = "chrome"
        self.browser = await chromium.launch(**options)
        self.addAsyncCleanup(self.browser.close)
        context = await self.browser.new_context(service_workers="block")

        async def abort_network(route: Route) -> None:
            await route.abort()

        await context.route("**/*", abort_network)
        self.page: Page = await context.new_page()

    async def _wait(self, expected: int = 6) -> tuple[str, int]:
        return await publisher._wait_for_photo_previews(
            self.page,
            expected=expected,
            timeout_s=0,
            diagnostic_dir=None,
        )

    async def test_six_previews_in_nested_images_are_counted_once(self) -> None:
        await self.page.set_content(_photo_dom(6))

        selector, count = await self._wait()

        self.assertEqual(count, 6)
        self.assertEqual(await self.page.locator(selector).count(), 6)

    async def test_unrelated_page_images_cannot_confirm_upload(self) -> None:
        # Даже preview-marker вне контейнера загрузки не является фото формы.
        unrelated = "".join(
            f'<img data-marker="preview({index})/image" '
            f'src="https://cdn.example.invalid/unrelated-{index}.jpg">'
            for index in range(6)
        )
        await self.page.set_content(_photo_dom(0) + unrelated)

        with self.assertRaisesRegex(publisher.StepError, r"0/6 превью"):
            await self._wait()

    async def test_unmarked_images_inside_drop_zone_are_not_previews(self) -> None:
        await self.page.set_content(
            '<div data-marker="images">'
            '<div data-marker="images/drop-zone">'
            + '<img src="blob:decoration">' * 6
            + '</div></div>'
        )

        with self.assertRaisesRegex(publisher.StepError, r"0/6 превью"):
            await self._wait()

    async def test_five_of_six_previews_fail_with_actual_count(self) -> None:
        await self.page.set_content(_photo_dom(5))

        with self.assertRaisesRegex(publisher.StepError, r"5/6 превью"):
            await self._wait()

    async def test_preview_marker_survives_blob_to_cdn_source_change(self) -> None:
        await self.page.set_content(_photo_dom(6))
        for source in ("blob:", "https://cdn.example.invalid/photo-"):
            with self.subTest(source=source):
                await self.page.locator("img").evaluate_all(
                    "(images, prefix) => images.forEach((image, index) => "
                    "image.setAttribute('src', prefix + index))",
                    source,
                )

                _, count = await self._wait()

                self.assertEqual(count, 6)


if __name__ == "__main__":
    unittest.main()
