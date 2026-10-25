"""
Тесты защиты локального сервиса (backend/app.py): CSRF-заголовок, Origin,
Host, path traversal в /assets, расширения загружаемых фото.

httpx/TestClient в проекте нет — ASGI-приложение вызывается напрямую, со
сконструированным scope. Так сырой путь с «..» и «%2F» доходит до приложения
в том виде, в каком его отдаёт uvicorn (path уже декодирован, raw_path — нет),
без нормализации клиентом.

Ни один тест не запускает браузер, CDP или публикацию: защищённым эндпоинтам
шлётся заведомо невалидное тело (422/404), а где валидная форма нужна для
проверки фото — фоновая задача подготовки замокана, каталог tmp/publish
подменён временным.

Ожидания написаны от требований безопасности, а не по коду защиты.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import quote

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app as app_module  # noqa: E402

LOCAL_HEADER = ("x-avito-local", "1")
GOOD_HOST = "localhost:7777"
GOOD_ORIGINS = ("http://localhost:7777", "http://127.0.0.1:7777")
EVIL_ORIGIN = "https://evil.example"


# ---------------------------------------------------------------------------
# Минимальный ASGI-клиент
# ---------------------------------------------------------------------------

def asgi_request(
    method: str,
    path: str,
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
    host: str = GOOD_HOST,
    raw_path: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Один запрос к app напрямую. path — уже декодированный (как у uvicorn)."""
    hdrs = [("host", host)] if host is not None else []
    hdrs += list(headers or [])
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": (raw_path if raw_path is not None else quote(path)).encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in hdrs],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 7777),
        "extensions": {},
    }
    result: dict = {"status": None, "headers": {}, "body": b""}

    async def run() -> None:
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message) -> None:
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
                result["headers"] = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in message["headers"]
                }
            elif message["type"] == "http.response.body":
                result["body"] += message.get("body", b"")

        await app_module.app(scope, receive, send)

    asyncio.run(run())
    return result["status"], result["headers"], result["body"]


def multipart(
    fields: dict[str, str],
    files: list[tuple[str, str, str, bytes]] | None = None,
) -> tuple[bytes, str]:
    """multipart/form-data; files: (поле, имя_файла, content_type, байты)."""
    boundary = "----secTestBoundary7f3a"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for name, filename, ctype, data in files or []:
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'
            ).encode("utf-8") + data + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Защищённые эндпоинты: (имя, путь, функция, строящая (тело, content-type)).
# Тело заведомо невалидное — валидный запрос ничего не запустит.
# ---------------------------------------------------------------------------

def _json_body(payload: dict) -> tuple[bytes, str]:
    return json.dumps(payload).encode("utf-8"), "application/json"


def _empty_multipart() -> tuple[bytes, str]:
    return multipart({})


_RANDOM_ID = str(uuid.uuid4())

PROTECTED_ENDPOINTS: list[tuple[str, str, callable]] = [
    ("publish_start", "/api/publish/start", _empty_multipart),
    ("publish_resume", f"/api/publish/resume/{_RANDOM_ID}", lambda: (b"", "application/json")),
    ("publish_close", f"/api/publish/close/{_RANDOM_ID}", lambda: (b"", "application/json")),
    ("publish_prepare", "/api/publish/prepare", _empty_multipart),
    ("seller_scan", "/api/seller/scan", lambda: _json_body({})),
    ("resale_scan", "/api/resale/scan", lambda: _json_body({})),
    ("outreach_send", f"/api/outreach/send/{_RANDOM_ID}", lambda: _json_body({})),
    ("outreach_collect", "/api/outreach/collect", lambda: _json_body({})),
]


def _post(path: str, build, extra_headers: list[tuple[str, str]]) -> tuple[int, dict, bytes]:
    body, ctype = build()
    return asgi_request(
        "POST", path,
        headers=[("content-type", ctype)] + extra_headers,
        body=body,
    )


class _InProjectRoot(unittest.TestCase):
    """app.py рассчитан на запуск из корня проекта (относительные пути)."""

    def setUp(self) -> None:
        self._cwd = os.getcwd()
        os.chdir(PROJECT_ROOT)
        self.addCleanup(os.chdir, self._cwd)


# ---------------------------------------------------------------------------
# 1. Нет X-Avito-Local -> 403
# ---------------------------------------------------------------------------

