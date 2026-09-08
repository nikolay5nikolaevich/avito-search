"""Регрессии сетевой навигации при открытии формы Авито."""

import asyncio
import os
import sys
import unittest
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import avito_publish_selectors as psel  # noqa: E402
import category_profiles  # noqa: E402
import publisher  # noqa: E402


class _Locator:
    def __init__(self, page: "_TransientDnsPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_Locator":
        return self

    async def is_visible(self) -> bool:
        return self.page.loaded and self.selector == psel.CATEGORY_TITLE


class _TransientDnsPage:
    def __init__(self, failures: tuple[str, ...] = ("ERR_NAME_NOT_RESOLVED",)) -> None:
        self.goto_calls = 0
        self.loaded = False
        self.failures = failures

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.goto_calls += 1
        if self.goto_calls <= len(self.failures):
            raise RuntimeError(
                f"Page.goto: net::{self.failures[self.goto_calls - 1]} at {url}"
            )
        self.loaded = True

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)


class OpenFormNavigationTests(unittest.TestCase):
    def test_open_form_retries_connection_closed_and_recovers(self) -> None:
        page = _TransientDnsPage(("ERR_CONNECTION_CLOSED",))

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ):
            state = asyncio.run(
                publisher._step_open_form(page, category_profiles.JACKETS)
            )

        self.assertEqual(state, "form")
        self.assertEqual(page.goto_calls, 2)

    def test_open_form_retries_transient_dns_failure_and_recovers(self) -> None:
        page = _TransientDnsPage()

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ):
            state = asyncio.run(
                publisher._step_open_form(page, category_profiles.JACKETS)
            )

        self.assertEqual(state, "form")
        self.assertTrue(page.loaded)
        self.assertEqual(page.goto_calls, 2)

    def test_open_form_retries_all_confirmed_transient_network_failures(self) -> None:
        page = _TransientDnsPage((
            "ERR_ABORTED",
            "ERR_CONNECTION_RESET",
            "ERR_TIMED_OUT",
        ))

        with mock.patch.object(
            publisher.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ) as sleep:
            state = asyncio.run(
                publisher._step_open_form(page, category_profiles.JACKETS)
            )

        self.assertEqual(state, "form")
        self.assertEqual(page.goto_calls, 4)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [5.0, 15.0, 30.0],
        )

    def test_open_form_does_not_retry_non_transient_navigation_error(self) -> None:
        page = _TransientDnsPage(("ERR_INVALID_URL",))

        with (
            mock.patch.object(
                publisher.asyncio,
                "sleep",
                new=mock.AsyncMock(),
            ) as sleep,
            self.assertRaisesRegex(RuntimeError, "ERR_INVALID_URL"),
        ):
            asyncio.run(
                publisher._step_open_form(page, category_profiles.JACKETS)
            )

        self.assertEqual(page.goto_calls, 1)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
