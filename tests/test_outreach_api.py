"""
Тесты эндпоинтов рассылки продавцам (backend/app.py, /api/outreach/*).

httpx/TestClient в проекте нет — обработчики вызываются напрямую через
asyncio.run, как в tests/test_strategist.py. outreach.collect_candidates и
outreach.send_messages всегда замоканы: ни одного обращения к живому Авито.
outreach_store в import/history тоже замокан, чтобы тесты не трогали боевой
outreach.db в корне проекта.
"""

from __future__ import annotations

import asyncio
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
import outreach  # noqa: E402


CANDIDATE_A = {
    "seller_key": "seller:abc123", "seller_id": "abc123",
    "profile_url": "https://www.avito.ru/brands/first",
    "seller_name": "Магазин А", "review_count": 80,
    "listing_url": "https://www.avito.ru/moskva/telefony/phone_1",
    "city": "moskva", "query": "телефон",
}
CANDIDATE_B = {
    "seller_key": "seller:def456", "seller_id": "def456",
    "profile_url": "https://www.avito.ru/brands/second",
    "seller_name": "Магазин Б", "review_count": 120,
    "listing_url": "https://www.avito.ru/moskva/telefony/phone_2",
    "city": "moskva", "query": "телефон",
}


class _FakeRequest:
    """Подмена fastapi.Request — эндпоинты используют только await .json()."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def _run_and_drain(coro):
    """Выполняет вызов эндпоинта и дожидается фоновых задач, которые он
    запустил через asyncio.create_task (иначе job останется 'running' —
    asyncio.run() отменяет недоделанные задачи при выходе из цикла)."""
    response = await coro
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    if pending:
        await asyncio.gather(*pending)
    return response


class OutreachApiTestCase(unittest.TestCase):
    """Общий сброс реестра задач — тесты не должны видеть чужие job'ы."""

    def setUp(self) -> None:
        self.app_module = app_module
        self.app_module.OUTREACH_JOBS.clear()
        self.addCleanup(self.app_module.OUTREACH_JOBS.clear)
        # Задачи отправки теперь пишут checkpoint на диск (outreach_state.py) —
        # уводим его во временную папку, чтобы тесты не засоряли tmp/outreach/.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.enterContext(mock.patch.object(self.app_module, "TMP_OUTREACH_DIR", Path(tmp_dir.name)))

    def _collect(self, payload):
        return asyncio.run(
            _run_and_drain(self.app_module.api_outreach_collect(_FakeRequest(payload)))
        )

    def _send(self, job_id, payload, drain=True):
        coro = self.app_module.api_outreach_send(job_id, _FakeRequest(payload))
        if drain:
            return asyncio.run(_run_and_drain(coro))
        return asyncio.run(coro)

    def _seed_done_collect_job(self, candidates=(CANDIDATE_A, CANDIDATE_B)):
        """Заводит уже завершённый сбор напрямую в реестре — без похода в Chrome."""
        job_id = "collect-job-1"
        self.app_module.OUTREACH_JOBS[job_id] = {
            "kind": "collect", "status": "done", "current": len(candidates),
            "total": len(candidates), "label": "", "error": None,
            "candidates": list(candidates), "send_job_id": None,
        }
        return job_id


# ── Валидация входа /collect ────────────────────────────────────────────────

class CollectValidationTests(OutreachApiTestCase):
    def test_empty_payload_rejected(self) -> None:
        response = self._collect({})
        self.assertEqual(response.status_code, 422)
        errors = json.loads(response.body)["errors"]
        fields = {error["field"] for error in errors}
        self.assertEqual(fields, {"city", "query", "limit"})

    def test_limit_out_of_range_rejected(self) -> None:
        for limit in (0, 151, "не число"):
            with self.subTest(limit=limit):
                response = self._collect({"city": "moskva", "query": "телефон", "limit": limit})
                self.assertEqual(response.status_code, 422)

    def test_unknown_city_rejected(self) -> None:
        response = self._collect({"city": "not-a-real-city", "query": "телефон", "limit": 15})
        self.assertEqual(response.status_code, 422)

    def test_valid_payload_does_not_touch_avito_without_mock_but_still_runs(self) -> None:
        # collect_candidates замокан на весь класс ниже; здесь просто убеждаемся,
        # что чистая валидация не мешает дальнейшему созданию задачи.
        with mock.patch.object(outreach, "collect_candidates", new=mock.AsyncMock(return_value=[])):
            response = self._collect({"city": "moskva", "query": "телефон", "limit": 15})
        self.assertEqual(response.status_code, 200)


