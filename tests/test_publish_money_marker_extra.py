"""
Дополнения к tests/test_publish_money_marker.py (бриф 03) — места, которые
тесты-доказательства не покрывают: атомарная запись при сбое, согласованность
списка шагов, общий текст причины для /status и /resume, проводка трекера
денежного клика через _run_single_item и промежуточную кнопку категории.

Запуск: .venv\\Scripts\\python.exe -m unittest tests.test_publish_money_marker_extra -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app  # noqa: E402
import atomic_write  # noqa: E402
import avito_publish_selectors as psel  # noqa: E402
import category_profiles  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402


def _draft(n_items: int = 2) -> publisher.DraftData:
    return publisher.DraftData(
        title="Доп", trade_type="tt", condition="c", size="s", brand="",
        color="col", description="d", price=100,
        locations=tuple(
            publisher.LocationData(city="Москва", address=f"ул. Доп, {k}")
            for k in range(1, n_items + 1)
        ),
        view_price_max=Decimal("1"), photo_paths=(), category="jackets",
    )


class AtomicWriteFailureTests(unittest.TestCase):
    def test_failed_fsync_keeps_previous_file_and_removes_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            atomic_write.write_text_atomic(target, "старое")

            with mock.patch.object(atomic_write.os, "fsync", side_effect=OSError(5, "EIO")):
                with self.assertRaises(OSError):
                    atomic_write.write_text_atomic(target, "новое")

            self.assertEqual(target.read_text(encoding="utf-8"), "старое")
            self.assertFalse((Path(tmp) / "state.json.tmp").exists())

    def test_save_publish_state_propagates_write_failure(self) -> None:
        """Сбой записи обязан дойти до вызывающего кода: publisher по нему
        останавливается ДО денежного клика."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(atomic_write.os, "replace", side_effect=OSError(28, "ENOSPC")):
                with self.assertRaises(OSError):
                    publish_state.save_publish_state(
                        Path(tmp) / "job", "job", {"status": "running"}, _draft(),
                    )


class ResumePlanSchemaTests(unittest.TestCase):
    def test_known_steps_match_publisher_steps(self) -> None:
        """Новый шаг в publisher.STEPS без обновления publish_state сделал бы
        любой checkpoint с ним «не распознанным» — ловим рассинхрон здесь."""
        self.assertEqual(
            set(publish_state.KNOWN_STEPS),
            set(publisher.STEP_LABELS) | {"", "prepare_variants"},
        )

    def test_unhashable_money_click_is_no_plan_not_a_crash(self) -> None:
        job = {"step": "continue_listing", "item_index": 2, "items_total": 3,
               "items_published": 1, "money_click": ["clicked"]}
        self.assertIsNone(publish_state.resume_plan(job))
        self.assertIn("не распознано", publish_state.resume_unavailable_reason(job))

    def test_reason_is_none_when_plan_exists(self) -> None:
        job = {"step": "fill_title", "item_index": 2, "items_total": 3, "items_published": 1}
        self.assertIsNotNone(publish_state.resume_plan(job))
        self.assertIsNone(publish_state.resume_unavailable_reason(job))

    def test_not_clicked_of_previous_item_does_not_affect_fresh_next_item(self) -> None:
        job = {"step": "open_next_form", "item_index": 1, "items_total": 3,
               "items_published": 1, "money_click": "clicked"}
        plan = publish_state.resume_plan(job)
        self.assertEqual(plan["mode"], "retry_item")
        self.assertEqual(plan["start_index"], 2)


class StatusAndResumeShareReasonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(app, "TMP_PUBLISH_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.PUBLISH_JOBS.clear()
        app.ACTIVE_PUBLISH_TASKS.clear()
        self.addCleanup(app.PUBLISH_JOBS.clear)
        self.addCleanup(app.ACTIVE_PUBLISH_TASKS.clear)

    async def test_unrecognized_checkpoint_same_reason_in_status_and_resume(self) -> None:
        job_id = str(uuid.uuid4())
        job = {"status": "failed", "step": "шаг_из_будущего", "items_total": 3,
               "item_index": 2, "items_published": 1}
        publish_state.save_publish_state(self.root / job_id, job_id, job, _draft(3))
        app.PUBLISH_JOBS[job_id] = dict(job)

        status = app._serialize_publish_status(job_id, app.PUBLISH_JOBS[job_id])
        with mock.patch.object(app, "_schedule_publish", lambda *a, **k: None):
            response = await app.api_publish_resume(job_id)

        self.assertIs(status["resume_available"], False)
        self.assertEqual(response.status_code, 409)
        self.assertIn("не распознано", status["resume_unavailable_reason"])
        self.assertEqual(json.loads(response.body)["error"], status["resume_unavailable_reason"])


# --- проводка трекера ------------------------------------------------------

async def _noop(*_a: Any, **_k: Any) -> None:
    return None


class RunSingleItemTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_mark_clicked_inside_a_step_sets_marker_and_writes_checkpoint(self) -> None:
        job: dict[str, Any] = {"money_click": None}
        written: list[Optional[str]] = []

        async def check_category_that_clicks(*_a: Any, **_k: Any) -> None:
            tracker = publisher._MONEY_CLICK_TRACKER.get()
            self.assertIsNotNone(tracker, "трекер не выставлен на время объявления")
            tracker.mark_clicked()
            raise publisher.UserActionRequired("стоп после промежуточного клика")

        async def open_form(*_a: Any, **_k: Any) -> str:
            return "form"

        with mock.patch.object(publisher, "_pause", _noop), \
             mock.patch.object(publisher, "_step_open_form", open_form), \
             mock.patch.object(publisher, "_step_select_category", _noop), \
             mock.patch.object(publisher, "_step_check_category", check_category_that_clicks):
            with self.assertRaises(publisher.UserActionRequired):
                await publisher._run_single_item(
                    object(), job, _draft(1), _draft(1).location_for(1), 1, 1,
                    category_profiles.JACKETS,
                    checkpoint_callback=lambda: written.append(job.get("money_click")),
                )

        self.assertEqual(job["money_click"], "clicked")
        self.assertEqual(written, ["clicked"])
        self.assertIsNone(publisher._MONEY_CLICK_TRACKER.get(), "трекер не снят после объявления")

    async def test_substituted_continue_listing_failure_stays_conservative(self) -> None:
        """Подменённый шаг (стенд) ничего не отмечает — его сбой не доказывает
        отсутствие клика, отметка остаётся None (resume: skip)."""
        job: dict[str, Any] = {"money_click": None}

        async def open_form(*_a: Any, **_k: Any) -> str:
            return "form"

        async def continue_listing_fails(_page: Any) -> tuple[str, Any]:
            raise publisher.StepError("сбой где-то внутри подменённого шага")

        patches = {
            "_pause": _noop, "_step_open_form": open_form,
            "_step_select_category": _noop, "_step_check_category": _noop,
            "_guard_against_reopened_draft": _noop, "_clear_and_type": _noop,
            "_step_upload_photos": _noop, "_step_fill_fields": _noop,
            "_step_fill_description": _noop, "_step_fill_price": _noop,
            "_step_fill_address": _noop, "_step_continue_listing": continue_listing_fails,
        }
        with mock.patch.multiple(publisher, **patches):
            with self.assertRaises(publisher.StepError):
                await publisher._run_single_item(
                    object(), job, _draft(1), _draft(1).location_for(1), 1, 1,
                    category_profiles.JACKETS, checkpoint_callback=lambda: None,
                )

        self.assertIsNone(job["money_click"])


class _Loc:
    def __init__(self, page: "_IntermediatePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_Loc":
        return self

    async def count(self) -> int:
        if self.selector == psel.ADDRESS_HIDDEN:
            return 1 if self.page.address_in_dom else 0
        return 1

    async def input_value(self, timeout: Optional[int] = None) -> str:
        if self.selector == psel.ADDRESS_HIDDEN:
            if self.page.address_unreadable:
                raise TimeoutError("адрес не читается")
            return ""
        return ""

    async def is_visible(self, timeout: Optional[int] = None) -> bool:
        if self.selector == psel.TITLE_INPUT:
            return self.page.title_visible
        return self.selector == psel.CATEGORY_CONFIRM_CONTINUE_BUTTON

    async def is_enabled(self, timeout: Optional[int] = None) -> bool:
        return True

    async def inner_text(self, timeout: Optional[int] = None) -> str:
        return "Продолжить"

    async def click(self, timeout: Optional[int] = None) -> None:
        self.page.clicks += 1
        self.page.url = self.page.url_after_click
        self.page.title_visible = True


class _IntermediatePage:
    def __init__(self, url_after_click: str = "https://www.avito.ru/additem") -> None:
        self.url = "https://www.avito.ru/additem"
        self.url_after_click = url_after_click
        self.title_visible = False
        self.address_in_dom = True
        self.address_unreadable = False
        self.clicks = 0

    def locator(self, selector: str) -> _Loc:
        return _Loc(self, selector)

    async def wait_for_selector(self, selector: str, *, timeout: int, state: str) -> _Loc:
        loc = self.locator(selector)
        if not await loc.is_visible():
            raise TimeoutError(selector)
        return loc


class IntermediateClickMarkerTests(unittest.IsolatedAsyncioTestCase):
    async def _run_with_tracker(self, page: _IntermediatePage, mark: Any) -> None:
        tracker = publisher._MoneyClickTracker(mark_clicked=mark)
        token = publisher._MONEY_CLICK_TRACKER.set(tracker)
        try:
            await publisher._continue_category_confirmation(page, category_profiles.TSHIRTS)
        finally:
            publisher._MONEY_CLICK_TRACKER.reset(token)

    async def test_cpxpromo_after_intermediate_click_marks_clicked(self) -> None:
        page = _IntermediatePage("https://www.avito.ru/cpxpromo/555")
        marks: list[str] = []
        with self.assertRaises(publisher.UserActionRequired):
            await self._run_with_tracker(page, lambda: marks.append("clicked"))
        self.assertEqual(page.clicks, 1)
        self.assertEqual(marks, ["clicked"])

    async def test_any_other_path_after_intermediate_click_also_marks_clicked(self) -> None:
        """Консервативное расширение брифа: увод с /additem куда угодно —
        объявление могло быть создано; повтор запрещаем."""
        page = _IntermediatePage("https://www.avito.ru/profile/items")
        marks: list[str] = []
        with self.assertRaises(publisher.UserActionRequired):
            await self._run_with_tracker(page, lambda: marks.append("clicked"))
        self.assertEqual(marks, ["clicked"])

    async def test_marker_write_failure_still_stops(self) -> None:
        page = _IntermediatePage("https://www.avito.ru/cpxpromo/555")

        def failing_mark() -> None:
            raise publisher.StepError("диск недоступен")

        with self.assertRaises(publisher.UserActionRequired):
            await self._run_with_tracker(page, failing_mark)

    async def test_normal_intermediate_click_does_not_mark(self) -> None:
        page = _IntermediatePage()
        marks: list[str] = []
        await self._run_with_tracker(page, lambda: marks.append("clicked"))
        self.assertEqual(page.clicks, 1)
        self.assertEqual(marks, [])

    async def test_address_absent_from_dom_counts_as_empty(self) -> None:
        page = _IntermediatePage()
        page.address_in_dom = False
        await self._run_with_tracker(page, lambda: None)
        self.assertEqual(page.clicks, 1)

    async def test_address_present_but_unreadable_blocks_click(self) -> None:
        page = _IntermediatePage()
        page.address_unreadable = True
        with self.assertRaises(publisher.UserActionRequired):
            await self._run_with_tracker(page, lambda: None)
        self.assertEqual(page.clicks, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