class MissingLocalHeaderRejected(_InProjectRoot):
    """Ожидание: любой не-GET запрос к /api/... без X-Avito-Local: 1 -> 403."""

    def test_post_without_header_is_403(self):
        for name, path, build in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=name):
                status, _, _ = _post(path, build, [])
                self.assertEqual(status, 403, f"{path} без X-Avito-Local должен давать 403")

    def test_post_without_header_with_good_origin_is_still_403(self):
        """Правильный Origin не заменяет заголовок X-Avito-Local."""
        for name, path, build in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=name):
                status, _, _ = _post(path, build, [("origin", GOOD_ORIGINS[0])])
                self.assertEqual(status, 403)

    def test_wrong_header_value_is_403(self):
        """Заголовок с другим значением (не «1») не считается подтверждением."""
        for name, path, build in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=name):
                status, _, _ = _post(path, build, [("x-avito-local", "0")])
                self.assertEqual(status, 403)

    def test_other_unsafe_methods_without_header_are_403(self):
        """PUT/DELETE/PATCH к /api/... тоже требуют заголовок (даже на несуществующем пути)."""
        for method in ("PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status, _, _ = asgi_request(method, "/api/publish/start")
                self.assertEqual(status, 403)


# ---------------------------------------------------------------------------
# 2. Заголовок есть, Origin чужой -> 403
# ---------------------------------------------------------------------------

class ForeignOriginRejected(_InProjectRoot):
    """Ожидание: X-Avito-Local: 1 + Origin: https://evil.example -> 403."""

    def test_post_with_header_and_evil_origin_is_403(self):
        for name, path, build in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=name):
                status, _, _ = _post(path, build, [LOCAL_HEADER, ("origin", EVIL_ORIGIN)])
                self.assertEqual(status, 403, f"{path}: чужой Origin должен давать 403")

    def test_origin_with_local_hostname_prefix_is_403(self):
        """Обход проверкой по префиксу/подстроке: localhost.evil.example не наш Origin."""
        for origin in ("http://localhost.evil.example", "http://localhost:7777.evil.example",
                       "https://evil.example/http://localhost:7777"):
            with self.subTest(origin=origin):
                status, _, _ = _post(
                    "/api/seller/scan", lambda: _json_body({}),
                    [LOCAL_HEADER, ("origin", origin)],
                )
                self.assertEqual(status, 403)

    def test_origin_null_is_403(self):
        """Origin: null (sandbox iframe, file://) — не наш источник."""
        status, _, _ = _post(
            "/api/seller/scan", lambda: _json_body({}),
            [LOCAL_HEADER, ("origin", "null")],
        )
        self.assertEqual(status, 403)


# ---------------------------------------------------------------------------
# 3. Легитимные запросы защитой не отклоняются
# ---------------------------------------------------------------------------

class LegitimateRequestsPassGuard(_InProjectRoot):
    """
    Ожидание: X-Avito-Local: 1 + свой Origin (или без Origin) -> защита пропускает.
    Тело невалидное, поэтому эндпоинт отвечает 4xx валидации; проверяем лишь,
    что это не отказ защиты (не 403) и не 5xx.
    """

    def _check(self, extra: list[tuple[str, str]]) -> None:
        for name, path, build in PROTECTED_ENDPOINTS:
            with self.subTest(endpoint=name):
                status, _, body = _post(path, build, [LOCAL_HEADER] + extra)
                self.assertNotEqual(status, 403, f"{path}: защита отклонила легитимный запрос")
                self.assertLess(status, 500, f"{path}: неожиданная ошибка сервера {status}")

    def test_without_origin_not_403(self):
        self._check([])

    def test_with_localhost_origin_not_403(self):
        self._check([("origin", GOOD_ORIGINS[0])])

    def test_with_loopback_ip_origin_not_403(self):
        self._check([("origin", GOOD_ORIGINS[1])])

    def test_get_requests_do_not_need_header(self):
        """GET (чтение) заголовка не требует: фронт и ссылки на экспорт продолжают работать."""
        status, _, _ = asgi_request("GET", "/api/publish/categories")
        self.assertEqual(status, 200)


# ---------------------------------------------------------------------------
# 4. Path traversal в /assets
# ---------------------------------------------------------------------------

README_PATH = Path(PROJECT_ROOT) / "README.md"