# ── Создание и завершение задачи сбора ──────────────────────────────────────

class CollectJobTests(OutreachApiTestCase):
    def test_collect_creates_running_job_and_calls_collect_candidates_once(self) -> None:
        fake_collect = mock.AsyncMock(return_value=[CANDIDATE_A])
        with mock.patch.object(outreach, "collect_candidates", new=fake_collect):
            response = self._collect({"city": "moskva", "query": "телефон", "limit": 15})
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        job_id = body["job_id"]
        self.assertEqual(body["status"], "running")
        fake_collect.assert_awaited_once()
        # После drain фоновая задача уже отработала и job — done.
        self.assertEqual(self.app_module.OUTREACH_JOBS[job_id]["status"], "done")

    def test_result_serializes_candidates(self) -> None:
        fake_collect = mock.AsyncMock(return_value=[CANDIDATE_A, CANDIDATE_B])
        with mock.patch.object(outreach, "collect_candidates", new=fake_collect):
            started = self._collect({"city": "moskva", "query": "телефон", "limit": 15})
        job_id = json.loads(started.body)["job_id"]

        status = asyncio.run(self.app_module.api_outreach_status(job_id))
        status_body = json.loads(status.body)
        self.assertEqual(status_body["status"], "done")

        result = asyncio.run(self.app_module.api_outreach_result(job_id))
        result_body = json.loads(result.body)
        self.assertEqual(result_body["candidates"], [CANDIDATE_A, CANDIDATE_B])

    def test_needs_user_action_on_stopped_error(self) -> None:
        fake_collect = mock.AsyncMock(side_effect=outreach.OutreachStoppedError("Авито показал капчу"))
        with mock.patch.object(outreach, "collect_candidates", new=fake_collect):
            started = self._collect({"city": "moskva", "query": "телефон", "limit": 15})
        job_id = json.loads(started.body)["job_id"]
        job = self.app_module.OUTREACH_JOBS[job_id]
        self.assertEqual(job["status"], "needs_user_action")
        self.assertIn("капчу", job["error"])

    def test_second_collect_while_running_is_rejected(self) -> None:
        # Уже идёт другой job — сажаем его в реестр напрямую, без похода в Chrome.
        self.app_module.OUTREACH_JOBS["already-running"] = {
            "kind": "collect", "status": "running", "current": 0, "total": 15,
            "label": "", "error": None, "candidates": None, "send_job_id": None,
        }
        response = self._collect({"city": "moskva", "query": "телефон", "limit": 15})
        self.assertEqual(response.status_code, 409)


# ── Отправка ────────────────────────────────────────────────────────────────

