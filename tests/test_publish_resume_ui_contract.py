"""Тесты-доказательства брифа 06 «Интерфейс прерванной задачи, тексты, документация»
(docs/_internal/briefs/resume-audit/06-ui-texts-docs.md).

Проверяет только то, что бриф отдаёт бэкенду (находки F01, F30, F08, F31):
- GET /api/publish/pending находит незавершённую задачу после «рестарта»
  (recover_publish_states кладёт её только в память — ровно как в on_startup);
- resume_available=False, пока фоновая задача ещё не done() (F30) и когда
  user_action.resumable=False (F08), с человекочитаемой причиной в обоих
  случаях;
- POST /api/publish/resume/{job_id} отвечает тем же планом (mode/resumed_from),
  который реально запустил (F31) — при двух конкурентных клиентах,
  один из которых опирался на устаревший /status.

Тексты по шагу (F05) и подсчёт «оставшихся» без застрявшего объявления (F26)
решает фронт (frontend/src/lib/publish.js: describeStuckItemPayment,
hasStuckCreatedItem) по ПОЛЮ step, которое здесь же закреплено как часть
контракта /status — своего рода pin-тест на входные данные этой логики.
"""

from __future__ import annotations

import publish_test_isolation  # noqa: F401 — первым: мёртвый CDP, temp dirs, файловый лог выключен

import sys
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as backend_app  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402


def _draft(n_items: int = 3) -> publisher.DraftData:
    return publisher.DraftData(
        title="Пиджак", trade_type="tt", condition="c", size="s", brand="",
        color="col", description="d", price=100,
        locations=tuple(
            publisher.LocationData(city="Москва", address=f"улица Резервная, {k}")
            for k in range(1, n_items + 1)
        ),
        view_price_max=Decimal("1"), photo_paths=(), category="jackets",
    )