class AssetsTraversal(_InProjectRoot):
    """Ожидание: выход из frontend/dist/assets через «..» даёт 404 без содержимого файла."""

    def _readme_marker(self) -> bytes:
        text = README_PATH.read_bytes()
        marker = text[:80].strip()
        self.assertTrue(marker, "README.md пуст — нечем проверять утечку")
        return marker

    def _assert_not_leaked(self, path: str, raw_path: str | None, marker: bytes | None) -> None:
        status, _, body = asgi_request("GET", path, raw_path=raw_path)
        self.assertEqual(status, 404, f"{raw_path or path}: ожидали 404, получили {status}")
        if marker is not None:
            self.assertNotIn(marker, body, "тело ответа содержит начало README.md")

    def test_encoded_slash_traversal_to_readme(self):
        marker = self._readme_marker()
        self._assert_not_leaked(
            "/assets/../../../README.md", "/assets/..%2F..%2F..%2FREADME.md", marker,
        )

    def test_raw_dotdot_traversal_to_readme(self):
        marker = self._readme_marker()
        self._assert_not_leaked("/assets/../../README.md", "/assets/../../README.md", marker)
        self._assert_not_leaked("/assets/../../../README.md", "/assets/../../../README.md", marker)

    def test_traversal_to_dotenv(self):
        """Тело не читаем и не выводим: .env содержит секреты, проверяем только статус."""
        self._assert_not_leaked(
            "/assets/../../../.env", "/assets/..%2F..%2F..%2F.env", None,
        )
        self._assert_not_leaked("/assets/../../../.env", "/assets/../../../.env", None)

    def test_backslash_traversal_to_readme(self):
        """Windows: «..\\» тоже выход из каталога (путь собирается через pathlib)."""
        marker = self._readme_marker()
        self._assert_not_leaked(
            "/assets/..\\..\\..\\README.md", "/assets/..%5C..%5C..%5CREADME.md", marker,
        )

    def test_absolute_path_is_not_served(self):
        """/assets//abs/path не должен превращаться в абсолютный путь на диске."""
        marker = self._readme_marker()
        abs_readme = str(README_PATH).replace("\\", "/")
        self._assert_not_leaked("/assets/" + abs_readme, None, marker)
        self._assert_not_leaked("/assets//" + abs_readme.lstrip("/"), None, marker)

    def test_existing_asset_still_served(self):
        assets_dir = Path(PROJECT_ROOT) / "frontend" / "dist" / "assets"
        files = [p for p in assets_dir.glob("*") if p.is_file()] if assets_dir.is_dir() else []
        if not files:
            self.skipTest("frontend/dist/assets пуст — фронтенд не собран")
        target = files[0]
        status, _, body = asgi_request("GET", f"/assets/{target.name}")
        self.assertEqual(status, 200)
        self.assertEqual(body, target.read_bytes())


# ---------------------------------------------------------------------------
# 5. Host
# ---------------------------------------------------------------------------

class HostHeader(_InProjectRoot):
    """Ожидание: чужой Host (DNS rebinding) -> 400; свой Host работает."""

    def test_foreign_host_rejected_on_get(self):
        status, _, _ = asgi_request("GET", "/api/publish/categories", host="evil.example")
        self.assertEqual(status, 400)

    def test_foreign_host_with_port_rejected(self):
        status, _, _ = asgi_request("GET", "/api/publish/categories", host="evil.example:7777")
        self.assertEqual(status, 400)

    def test_foreign_host_rejected_on_post_even_with_header(self):
        status, _, _ = asgi_request(
            "POST", "/api/seller/scan", host="evil.example",
            headers=[LOCAL_HEADER, ("content-type", "application/json")], body=b"{}",
        )
        self.assertEqual(status, 400)

    def test_foreign_host_rejected_for_index_and_assets(self):
        for path in ("/", "/assets/anything.js"):
            with self.subTest(path=path):
                status, _, _ = asgi_request("GET", path, host="evil.example")
                self.assertEqual(status, 400)

    def test_local_hosts_accepted(self):
        for host in ("localhost:7777", "127.0.0.1:7777"):
            with self.subTest(host=host):
                status, _, _ = asgi_request("GET", "/api/publish/categories", host=host)
                self.assertEqual(status, 200)


