"""
Тесты эндпоинтов «Поиска под перепродажу» (backend/app.py, /api/resale/*).

httpx/TestClient в проекте нет — обработчики вызываются напрямую через
asyncio.run, как в tests/test_seller_scan_api.py. resale_scan.run_resale_scan
всегда замокан: ни одного обращения к живому Авито и к claude CLI.

Имена, которых спека (docs/specs/resale-finder.md) не фиксирует и которые
я выбрал сама для этих тестов (сверить с реализацией другого агента):
  - app.RESALE_JOBS                      — реестр задач (как SELLER_JOBS)
  - app.api_resale_scan(request)         — POST /api/resale/scan
  - app.api_resale_scan_status(job_id)   — GET  /api/resale/status/{job_id}
  - app.api_resale_scan_result(job_id)   — GET  /api/resale/result/{job_id}
  - app.api_resale_scan_export_csv(job_id) — GET /api/resale/export.csv?job_id=
"""

from __future__ import annotations

import asyncio
import csv
import io
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
import resale_scan  # noqa: E402
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


# ── Фикстура отчёта (build_report(...) + добавки run_resale_scan) ──────────

REPORT_STUB = {
    "deals": [
        {
            "item_id": "111", "url": "https://www.avito.ru/moskva/noutbuki/item_111",
            "title": "Acer Nitro 5 RTX 3060 состояние отличное", "price": 40000,
            "market_price": 54000, "discount_pct": 25.9, "sample": 6,
            "model": "Acer Nitro 5 · RTX 3060", "model_key": "acer nitro 5 | rtx 3060",
            "condition": "working", "reason": "исправен", "low_data": False,
        },
        {
            "item_id": "222", "url": "https://www.avito.ru/moskva/noutbuki/item_222",
            "title": "ASUS ROG Strix G15 GTX 1650", "price": 20000,
            "market_price": 25000, "discount_pct": 20.0, "sample": 2,
            "model": "ASUS ROG Strix G15 · GTX 1650", "model_key": "asus rog strix g15 | gtx 1650",
            "condition": "unknown", "reason": "состояние не указано в объявлении", "low_data": True,
        },
    ],
    "groups": [
        {"model_key": "acer nitro 5 | rtx 3060", "model": "Acer Nitro 5 · RTX 3060",
         "sample": 6, "market_price": 54000, "min_price": 40000, "max_price": 60000,
         "low_data": False},
        {"model_key": "asus rog strix g15 | gtx 1650", "model": "ASUS ROG Strix G15 · GTX 1650",
         "sample": 2, "market_price": 25000, "min_price": 20000, "max_price": 30000,
         "low_data": True},
    ],
    "excluded": {"parts": 1, "not_laptop": 2, "unrecognized": 0, "errors": 0},
    "total_items": 20,
    "topup_queries": [],
    "query": "игровой ноутбук",
    "city": "moskva",
}


class ResaleApiTestCase(unittest.TestCase):
    """Общий сброс реестров задач — тесты не должны видеть чужие job'ы."""

    def setUp(self) -> None:
        self.app_module = app_module
        self.app_module.RESALE_JOBS.clear()
        self.app_module.SELLER_JOBS.clear()
        self.app_module.OUTREACH_JOBS.clear()
        self.app_module.PUBLISH_JOBS.clear()
        self.addCleanup(self.app_module.RESALE_JOBS.clear)
        self.addCleanup(self.app_module.SELLER_JOBS.clear)
        self.addCleanup(self.app_module.OUTREACH_JOBS.clear)
        self.addCleanup(self.app_module.PUBLISH_JOBS.clear)

    def _scan(self, payload):
        return asyncio.run(
            _run_and_drain(self.app_module.api_resale_scan(_FakeRequest(payload)))
        )

    def _valid_payload(self, **overrides):
        payload = {"query": "игровой ноутбук", "city": "moskva"}
        payload.update(overrides)
        return payload


# ── Валидация входа (422) ───────────────────────────────────────────────────

class ScanValidationTests(ResaleApiTestCase):
    def test_empty_payload_rejected_with_query_and_city_fields(self) -> None:
        response = self._scan({})
        self.assertEqual(response.status_code, 422)
        fields = {e["field"] for e in json.loads(response.body)["errors"]}
        self.assertIn("query", fields)
        self.assertIn("city", fields)

    def test_unknown_city_slug_rejected(self) -> None:
        response = self._scan(self._valid_payload(city="не-существующий-слаг-xyz"))
        self.assertEqual(response.status_code, 422)
        fields = {e["field"] for e in json.loads(response.body)["errors"]}
        self.assertIn("city", fields)

    def test_threshold_pct_out_of_range_rejected(self) -> None:
        for bad in (4, 80.1, 0, -5):
            with self.subTest(bad=bad):
                response = self._scan(self._valid_payload(threshold_pct=bad))
                self.assertEqual(response.status_code, 422)

    def test_max_items_out_of_range_rejected(self) -> None:
        for bad in (19, 301, 0, -1):
            with self.subTest(bad=bad):
                response = self._scan(self._valid_payload(max_items=bad))
                self.assertEqual(response.status_code, 422)

    def test_boundary_threshold_and_max_items_accepted(self) -> None:
        fake_run = mock.AsyncMock(return_value=REPORT_STUB)
        with mock.patch.object(resale_scan, "run_resale_scan", new=fake_run):
            response = self._scan(self._valid_payload(threshold_pct=5, max_items=20))
        self.assertEqual(response.status_code, 200)
        with mock.patch.object(resale_scan, "run_resale_scan", new=fake_run):
            response = self._scan(self._valid_payload(threshold_pct=80, max_items=300))
        self.assertEqual(response.status_code, 200)


# ── Занятость Chrome другой задачей (409) ───────────────────────────────────