class SendValidationTests(OutreachApiTestCase):
    def test_rejects_without_confirmed_true(self) -> None:
        job_id = self._seed_done_collect_job()
        response = self._send(
            job_id,
            {"candidate_keys": ["seller:abc123"], "message": "Привет"},
            drain=False,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.app_module.OUTREACH_JOBS[job_id].get("send_job_id"))

    def test_rejects_candidate_keys_outside_collection(self) -> None:
        job_id = self._seed_done_collect_job()
        response = self._send(
            job_id,
            {"candidate_keys": ["seller:abc123", "seller:unknown"], "message": "Привет", "confirmed": True},
            drain=False,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.app_module.OUTREACH_JOBS[job_id].get("send_job_id"))

    def test_rejects_when_collect_job_unknown(self) -> None:
        response = self._send(
            "no-such-job",
            {"candidate_keys": ["seller:abc123"], "message": "Привет", "confirmed": True},
            drain=False,
        )
        self.assertEqual(response.status_code, 404)

    def test_rejects_empty_message(self) -> None:
        job_id = self._seed_done_collect_job()
        response = self._send(
            job_id,
            {"candidate_keys": ["seller:abc123"], "message": "   ", "confirmed": True},
            drain=False,
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_message_over_max_length(self) -> None:
        # Предел — maxlength поля ввода в мессенджере Авито (1000 символов),
        # см. OUTREACH_MESSAGE_MAX_LENGTH в app.py. Превышение браузер обрежет
        # молча, поэтому ловим на входе, а не на сверке обратным чтением.
        job_id = self._seed_done_collect_job()
        response = self._send(
            job_id,
            {"candidate_keys": ["seller:abc123"], "message": "а" * 1001, "confirmed": True},
            drain=False,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.app_module.OUTREACH_JOBS[job_id].get("send_job_id"))


class SendJobTests(OutreachApiTestCase):
    def test_merge_keeps_one_final_result_per_seller_and_recalculates_counters(self) -> None:
        candidate_c = {**CANDIDATE_B, "seller_key": "seller:ghi789", "seller_id": "ghi789"}
        job = {
            "result": {
                "sent": 1, "failed": 1, "skipped": 0, "uncertain": 0,
                "results": [
                    {**CANDIDATE_A, "status": "failed"},
                    {**CANDIDATE_B, "status": "sent"},
                ],
                "preserved": "value",
            },
            "sent_keys": ["seller:def456"],
        }
        resumed = {
            "sent": 1, "failed": 1, "skipped": 0, "uncertain": 0,
            "results": [
                {**CANDIDATE_A, "status": "sent"},
                {**candidate_c, "status": "failed"},
            ],
        }

        self.app_module._merge_outreach_result(job, resumed)

        self.assertEqual(
            [(row["seller_key"], row["status"]) for row in job["result"]["results"]],
            [("seller:abc123", "sent"), ("seller:def456", "sent"), ("seller:ghi789", "failed")],
        )
        self.assertEqual(
            {key: job["result"][key] for key in ("sent", "failed", "skipped", "uncertain")},
            {"sent": 2, "failed": 1, "skipped": 0, "uncertain": 0},
        )
        self.assertEqual(job["result"]["preserved"], "value")
        self.assertEqual(job["sent_keys"], ["seller:abc123", "seller:def456"])
        self.assertEqual(job["summary"], "Отправлено: 2, пропущено: 0, ошибок: 1.")

    def test_send_success_records_result_and_summary(self) -> None:
        job_id = self._seed_done_collect_job()
        fake_result = {
            "sent": 1, "failed": 0, "skipped": 0,
            "results": [{**CANDIDATE_A, "status": "sent"}],
        }
        fake_send = mock.AsyncMock(return_value=fake_result)
        with mock.patch.object(outreach, "send_messages", new=fake_send):
            started = self._send(
                job_id,
                {"candidate_keys": ["seller:abc123"], "message": "Привет", "confirmed": True},
            )
        body = json.loads(started.body)
        send_job_id = body["job_id"]
        self.assertNotEqual(send_job_id, job_id)
        fake_send.assert_awaited_once()
        awaited_candidates = fake_send.await_args.args[0]
        self.assertEqual([c["seller_key"] for c in awaited_candidates], ["seller:abc123"])

        result = asyncio.run(self.app_module.api_outreach_result(send_job_id))
        result_body = json.loads(result.body)
        self.assertEqual(result_body["sent"], 1)
        self.assertIn("Отправлено: 1", result_body["summary"])
        self.assertEqual(self.app_module.OUTREACH_JOBS[job_id]["send_job_id"], send_job_id)

    def test_repeat_send_on_same_collect_job_is_409(self) -> None:
        job_id = self._seed_done_collect_job()
        fake_send = mock.AsyncMock(return_value={"sent": 1, "failed": 0, "skipped": 0, "results": []})
        with mock.patch.object(outreach, "send_messages", new=fake_send):
            first = self._send(
                job_id,
                {"candidate_keys": ["seller:abc123"], "message": "Привет", "confirmed": True},
            )
        self.assertEqual(first.status_code, 200)

        second = self._send(
            job_id,
            {"candidate_keys": ["seller:def456"], "message": "Привет", "confirmed": True},
            drain=False,
        )
        self.assertEqual(second.status_code, 409)
        fake_send.assert_awaited_once()  # второй раз send_messages вообще не вызван

    def test_stopped_error_becomes_needs_user_action_with_partial_result(self) -> None:
        job_id = self._seed_done_collect_job()
        partial = {"sent": 0, "failed": 0, "skipped": 0, "uncertain": 1,
                   "results": [{**CANDIDATE_A, "status": "uncertain"}]}
        fake_send = mock.AsyncMock(
            side_effect=outreach.OutreachStoppedError("Результат неизвестен", partial)
        )
        with mock.patch.object(outreach, "send_messages", new=fake_send):
            started = self._send(
                job_id,
                {"candidate_keys": ["seller:abc123"], "message": "Привет", "confirmed": True},
            )
        send_job_id = json.loads(started.body)["job_id"]
        job = self.app_module.OUTREACH_JOBS[send_job_id]
        self.assertEqual(job["status"], "needs_user_action")
        self.assertIn("неизвестен", job["error"])
        self.assertEqual(job["result"], partial)

    def test_stopped_error_summary_tells_user_to_check_chat_manually(self) -> None:
        job_id = self._seed_done_collect_job()
        partial = {"sent": 0, "failed": 0, "skipped": 0, "uncertain": 1,
                   "results": [{**CANDIDATE_A, "status": "uncertain"}]}
        fake_send = mock.AsyncMock(
            side_effect=outreach.OutreachStoppedError("Результат неизвестен", partial)
        )
        with mock.patch.object(outreach, "send_messages", new=fake_send):
            started = self._send(
                job_id,
                {"candidate_keys": ["seller:abc123"], "message": "Привет", "confirmed": True},
            )
        send_job_id = json.loads(started.body)["job_id"]
        job = self.app_module.OUTREACH_JOBS[send_job_id]
        # uncertain > 0 обязан объяснить пользователю, что делать, а не спрятаться
        # за нулями в "Отправлено/пропущено/ошибок".
        self.assertIn("чат", job["summary"].lower())
        self.assertIn("Неизвестных: 1", job["summary"])

    def test_second_send_rejected_while_browser_busy_with_other_job(self) -> None:
        job_id = self._seed_done_collect_job()
        self.app_module.OUTREACH_JOBS["other-running"] = {
            "kind": "send", "status": "running", "current": 0, "total": 1,
            "label": "", "error": None, "result": None, "summary": "",
        }
        response = self._send(
            job_id,
            {"candidate_keys": ["seller:abc123"], "message": "Привет", "confirmed": True},
            drain=False,
        )
        self.assertEqual(response.status_code, 409)


# ── Импорт и история (простой passthrough к outreach_store) ────────────────

class ImportHistoryTests(OutreachApiTestCase):
    def test_import_passes_urls_to_store(self) -> None:
        fake_import = mock.MagicMock(return_value={"imported": 1, "skipped": 0, "invalid": []})
        with mock.patch.object(self.app_module.outreach_store, "import_contact_urls", new=fake_import):
            response = asyncio.run(
                self.app_module.api_outreach_import(_FakeRequest({"urls": ["https://www.avito.ru/brands/x"]}))
            )
        self.assertEqual(json.loads(response.body), {"imported": 1, "skipped": 0, "invalid": []})
        fake_import.assert_called_once_with(["https://www.avito.ru/brands/x"])

    def test_import_rejects_non_list_urls(self) -> None:
        response = asyncio.run(self.app_module.api_outreach_import(_FakeRequest({"urls": "не список"})))
        self.assertEqual(response.status_code, 422)

    def test_history_wraps_contacts(self) -> None:
        fake_list = mock.MagicMock(return_value=[{"seller_key": "seller:abc123"}])
        with mock.patch.object(self.app_module.outreach_store, "list_contacts", new=fake_list):
            response = asyncio.run(self.app_module.api_outreach_history())
        self.assertEqual(json.loads(response.body), {"contacts": [{"seller_key": "seller:abc123"}]})


if __name__ == "__main__":
    unittest.main()
