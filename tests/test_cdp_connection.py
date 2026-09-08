"""Регрессии подключения Playwright к пользовательскому Chrome по CDP."""

import asyncio
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock
from urllib.error import URLError


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import browser  # noqa: E402
import publisher  # noqa: E402
from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402


class _HttpResponse:
    def __init__(self, payload: object = None) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _Context:
    async def add_init_script(self, _script: str) -> None:
        return None


class _Browser:
    def __init__(self) -> None:
        self.contexts = [_Context()]


class _Chromium:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def connect_over_cdp(self, url: str, **kwargs: object) -> _Browser:
        self.calls.append((url, kwargs))
        return _Browser()


class _Playwright:
    def __init__(self) -> None:
        self.chromium = _Chromium()


class _PlaywrightManager:
    async def __aenter__(self) -> _Playwright:
        return _Playwright()

    async def __aexit__(self, *_args: object) -> None:
        return None


class CdpConnectionTest(unittest.TestCase):
    def test_ws_endpoints_skip_http_preflight(self) -> None:
        """Прямой browser WebSocket URL сразу передаётся Playwright."""
        for scheme in ("ws", "wss"):
            with self.subTest(scheme=scheme):
                cdp_url = (
                    f"{scheme}://browser.example/devtools/browser/session-id"
                )
                pw = _Playwright()
                with mock.patch.object(
                    browser,
                    "urlopen",
                    side_effect=AssertionError("HTTP preflight запрещён для ws"),
                ) as mocked_urlopen:
                    context = asyncio.run(
                        browser.connect_over_cdp(pw, cdp_url)
                    )

                self.assertIsInstance(context, _Context)
                mocked_urlopen.assert_not_called()
                self.assertEqual(
                    pw.chromium.calls,
                    [(cdp_url, {"timeout": 30_000})],
                )

    def test_http_preflight_preserves_endpoint_path_prefix(self) -> None:
        """Служебные /json URL добавляются после HTTP path-prefix."""
        requested_urls: list[str] = []

        def fake_urlopen(request: object, *, timeout: float) -> _HttpResponse:
            del timeout
            url = request.full_url  # type: ignore[attr-defined]
            requested_urls.append(url)
            if "/json/version" in url:
                return _HttpResponse({"Browser": "Chrome/150.0"})
            if "/json/list" in url:
                return _HttpResponse([])
            raise AssertionError(f"Неожиданный HTTP-запрос: {url}")

        cdp_url = "https://gateway.example/cdp/session-42/?token=secret"
        pw = _Playwright()
        with mock.patch.object(browser, "urlopen", side_effect=fake_urlopen):
            context = asyncio.run(browser.connect_over_cdp(pw, cdp_url))

        self.assertIsInstance(context, _Context)
        self.assertEqual(
            requested_urls,
            [
                "https://gateway.example/cdp/session-42/json/version?token=secret",
                "https://gateway.example/cdp/session-42/json/list?token=secret",
            ],
        )
        self.assertEqual(
            pw.chromium.calls,
            [(cdp_url, {"timeout": 30_000})],
        )

    def test_omnibox_target_is_never_closed_before_30_second_connect(self) -> None:
        """Служебный browser_ui нельзя закрывать через HTTP preflight."""
        requested_urls: list[str] = []

        def fake_urlopen(request: object, *, timeout: float) -> _HttpResponse:
            self.assertLessEqual(timeout, 3.0)
            url = request.full_url  # type: ignore[attr-defined]
            requested_urls.append(url)
            if url.endswith("/json/version"):
                return _HttpResponse({"Browser": "Chrome/150.0"})
            if url.endswith("/json/list"):
                return _HttpResponse(
                    [
                        {
                            "id": "popup-target",
                            "type": "browser_ui",
                            "title": "Omnibox Popup",
                        },
                        {
                            "id": "ordinary-page",
                            "type": "page",
                            "title": "Omnibox Popup",
                        },
                        {
                            "id": "side-panel",
                            "type": "browser_ui",
                            "title": "Side panel",
                        },
                        {
                            "id": "promo-kept-for-non-publish-consumer",
                            "type": "page",
                            "title": "Настройка цены | Авито",
                            "url": (
                                "https://www.avito.ru/cpxpromo/8330238411"
                                "?vasFrom=item_add"
                            ),
                        },
                    ]
                )
            raise AssertionError(f"Неожиданный HTTP-запрос: {url}")

        pw = _Playwright()
        with mock.patch.object(browser, "urlopen", side_effect=fake_urlopen):
            context = asyncio.run(
                browser.connect_over_cdp(pw, "http://127.0.0.1:9222")
            )

        self.assertIsInstance(context, _Context)
        self.assertEqual(
            requested_urls,
            [
                "http://127.0.0.1:9222/json/version",
                "http://127.0.0.1:9222/json/list",
            ],
        )
        self.assertEqual(
            pw.chromium.calls,
            [("http://127.0.0.1:9222", {"timeout": 30_000})],
        )

    def test_preflight_closes_only_unresponsive_avito_page(self) -> None:
        """Publisher закрывает зависшую вкладку Авито, сохраняя все рабочие вкладки."""
        requested_urls: list[str] = []

        async def fake_probe(target: dict[str, object]) -> bool:
            return target.get("id") != "zombie-avito-home"

        def fake_urlopen(request: object, *, timeout: float) -> _HttpResponse:
            self.assertLessEqual(timeout, 3.0)
            url = request.full_url  # type: ignore[attr-defined]
            requested_urls.append(url)
            if url.endswith("/json/version"):
                return _HttpResponse({"Browser": "Chrome/151.0"})
            if url.endswith("/json/list"):
                return _HttpResponse(
                    [
                        {
                            "id": "zombie-avito-home",
                            "type": "page",
                            "title": "Новое сообщение",
                            "url": "https://www.avito.ru/",
                            "webSocketDebuggerUrl": (
                                "ws://127.0.0.1:9222/devtools/page/zombie-avito-home"
                            ),
                        },
                        {
                            "id": "filled-form",
                            "type": "page",
                            "title": "Новое объявление",
                            "url": "https://www.avito.ru/additem",
                            "webSocketDebuggerUrl": (
                                "ws://127.0.0.1:9222/devtools/page/filled-form"
                            ),
                        },
                        {
                            "id": "responsive-promo",
                            "type": "page",
                            "title": "Настройка цены | Авито",
                            "url": (
                                "https://www.avito.ru/cpxpromo/8330238411"
                                "?vasFrom=item_add"
                            ),
                            "webSocketDebuggerUrl": (
                                "ws://127.0.0.1:9222/devtools/page/responsive-promo"
                            ),
                        },
                        {
                            "id": "foreign-page",
                            "type": "page",
                            "title": "Не Авито",
                            "url": "https://example.com/cpxpromo/8330238411",
                            "webSocketDebuggerUrl": (
                                "ws://127.0.0.1:9222/devtools/page/foreign-page"
                            ),
                        },
                    ]
                )
            if url.endswith("/json/close/zombie-avito-home"):
                return _HttpResponse({"result": "Target is closing"})
            raise AssertionError(f"Неожиданный HTTP-запрос: {url}")

        pw = _Playwright()
        with (
            mock.patch.object(browser, "urlopen", side_effect=fake_urlopen),
            mock.patch.object(
                browser,
                "_probe_page_target",
                new=mock.AsyncMock(side_effect=fake_probe),
                create=True,
            ) as probe_mock,
        ):
            context = asyncio.run(
                browser.connect_over_cdp(
                    pw,
                    "http://127.0.0.1:9222",
                    repair_unresponsive_avito_pages=True,
                )
            )

        self.assertIsInstance(context, _Context)
        self.assertEqual(
            requested_urls,
            [
                "http://127.0.0.1:9222/json/version",
                "http://127.0.0.1:9222/json/list",
                "http://127.0.0.1:9222/json/close/zombie-avito-home",
            ],
        )
        self.assertEqual(probe_mock.await_count, 4)
        self.assertEqual(
            pw.chromium.calls,
            [("http://127.0.0.1:9222", {"timeout": 30_000})],
        )

    def test_preflight_never_closes_unresponsive_foreign_page(self) -> None:
        """Чужую зависшую вкладку publisher не закрывает без разрешения пользователя."""
        requested_urls: list[str] = []

        def fake_urlopen(request: object, *, timeout: float) -> _HttpResponse:
            del timeout
            url = request.full_url  # type: ignore[attr-defined]
            requested_urls.append(url)
            if url.endswith("/json/version"):
                return _HttpResponse({"Browser": "Chrome/151.0"})
            if url.endswith("/json/list"):
                return _HttpResponse(
                    [
                        {
                            "id": "foreign-zombie",
                            "type": "page",
                            "title": "Важный редактор",
                            "url": "https://example.com/editor",
                            "webSocketDebuggerUrl": (
                                "ws://127.0.0.1:9222/devtools/page/foreign-zombie"
                            ),
                        }
                    ]
                )
            raise AssertionError(f"Вкладка другого сайта не должна закрываться: {url}")

        pw = _Playwright()
        with (
            mock.patch.object(browser, "urlopen", side_effect=fake_urlopen),
            mock.patch.object(
                browser,
                "_probe_page_target",
                new=mock.AsyncMock(return_value=False),
                create=True,
            ),
            self.assertRaisesRegex(
                browser.CdpUnresponsivePageError,
                "Важный редактор.*example[.]com/editor",
            ),
        ):
            asyncio.run(
                browser.connect_over_cdp(
                    pw,
                    "http://127.0.0.1:9222",
                    repair_unresponsive_avito_pages=True,
                )
            )

        self.assertEqual(
            requested_urls,
            [
                "http://127.0.0.1:9222/json/version",
                "http://127.0.0.1:9222/json/list",
            ],
        )
        self.assertEqual(pw.chromium.calls, [])

    def test_publisher_cleanup_closes_only_exact_stale_avito_publish_routes(self) -> None:
        requested_urls: list[str] = []

        def fake_urlopen(request: object, *, timeout: float) -> _HttpResponse:
            del timeout
            url = request.full_url  # type: ignore[attr-defined]
            requested_urls.append(url)
            if url.endswith("/json/version"):
                return _HttpResponse({"Browser": "Chrome/151.0"})
            if url.endswith("/json/list"):
                return _HttpResponse([
                    {
                        "id": "stale-form",
                        "type": "page",
                        "url": "https://www.avito.ru/additem?draftId=10",
                    },
                    {
                        "id": "stale-price",
                        "type": "page",
                        "url": "https://www.avito.ru/cpxpromo/8330238411",
                    },
                    {
                        "id": "avito-home",
                        "type": "page",
                        "url": "https://www.avito.ru/",
                    },
                    {
                        "id": "foreign-form",
                        "type": "page",
                        "url": "https://example.com/additem",
                    },
                    {
                        "id": "ad-frame",
                        "type": "iframe",
                        "url": "https://www.avito.ru/additem",
                    },
                ])
            if url.endswith("/json/close/stale-form"):
                return _HttpResponse({"result": "Target is closing"})
            if url.endswith("/json/close/stale-price"):
                return _HttpResponse({"result": "Target is closing"})
            raise AssertionError(f"Неожиданный запрос: {url}")

        pw = _Playwright()
        with mock.patch.object(browser, "urlopen", side_effect=fake_urlopen):
            asyncio.run(
                browser.connect_over_cdp(
                    pw,
                    "http://127.0.0.1:9222",
                    cleanup_stale_publish_pages=True,
                )
            )

        self.assertEqual(requested_urls, [
            "http://127.0.0.1:9222/json/version",
            "http://127.0.0.1:9222/json/list",
            "http://127.0.0.1:9222/json/close/stale-form",
            "http://127.0.0.1:9222/json/close/stale-price",
        ])

    def test_unavailable_http_endpoint_is_reported_before_websocket_connect(self) -> None:
        """Недоступный Chrome отличается от зависшей инициализации Playwright."""
        pw = _Playwright()

        with (
            mock.patch.object(
                browser,
                "urlopen",
                side_effect=URLError("connection refused"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "CDP endpoint недоступен.*перезапусти start-chrome[.]bat",
            ),
        ):
            asyncio.run(
                browser.connect_over_cdp(pw, "http://127.0.0.1:9222")
            )

        self.assertEqual(pw.chromium.calls, [])

    def test_connected_websocket_initialization_timeout_has_precise_message(self) -> None:
        """Доступный endpoint с зависшим Playwright не называется отсутствующим Chrome."""

        def fake_urlopen(request: object, *, timeout: float) -> _HttpResponse:
            del timeout
            url = request.full_url  # type: ignore[attr-defined]
            if url.endswith("/json/version"):
                return _HttpResponse({"Browser": "Chrome/150.0"})
            if url.endswith("/json/list"):
                return _HttpResponse([])
            raise AssertionError(f"Неожиданный HTTP-запрос: {url}")

        pw = _Playwright()
        pw.chromium.connect_over_cdp = mock.AsyncMock(  # type: ignore[method-assign]
            side_effect=PlaywrightTimeoutError(
                "Timeout 30000ms exceeded.\nCall log:\n  - <ws connected>"
            )
        )

        with (
            mock.patch.object(browser, "urlopen", side_effect=fake_urlopen),
            self.assertRaisesRegex(
                browser.CdpInitializationTimeoutError,
                "WebSocket подключён.*инициализацию.*30 секунд.*"
                "перезапусти start-chrome[.]bat",
            ),
        ):
            asyncio.run(
                browser.connect_over_cdp(pw, "http://127.0.0.1:9222")
            )

        pw.chromium.connect_over_cdp.assert_awaited_once_with(
            "http://127.0.0.1:9222",
            timeout=30_000,
        )

    def test_publisher_preserves_precise_cdp_timeout_diagnostic(self) -> None:
        """Publisher не должен заменять точную CDP-ошибку на «Chrome не найден»."""
        job = {"drafts_total": 1}
        draft = publisher.DraftData(
            title="Футболка Zara мужская",
            trade_type="Продаю своё",
            condition="Новое с биркой",
            size="48 (M)",
            brand="Zara",
            color="Белый",
            description="Описание",
            price=990,
            locations=(
                publisher.LocationData("Санкт-Петербург", "Невский проспект, 40"),
            ),
            view_price_max=Decimal("0.5"),
            photo_paths=("photo.jpg",),
            category="tshirts",
            item_type="Футболка",
        )
        precise_error = browser.CdpInitializationTimeoutError(
            "WebSocket подключён, но Playwright не завершил инициализацию за "
            "30 секунд. Перезапусти start-chrome.bat."
        )
        connect_mock = mock.AsyncMock(side_effect=precise_error)

        with (
            mock.patch(
                "playwright.async_api.async_playwright",
                return_value=_PlaywrightManager(),
            ),
            mock.patch.object(
                browser,
                "connect_over_cdp",
                new=connect_mock,
            ),
            mock.patch.object(
                publisher,
                "_dump_failure",
                new=mock.AsyncMock(return_value=None),
            ),
        ):
            asyncio.run(
                publisher.run_publish_job(
                    "cdp-timeout-test",
                    job,
                    draft,
                    cdp_url="http://127.0.0.1:9222",
                )
            )

        self.assertEqual(job["status"], "failed")
        self.assertIn("WebSocket подключён", job["error"])
        self.assertIn("перезапусти start-chrome.bat", job["error"].lower())
        self.assertNotIn("Chrome не найден", job["error"])
        connect_mock.assert_awaited_once_with(
            mock.ANY,
            "http://127.0.0.1:9222",
            repair_unresponsive_avito_pages=True,
            cleanup_stale_publish_pages=True,
        )


if __name__ == "__main__":
    unittest.main()
