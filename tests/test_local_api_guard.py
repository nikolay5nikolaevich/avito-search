"""
Тесты защиты локального API (backend/app.py): CSRF, DNS-rebinding, path traversal,
белый список расширений фото.

httpx/TestClient в проекте нет — ASGI-приложение вызывается напрямую.
"""

from __future__ import annotations

import asyncio
import os
import sys
import io
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app as app_module  # noqa: E402
import stats_collector  # noqa: E402


def _call(method: str, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """Один ASGI-запрос без тела; возвращает (статус, тело)."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 7777),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app_module.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


HOST = {"host": "127.0.0.1:7777"}


class CsrfGuardTests(unittest.TestCase):
    def test_post_without_local_header_forbidden(self) -> None:
        status, _ = _call("POST", "/api/publish/close/nope", HOST)
        self.assertEqual(status, 403)

    def test_post_with_foreign_origin_forbidden(self) -> None:
        headers = {**HOST, "x-avito-local": "1", "origin": "https://evil.example"}
        status, _ = _call("POST", "/api/publish/close/nope", headers)
        self.assertEqual(status, 403)

    def test_post_with_null_origin_forbidden(self) -> None:
        headers = {**HOST, "x-avito-local": "1", "origin": "null"}
        status, _ = _call("POST", "/api/publish/close/nope", headers)
        self.assertEqual(status, 403)

    def test_post_with_header_and_own_origin_passes_guard(self) -> None:
        headers = {**HOST, "x-avito-local": "1", "origin": "http://localhost:7777"}
        status, _ = _call("POST", "/api/publish/close/nope", headers)
        self.assertNotEqual(status, 403)

    def test_post_with_header_without_origin_passes_guard(self) -> None:
        status, _ = _call("POST", "/api/publish/close/nope", {**HOST, "x-avito-local": "1"})
        self.assertNotEqual(status, 403)

    def test_get_needs_no_header(self) -> None:
        status, _ = _call("GET", "/api/bootstrap", HOST)
        self.assertEqual(status, 200)


class HostGuardTests(unittest.TestCase):
    def test_foreign_host_rejected(self) -> None:
        status, _ = _call("GET", "/api/bootstrap", {"host": "evil.example:7777"})
        self.assertEqual(status, 400)

    def test_localhost_allowed(self) -> None:
        status, _ = _call("GET", "/api/bootstrap", {"host": "localhost:7777"})
        self.assertEqual(status, 200)


class AssetsTraversalTests(unittest.TestCase):
    def test_parent_dir_path_is_404(self) -> None:
        # uvicorn декодирует %2F, поэтому в scope["path"] уже живые "../"
        status, _ = _call("GET", "/assets/../../../README.md", HOST)
        self.assertEqual(status, 404)

    def test_missing_asset_is_404(self) -> None:
        status, _ = _call("GET", "/assets/no-such-file.js", HOST)
        self.assertEqual(status, 404)


class PhotoExtensionTests(unittest.TestCase):
    def test_whitelist(self) -> None:
        ext = lambda name: app_module._photo_ext(SimpleNamespace(filename=name))  # noqa: E731
        self.assertEqual(ext("a.JPG"), ".jpg")
        self.assertEqual(ext("a.heic"), ".heic")
        self.assertEqual(ext("noext"), ".jpg")
        self.assertIsNone(ext("a.html"))
        self.assertIsNone(ext("a.svg"))
        self.assertIsNone(ext("a.jpg.exe"))


class ErrorTruncationTests(unittest.TestCase):
    def test_short_error_limit(self) -> None:
        self.assertEqual(len(app_module._short_error(ValueError("x" * 1000))), 300)


class TokenErrorBodyTests(unittest.TestCase):
    def _http_error(self, url: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(url, 401, "x", {}, io.BytesIO(b"SECRET-BODY"))

    def test_token_body_not_in_error(self) -> None:
        url = f"{stats_collector.BASE_URL}/token"
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(url)):
            with self.assertRaises(stats_collector.StatsCollectorError) as ctx:
                stats_collector._http_json("POST", url)
        self.assertNotIn("SECRET-BODY", str(ctx.exception))

    def test_other_endpoint_keeps_body(self) -> None:
        url = f"{stats_collector.BASE_URL}/core/v1/accounts"
        with mock.patch("urllib.request.urlopen", side_effect=self._http_error(url)):
            with self.assertRaises(stats_collector.StatsCollectorError) as ctx:
                stats_collector._http_json("GET", url)
        self.assertIn("SECRET-BODY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
