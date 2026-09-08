"""Регрессия диагностики падения Node-драйвера Playwright при старте."""

import asyncio
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import publisher  # noqa: E402


class _BrokenPlaywrightManager:
    async def __aenter__(self):
        raise Exception("Connection closed while reading from the driver")

    async def __aexit__(self, *_args):
        return None


class PublishDriverStartupTest(unittest.TestCase):
    def test_driver_crash_is_reported_as_chrome_connection_step(self) -> None:
        """Сбой драйвера не должен превращаться в непонятный шаг unknown."""
        job = {"drafts_total": 8}
        draft = publisher.DraftData(
            title="Футболка Zara мужская",
            trade_type="Продаю своё",
            condition="Новое с биркой",
            size="48 (M)",
            brand="Zara",
            color="Белый",
            description="Описание",
            price=990,
            locations=tuple(
                publisher.LocationData(
                    "Санкт-Петербург", f"Невский проспект, {number}",
                )
                for number in range(40, 48)
            ),
            view_price_max=Decimal("0.5"),
            photo_paths=("photo.jpg",),
            category="tshirts",
            item_type="Футболка",
        )

        with (
            mock.patch(
                "playwright.async_api.async_playwright",
                return_value=_BrokenPlaywrightManager(),
            ),
            mock.patch.object(
                publisher,
                "_dump_failure",
                new=mock.AsyncMock(return_value=None),
            ),
        ):
            asyncio.run(
                publisher.run_publish_job(
                    "driver-startup-test",
                    job,
                    draft,
                    cdp_url="http://localhost:9222",
                )
            )

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job.get("step"), "connect_chrome")
        self.assertNotIn("unknown", job["error"])
        self.assertIn("драйвер Playwright", job["error"])
        self.assertIn("обычного PowerShell", job["error"])
        self.assertIn("0 из 8", job["error"])


if __name__ == "__main__":
    unittest.main()
