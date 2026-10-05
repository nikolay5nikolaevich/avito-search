"""
Тесты эндпоинтов «Разбора продавца» (backend/app.py, /api/seller/*).

httpx/TestClient в проекте нет — обработчики вызываются напрямую через
asyncio.run, как в tests/test_outreach_api.py. seller_scan.scan_seller всегда
замокан: ни одного обращения к живому Авито.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app as app_module  # noqa: E402
import seller_scan  # noqa: E402
from parser import AvitoBlockedError  # noqa: E402


class _FakeRequest:
    """Подмена fastapi.Request — эндпоинт использует только await .json()."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def _read_streaming_body(response) -> bytes:
    """StreamingResponse.body_iterator — async-генератор, читаем его целиком."""
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


async def _run_and_drain(coro):
    """Дожидается фоновых задач, запущенных через asyncio.create_task."""
    response = await coro
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    if pending:
        await asyncio.gather(*pending)
    return response


ITEM_A = {"url": "https://www.avito.ru/moskva/telefony/a_1234567", "title": "А",
          "views_total": 100, "views_today": 5, "error": None}
ITEM_B = {"url": "https://www.avito.ru/moskva/telefony/b_2234567", "title": "Б",
          "views_total": 50, "views_today": 20, "error": None}
ITEM_C_NO_VIEWS = {"url": "https://www.avito.ru/moskva/telefony/c_3234567", "title": "В",
                    "views_total": None, "views_today": None,
                    "error": "Не удалось прочитать просмотры на странице объявления"}


class SellerScanApiTestCase(unittest.TestCase):
    """Общий сброс реестра задач — тесты не должны видеть чужие job'ы."""

    def setUp(self) -> None:
        self.app_module = app_module
        self.app_module.SELLER_JOBS.clear()
        self.app_module.OUTREACH_JOBS.clear()
        self.app_module.PUBLISH_JOBS.clear()
        self.addCleanup(self.app_module.SELLER_JOBS.clear)
        self.addCleanup(self.app_module.OUTREACH_JOBS.clear)
        self.addCleanup(self.app_module.PUBLISH_JOBS.clear)

    def _scan(self, payload):
        return asyncio.run(
            _run_and_drain(self.app_module.api_seller_scan(_FakeRequest(payload)))
        )


# ── Валидация входа ─────────────────────────────────────────────────────────

class ScanValidationTests(SellerScanApiTestCase):
    def test_empty_payload_rejected(self) -> None:
        response = self._scan({})
        self.assertEqual(response.status_code, 422)
        errors = json.loads(response.body)["errors"]
        fields = {error["field"] for error in errors}
        self.assertEqual(fields, {"url", "limit"})

    def test_bad_url_rejected(self) -> None:
        response = self._scan({"url": "https://evil.example/brands/x", "limit": 10})
        self.assertEqual(response.status_code, 422)

    def test_limit_out_of_range_rejected(self) -> None:
        for limit in (0, 301, "не число"):
            with self.subTest(limit=limit):
                response = self._scan({
                    "url": "https://www.avito.ru/brands/abc123", "limit": limit,
                })
                self.assertEqual(response.status_code, 422)

    def test_valid_payload_creates_job(self) -> None:
        fake_scan = mock.AsyncMock(return_value={
            "found_total": 2, "scanned": 0, "errors": 0, "items": [],
        })
        with mock.patch.object(seller_scan, "scan_seller", new=fake_scan):
            response = self._scan({"url": "https://www.avito.ru/brands/abc123", "limit": 10})
        self.assertEqual(response.status_code, 200)
        fake_scan.assert_awaited_once()
        # URL должен дойти до сканера уже нормализованным (bare /brands/<id>
        # дополняется до /items/all — см. seller_scan.validate_profile_url).
        awaited_url = fake_scan.await_args.args[0]
        self.assertEqual(awaited_url, "https://www.avito.ru/brands/abc123/items/all")


# ── Занятость Chrome другой задачей ─────────────────────────────────────────

class BusyTests(SellerScanApiTestCase):
    def _payload(self):
        return {"url": "https://www.avito.ru/brands/abc123", "limit": 10}

    def test_second_scan_while_running_is_rejected(self) -> None:
        self.app_module.SELLER_JOBS["already-running"] = {
            "status": "running", "current": 0, "total": 10, "label": "",
            "error": None, "result": None, "profile_url": "x", "limit": 10,
        }
        response = self._scan(self._payload())
        self.assertEqual(response.status_code, 409)

    def test_rejected_while_outreach_running(self) -> None:
        self.app_module.OUTREACH_JOBS["outreach-1"] = {
            "kind": "collect", "status": "running", "current": 0, "total": 5,
            "label": "", "error": None, "candidates": None, "send_job_id": None,
        }
        response = self._scan(self._payload())
        self.assertEqual(response.status_code, 409)

    def test_rejected_while_publish_running(self) -> None:
        self.app_module.PUBLISH_JOBS["publish-1"] = {"status": "running"}
        response = self._scan(self._payload())
        self.assertEqual(response.status_code, 409)


