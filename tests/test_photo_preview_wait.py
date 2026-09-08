"""Регрессии ожидания превью после загрузки фотографий на Авито."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import avito_publish_selectors as psel  # noqa: E402
import publisher  # noqa: E402


class _LatePreviewLocator:
    def __init__(self, page: "_LatePreviewPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        if self.selector != psel.PHOTO_PREVIEW_CANDIDATES[0]:
            return 0
        self.page.primary_reads += 1
        if self.page.primary_reads == 1:
            return 0
        return self.page.expected


class _LatePreviewPage:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.primary_reads = 0

    def locator(self, selector: str) -> _LatePreviewLocator:
        return _LatePreviewLocator(self, selector)


class PhotoPreviewWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_rechecks_first_candidate_when_previews_appear_late(self) -> None:
        """Рабочий первый селектор нельзя навсегда отбрасывать после одного miss."""

        page = _LatePreviewPage(expected=2)
        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ):
            selector, count = await publisher._wait_for_photo_previews(
                page,
                expected=2,
                timeout_s=1.0,
                diagnostic_dir=None,
            )

        self.assertEqual(selector, psel.PHOTO_PREVIEW_CANDIDATES[0])
        self.assertEqual(count, 2)
        self.assertGreaterEqual(page.primary_reads, 2)


if __name__ == "__main__":
    unittest.main()
