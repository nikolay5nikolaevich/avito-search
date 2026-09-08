"""Дисковый checkpoint и возобновление публикации без повторов."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import category_profiles  # noqa: E402
import app as backend_app  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402


def make_draft() -> publisher.DraftData:
    return publisher.DraftData(
        title="Кроссовки Heckel",
        trade_type="Продаю своё",
        condition="Новое",
        size="42",
        brand="Heckel",
        color="Чёрный",
        description="Описание",
        price=5000,
        locations=(
            publisher.LocationData("Москва", "Тверская, 10"),
            publisher.LocationData("Химки", "улица Ленина, 2"),
            publisher.LocationData("Мытищи", "проспект Мира, 3"),
        ),
        view_price_max=Decimal("2"),
        photo_paths=("tmp/publish/job-1/photo_01.jpg",),
        category="sneakers",
    )


class PublishStateFileTests(unittest.TestCase):
    def test_state_round_trip_is_atomic_and_preserves_decimal_data(self) -> None:
        job = {
            "status": "failed",
            "items_total": 3,
            "item_index": 2,
            "items_published": 1,
            "published_urls": ["https://www.avito.ru/items/edit/100"],
            "applied_view_prices": [{"item_index": 1, "price": "1.7"}],
            "address_warnings": [],
            "brand_selected": "Без бренда",
            "prep_id": "prep-1",
        }

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-1"
            publish_state.save_publish_state(job_dir, "job-1", job, make_draft())

            self.assertFalse((job_dir / "publish-state.json.tmp").exists())
            restored_job, restored_draft = publish_state.load_publish_state(job_dir)

        self.assertEqual(restored_job["published_urls"], job["published_urls"])
        self.assertEqual(restored_draft.view_price_max, Decimal("2"))
        self.assertEqual(restored_draft.locations[2].city, "Мытищи")

    def test_legacy_checkpoint_without_view_price_max_restores_from_view_price(self) -> None:
        """Старые чекпоинты (~40 сохранённых задач) хранят только view_price;
        сервер должен восстановить их без падения, взяв это значение как потолок."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-legacy"
            job_dir.mkdir(parents=True)
            legacy_draft = {
                "title": "Кроссовки Heckel",
                "trade_type": "Продаю своё",
                "condition": "Новое",
                "size": "42",
                "brand": "Heckel",
                "color": "Чёрный",
                "description": "Описание",
                "price": 5000,
                "locations": [
                    {"city": "Москва", "address": "Тверская, 10"},
                ],
                "view_price": "0.4",
                "photo_paths": ["tmp/publish/job-1/photo_01.jpg"],
                "category": "sneakers",
            }
            payload = {
                "version": publish_state.STATE_VERSION,
                "job_id": "job-legacy",
                "job": {"status": "failed"},
                "draft": legacy_draft,
            }
            (job_dir / publish_state.STATE_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            _job, draft = publish_state.load_publish_state(job_dir)

        self.assertEqual(draft.view_price_max, Decimal("0.4"))

    def test_recovery_marks_running_job_resumable_instead_of_restarting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_state.save_publish_state(
                root / "job-1",
                "job-1",
                {
                    "status": "running",
                    "items_total": 3,
                    "item_index": 2,
                    "items_published": 1,
                    "published_urls": ["https://www.avito.ru/items/edit/100"],
                },
                make_draft(),
            )

            recovered = publish_state.recover_publish_states(root)

        job, _draft = recovered["job-1"]
        self.assertEqual(job["status"], "interrupted")
        self.assertTrue(job["user_action"]["resumable"])
        self.assertEqual(job["items_published"], 1)

    def test_recovery_never_offers_to_repeat_item_inside_financial_flow(self) -> None:
        """Объявление, дошедшее до финансовых шагов, повторно не отправляется.

        Продолжение при этом доступно — но только со СЛЕДУЮЩЕГО объявления:
        повтор текущего создал бы дубль или вторую оплату.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_state.save_publish_state(
                root / "job-financial",
                "job-financial",
                {
                    "status": "running",
                    "step": "continue_view_price",
                    "items_total": 3,
                    "item_index": 2,
                    "items_published": 1,
                    "published_urls": ["https://www.avito.ru/items/edit/100"],
                },
                make_draft(),
            )

            recovered = publish_state.recover_publish_states(root)

        job, _draft = recovered["job-financial"]
        self.assertEqual(job["status"], "interrupted")
        # Продолжение возможно, но объявление №2 пропускается, а не повторяется
        self.assertTrue(job["user_action"]["resumable"])
        self.assertEqual(job["user_action"]["skipped_item"], 2)
        self.assertEqual(job["user_action"]["next_item_index"], 3)
        self.assertEqual(publish_state.resume_plan(job), {
            "mode": "skip_item",
            "start_index": 3,
            "skipped_item": 2,
            "items_total": 3,
        })
        # Сообщение пользователю обязано объяснять судьбу пропущенного
        self.assertIn("№2", job["error"])


class PublishResumeSafetyTests(unittest.TestCase):
    def test_inflight_item_is_resumable_only_before_financial_transition(self) -> None:
        base_job = {
            "items_total": 13,
            "item_index": 10,
            "items_published": 9,
        }

        for step in (
            "connect_chrome",
            "open_form",
            "select_category",
            "check_category",
            "fill_title",
            "upload_photos",
            "fill_fields",
            "fill_description",
            "fill_item_price",
            "fill_address",
        ):
            with self.subTest(step=step):
                self.assertTrue(
                    publish_state.is_publish_resume_safe({**base_job, "step": step})
                )

        for step in (
            "continue_listing",
            "fill_view_price",
            "continue_view_price",
            "skip_services",
        ):
            with self.subTest(step=step):
                self.assertFalse(
                    publish_state.is_publish_resume_safe({**base_job, "step": step})
                )

    def test_completed_item_checkpoint_can_resume_the_next_item(self) -> None:
        self.assertTrue(
            publish_state.is_publish_resume_safe({
                "items_total": 13,
                "item_index": 9,
                "items_published": 9,
                "step": "skip_services",
            })
        )


class PublishResumePlanTests(unittest.TestCase):
    """Второй режим возобновления: retry_item (до финансов) vs skip_item
    (объявление уже создано на Авито — повторно не трогаем, едем дальше)."""

    def test_retry_item_before_financial_step(self) -> None:
        plan = publish_state.resume_plan({
            "items_total": 13,
            "items_published": 6,
            "item_index": 7,
            "step": "fill_fields",
        })
        self.assertEqual(plan, {
            "mode": "retry_item",
            "start_index": 7,
            "skipped_item": None,
            "items_total": 13,
        })

    def test_skip_item_after_financial_step(self) -> None:
        """Боевой кейс пользователя: объявление №7 создано на Авито (сбой на
        fill_view_price случился уже после continue_listing), повторный
        финансовый клик по нему запрещён — пакет продолжается с №8."""
        plan = publish_state.resume_plan({
            "items_total": 13,
            "items_published": 6,
            "item_index": 7,
            "step": "fill_view_price",
        })
        self.assertEqual(plan, {
            "mode": "skip_item",
            "start_index": 8,
            "skipped_item": 7,
            "items_total": 13,
        })

    def test_already_skipped_item_is_never_offered_again(self) -> None:
        """Защита от дубля и второй оплаты.

        Сразу после возобновления с пропуском job["item_index"] ещё хранит
        номер пропущенного объявления от прошлого прогона. Если поверить ему,
        план предложит «повторить» уже созданное на Авито объявление №7 —
        это дубль и повторное списание. Продолжать можно только с №8.
        """
        plan = publish_state.resume_plan({
            "items_total": 13,
            "items_published": 6,
            "item_index": 7,
            "skipped_items": [7],
            "step": "connect_chrome",
        })
        self.assertEqual(plan, {
            "mode": "retry_item",
            "start_index": 8,
            "skipped_item": None,
            "items_total": 13,
        })

    def test_item_after_skip_is_retried_not_skipped(self) -> None:
        """Реальный сбой 26.08.2026: после пропуска №7 прогон упал на загрузке
        фото объявления №8. Объявление №8 на Авито создано НЕ было, поэтому его
        нужно повторить, а не потерять, объявив пропущенным."""
        plan = publish_state.resume_plan({
            "items_total": 13,
            "items_published": 6,
            "item_index": 8,
            "skipped_items": [7],
            "step": "upload_photos",
        })
        self.assertEqual(plan, {
            "mode": "retry_item",
            "start_index": 8,
            "skipped_item": None,
            "items_total": 13,
        })

    def test_second_skip_continues_after_both_skipped_items(self) -> None:
        """Два пропуска подряд не сбивают счёт: №7 пропущен раньше, №8 создан
        на Авито и падает на финансовом шаге — продолжаем с №9."""
        plan = publish_state.resume_plan({
            "items_total": 13,
            "items_published": 6,
            "item_index": 8,
            "skipped_items": [7],
            "step": "continue_view_price",
        })
        self.assertEqual(plan, {
            "mode": "skip_item",
            "start_index": 9,
            "skipped_item": 8,
            "items_total": 13,
        })

    def test_none_when_everything_is_published_or_skipped(self) -> None:
        """Отправлено 12, одно пропущено — незакрытых объявлений не осталось."""
        self.assertIsNone(publish_state.resume_plan({
            "items_total": 13,
            "items_published": 12,
            "item_index": 13,
            "skipped_items": [7],
            "step": "open_next_form",
        }))

    def test_none_when_stopped_on_last_item_after_financial_step(self) -> None:
        """Пропускать нечего — остановка пришлась на последнее объявление
        пакета, продолжать после пропуска было бы нечем."""
        plan = publish_state.resume_plan({
            "items_total": 13,
            "items_published": 12,
            "item_index": 13,
            "step": "continue_view_price",
        })
        self.assertIsNone(plan)

    def test_none_when_everything_already_published(self) -> None:
        plan = publish_state.resume_plan({
            "items_total": 13,
            "items_published": 13,
            "item_index": 13,
            "step": "skip_services",
        })
        self.assertIsNone(plan)

    def test_none_on_garbage_fields(self) -> None:
        plan = publish_state.resume_plan({
            "items_total": "не число",
            "items_published": 6,
            "item_index": 7,
        })
        self.assertIsNone(plan)


class _Page:
    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _Context:
    async def new_page(self) -> _Page:
        return _Page()


class _PlaywrightManager:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


class PublishResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_during_variant_preparation_rebuilds_variants(self) -> None:
        job_id = "job-prep-resume"
        job: dict[str, object] = {
            "status": "interrupted",
            "items_total": 3,
            "items_published": 0,
            "prep_id": None,
        }

        async def prepare(_prep_id: str, prep_job: dict[str, object], **_kwargs: object) -> None:
            prep_job["status"] = "done"

        async def publish(
            _job_id: str,
            current_job: dict[str, object],
            _draft: publisher.DraftData,
            **kwargs: object,
        ) -> None:
            self.assertEqual(kwargs["start_index"], 1)
            current_job["status"] = "done"

        prepare_mock = mock.AsyncMock(side_effect=prepare)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend_app.PUBLISH_JOBS[job_id] = job
            try:
                with (
                    mock.patch.object(backend_app, "TMP_PUBLISH_DIR", root),
                    mock.patch.object(
                        backend_app.preparation,
                        "run_prep_job",
                        new=prepare_mock,
                    ),
                    mock.patch.object(
                        backend_app.publisher,
                        "run_publish_job",
                        new=publish,
                    ),
                ):
                    await backend_app._run_publish_and_cleanup(
                        job_id,
                        make_draft(),
                        root / job_id,
                        None,
                        start_index=1,
                    )
            finally:
                backend_app.PUBLISH_JOBS.pop(job_id, None)

        self.assertEqual(prepare_mock.await_count, 1)

    async def test_resume_endpoint_schedules_only_unpublished_items(self) -> None:
        job_id = "job-resume"
        job = {
            "status": "needs_user_action",
            "step": "open_form",
            "items_total": 3,
            "item_index": 2,
            "items_published": 1,
            "published_urls": ["https://www.avito.ru/items/edit/101"],
            "prep_id": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_state.save_publish_state(root / job_id, job_id, job, make_draft())
            backend_app.PUBLISH_JOBS[job_id] = dict(job)
            try:
                with (
                    mock.patch.object(backend_app, "TMP_PUBLISH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_publish") as schedule,
                ):
                    response = await backend_app.api_publish_resume(job_id)
            finally:
                backend_app.PUBLISH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"resumed_from":2', response.body)
        self.assertEqual(schedule.call_args.kwargs["start_index"], 2)

    async def test_resume_endpoint_skips_item_created_after_financial_step(self) -> None:
        """Остановка на continue_view_price означает, что объявление №2 уже
        создано на Авито (шаг после continue_listing) — resume НЕ повторяет
        его (никаких финансовых кликов второй раз), а едет с №3 и запоминает
        пропущенный номер для ручной проверки пользователем."""
        job_id = "job-skip-resume"
        saved_job = {
            "status": "failed",
            "step": "continue_view_price",
            "items_total": 3,
            "item_index": 2,
            "items_published": 1,
            "published_urls": ["https://www.avito.ru/items/edit/101"],
            "prep_id": None,
        }
        # Память намеренно выглядит безопасной: endpoint обязан доверять диску.
        backend_app.PUBLISH_JOBS[job_id] = {
            **saved_job,
            "step": "open_form",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_state.save_publish_state(
                root / job_id,
                job_id,
                saved_job,
                make_draft(),
            )
            try:
                with (
                    mock.patch.object(backend_app, "TMP_PUBLISH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_publish") as schedule,
                ):
                    response = await backend_app.api_publish_resume(job_id)
                # Читаем до pop в finally — задача ещё жива в памяти.
                skipped_items = backend_app.PUBLISH_JOBS[job_id]["skipped_items"]
            finally:
                backend_app.PUBLISH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["mode"], "skip_item")
        self.assertEqual(payload["skipped_item"], 2)
        self.assertEqual(payload["resumed_from"], 3)
        self.assertEqual(schedule.call_args.kwargs["start_index"], 3)
        self.assertEqual(skipped_items, [2])

    async def test_resume_endpoint_rejects_when_skip_would_leave_nothing(self) -> None:
        """Остановка на последнем объявлении пакета после финансового шага —
        пропускать некого продолжать, resume запрещён."""
        job_id = "job-resume-none-left"
        saved_job = {
            "status": "failed",
            "step": "continue_view_price",
            "items_total": 3,
            "item_index": 3,
            "items_published": 2,
            "published_urls": [
                "https://www.avito.ru/items/edit/101",
                "https://www.avito.ru/items/edit/102",
            ],
            "prep_id": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_state.save_publish_state(
                root / job_id,
                job_id,
                saved_job,
                make_draft(),
            )
            try:
                with (
                    mock.patch.object(backend_app, "TMP_PUBLISH_DIR", root),
                    mock.patch.object(backend_app, "_schedule_publish") as schedule,
                ):
                    response = await backend_app.api_publish_resume(job_id)
            finally:
                backend_app.PUBLISH_JOBS.pop(job_id, None)

        self.assertEqual(response.status_code, 409)
        schedule.assert_not_called()

    async def test_resume_starts_at_first_unpublished_item(self) -> None:
        seen: list[int] = []

        async def run_item(
            _page: object,
            _job: dict[str, object],
            _data: publisher.DraftData,
            _location: publisher.LocationData,
            item_index: int,
            _items_total: int,
            _profile: object,
            *,
            active_page_ref: list[object] | None,
            checkpoint_callback: object,
        ) -> tuple[str, str, Decimal, None]:
            del active_page_ref, checkpoint_callback
            seen.append(item_index)
            item_id = str(100 + item_index)
            return item_id, f"https://www.avito.ru/items/edit/{item_id}", Decimal("1.7"), None

        job: dict[str, object] = {
            "status": "failed",
            "items_total": 3,
            "item_index": 2,
            "items_published": 1,
            "published_urls": ["https://www.avito.ru/items/edit/101"],
            "applied_view_prices": [{"item_index": 1, "price": "1.7"}],
            "address_warnings": [],
        }

        with (
            mock.patch(
                "playwright.async_api.async_playwright",
                return_value=_PlaywrightManager(),
            ),
            mock.patch(
                "browser.connect_over_cdp",
                new=mock.AsyncMock(return_value=_Context()),
            ),
            mock.patch.object(
                publisher,
                "_run_publish_preflight",
                new=mock.AsyncMock(return_value="Без бренда"),
            ),
            mock.patch.object(publisher, "_run_single_item", new=run_item),
            mock.patch.object(publisher, "_pause", new=mock.AsyncMock()),
            mock.patch.object(publisher.asyncio, "sleep", new=mock.AsyncMock()),
            mock.patch.object(
                publisher,
                "_dump_failure",
                new=mock.AsyncMock(return_value=None),
            ),
        ):
            await publisher.run_publish_job(
                "job-1",
                job,
                make_draft(),
                cdp_url="http://127.0.0.1:9222",
                start_index=2,
            )

        self.assertEqual(seen, [2, 3])
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["items_published"], 3)
        self.assertEqual(len(job["published_urls"]), 3)


if __name__ == "__main__":
    unittest.main()