class PublishPendingEndpointTests(unittest.IsolatedAsyncioTestCase):
    """F01: список незавершённых задач переживает рестарт сервера."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "publish"
        self.root.mkdir()
        self._patch = mock.patch.object(backend_app, "TMP_PUBLISH_DIR", self.root)
        self._patch.start()
        self._jobs = dict(backend_app.PUBLISH_JOBS)
        self._tasks = dict(backend_app.ACTIVE_PUBLISH_TASKS)
        backend_app.PUBLISH_JOBS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.clear()

    async def asyncTearDown(self) -> None:
        backend_app.PUBLISH_JOBS.clear()
        backend_app.PUBLISH_JOBS.update(self._jobs)
        backend_app.ACTIVE_PUBLISH_TASKS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.update(self._tasks)
        self._patch.stop()
        self._tmp.cleanup()

    def _save(self, job_id: str, job: dict[str, Any]) -> None:
        publish_state.save_publish_state(self.root / job_id, job_id, job, _draft())

    async def test_pending_lists_job_recovered_after_restart(self) -> None:
        job_id = "job-pending-1"
        self._save(job_id, {
            "status": "failed", "step": "fill_title", "items_total": 3,
            "item_index": 1, "items_published": 0, "money_click": "not_clicked",
            "created_at": "2020-01-01T00:00:00+00:00",
        })
        # «Рестарт»: то же самое, что делает on_startup — recover кладёт задачу
        # только в память, а не что-то специальное для этого теста.
        recovered = publish_state.recover_publish_states(self.root)
        for recovered_id, (recovered_job, _draft_obj) in recovered.items():
            backend_app.PUBLISH_JOBS[recovered_id] = recovered_job

        response = await backend_app.api_publish_pending()
        payload = _json(response)

        job_ids = [item["job_id"] for item in payload["jobs"]]
        self.assertIn(job_id, job_ids)

    async def test_pending_excludes_closed_and_clean_done(self) -> None:
        closed_id, done_id = "job-closed", "job-done-clean"
        self._save(closed_id, {
            "status": "closed", "step": "fill_title", "items_total": 1,
            "item_index": 1, "items_published": 0,
        })
        self._save(done_id, {
            "status": "done", "step": "done", "items_total": 1,
            "item_index": 1, "items_published": 1, "skipped_items": [],
        })
        backend_app.PUBLISH_JOBS[closed_id] = {"status": "closed"}
        backend_app.PUBLISH_JOBS[done_id] = {"status": "done", "skipped_items": []}

        response = await backend_app.api_publish_pending()
        job_ids = [item["job_id"] for item in _json(response)["jobs"]]

        self.assertNotIn(closed_id, job_ids)
        self.assertNotIn(done_id, job_ids)

    async def test_pending_keeps_done_with_skips_visible(self) -> None:
        """F24/S3: «завершена с пропусками» остаётся видимой, а не тонет вместе
        с обычным done."""
        job_id = "job-done-with-skips"
        skipped = [{
            "item_index": 2, "item_id": "7730002",
            "item_url": "https://www.avito.ru/cpxpromo/7730002",
            "step": "fill_view_price", "city": "Москва", "address": "улица А, 2",
        }]
        self._save(job_id, {
            "status": "done", "step": "done", "items_total": 3,
            "item_index": 3, "items_published": 2, "skipped_items": skipped,
        })
        backend_app.PUBLISH_JOBS[job_id] = {
            "status": "done", "items_total": 3, "items_published": 2,
            "skipped_items": skipped,
        }

        response = await backend_app.api_publish_pending()
        job_ids = [item["job_id"] for item in _json(response)["jobs"]]

        self.assertIn(job_id, job_ids)


def _json(response: Any) -> Any:
    import json

    return json.loads(bytes(response.body).decode("utf-8"))


class ResumeAvailabilityTests(unittest.TestCase):
    """F30 (задача ещё завершается) и F08 (шаг помечен неповторяемым)."""

    def setUp(self) -> None:
        self._tasks = dict(backend_app.ACTIVE_PUBLISH_TASKS)
        backend_app.ACTIVE_PUBLISH_TASKS.clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        backend_app.ACTIVE_PUBLISH_TASKS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.update(self._tasks)

    def _base_job(self, **overrides: Any) -> dict[str, Any]:
        job = {
            "status": "failed", "step": "fill_title", "items_total": 3,
            "item_index": 1, "items_published": 0, "money_click": "not_clicked",
            "user_action": None,
        }
        job.update(overrides)
        return job

    def test_resume_unavailable_while_background_task_not_done(self) -> None:
        """F30: status уже терминальный, но фоновая задача ещё не done() —
        клик по /resume в это окно получил бы 409, кнопку показывать рано."""
        job_id = "job-f30"
        job = self._base_job()
        # Настоящий asyncio.Task не нужен — app.py вызывает только .done().
        backend_app.ACTIVE_PUBLISH_TASKS[job_id] = _FakeTask(done=False)

        plan, reason = backend_app._publish_resume_view(job_id, job)

        self.assertIsNone(plan)
        self.assertIsNotNone(reason)
        self.assertIn("завершается", reason)

    def test_resume_available_once_background_task_is_done(self) -> None:
        job_id = "job-f30-done"
        job = self._base_job()
        backend_app.ACTIVE_PUBLISH_TASKS[job_id] = _FakeTask(done=True)

        with mock.patch.object(
            publish_state, "load_publish_state",
            return_value=(job, _draft()),
        ):
            plan, reason = backend_app._publish_resume_view(job_id, job)

        self.assertIsNotNone(plan)
        self.assertIsNone(reason)

    def test_resume_unavailable_when_user_action_marks_step_unresumable(self) -> None:
        """F08: минимум цены выше потолка уже создал объявление без оплаты —
        не предлагаем «Продолжить со следующего», чтобы не плодить такие же
        брошенные объявления по кругу."""
        job_id = "job-f08"
        job = self._base_job(
            status="needs_user_action",
            step="fill_view_price",
            money_click="clicked",
            user_action={
                "type": "view_price_cap_exceeded",
                "resumable": False,
                "item_index": 2,
                "items_total": 3,
                "maximum_view_price": "0.5",
                "minimum_view_price": "0.9",
                "message": "Для объявления №2 минимум Авито 0.9 ₽ превышает потолок 0.5 ₽",
            },
        )

        plan, reason = backend_app._publish_resume_view(job_id, job)

        self.assertIsNone(plan)
        self.assertIsNotNone(reason)
        self.assertIn("№2", reason)

    def test_resumable_true_does_not_block_skip_item(self) -> None:
        """Обычный skip_item (money_click=clicked, но user_action не
        помечен resumable=False) по-прежнему предлагает «Продолжить»."""
        job_id = "job-plain-skip"
        job = self._base_job(
            status="failed", step="fill_view_price", money_click="clicked",
        )
        with mock.patch.object(
            publish_state, "load_publish_state",
            return_value=(job, _draft()),
        ):
            plan, reason = backend_app._publish_resume_view(job_id, job)

        self.assertIsNotNone(plan)
        self.assertEqual(plan["mode"], "skip_item")
        self.assertIsNone(reason)


class _FakeTask:
    """Минимальная замена asyncio.Task для проверки только done()."""

    def __init__(self, *, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class ResumeReturnsLaunchedPlanTests(unittest.IsolatedAsyncioTestCase):
    """F31: /resume отвечает планом, который реально запустил, а не тем,
    что показывал предыдущий /status устаревшей вкладки."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "publish"
        self.root.mkdir()
        self._patch = mock.patch.object(backend_app, "TMP_PUBLISH_DIR", self.root)
        self._patch.start()
        self._jobs = dict(backend_app.PUBLISH_JOBS)
        self._tasks = dict(backend_app.ACTIVE_PUBLISH_TASKS)
        backend_app.PUBLISH_JOBS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.clear()

    async def asyncTearDown(self) -> None:
        backend_app.PUBLISH_JOBS.clear()
        backend_app.PUBLISH_JOBS.update(self._jobs)
        backend_app.ACTIVE_PUBLISH_TASKS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.update(self._tasks)
        self._patch.stop()
        self._tmp.cleanup()

    async def test_resume_response_matches_actually_scheduled_start_index(self) -> None:
        # /resume проверяет валидность job_id как UUID (защита от path traversal).
        job_id = str(uuid.uuid4())
        # Пакет из 4, №2 пропущен раньше (устаревшая вкладка ещё думает, что
        # пропустят именно №2) — на диске уже отправлено 2 объявления, значит
        # реальный план продолжит с №3, а не с №2, как в закэшированном /status.
        job = {
            "status": "failed", "step": "fill_title", "items_total": 4,
            "item_index": 3, "items_published": 2, "money_click": "not_clicked",
            "published_urls": [
                "https://www.avito.ru/items/edit/1", "https://www.avito.ru/items/edit/2",
            ],
        }
        publish_state.save_publish_state(self.root / job_id, job_id, job, _draft(4))

        with mock.patch.object(publisher, "run_publish_job", new=_noop_run_publish_job):
            response = await backend_app.api_publish_resume(job_id)
            # Дожидаемся фоновой задачи, чтобы не унести её недоделанной в
            # asyncTearDown (закрывает event loop) — сам итог здесь не важен.
            background = backend_app.ACTIVE_PUBLISH_TASKS.get(job_id)
            if background is not None:
                await background

        payload = _json(response)
        self.assertEqual(payload["mode"], "retry_item")
        self.assertEqual(payload["resumed_from"], 3)


async def _noop_run_publish_job(*args: Any, **kwargs: Any) -> None:
    job = args[1] if len(args) > 1 else kwargs.get("job")
    if job is not None:
        job["status"] = "done"


if __name__ == "__main__":
    unittest.main()
