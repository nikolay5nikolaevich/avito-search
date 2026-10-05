"""RED-контракт для брифа 04: у Chrome и prep только один незакрытый пакет.

Запуск: .venv\\Scripts\\python.exe -m unittest tests.test_publish_single_package -v
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi import UploadFile
from starlette.datastructures import Headers


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app as backend_app  # noqa: E402
import category_profiles  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402


def _draft(items_total: int = 2) -> publisher.DraftData:
    return publisher.DraftData(
        title="Кроссовки", trade_type="Продаю своё", condition="Новое",
        size="42", brand="", color="Чёрный", description="Описание",
        price=5000,
        locations=tuple(
            publisher.LocationData("Москва", f"Тверская, {index}")
            for index in range(1, items_total + 1)
        ),
        view_price_max=Decimal("2"), photo_paths=(), category="sneakers",
    )


class PublishSinglePackageApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "publish"
        self.root.mkdir()
        self._patch = mock.patch.object(backend_app, "TMP_PUBLISH_DIR", self.root)
        self._patch.start()
        self._jobs = dict(backend_app.PUBLISH_JOBS)
        self._preps = dict(backend_app.PREP_JOBS)
        self._tasks = dict(backend_app.ACTIVE_PUBLISH_TASKS)
        backend_app.PUBLISH_JOBS.clear()
        backend_app.PREP_JOBS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.clear()

    async def asyncTearDown(self) -> None:
        backend_app.PUBLISH_JOBS.clear()
        backend_app.PUBLISH_JOBS.update(self._jobs)
        backend_app.PREP_JOBS.clear()
        backend_app.PREP_JOBS.update(self._preps)
        backend_app.ACTIVE_PUBLISH_TASKS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.update(self._tasks)
        self._patch.stop()
        self._tmp.cleanup()

    @staticmethod
    def _upload() -> UploadFile:
        return UploadFile(
            io.BytesIO(b"\xff\xd8test-photo"),
            filename="photo.jpg",
            headers=Headers({"content-type": "image/jpeg"}),
        )

    async def _start(self, prep_id: str = "") -> Any:
        profile = category_profiles.get_profile("sneakers")
        return await backend_app.api_publish_start(
            title="Кроссовки", trade_type=next(iter(profile.trade_type_options)),
            condition=next(iter(profile.condition_options)),
            size=next(iter(profile.size_options)), brand="Heckel",
            color=next(iter(profile.color_options)), description="Описание пары",
            price="5000",
            locations_json=json.dumps(
                [{"city": "Москва", "address": "Тверская, 1"}],
                ensure_ascii=False,
            ),
            view_price_max="2", drafts_count="1", prep_id=prep_id,
            category=profile.key, photos=[self._upload()],
        )

    def _save_resumable(self, job_id: str, *, status: str = "failed") -> None:
        job = {
            "status": status,
            "step": "fill_title",
            "items_total": 2,
            "item_index": 1,
            "items_published": 0,
            "prep_id": None,
            "money_click": "not_clicked",
        }
        publish_state.save_publish_state(self.root / job_id, job_id, job, _draft())

    async def test_start_rejects_each_active_status_and_returns_active_job_id(self) -> None:
        """Убрать общий gate позволит двум пакетам управлять одним Chrome."""
        for status in ("queued", "running"):
            with self.subTest(status=status):
                backend_app.PUBLISH_JOBS.clear()
                active_id = str(uuid.uuid4())
                backend_app.PUBLISH_JOBS[active_id] = {"status": status}
                with mock.patch.object(backend_app, "_schedule_publish") as schedule:
                    response = await self._start()

                payload = json.loads(response.body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(payload["job_id"], active_id)
                schedule.assert_not_called()

    async def test_start_failure_before_schedule_releases_reservation(self) -> None:
        """Сбой сборки draft не должен оставить вечный queued и заблокировать Chrome."""
        with mock.patch.object(
            backend_app.publisher, "build_draft_data", side_effect=ValueError("bad draft"),
        ):
            response = await self._start()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(backend_app.PUBLISH_JOBS, {})
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_cancelled_upload_releases_reservation(self) -> None:
        """Отмена HTTP-запроса при чтении фото не оставляет вечный queued."""
        with mock.patch.object(UploadFile, "read", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self._start()

        self.assertEqual(backend_app.PUBLISH_JOBS, {})
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_start_rejects_unclosed_job_after_progress_or_financial_step(self) -> None:
        """Failed-пакет с прогрессом или денежным шагом ещё нельзя забыть."""
        unsafe_jobs = (
            {"status": "failed", "items_published": 1, "step": "fill_title"},
            {"status": "failed", "items_published": 0, "step": "continue_listing"},
        )
        for job in unsafe_jobs:
            with self.subTest(job=job):
                backend_app.PUBLISH_JOBS.clear()
                active_id = str(uuid.uuid4())
                backend_app.PUBLISH_JOBS[active_id] = job
                with mock.patch.object(backend_app, "_schedule_publish") as schedule:
                    response = await self._start()

                payload = json.loads(response.body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(payload["job_id"], active_id)
                schedule.assert_not_called()

    async def test_start_rejects_unseen_disk_job_with_published_item(self) -> None:
        """Второй сервер обязан видеть частичный пакет на диске, даже если его нет в памяти."""
        blocker_id = str(uuid.uuid4())
        disk_job = {
            "status": "failed", "step": "fill_title", "items_total": 2,
            "item_index": 2, "items_published": 1, "prep_id": None,
            "money_click": "not_clicked",
        }
        publish_state.save_publish_state(self.root / blocker_id, blocker_id, disk_job, _draft())

        with mock.patch.object(backend_app, "_schedule_publish"):
            response = await self._start()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["job_id"], blocker_id)

    async def test_start_uses_closed_disk_status_over_stale_failed_memory(self) -> None:
        """Закрытый другим сервером пакет не остаётся вечно блокирующим локальную память."""
        old_id = str(uuid.uuid4())
        backend_app.PUBLISH_JOBS[old_id] = {
            "status": "failed", "items_published": 1, "step": "fill_title",
        }
        self._save_resumable(old_id, status="closed")

        with mock.patch.object(backend_app, "_schedule_publish", return_value=None):
            response = await self._start()

        self.assertEqual(response.status_code, 200)

    async def test_start_rejects_early_step_after_money_click(self) -> None:
        """Денежный клик категории может быть записан ещё на раннем step."""
        blocker_id = str(uuid.uuid4())
        backend_app.PUBLISH_JOBS[blocker_id] = {
            "status": "failed", "step": "check_category",
            "items_published": 0, "money_click": "clicked",
        }

        with mock.patch.object(backend_app, "_schedule_publish"):
            response = await self._start()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["job_id"], blocker_id)

    async def test_resume_rejects_another_job_while_package_is_active(self) -> None:
        """Проверка только ACTIVE_PUBLISH_TASKS самого job_id пустит второй пакет."""
        target_id = str(uuid.uuid4())
        active_id = str(uuid.uuid4())
        self._save_resumable(target_id)
        backend_app.PUBLISH_JOBS[active_id] = {"status": "running"}

        with mock.patch.object(backend_app, "_schedule_publish") as schedule:
            response = await backend_app.api_publish_resume(target_id)

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["job_id"], active_id)
        schedule.assert_not_called()

    async def test_start_rejects_prep_referenced_by_unclosed_job(self) -> None:
        """Повторный prep_id дал бы двум пакетам одни варианты и фото."""
        prep_id = "prepared-once"
        backend_app.PREP_JOBS[prep_id] = {"status": "done"}

        with mock.patch.object(backend_app, "_schedule_publish") as schedule:
            first = await self._start(prep_id)
            first_payload = json.loads(first.body)
            backend_app.PUBLISH_JOBS[first_payload["job_id"]].update({
                "status": "failed",
                "items_published": 0,
                "step": "fill_title",
            })
            second = await self._start(prep_id)

        second_payload = json.loads(second.body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second_payload["job_id"], first_payload["job_id"])
        self.assertEqual(schedule.call_count, 1)

    async def test_resume_rejects_closed_job(self) -> None:
        """Игнорирование lifecycle status позволяет повторно открыть закрытый пакет."""
        job_id = str(uuid.uuid4())
        self._save_resumable(job_id, status="closed")

        with mock.patch.object(backend_app, "_schedule_publish") as schedule:
            response = await backend_app.api_publish_resume(job_id)

        self.assertEqual(response.status_code, 409)
        self.assertTrue(json.loads(response.body)["error"])
        schedule.assert_not_called()

    async def test_resume_reads_checkpoint_after_acquiring_lock(self) -> None:
        """Закрытие другого процесса перед lock не должно оставить старый план resume."""
        job_id = str(uuid.uuid4())
        self._save_resumable(job_id)
        real_acquire = publish_state.acquire_publish_lock

        def close_before_lock(root: Path, owner_id: str) -> Any:
            saved_job, draft = publish_state.load_publish_state(self.root / job_id)
            saved_job["status"] = "closed"
            publish_state.save_publish_state(self.root / job_id, job_id, saved_job, draft)
            return real_acquire(root, owner_id)

        async def no_publish(*_args: Any, **_kwargs: Any) -> None:
            return None

        with (
            mock.patch.object(publish_state, "acquire_publish_lock", new=close_before_lock),
            mock.patch.object(backend_app, "_run_publish_and_cleanup", new=no_publish),
        ):
            response = await backend_app.api_publish_resume(job_id)
            await asyncio.sleep(0)

        self.assertEqual(response.status_code, 409)
        saved_job, _draft_data = publish_state.load_publish_state(self.root / job_id)
        self.assertEqual(saved_job["status"], "closed")

    async def test_close_persists_closed_checkpoint_and_blocks_resume(self) -> None:
        """Без close пользователь не может явно завершить остановленный пакет."""
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                job_id = str(uuid.uuid4())
                self._save_resumable(job_id, status=status)
                backend_app.PUBLISH_JOBS[job_id] = {"status": status}

                response = await backend_app.api_publish_close(job_id)

                self.assertEqual(response.status_code, 200)
                saved_job, _draft_data = publish_state.load_publish_state(self.root / job_id)
                self.assertEqual(saved_job["status"], "closed")
                with mock.patch.object(backend_app, "_schedule_publish") as schedule:
                    resumed = await backend_app.api_publish_resume(job_id)
                self.assertEqual(resumed.status_code, 409)
                schedule.assert_not_called()

    async def test_close_rejects_foreign_live_lock_without_changing_checkpoint(self) -> None:
        """Другой сервер не может закрыть checkpoint, пока первый публикует."""
        job_id = str(uuid.uuid4())
        self._save_resumable(job_id, status="failed")
        owner_id = str(uuid.uuid4())
        handle = publish_state.acquire_publish_lock(self.root, owner_id)
        try:
            response = await backend_app.api_publish_close(job_id)
            saved_job, _draft_data = publish_state.load_publish_state(self.root / job_id)
        finally:
            publish_state.release_publish_lock(handle)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["job_id"], owner_id)
        self.assertEqual(saved_job["status"], "failed")

    async def test_close_missing_job_is_404_even_while_another_job_runs(self) -> None:
        """Чужой lock не превращает отсутствующий job_id в занятый пакет."""
        handle = publish_state.acquire_publish_lock(self.root, str(uuid.uuid4()))
        try:
            response = await backend_app.api_publish_close(str(uuid.uuid4()))
        finally:
            publish_state.release_publish_lock(handle)

        self.assertEqual(response.status_code, 404)

    async def test_close_recovered_running_checkpoint(self) -> None:
        """После аварии старый disk running не означает живую публикацию."""
        job_id = str(uuid.uuid4())
        self._save_resumable(job_id, status="running")
        backend_app.PUBLISH_JOBS[job_id] = {"status": "interrupted"}

        response = await backend_app.api_publish_close(job_id)

        self.assertEqual(response.status_code, 200)
        saved_job, _draft_data = publish_state.load_publish_state(self.root / job_id)
        self.assertEqual(saved_job["status"], "closed")

    async def test_close_needs_user_action_checkpoint(self) -> None:
        """Пакет, требующий ручной проверки, можно явно закрыть."""
        job_id = str(uuid.uuid4())
        self._save_resumable(job_id, status="needs_user_action")
        backend_app.PUBLISH_JOBS[job_id] = {"status": "needs_user_action"}

        response = await backend_app.api_publish_close(job_id)

        self.assertEqual(response.status_code, 200)
        saved_job, _draft_data = publish_state.load_publish_state(self.root / job_id)
        self.assertEqual(saved_job["status"], "closed")

    async def test_done_keeps_prep_referenced_by_another_unclosed_job(self) -> None:
        """Без проверки ссылок done одного job удалит варианты для второго."""
        prep_id = "shared-prep"
        done_id = str(uuid.uuid4())
        waiting_id = str(uuid.uuid4())
        prep_dir = self.root / f"prep_{prep_id}"
        prep_dir.mkdir()
        backend_app.PREP_JOBS[prep_id] = {"status": "done"}
        backend_app.PUBLISH_JOBS[done_id] = {"status": "done", "prep_id": prep_id}
        backend_app.PUBLISH_JOBS[waiting_id] = {"status": "failed", "prep_id": prep_id}

        async def publish_done(*_args: Any, **_kwargs: Any) -> None:
            return None

        with (
            mock.patch.object(backend_app.publisher, "run_publish_job", new=publish_done),
            mock.patch.object(backend_app.journal, "log_event"),
        ):
            await backend_app._run_publish_and_cleanup(
                done_id, _draft(), self.root / done_id, prep_id,
            )

        self.assertTrue(prep_dir.is_dir())
        self.assertIn(prep_id, backend_app.PREP_JOBS)

    async def test_done_keeps_prep_referenced_only_on_disk(self) -> None:
        """Локальная память первого сервера не знает задач второго сервера."""
        prep_id = "shared-on-disk"
        done_id = str(uuid.uuid4())
        waiting_id = str(uuid.uuid4())
        prep_dir = self.root / f"prep_{prep_id}"
        prep_dir.mkdir()
        backend_app.PREP_JOBS[prep_id] = {"status": "done"}
        backend_app.PUBLISH_JOBS[done_id] = {"status": "done", "prep_id": prep_id}
        disk_job = {
            "status": "failed", "step": "fill_title", "items_total": 2,
            "item_index": 1, "items_published": 0, "prep_id": prep_id,
            "money_click": "not_clicked",
        }
        publish_state.save_publish_state(self.root / waiting_id, waiting_id, disk_job, _draft())

        async def publish_done(*_args: Any, **_kwargs: Any) -> None:
            return None

        with (
            mock.patch.object(backend_app.publisher, "run_publish_job", new=publish_done),
            mock.patch.object(backend_app.journal, "log_event"),
        ):
            await backend_app._run_publish_and_cleanup(
                done_id, _draft(), self.root / done_id, prep_id,
            )

        self.assertTrue(prep_dir.is_dir())
        self.assertIn(prep_id, backend_app.PREP_JOBS)

    async def test_status_exposes_age_and_treats_legacy_job_as_stale(self) -> None:
        """Без возраста UI не отличит свежий пакет от старого checkpoint."""
        fresh_id = str(uuid.uuid4())
        legacy_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        backend_app.PUBLISH_JOBS[fresh_id] = {
            "status": "failed", "created_at": created_at,
        }
        backend_app.PUBLISH_JOBS[legacy_id] = {"status": "failed"}

        fresh = json.loads((await backend_app.api_publish_status(fresh_id)).body)
        legacy = json.loads((await backend_app.api_publish_status(legacy_id)).body)

        self.assertEqual(fresh["created_at"], created_at)
        self.assertIs(fresh["is_older_than_24h"], False)
        self.assertIsNone(legacy["created_at"])
        self.assertIs(legacy["is_older_than_24h"], True)

    async def test_recovery_keeps_closed_checkpoint_closed(self) -> None:
        """Recovery не должен превращать закрытый пользователем пакет в interrupted."""
        job_id = str(uuid.uuid4())
        self._save_resumable(job_id, status="closed")

        recovered = publish_state.recover_publish_states(self.root)

        self.assertIn(job_id, recovered)
        self.assertEqual(recovered[job_id][0]["status"], "closed")


class PublishLockTests(unittest.TestCase):
    def test_real_subprocess_lock_blocks_and_releases(self) -> None:
        """Advisory lock действительно исключает другой процесс, а не только PID-файл."""
        code = "\n".join((
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "from publish_state import acquire_publish_lock, release_publish_lock",
            "import os",
            "handle = acquire_publish_lock(Path(sys.argv[2]), sys.argv[3])",
            # PID интерпретатора, а не Popen.pid: на Windows .venv\Scripts\python.exe —
            # лаунчер, который запускает настоящий python.exe дочерним процессом.
            "print('READY', os.getpid(), flush=True)",
            "sys.stdin.readline()",
            "release_publish_lock(handle)",
        ))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner_id = str(uuid.uuid4())
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(BACKEND_DIR), str(root), owner_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                assert child.stdout is not None
                ready = child.stdout.readline().split()
                error = child.stderr.read() if child.poll() is not None else ""
                self.assertEqual(ready[:1], ["READY"], error)
                owner_pid = int(ready[1])
                with self.assertRaises(publish_state.PublishLockBusyError) as raised:
                    publish_state.acquire_publish_lock(root, str(uuid.uuid4()))
                self.assertEqual(raised.exception.pid, owner_pid)
                self.assertEqual(raised.exception.job_id, owner_id)
            finally:
                assert child.stdin is not None
                child.stdin.close()
                child.wait(timeout=5)
                child.stdout.close()
                assert child.stderr is not None
                child.stderr.close()
            handle = publish_state.acquire_publish_lock(root, str(uuid.uuid4()))
            publish_state.release_publish_lock(handle)

    def test_live_pid_lock_rejects_second_process(self) -> None:
        """Без межпроцессного lock два сервера могут писать один Chrome."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            foreign_job_id = str(uuid.uuid4())
            (root / "publish.lock").write_text(
                json.dumps({"pid": os.getpid(), "job_id": foreign_job_id}),
                encoding="utf-8",
            )

            with self.assertRaises(publish_state.PublishLockBusyError) as raised:
                publish_state.acquire_publish_lock(root, str(uuid.uuid4()))

        self.assertEqual(raised.exception.pid, os.getpid())
        self.assertEqual(raised.exception.job_id, foreign_job_id)

    def test_dead_pid_lock_is_reclaimed(self) -> None:
        """Старый lock после аварии не должен навечно блокировать публикацию."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "publish.lock").write_text(
                json.dumps({"pid": 999_999_999, "job_id": str(uuid.uuid4())}),
                encoding="utf-8",
            )
            handle = publish_state.acquire_publish_lock(root, str(uuid.uuid4()))
            publish_state.release_publish_lock(handle)
            again = publish_state.acquire_publish_lock(root, str(uuid.uuid4()))
            publish_state.release_publish_lock(again)


if __name__ == "__main__":
    unittest.main()
