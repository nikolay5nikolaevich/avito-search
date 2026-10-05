"""Контракт API фонового поиска IT-контактов без сетевых запросов."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app as app_module  # noqa: E402
import it_outreach  # noqa: E402


class _FakeRequest:
    def __init__(self, payload, error: Exception | None = None):
        self.payload = payload
        self.error = error

    async def json(self):
        if self.error:
            raise self.error
        return self.payload


async def _run_and_drain(coro):
    response = await coro
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    if pending:
        await asyncio.gather(*pending)
    return response


async def _read_stream(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


class ItOutreachApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = str(Path(self.temp_dir.name) / "it_outreach.db")
        app_module.IT_OUTREACH_JOBS.clear()
        app_module.ACTIVE_IT_OUTREACH_TASKS.clear()
        self.addCleanup(app_module.IT_OUTREACH_JOBS.clear)
        self.addCleanup(app_module.ACTIVE_IT_OUTREACH_TASKS.clear)
        self.enterContext(mock.patch.object(app_module, "IT_OUTREACH_DB_PATH", self.db_path))

    def _start(self, payload, error: Exception | None = None):
        return asyncio.run(_run_and_drain(app_module.api_it_outreach_start(_FakeRequest(payload, error))))


class StartValidationTests(ItOutreachApiTestCase):
    def test_invalid_json_and_limit_are_400(self) -> None:
        self.assertEqual(self._start(None, ValueError("bad json")).status_code, 400)
        for limit in (0, 101, 1.5, True, "x"):
            with self.subTest(limit=limit):
                self.assertEqual(self._start({"limit": limit}).status_code, 400)

    def test_second_running_job_is_409(self) -> None:
        app_module.IT_OUTREACH_JOBS["running"] = {"status": "running"}
        self.assertEqual(self._start({"limit": 1}).status_code, 409)


class DirectRouteTests(unittest.TestCase):
    def test_it_outreach_route_redirects_to_hash_router(self) -> None:
        response = asyncio.run(app_module.it_outreach_page())
        self.assertIn(response.status_code, (302, 307))
        self.assertEqual(response.headers["location"], "/#/it-outreach")


class JobTests(ItOutreachApiTestCase):
    def test_records_results_and_uses_known_domains(self) -> None:
        app_module.it_outreach_store.upsert_company(
            {"domain": "known.ru", "status": "no_email"}, self.db_path
        )
        inspected = {
            "domain": "new.ru", "company_name": "Acme", "site_url": "https://new.ru",
            "email": "hr@new.ru", "email_kind": "hr", "status": "found_email",
        }
        with mock.patch.object(it_outreach, "discover_sites", return_value=["https://new.ru"] ) as discover, \
                mock.patch.object(it_outreach, "inspect_site", return_value=inspected):
            started = self._start({"limit": 2})
        job_id = json.loads(started.body)["job_id"]
        discover.assert_called_once_with(2, {"known.ru"})
        status = json.loads(asyncio.run(app_module.api_it_outreach_status(job_id)).body)
        self.assertEqual(status, {
            "status": "done", "scanned": 1, "total": 1, "found_emails": 1,
            "current_site": "", "error": None,
        })
        companies = json.loads(asyncio.run(app_module.api_it_outreach_results()).body)["companies"]
        self.assertEqual([row["domain"] for row in companies], ["new.ru", "known.ru"])

    def test_site_error_is_saved_and_next_site_runs(self) -> None:
        good = {"domain": "good.ru", "site_url": "https://good.ru", "status": "no_email"}
        with mock.patch.object(it_outreach, "discover_sites", return_value=["https://bad.ru", "https://good.ru"]), \
                mock.patch.object(it_outreach, "inspect_site", side_effect=[RuntimeError("offline"), good]):
            started = self._start({"limit": 2})
        job_id = json.loads(started.body)["job_id"]
        status = json.loads(asyncio.run(app_module.api_it_outreach_status(job_id)).body)
        self.assertEqual((status["status"], status["scanned"], status["total"]), ("done", 2, 2))
        companies = json.loads(asyncio.run(app_module.api_it_outreach_results()).body)["companies"]
        self.assertEqual({row["domain"]: row["status"] for row in companies}, {"bad.ru": "error", "good.ru": "no_email"})

    def test_source_unavailable_blocks_job(self) -> None:
        with mock.patch.object(it_outreach, "discover_sites", side_effect=it_outreach.SourceUnavailableError("Taginfo down")):
            started = self._start({"limit": 1})
        job_id = json.loads(started.body)["job_id"]
        status = json.loads(asyncio.run(app_module.api_it_outreach_status(job_id)).body)
        self.assertEqual(status["status"], "blocked")
        self.assertIn("Taginfo down", status["error"])

    def test_unknown_status_is_404(self) -> None:
        self.assertEqual(asyncio.run(app_module.api_it_outreach_status("missing")).status_code, 404)


class CsvTests(ItOutreachApiTestCase):
    def test_csv_has_bom_attribution_and_formula_protection(self) -> None:
        app_module.it_outreach_store.upsert_company({
            "domain": "evil.ru", "company_name": "=cmd()", "city": "+city",
            "site_url": "https://evil.ru", "email": "@mail", "status": "found_email",
        }, self.db_path)
        response = asyncio.run(app_module.api_it_outreach_export_csv())
        body = asyncio.run(_read_stream(response))
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["Компания"], "'=cmd()")
        self.assertEqual(rows[0]["Город"], "'+city")
        self.assertEqual(rows[0]["Email"], "'@mail")
        self.assertEqual(rows[0]["Источник"], "OpenStreetMap (ODbL): https://www.openstreetmap.org/copyright")


if __name__ == "__main__":
    unittest.main()