# ── Жизненный цикл задачи ───────────────────────────────────────────────────

class ScanJobTests(SellerScanApiTestCase):
    def test_success_populates_status_and_result(self) -> None:
        fake_scan = mock.AsyncMock(return_value={
            "found_total": 170, "scanned": 3, "errors": 1,
            "items": [ITEM_B, ITEM_A, ITEM_C_NO_VIEWS],
        })
        with mock.patch.object(seller_scan, "scan_seller", new=fake_scan):
            started = self._scan({"url": "https://www.avito.ru/brands/abc123", "limit": 3})
        job_id = json.loads(started.body)["job_id"]

        status = asyncio.run(self.app_module.api_seller_scan_status(job_id))
        status_body = json.loads(status.body)
        self.assertEqual(status_body["status"], "done")

        result = asyncio.run(self.app_module.api_seller_scan_result(job_id))
        result_body = json.loads(result.body)
        self.assertEqual(result_body["found_total"], 170)
        self.assertEqual(result_body["scanned"], 3)
        self.assertEqual(result_body["errors"], 1)
        # По просмотрам всего: А(100) > Б(50) > В(None, в конце, с пометкой)
        self.assertEqual(
            [item["url"] for item in result_body["items_by_total"]],
            [ITEM_A["url"], ITEM_B["url"], ITEM_C_NO_VIEWS["url"]],
        )
        # По просмотрам сегодня: Б(20) > А(5) > В(None)
        self.assertEqual(
            [item["url"] for item in result_body["items_by_today"]],
            [ITEM_B["url"], ITEM_A["url"], ITEM_C_NO_VIEWS["url"]],
        )

    def test_avito_blocked_error_stops_whole_task(self) -> None:
        fake_scan = mock.AsyncMock(side_effect=AvitoBlockedError("капча"))
        with mock.patch.object(seller_scan, "scan_seller", new=fake_scan):
            started = self._scan({"url": "https://www.avito.ru/brands/abc123", "limit": 10})
        job_id = json.loads(started.body)["job_id"]
        job = self.app_module.SELLER_JOBS[job_id]
        self.assertEqual(job["status"], "blocked")
        self.assertIn("заблокировал", job["error"])

    def test_unexpected_error_marks_job_error(self) -> None:
        fake_scan = mock.AsyncMock(side_effect=RuntimeError("что-то пошло не так"))
        with mock.patch.object(seller_scan, "scan_seller", new=fake_scan):
            started = self._scan({"url": "https://www.avito.ru/brands/abc123", "limit": 10})
        job_id = json.loads(started.body)["job_id"]
        job = self.app_module.SELLER_JOBS[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("что-то пошло не так", job["error"])

    def test_unknown_job_status_and_result_404(self) -> None:
        status = asyncio.run(self.app_module.api_seller_scan_status("no-such-job"))
        self.assertEqual(status.status_code, 404)
        result = asyncio.run(self.app_module.api_seller_scan_result("no-such-job"))
        self.assertEqual(result.status_code, 404)


# ── CSV-экспорт ──────────────────────────────────────────────────────────────

class ExportCsvTests(SellerScanApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_module.SELLER_JOBS["job-csv"] = {
            "status": "done", "current": 2, "total": 2, "label": "", "error": None,
            "profile_url": "https://www.avito.ru/brands/abc123/items/all", "limit": 2,
            "result": {
                "found_total": 2, "scanned": 2, "errors": 0, "items": [ITEM_B, ITEM_A],
            },
        }

    async def _export(self, sort: str) -> str:
        response = await self.app_module.api_seller_scan_export_csv("job-csv", sort=sort)
        self.assertEqual(response.media_type, "text/csv; charset=utf-8")
        body = await _read_streaming_body(response)
        return body.decode("utf-8-sig")

    def test_csv_sorted_by_total_views_with_bom_and_header(self) -> None:
        text = asyncio.run(self._export("total"))
        lines = text.splitlines()
        self.assertEqual(
            lines[0], "Название,Ссылка,Просмотров всего,Просмотров сегодня,Ошибка"
        )
        # А(100) идёт раньше Б(50)
        self.assertIn("А,", lines[1])
        self.assertIn("Б,", lines[2])

    def test_csv_sorted_by_today_views(self) -> None:
        text = asyncio.run(self._export("today"))
        lines = text.splitlines()
        # Б(20 сегодня) идёт раньше А(5 сегодня)
        self.assertIn("Б,", lines[1])
        self.assertIn("А,", lines[2])


if __name__ == "__main__":
    unittest.main()