# ---------------------------------------------------------------------------
# 6. Расширения загружаемых фото
# ---------------------------------------------------------------------------

# Исполняемый/активный контент под видом картинки: расширение плохое, MIME врёт.
BAD_PHOTOS = [
    ("evil.exe", "image/jpeg"),
    ("evil.exe", "application/octet-stream"),
    ("evil.html", "image/png"),
    ("evil.html", "text/html"),
]


def _body_mentions_photos(raw: bytes) -> bool:
    low = raw.decode("utf-8", errors="replace").lower()
    return "photo" in low or "фото" in low or "формат" in low or "расширен" in low


class PhotoExtensions(_InProjectRoot):
    """Ожидание: расширение .exe/.html отклоняется (400/422) независимо от заявленного MIME."""

    def test_prepare_rejects_bad_extension_even_with_valid_form(self):
        """Остальная форма валидна — отказ может быть только из-за фото; задача не создаётся."""
        for filename, ctype in BAD_PHOTOS:
            with self.subTest(file=filename, mime=ctype), \
                    tempfile.TemporaryDirectory() as tmp:
                async def _noop(*args, **kwargs):
                    return None

                jobs_before = dict(app_module.PREP_JOBS)
                body, content_type = multipart(
                    {"title": "Тест", "description": "Описание", "drafts_count": "1"},
                    [("photos", filename, ctype, b"MZ\x90\x00 not an image")],
                )
                with mock.patch.object(app_module, "TMP_PUBLISH_DIR", Path(tmp) / "publish"), \
                        mock.patch.object(app_module, "_run_prep_job_with_journal", _noop):
                    status, _, resp = asgi_request(
                        "POST", "/api/publish/prepare",
                        headers=[LOCAL_HEADER, ("content-type", content_type)], body=body,
                    )
                    new_jobs = set(app_module.PREP_JOBS) - set(jobs_before)
                    for prep_id in new_jobs:
                        app_module.PREP_JOBS.pop(prep_id, None)
                    saved = [p.name for p in (Path(tmp) / "publish").rglob("*") if p.is_file()]
                self.assertIn(status, (400, 422), f"{filename} ({ctype}): статус {status}")
                self.assertFalse(new_jobs, f"{filename} ({ctype}): задача подготовки создана")
                self.assertFalse(saved, f"{filename} ({ctype}): файл записан на диск: {saved}")

    def test_publish_start_rejects_bad_extension(self):
        """
        Форма заведомо неполная (ничего не запустится) -> 422 в любом случае;
        доказываем, что среди причин отказа названо фото.
        """
        for filename, ctype in BAD_PHOTOS:
            with self.subTest(file=filename, mime=ctype):
                body, content_type = multipart(
                    {}, [("photos", filename, ctype, b"MZ\x90\x00 not an image")],
                )
                with mock.patch.object(app_module, "_start_publish_conflict", return_value=None):
                    status, _, resp = asgi_request(
                        "POST", "/api/publish/start",
                        headers=[LOCAL_HEADER, ("content-type", content_type)], body=body,
                    )
                self.assertIn(status, (400, 422), f"{filename} ({ctype}): статус {status}")
                self.assertTrue(
                    _body_mentions_photos(resp),
                    f"{filename} ({ctype}): в ответе нет претензии к фото: {resp[:300]!r}",
                )

    def test_prepare_accepts_good_extension(self):
        """Контроль: .jpg не отклоняется из-за фото (не сломали легитимную загрузку)."""
        with tempfile.TemporaryDirectory() as tmp:
            async def _noop(*args, **kwargs):
                return None

            jobs_before = set(app_module.PREP_JOBS)
            body, content_type = multipart(
                {"title": "Тест", "description": "Описание", "drafts_count": "1"},
                [("photos", "ok.jpg", "image/jpeg", b"\xff\xd8\xff\xe0 fake")],
            )
            with mock.patch.object(app_module, "TMP_PUBLISH_DIR", Path(tmp) / "publish"), \
                    mock.patch.object(app_module, "_run_prep_job_with_journal", _noop):
                status, _, _ = asgi_request(
                    "POST", "/api/publish/prepare",
                    headers=[LOCAL_HEADER, ("content-type", content_type)], body=body,
                )
            for prep_id in set(app_module.PREP_JOBS) - jobs_before:
                app_module.PREP_JOBS.pop(prep_id, None)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