class BusyTests(ResaleApiTestCase):
    def test_rejected_while_seller_scan_running(self) -> None:
        self.app_module.SELLER_JOBS["seller-1"] = {"status": "running"}
        response = self._scan(self._valid_payload())
        self.assertEqual(response.status_code, 409)

    def test_rejected_while_outreach_running(self) -> None:
        self.app_module.OUTREACH_JOBS["outreach-1"] = {"status": "running"}
        response = self._scan(self._valid_payload())
        self.assertEqual(response.status_code, 409)

    def test_rejected_while_publish_running(self) -> None:
        self.app_module.PUBLISH_JOBS["publish-1"] = {"status": "running"}
        response = self._scan(self._valid_payload())
        self.assertEqual(response.status_code, 409)

    def test_rejected_while_another_resale_scan_running(self) -> None:
        self.app_module.RESALE_JOBS["resale-1"] = {"status": "running"}
        response = self._scan(self._valid_payload())
        self.assertEqual(response.status_code, 409)


# ── Жизненный цикл задачи ────────────────────────────────────────────────────

class ScanJobTests(ResaleApiTestCase):
    def test_success_populates_status_and_result(self) -> None:
        fake_run = mock.AsyncMock(return_value=REPORT_STUB)
        with mock.patch.object(resale_scan, "run_resale_scan", new=fake_run):
            started = self._scan(self._valid_payload())
        self.assertEqual(started.status_code, 200)
        job_id = json.loads(started.body)["job_id"]
        fake_run.assert_awaited_once()

        status = asyncio.run(self.app_module.api_resale_scan_status(job_id))
        status_body = json.loads(status.body)
        self.assertEqual(status_body["status"], "done")
        self.assertIn("stage", status_body)
        self.assertIn("current", status_body)
        self.assertIn("total", status_body)

        result = asyncio.run(self.app_module.api_resale_scan_result(job_id))
        result_body = json.loads(result.body)
        self.assertEqual(len(result_body["deals"]), 2)
        self.assertEqual(result_body["total_items"], 20)
        self.assertEqual(result_body["excluded"]["parts"], 1)

    def test_avito_blocked_error_sets_blocked_status(self) -> None:
        fake_run = mock.AsyncMock(side_effect=AvitoBlockedError("капча"))
        with mock.patch.object(resale_scan, "run_resale_scan", new=fake_run):
            started = self._scan(self._valid_payload())
        job_id = json.loads(started.body)["job_id"]
        job = self.app_module.RESALE_JOBS[job_id]
        self.assertEqual(job["status"], "blocked")

    def test_unexpected_error_marks_job_error_not_crash(self) -> None:
        fake_run = mock.AsyncMock(side_effect=RuntimeError("что-то пошло не так"))
        with mock.patch.object(resale_scan, "run_resale_scan", new=fake_run):
            started = self._scan(self._valid_payload())
        job_id = json.loads(started.body)["job_id"]
        job = self.app_module.RESALE_JOBS[job_id]
        self.assertEqual(job["status"], "error")
        self.assertIn("что-то пошло не так", job["error"])

    def test_unknown_job_status_and_result_404(self) -> None:
        status = asyncio.run(self.app_module.api_resale_scan_status("no-such-job"))
        self.assertEqual(status.status_code, 404)
        result = asyncio.run(self.app_module.api_resale_scan_result("no-such-job"))
        self.assertEqual(result.status_code, 404)


# ── CSV-экспорт ──────────────────────────────────────────────────────────────

class ExportCsvTests(ResaleApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_module.RESALE_JOBS["job-csv"] = {
            "status": "done", "stage": "done", "current": 20, "total": 20,
            "error": None, "query": "игровой ноутбук", "city": "moskva",
            "result": REPORT_STUB,
        }

    async def _export(self) -> tuple[bytes, str]:
        response = await self.app_module.api_resale_scan_export_csv("job-csv")
        self.assertEqual(response.media_type, "text/csv; charset=utf-8")
        body = await _read_streaming_body(response)
        return body, body.decode("utf-8-sig")

    def test_csv_has_bom_and_exact_header(self) -> None:
        body, text = asyncio.run(self._export())
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        lines = text.splitlines()
        self.assertEqual(
            lines[0],
            "Модель,Название,Цена,Рынок,Скидка %,Выборка,Мало данных,Состояние,Ссылка",
        )

    def test_csv_rows_match_deals(self) -> None:
        _, text = asyncio.run(self._export())
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)  # заголовок + 2 лота

        # Первый лот — обычный, не «мало данных».
        self.assertIn("Acer Nitro 5 · RTX 3060", lines[1])
        self.assertIn("40000", lines[1])
        self.assertIn("54000", lines[1])
        self.assertIn("25.9", lines[1])
        self.assertIn("6", lines[1])
        self.assertIn("working", lines[1])
        self.assertIn(REPORT_STUB["deals"][0]["url"], lines[1])

        # Второй лот — low_data=True.
        self.assertIn("ASUS ROG Strix G15 · GTX 1650", lines[2])
        self.assertIn("20000", lines[2])
        self.assertIn("unknown", lines[2])

    def test_csv_low_data_flag_marks_row_distinctly(self) -> None:
        # ДОПУЩЕНИЕ: спека не фиксирует точный текст в колонке «Мало данных» —
        # проверяю только, что строка с low_data=True отличается от строки
        # с low_data=False (не одинаковое пустое значение в обеих).
        _, text = asyncio.run(self._export())
        rows = list(csv.reader(io.StringIO(text)))
        header, row_normal, row_low_data = rows[0], rows[1], rows[2]
        low_data_index = header.index("Мало данных")
        self.assertNotEqual(row_normal[low_data_index], row_low_data[low_data_index])


if __name__ == "__main__":
    unittest.main()
