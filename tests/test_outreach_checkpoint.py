"""Дисковый checkpoint и возобновление рассылки без повторных писем.

Калька с tests/test_publish_checkpoint.py — тот же дух: retry_item (до клика
отправки) и skip_item (клик уже сделан, кандидата не трогаем).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app as backend_app  # noqa: E402
import outreach  # noqa: E402
import outreach_state  # noqa: E402


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self) -> dict:
        return self.payload


def make_job(**overrides) -> dict:
    job = {
        "job_id": "job-1",
        "kind": "send",
        "status": "failed",
        "items_total": 5,
        "item_index": 3,
        "step": "fill_message",
        "candidates": [{"seller_key": f"seller:{i}"} for i in range(1, 6)],
        "message": "Шаблон письма",
        "sent_keys": [],
        "skipped_items": [],
        "created_at": "2026-09-22T00:00:00+00:00",
    }
    job.update(overrides)
    return job


class OutreachStateFileTests(unittest.TestCase):
    def test_state_round_trip_is_atomic(self) -> None:
        job = make_job()
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-1"
            outreach_state.save_outreach_state(job_dir, job)

            self.assertFalse((job_dir / "outreach-state.json.tmp").exists())
            restored = outreach_state.load_outreach_state(job_dir)

        self.assertEqual(restored["candidates"], job["candidates"])
        self.assertEqual(restored["message"], "Шаблон письма")
        self.assertEqual(restored["item_index"], 3)

    def test_load_rejects_unknown_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-1"
            job_dir.mkdir(parents=True)
            (job_dir / outreach_state.STATE_FILENAME).write_text(
                json.dumps({"version": 999, "job_id": "job-1", "job": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                outreach_state.load_outreach_state(job_dir)


class OutreachResumePlanTests(unittest.TestCase):
    """Два режима возобновления: retry_item (до send_click) vs skip_item
    (клик уже сделан — кандидата пропускаем, второе письмо продавцу недопустимо)."""

    def test_retry_item_before_send_click(self) -> None:
        for step in (
            "", "connect_chrome", "open_listing", "check_seller", "open_chat", "fill_message",
        ):
            with self.subTest(step=step):
                plan = outreach_state.resume_plan(make_job(step=step, item_index=3))
                self.assertEqual(plan, {
                    "mode": "retry_item",
                    "start_index": 3,
                    "skipped_item": None,
                    "items_total": 5,
                })

    def test_skip_item_stopped_on_send_click(self) -> None:
        plan = outreach_state.resume_plan(make_job(step="send_click", item_index=3))
        self.assertEqual(plan, {
            "mode": "skip_item",
            "start_index": 4,
            "skipped_item": 3,
            "items_total": 5,
        })

    def test_skip_item_stopped_on_confirm_sent(self) -> None:
        plan = outreach_state.resume_plan(make_job(step="confirm_sent", item_index=3))
        self.assertEqual(plan, {
            "mode": "skip_item",
            "start_index": 4,
            "skipped_item": 3,
            "items_total": 5,
        })

    def test_skip_item_stopped_on_back_to_search(self) -> None:
        # Сообщение уже отправлено, но само возвращение к выдаче не удалось —
        # повторный клик по этому кандидату всё равно запрещён.
        plan = outreach_state.resume_plan(make_job(step="back_to_search", item_index=3))
        self.assertEqual(plan, {
            "mode": "skip_item",
            "start_index": 4,
            "skipped_item": 3,
            "items_total": 5,
        })

    def test_none_when_nothing_started(self) -> None:
        plan = outreach_state.resume_plan(make_job(items_total=0, item_index=0, step=""))
        self.assertIsNone(plan)

    def test_none_when_skip_would_leave_nothing(self) -> None:
        # Остановка на последнем кандидате пакета после клика — пропускать
        # нечего продолжать.
        plan = outreach_state.resume_plan(
            make_job(items_total=5, item_index=5, step="confirm_sent")
        )
        self.assertIsNone(plan)

    def test_none_on_garbage_fields(self) -> None:
        plan = outreach_state.resume_plan({"items_total": "не число", "item_index": 3})
        self.assertIsNone(plan)

    def test_retry_from_scratch_when_nothing_started_yet(self) -> None:
        plan = outreach_state.resume_plan(make_job(item_index=0, step=""))
        self.assertEqual(plan, {
            "mode": "retry_item",
            "start_index": 1,
            "skipped_item": None,
            "items_total": 5,
        })


class SendMessagesStartIndexTests(unittest.IsolatedAsyncioTestCase):
    """start_index не должен трогать браузер для кандидатов до него."""

    async def test_candidates_before_start_index_never_reach_browser(self) -> None:
        candidates = [
            {
                "seller_name": f"Продавец {i}",
                "seller_id": f"id{i}",
                "profile_url": f"https://www.avito.ru/user/id{i}/profile",
                "listing_url": f"https://www.avito.ru/moskva/telefony/item_{i}",
            }
            for i in (1, 2, 3)
        ]

        class ThinPage:
            def __init__(self) -> None:
                self.goto_calls: list[str] = []

            async def goto(self, url: str, **kwargs) -> None:
                self.goto_calls.append(url)
                # Обрываем цепочку сразу после goto — дальше в сценарии не идём,
                # нас интересует только факт (не)обращения к странице.
                raise TimeoutError("не загрузилось")

        page = ThinPage()

        @asynccontextmanager
        async def _fake_owned_page():
            yield page

        is_contacted_calls: list[tuple] = []
        steps: list[tuple[str, int]] = []

        def fake_is_contacted(seller_id, profile_url):
            is_contacted_calls.append((seller_id, profile_url))
            return False

        with (
            mock.patch.object(outreach, "_owned_page", _fake_owned_page),
            mock.patch.object(outreach, "is_contacted", side_effect=fake_is_contacted),
            mock.patch.object(outreach, "record_contact"),
        ):
            result = await outreach.send_messages(
                candidates, "Письмо",
                step_cb=lambda name, idx: steps.append((name, idx)),
                start_index=3,
            )

        # Только кандидат №3 (start_index=3) дошёл до браузера.
        self.assertEqual(page.goto_calls, ["https://www.avito.ru/moskva/telefony/item_3"])
        self.assertEqual(is_contacted_calls, [("id3", "https://www.avito.ru/user/id3/profile")])
        self.assertEqual(result["failed"], 1)
        # connect_chrome заводится сразу со start_index, open_listing — только для №3.
        self.assertIn(("connect_chrome", 3), steps)
        self.assertIn(("open_listing", 3), steps)
        self.assertFalse(any(item_index in (1, 2) for _step, item_index in steps))


class OutreachResumeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_rejected_while_task_already_running(self) -> None:
        job_id = "job-running"

        async def _never_ends() -> None:
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(_never_ends())
        backend_app.ACTIVE_OUTREACH_TASKS[job_id] = task
        try:
            response = await backend_app.api_outreach_resume(job_id, _FakeRequest({"action": "retry_current"}))
        finally:
            task.cancel()
            backend_app.ACTIVE_OUTREACH_TASKS.pop(job_id, None)
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.assertEqual(response.status_code, 409)

    async def test_resume_returns_404_when_state_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(backend_app, "TMP_OUTREACH_DIR", Path(tmp)):
                response = await backend_app.api_outreach_resume("no-such-job", _FakeRequest({"action": "retry_current"}))
        self.assertEqual(response.status_code, 404)

    async def test_resume_skips_item_and_schedules_next(self) -> None:
        job_id = "job-skip"
        saved_job = make_job(
            job_id=job_id, status="failed", step="send_click", item_index=2,
            items_total=4, candidates=[{"seller_key": f"seller:{i}"} for i in range(1, 5)],
        )
        def _fake_schedule(_job_id: str, coro) -> None:
            # Реального прогона в браузер не нужно — закрываем незапущенную
            # корутину, чтобы не ловить "coroutine was never awaited".
            coro.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outreach_state.save_outreach_state(root / job_id, saved_job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_OUTREACH_DIR", root),
                    mock.patch.object(
                        backend_app, "_schedule_outreach", side_effect=_fake_schedule,
                    ) as schedule,
                ):
                    response = await backend_app.api_outreach_resume(job_id, _FakeRequest({"action": "skip_current"}))
                skipped_items = backend_app.OUTREACH_JOBS[job_id]["skipped_items"]
            finally:
                backend_app.OUTREACH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["mode"], "skip_item")
        self.assertEqual(payload["skipped_item"], 2)
        self.assertEqual(payload["resumed_from"], 3)
        self.assertEqual(skipped_items, [2])
        schedule.assert_called_once()
        self.assertEqual(schedule.call_args.args[0], job_id)

    async def test_retry_current_schedules_same_preclick_candidate(self) -> None:
        job_id = "job-retry"
        saved_job = make_job(job_id=job_id, step="open_chat", item_index=2)

        def _fake_schedule(_job_id: str, coro) -> None:
            coro.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outreach_state.save_outreach_state(root / job_id, saved_job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_OUTREACH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_outreach", side_effect=_fake_schedule) as schedule,
                ):
                    response = await backend_app.api_outreach_resume(
                        job_id, _FakeRequest({"action": "retry_current"}),
                    )
            finally:
                backend_app.OUTREACH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["resumed_from"], 2)
        schedule.assert_called_once()

    async def test_skip_current_before_click_records_skipped_result(self) -> None:
        job_id = "job-skip-preclick"
        saved_job = make_job(job_id=job_id, step="open_chat", item_index=2)

        def _fake_schedule(_job_id: str, coro) -> None:
            coro.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outreach_state.save_outreach_state(root / job_id, saved_job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_OUTREACH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_outreach", side_effect=_fake_schedule),
                ):
                    response = await backend_app.api_outreach_resume(
                        job_id, _FakeRequest({"action": "skip_current"}),
                    )
                    job = backend_app.OUTREACH_JOBS[job_id]
            finally:
                backend_app.OUTREACH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(job["skipped"], 1)
        self.assertEqual(job["results"][-1]["status"], "skipped")

    async def test_skip_after_back_to_search_preserves_confirmed_sent_result(self) -> None:
        job_id = "job-sent"
        candidate = {"seller_key": "seller:2"}
        saved_job = make_job(
            job_id=job_id, step="back_to_search", item_index=2,
            result={
                "sent": 1, "failed": 0, "skipped": 0, "uncertain": 0,
                "results": [{**candidate, "status": "sent"}],
            },
            sent=1, results=[{**candidate, "status": "sent"}], sent_keys=["seller:2"],
        )

        def _fake_schedule(_job_id: str, coro) -> None:
            coro.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outreach_state.save_outreach_state(root / job_id, saved_job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_OUTREACH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_outreach", side_effect=_fake_schedule),
                ):
                    response = await backend_app.api_outreach_resume(
                        job_id, _FakeRequest({"action": "skip_current"}),
                    )
                    job = backend_app.OUTREACH_JOBS[job_id]
            finally:
                backend_app.OUTREACH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(job["sent"], 1)
        self.assertEqual(job["uncertain"], 0)
        self.assertEqual(job["results"][-1]["status"], "sent")

    async def test_skip_saves_next_candidate_checkpoint_before_schedule(self) -> None:
        job_id = "job-checkpoint"
        saved_job = make_job(job_id=job_id, step="open_chat", item_index=2)
        captured = {}

        def _fake_schedule(_job_id: str, coro) -> None:
            captured.update(outreach_state.load_outreach_state(root / job_id))
            coro.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outreach_state.save_outreach_state(root / job_id, saved_job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_OUTREACH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_outreach", side_effect=_fake_schedule),
                ):
                    response = await backend_app.api_outreach_resume(
                        job_id, _FakeRequest({"action": "skip_current"}),
                    )
            finally:
                backend_app.OUTREACH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["status"], "running")
        self.assertEqual(captured["item_index"], 3)
        self.assertEqual(captured["step"], "connect_chrome")
        self.assertEqual(captured["skipped_items"], [2])
        self.assertEqual(captured["results"][-1]["status"], "skipped")

    async def test_retry_current_after_send_click_is_rejected_but_skip_is_allowed(self) -> None:
        job_id = "job-after-click"
        saved_job = make_job(job_id=job_id, step="send_click", item_index=2)

        def _fake_schedule(_job_id: str, coro) -> None:
            coro.close()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outreach_state.save_outreach_state(root / job_id, saved_job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_OUTREACH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_outreach", side_effect=_fake_schedule) as schedule,
                ):
                    rejected = await backend_app.api_outreach_resume(
                        job_id, _FakeRequest({"action": "retry_current"}),
                    )
                    allowed = await backend_app.api_outreach_resume(
                        job_id, _FakeRequest({"action": "skip_current"}),
                    )
            finally:
                backend_app.OUTREACH_JOBS.pop(job_id, None)

        self.assertEqual(rejected.status_code, 409)
        self.assertIn("дубл", json.loads(rejected.body)["error"].lower())
        self.assertEqual(allowed.status_code, 200)
        schedule.assert_called_once()

    async def test_skip_last_candidate_completes_without_starting_chrome(self) -> None:
        job_id = "job-last"
        saved_job = make_job(
            job_id=job_id, step="send_click", item_index=5, items_total=5,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outreach_state.save_outreach_state(root / job_id, saved_job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_OUTREACH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_outreach") as schedule,
                ):
                    response = await backend_app.api_outreach_resume(
                        job_id, _FakeRequest({"action": "skip_current"}),
                    )
                    job = backend_app.OUTREACH_JOBS[job_id]
            finally:
                backend_app.OUTREACH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["uncertain"], 1)
        self.assertEqual(job["results"][-1]["status"], "uncertain")
        schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
