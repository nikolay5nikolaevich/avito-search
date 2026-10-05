"""
Тесты-доказательства для брифа docs/_internal/briefs/resume-audit/03-checkpoint-money-marker.md
(находки F20, F03, F07/S1, F06, F36, F27; fail-closed по опровергнутым F09/F11/F12).

Реализации ЕЩЁ НЕТ — код напишет другой агент после этих тестов. Поэтому
большинство тестов здесь ожидаемо КРАСНЫЕ: они фиксируют интерфейс,
согласованный в брифе (job["money_click"], resume_unavailable_reason,
_guard_against_reopened_draft(..., own_draft_allowed=...), fsync перед
replace), а не текущее поведение кода.

Структура (разделы независимы, можно запускать точечно):
  A. resume_plan: чистая таблица «шаг + money_click -> план» (без диска).
  B. save_publish_state: os.fsync должен быть вызван ДО os.replace.
  C. F06: /status и /resume должны видеть один и тот же план — план с диска,
     а не из памяти; недоступный диск -> resume_unavailable_reason.
  D. F03/F07/S1: денежная отметка вокруг РЕАЛЬНОГО _step_continue_listing
     (сбои до/после click(), сам click() бросает исключение, разовый сбой
     записи, сброс отметки между объявлениями, старый checkpoint без поля).
  E. F36: промежуточная кнопка категории — те же строгие проверки, что и у
     финальной, и денежная отметка, если клик всё же увёл на /cpxpromo.
  F. F27: защита от переоткрытой формы работает для №1 и снимается для
     retry_item того же номера (own_draft_allowed), плюс api_publish_resume
     проставляет job["resume_retry_item"].

Запуск точечно:
  .venv\\Scripts\\python.exe -m unittest tests.test_publish_money_marker -v
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import avito_publish_selectors as psel  # noqa: E402
import category_profiles  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402
import app  # noqa: E402


# =============================================================================
# Раздел A. resume_plan: чистая таблица «шаг + money_click -> план»
# =============================================================================

def _job(
    *,
    step: str,
    item_index: int = 2,
    items_total: int = 3,
    items_published: int = 1,
    money_click: Any = "__absent__",
    skipped_items: Optional[list[int]] = None,
) -> dict[str, Any]:
    """Минимальный job-словарь для чистых тестов resume_plan.

    По умолчанию item_index == pending (объявление «в работе») — именно этот
    случай проверяет отметка money_click. money_click="__absent__" — ключа
    вовсе нет (старый checkpoint без поля, находка F07/S1 п.6).
    """
    data: dict[str, Any] = {
        "step": step,
        "item_index": item_index,
        "items_total": items_total,
        "items_published": items_published,
        "skipped_items": list(skipped_items or []),
    }
    if money_click != "__absent__":
        data["money_click"] = money_click
    return data


class ResumePlanMoneyClickTableTests(unittest.TestCase):
    """Таблица «шаг × money_click -> план» из интерфейса брифа 03."""

    # --- money_click == "clicked" -> skip_item ПРИ ЛЮБОМ шаге (п.11) --------

    def test_clicked_marker_forces_skip_even_on_safe_step_check_category(self) -> None:
        job = _job(step="check_category", money_click="clicked")
        plan = publish_state.resume_plan(job)
        self.assertIsNotNone(plan, "clicked на безопасном шаге не должен разрешать retry")
        self.assertEqual(plan["mode"], "skip_item")
        self.assertEqual(plan["skipped_item"], 2)

    def test_clicked_marker_on_continue_listing_is_skip_item(self) -> None:
        job = _job(step="continue_listing", money_click="clicked")
        plan = publish_state.resume_plan(job)
        self.assertEqual(plan["mode"], "skip_item")

    def test_clicked_marker_on_post_financial_step_is_skip_item(self) -> None:
        job = _job(step="skip_services", money_click="clicked")
        plan = publish_state.resume_plan(job)
        self.assertEqual(plan["mode"], "skip_item")

    # --- continue_listing: not_clicked -> retry, None/нет ключа -> skip ------

    def test_continue_listing_not_clicked_is_retry_item(self) -> None:
        job = _job(step="continue_listing", money_click="not_clicked")
        plan = publish_state.resume_plan(job)
        self.assertEqual(plan["mode"], "retry_item")
        self.assertEqual(plan["start_index"], 2)

    def test_continue_listing_none_marker_is_conservative_skip(self) -> None:
        """Клик мог пройти, могли не пройти проверки после click() — неизвестно,
        поэтому дефолт для continue_listing без явной not_clicked — skip."""
        job = _job(step="continue_listing", money_click=None)
        plan = publish_state.resume_plan(job)
        self.assertEqual(plan["mode"], "skip_item")

    def test_old_checkpoint_without_money_click_field_on_continue_listing_skips(
        self,
    ) -> None:
        """П.6 брифа: старый checkpoint без поля money_click читается так же
        консервативно, как сегодня — skip_item, не retry."""
        job = _job(step="continue_listing", money_click="__absent__")
        self.assertNotIn("money_click", job)
        plan = publish_state.resume_plan(job)
        self.assertEqual(plan["mode"], "skip_item")

    # --- безопасные шаги без денежной отметки — поведение не меняется -------

    def test_safe_step_without_marker_is_still_retry_item(self) -> None:
        job = _job(step="fill_address", money_click="__absent__")
        plan = publish_state.resume_plan(job)
        self.assertEqual(plan["mode"], "retry_item")

    # --- fail-closed: money_click вне {None, "clicked", "not_clicked"} -------

    def test_unknown_money_click_value_on_continue_listing_is_no_plan(self) -> None:
        job = _job(step="continue_listing", money_click="maybe")
        plan = publish_state.resume_plan(job)
        self.assertIsNone(plan, "мусорное значение money_click должно давать None, а не план")

    def test_unknown_money_click_value_on_safe_step_is_also_no_plan(self) -> None:
        """Fail-closed не должен зависеть от того, на каком шаге встретилось
        мусорное значение — это признак несогласованного checkpoint."""
        job = _job(step="fill_address", money_click="maybe")
        plan = publish_state.resume_plan(job)
        self.assertIsNone(plan)

    # --- fail-closed: прочие опровергнутые находки (F09/F11/F12) ------------

    def test_item_index_ahead_of_pending_is_no_plan(self) -> None:
        """F09: item_index забежал вперёд расчётного pending — несогласованно."""
        job = _job(step="fill_address", item_index=5, items_total=5, items_published=1)
        # pending = 1 (published) + 0 (skipped) + 1 = 2; item_index=5 > pending.
        plan = publish_state.resume_plan(job)
        self.assertIsNone(plan, "item_index > pending должен давать None, а не тихий план")

    def test_empty_step_at_positive_item_index_is_no_plan(self) -> None:
        """F11: пустой step допустим только для item_index==0 (пакет не начат).
        При item_index>0 это несогласованное состояние — fail-closed None."""
        job = _job(step="", item_index=2, items_total=3, items_published=0)
        plan = publish_state.resume_plan(job)
        self.assertIsNone(plan)

    def test_unknown_step_name_is_no_plan(self) -> None:
        job = _job(step="totally_unknown_step_xyz")
        plan = publish_state.resume_plan(job)
        self.assertIsNone(plan)


# =============================================================================
# Раздел B. save_publish_state: fsync перед replace (F20)
# =============================================================================

class SavePublishStateFsyncOrderTests(unittest.TestCase):
    def _draft(self) -> Any:
        return publisher.DraftData(
            title="Т", trade_type="tt", condition="c", size="s", brand="",
            color="col", description="d", price=100,
            locations=(publisher.LocationData(city="Москва", address="ул. Тестовая, 1"),),
            view_price_max=Decimal("1"), photo_paths=(),
            category="jackets",
        )

    def test_fsync_called_before_replace(self) -> None:
        calls: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fake_fsync(fd: int) -> None:
            calls.append("fsync")
            # Не вызываем настоящий fsync намеренно избегать проблем с
            # закрытым/некорректным fd в моменте патча — важен только порядок.

        def fake_replace(src: Any, dst: Any) -> None:
            calls.append("replace")
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-fsync"
            with mock.patch.object(publish_state.os, "fsync", side_effect=fake_fsync), \
                 mock.patch.object(publish_state.os, "replace", side_effect=fake_replace):
                publish_state.save_publish_state(
                    job_dir, "job-fsync", {"status": "running"}, self._draft(),
                )

        self.assertIn("fsync", calls, "save_publish_state ни разу не вызвал os.fsync")
        self.assertIn("replace", calls, "save_publish_state ни разу не вызвал os.replace "
                                         "(вместо этого используется Path.replace?)")
        self.assertLess(
            calls.index("fsync"), calls.index("replace"),
            "os.fsync должен быть вызван ДО os.replace, иначе он бессмысленен",
        )
        del real_fsync  # только для симметрии сигнатуры, не используется

    def test_state_file_still_readable_after_real_save(self) -> None:
        """Сам факт добавления fsync не должен ломать обычную запись/чтение."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job-real"
            publish_state.save_publish_state(
                job_dir, "job-real", {"status": "running", "step": "open_form"},
                self._draft(),
            )
            job, draft = publish_state.load_publish_state(job_dir)
            self.assertEqual(job["status"], "running")
            self.assertEqual(draft.title, "Т")


# =============================================================================
# Раздел C. F06: /status и /resume должны видеть один план — с диска
# =============================================================================

def _draft_for_disk(n_items: int = 2) -> Any:
    locations = tuple(
        publisher.LocationData(city="Москва", address=f"ул. Диск, {k}")
        for k in range(1, n_items + 1)
    )
    return publisher.DraftData(
        title="Диск-тест", trade_type="tt", condition="c", size="s", brand="",
        color="col", description="d", price=100,
        locations=locations, view_price_max=Decimal("1"), photo_paths=(),
        category="jackets",
    )


class StatusResumeDiskAgreementTests(unittest.IsolatedAsyncioTestCase):
    """F06: единственный источник правды для плана — диск."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._tmp_publish_dir = Path(self._tmp.name)
        self._patchers = [
            mock.patch.object(app, "TMP_PUBLISH_DIR", self._tmp_publish_dir),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(self._tmp.cleanup)
        for p in self._patchers:
            self.addCleanup(p.stop)
        app.PUBLISH_JOBS.clear()
        app.ACTIVE_PUBLISH_TASKS.clear()
        self.addCleanup(app.PUBLISH_JOBS.clear)
        self.addCleanup(app.ACTIVE_PUBLISH_TASKS.clear)

    def _write_disk_state(self, job_id: str, job: dict[str, Any]) -> None:
        publish_state.save_publish_state(
            self._tmp_publish_dir / job_id, job_id, job, _draft_for_disk(),
        )

    async def test_status_reads_plan_from_disk_not_from_memory(self) -> None:
        """Память отстаёт (ещё думает, что объявление создано), диск —
        последняя УСПЕШНО записанная (безопасная) правда. /status обязан
        верить диску, а не памяти."""
        job_id = "job-disk-ahead"
        # Диск: безопасный шаг, объявление №1 ещё не создано -> retry_item.
        self._write_disk_state(job_id, {
            "status": "failed", "step": "fill_address",
            "items_total": 2, "item_index": 1, "items_published": 0,
        })
        # Память: устаревшая на вид «уже создано» (что и было исходным F06).
        memory_job = {
            "status": "failed", "step": "continue_listing", "money_click": "clicked",
            "items_total": 2, "item_index": 1, "items_published": 0,
        }
        app.PUBLISH_JOBS[job_id] = memory_job

        payload = app._serialize_publish_status(job_id, memory_job)

        self.assertIsNotNone(
            payload["resume_plan"],
            "план должен браться с диска, а не быть None из-за путаницы с памятью",
        )
        self.assertEqual(
            payload["resume_plan"]["mode"], "retry_item",
            "статус отражает ПАМЯТЬ (skip_item), а должен отражать ДИСК (retry_item)",
        )
        self.assertIsNone(payload.get("resume_unavailable_reason"))

    async def test_status_unreadable_disk_gives_resume_unavailable_with_reason(
        self,
    ) -> None:
        job_id = "job-no-disk-file"
        memory_job = {
            "status": "failed", "step": "fill_address",
            "items_total": 2, "item_index": 1, "items_published": 0,
        }
        app.PUBLISH_JOBS[job_id] = memory_job
        # Намеренно НЕ пишем ничего на диск для этого job_id.

        payload = app._serialize_publish_status(job_id, memory_job)

        self.assertIs(payload["resume_available"], False)
        self.assertIsNone(payload["resume_plan"])
        reason = payload.get("resume_unavailable_reason")
        self.assertIsInstance(reason, str)
        self.assertTrue(reason.strip(), "resume_unavailable_reason должен быть непустой строкой")

    async def test_status_and_resume_agree_on_the_same_disk_plan(self) -> None:
        """Требование п.7: /status и /resume должны дать ОДИН план, когда
        память и диск разошлись (запись не удалась)."""
        job_id = str(uuid.uuid4())
        self._write_disk_state(job_id, {
            "status": "failed", "step": "fill_address",
            "items_total": 2, "item_index": 1, "items_published": 0,
        })
        # Память нарочно другая (расхождение памяти/диска).
        memory_job = {
            "status": "failed", "step": "continue_listing", "money_click": "clicked",
            "items_total": 2, "item_index": 1, "items_published": 0,
        }
        app.PUBLISH_JOBS[job_id] = memory_job

        status_payload = app._serialize_publish_status(job_id, memory_job)

        with mock.patch.object(app, "_schedule_publish", lambda *a, **k: None):
            response = await app.api_publish_resume(job_id)
        self.assertEqual(response.status_code, 200, response.body)
        import json
        resume_payload = json.loads(response.body)

        self.assertEqual(
            status_payload["resume_plan"]["mode"], resume_payload["mode"],
            "/status и /resume выбрали РАЗНЫЕ планы для одного и того же checkpoint",
        )


# =============================================================================
# Инфраструктура для разделов D/E/F: реальный _step_continue_listing /
# _step_check_category / _guard_against_reopened_draft на фейковой странице,
# всё остальное в _run_single_item / run_publish_job замокано в no-op.
# =============================================================================

class _GuardPassed(Exception):
    """Сигнал «дошли до шага после guard» — используется, чтобы отличить
    «guard заблокировал» от «guard пропустил» без дожатия всего объявления."""


class _Loc:
    def __init__(self, page: "MoneyClickPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_Loc":
        return self

    async def count(self) -> int:
        return 1

    async def is_visible(self, timeout: Optional[int] = None) -> bool:
        return self.page._visible(self.selector)

    async def is_enabled(self, timeout: Optional[int] = None) -> bool:
        return self.page._enabled(self.selector)

    async def input_value(self, timeout: Optional[int] = None) -> str:
        return self.page._value(self.selector)

    async def inner_text(self, timeout: Optional[int] = None) -> str:
        return self.page._text(self.selector)

    async def get_attribute(self, name: str, timeout: Optional[int] = None) -> Optional[str]:
        return None

    async def click(self, timeout: Optional[int] = None, no_wait_after: Optional[bool] = None) -> None:
        await self.page._click(self.selector)


class _AliasPage:
    """Отдельный (не `is page`) объект с тем же url/is_closed — заставляет
    `_resolve_cpxpromo_page` найти «новую вкладку» мгновенно, без реального
    2-секундного опроса context.pages."""

    def __init__(self, target: "MoneyClickPage") -> None:
        self._target = target

    @property
    def url(self) -> str:
        return self._target.url

    def is_closed(self) -> bool:
        return self._target.closed


class MoneyClickPage:
    """Минимальная страница, реализующая ровно DOM-контракт
    _step_continue_listing (SAVE_AND_EXIT_BUTTON/TITLE_INPUT/FORM_CONTINUE_BUTTON)."""

    def __init__(self, item_id: str = "900001") -> None:
        self.url = "https://www.avito.ru/additem"
        self.item_id = item_id
        self.closed = False
        self.click_count = 0
        self.click_error: Optional[BaseException] = None
        self.reach_cpxpromo = True

        self.save_and_exit_visible = True
        self.title_visible = True
        self.title_value = "Заголовок объявления"
        self.button_visible = True
        self.button_text = "Продолжить"
        self.button_enabled = True

        self.context = SimpleNamespace(pages=[_AliasPage(self)])

    def reset_for_new_item(self, title: str) -> None:
        """Имитирует то, что реальный open_form делает между объявлениями:
        свежая форма /additem, пустой заголовок, кнопка снова доступна."""
        self.url = "https://www.avito.ru/additem"
        self.save_and_exit_visible = True
        self.title_visible = True
        self.title_value = title
        self.button_visible = True
        self.button_text = "Продолжить"
        self.button_enabled = True
        self.click_error = None
        self.reach_cpxpromo = True

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, **_kwargs: Any) -> None:
        return None

    async def content(self) -> str:
        return "<html></html>"

    def locator(self, selector: str) -> _Loc:
        return _Loc(self, selector)

    def _visible(self, selector: str) -> bool:
        if selector == psel.SAVE_AND_EXIT_BUTTON:
            return self.save_and_exit_visible
        if selector == psel.TITLE_INPUT:
            return self.title_visible
        if selector == psel.FORM_CONTINUE_BUTTON:
            return self.button_visible
        return False

    def _enabled(self, selector: str) -> bool:
        if selector == psel.FORM_CONTINUE_BUTTON:
            return self.button_enabled
        return True

    def _value(self, selector: str) -> str:
        if selector == psel.TITLE_INPUT:
            return self.title_value
        return ""

    def _text(self, selector: str) -> str:
        if selector == psel.FORM_CONTINUE_BUTTON:
            return self.button_text
        return ""

    async def _click(self, selector: str) -> None:
        if selector != psel.FORM_CONTINUE_BUTTON:
            return
        self.click_count += 1
        if self.click_error is not None:
            raise self.click_error
        if self.reach_cpxpromo:
            self.url = f"https://www.avito.ru/cpxpromo/{self.item_id}"


def _make_draft(n_items: int = 1, view_price_max: str = "2") -> Any:
    locations = tuple(
        publisher.LocationData(city="Москва", address=f"ул. Денежная, {k}")
        for k in range(1, n_items + 1)
    )
    return publisher.DraftData(
        title="Денежный тест", trade_type="tt", condition="c", size="s", brand="",
        color="col", description="описание", price=1000,
        locations=locations, view_price_max=Decimal(view_price_max), photo_paths=(),
        category="jackets",
    )


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


async def _noop_form_state(*_a: Any, **_k: Any) -> str:
    return "form"


async def _noop_fill_fields(*_a: Any, **_k: Any) -> str:
    return "Без бренда"


async def _noop_fill_view_price(*_a: Any, **_k: Any) -> Decimal:
    return Decimal("0.4")


async def _noop_fill_address(*_a: Any, **_k: Any) -> Optional[dict[str, str]]:
    return None


class _PreflightPage:
    """Одноразовая страница предполётной проверки (_run_publish_preflight).

    Не должна делить состояние (closed!) с рабочей страницей объявлений —
    настоящий код открывает под неё СВОЮ вкладку и закрывает её же."""

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self, page_factory: Callable[[], Any]) -> None:
        self._factory = page_factory
        self.pages: list[Any] = []

    async def new_page(self) -> Any:
        if not self.pages:
            # Первый new_page() в run_publish_job — предполётная проверка
            # (_run_publish_preflight), у неё СВОЯ вкладка, закрывается сама
            # ДО начала цикла по объявлениям.
            preflight = _PreflightPage()
            self.pages.append(preflight)
            return preflight
        page = self._factory()
        self.pages.append(page)
        return page


class _PlaywrightManager:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


@contextlib.contextmanager
def _run_job_context(
    page_factory: Callable[[], Any],
    *,
    keep_real_continue_listing: bool = True,
    keep_real_check_category: bool = False,
):
    """Готовит run_publish_job к прогону на фейковой странице: CDP/Playwright
    подменены, все _step_* — no-op, КРОМЕ явно оставленных реальными."""
    context = _FakeContext(page_factory)

    async def fake_connect(*_a: Any, **_k: Any) -> _FakeContext:
        return context

    fakes: dict[str, Any] = {
        "_step_open_form": _noop_form_state,
        "_step_select_category": _noop,
        "_clear_and_type": _noop,
        "_step_upload_photos": _noop,
        "_step_fill_fields": _noop_fill_fields,
        "_step_fill_description": _noop,
        "_step_fill_price": _noop,
        "_step_fill_address": _noop_fill_address,
        "_step_fill_view_price": _noop_fill_view_price,
        "_step_continue_view_price": _noop,
        "_step_skip_services": _noop,
    }
    if not keep_real_check_category:
        fakes["_step_check_category"] = _noop
    if not keep_real_continue_listing:
        async def _fake_continue_listing(page: Any) -> tuple[str, Any]:
            return "900001", page
        fakes["_step_continue_listing"] = _fake_continue_listing

    with contextlib.ExitStack() as s:
        s.enter_context(mock.patch("browser.connect_over_cdp", fake_connect))
        s.enter_context(
            mock.patch("playwright.async_api.async_playwright", lambda: _PlaywrightManager())
        )
        s.enter_context(mock.patch.object(publisher, "_pause", _noop))
        s.enter_context(mock.patch.object(publisher, "DRAFT_PAUSE_MIN_S", 0.0))
        s.enter_context(mock.patch.object(publisher, "DRAFT_PAUSE_MAX_S", 0.0))
        s.enter_context(mock.patch.object(publisher.journal, "record_listing", lambda *a, **k: None))
        s.enter_context(mock.patch.object(publisher.journal, "log_event", lambda *a, **k: None))
        for name, fake in fakes.items():
            s.enter_context(mock.patch.object(publisher, name, fake))
        yield context


def _job_dict(items_total: int = 1) -> dict[str, Any]:
    return {
        "status": "queued",
        "step": "",
        "step_label": "",
        "done": 0,
        "total": publisher.TOTAL_STEPS,
        "error": None,
        "debug_dir": None,
        "result_url": None,
        "item_index": 0,
        "items_total": items_total,
        "items_published": 0,
        "published_urls": [],
        "brand_selected": None,
        "applied_view_prices": [],
        "address_warnings": [],
        "user_action": None,
        "prep_id": None,
    }


class _RunJobMixin:
    """Общая обвязка: временный tmp_dir + прогон run_publish_job до конца."""

    def setUp(self) -> None:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._debug_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._debug_tmp.cleanup)
        self._debug_patch = mock.patch.object(
            publisher, "DEBUG_PUBLISH_DIR", Path(self._debug_tmp.name)
        )
        self._debug_patch.start()
        self.addCleanup(self._debug_patch.stop)

    @property
    def tmp_dir(self) -> Path:
        return Path(self._tmp.name)

    async def _run(
        self,
        job: dict[str, Any],
        draft: Any,
        page_factory: Callable[[], Any],
        *,
        keep_real_continue_listing: bool = True,
        keep_real_check_category: bool = False,
        start_index: Optional[int] = None,
    ) -> None:
        with _run_job_context(
            page_factory,
            keep_real_continue_listing=keep_real_continue_listing,
            keep_real_check_category=keep_real_check_category,
        ):
            await publisher.run_publish_job(
                "job-money", job, draft,
                cdp_url="http://127.0.0.1:9222",
                tmp_dir=str(self.tmp_dir),
                start_index=start_index,
            )

    def _load_disk(self) -> dict[str, Any]:
        job, _draft = publish_state.load_publish_state(self.tmp_dir)
        return job


# =============================================================================
# Раздел D. F03/F07/S1: денежная отметка вокруг continue_listing
# =============================================================================

class ContinueListingMoneyMarkerTests(_RunJobMixin, unittest.IsolatedAsyncioTestCase):
    async def test_failure_before_click_marks_not_clicked_and_resume_retries(self) -> None:
        """П.1: неизвестная подпись кнопки — click() НЕ вызывается вовсе."""
        page = MoneyClickPage()
        page.button_text = "Продолжить и оплатить"  # неизвестная подпись — стоп
        job = _job_dict(items_total=1)
        draft = _make_draft(1)

        await self._run(job, draft, lambda: page)

        self.assertEqual(page.click_count, 0, "click() не должен был вызываться")
        self.assertEqual(job["status"], "failed")
        disk_job = self._load_disk()
        self.assertEqual(
            disk_job.get("money_click"), "not_clicked",
            "сбой ДО click() должен оставить на диске not_clicked",
        )
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["mode"], "retry_item")
        self.assertEqual(plan["start_index"], 1)

    async def test_save_and_exit_not_visible_before_click_marks_not_clicked(self) -> None:
        """П.1, другой пред-клик сбой: форма не подтверждена заполненной."""
        page = MoneyClickPage()
        page.save_and_exit_visible = False
        job = _job_dict(items_total=1)
        draft = _make_draft(1)

        await self._run(job, draft, lambda: page)

        self.assertEqual(page.click_count, 0)
        disk_job = self._load_disk()
        self.assertEqual(disk_job.get("money_click"), "not_clicked")
        self.assertEqual(publish_state.resume_plan(disk_job)["mode"], "retry_item")

    async def test_disabled_button_before_click_marks_not_clicked(self) -> None:
        page = MoneyClickPage()
        page.button_enabled = False
        job = _job_dict(items_total=1)
        draft = _make_draft(1)

        await self._run(job, draft, lambda: page)

        self.assertEqual(page.click_count, 0)
        disk_job = self._load_disk()
        self.assertEqual(disk_job.get("money_click"), "not_clicked")

    async def test_failure_after_click_marks_clicked_and_resume_skips(self) -> None:
        """П.2: click() прошёл успешно, но экран /cpxpromo не подтверждён —
        объявление могло быть создано, повторный клик запрещён.

        Пакет из 2 (НЕ 1): пропуск последнего объявления пакета контрактом
        запрещён (см. tests/test_publish_checkpoint.py::
        test_none_when_stopped_on_last_item_after_financial_step) — при
        items_total=1 skip_item со start_index=2 зажался бы до плана None
        («пропускать нечего»), и тест перестал бы что-либо проверять."""
        page = MoneyClickPage()
        page.reach_cpxpromo = False  # click() успешен, но URL не меняется
        job = _job_dict(items_total=2)
        draft = _make_draft(2)

        with mock.patch.object(publisher, "PUBLISH_TRANSITION_TIMEOUT_S", 0.01):
            await self._run(job, draft, lambda: page)

        self.assertEqual(page.click_count, 1, "click() должен был реально произойти")
        self.assertEqual(job["status"], "failed")
        disk_job = self._load_disk()
        self.assertEqual(
            disk_job.get("money_click"), "clicked",
            "click() прошёл успешно — отметка должна стать clicked, даже если "
            "последующее подтверждение /cpxpromo не удалось",
        )
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["mode"], "skip_item")
        self.assertEqual(plan["skipped_item"], 1)
        self.assertEqual(plan["start_index"], 2)

    async def test_click_itself_raising_does_not_become_not_clicked(self) -> None:
        """П.3: click() САМ бросил исключение — клик мог уйти на сервер Авито,
        поэтому это НЕ not_clicked (иначе резюм повторит уже созданное).

        Пакет из 2 по той же причине, что и в предыдущем тесте: при
        items_total=1 план для «clicked-или-неизвестно» — None (пропускать
        последнее нельзя), и утверждение `if plan is not None` молчаливо
        ничего не проверяло бы."""
        page = MoneyClickPage()
        page.click_error = TimeoutError("Playwright: click timeout")
        job = _job_dict(items_total=2)
        draft = _make_draft(2)

        await self._run(job, draft, lambda: page)

        self.assertEqual(page.click_count, 1, "click() был вызван (и бросил исключение)")
        disk_job = self._load_disk()
        self.assertNotEqual(
            disk_job.get("money_click"), "not_clicked",
            "click() бросил исключение — это НЕ доказательство отсутствия клика, "
            "not_clicked здесь означал бы риск дубля",
        )
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(
            plan, "неопределённый исход клика не должен давать «пропускать нечего»"
        )
        self.assertEqual(
            plan["mode"], "skip_item",
            "неопределённый исход клика должен приводить к skip, а не retry",
        )

    async def test_one_time_checkpoint_write_failure_before_click_prevents_click_and_retries(
        self,
    ) -> None:
        """П.4: разовый сбой ЗАПИСИ (не самого клика) перед continue_listing —
        click() не вызывается, а после снятия сбоя resume повторяет объявление."""
        page = MoneyClickPage()
        job = _job_dict(items_total=1)
        draft = _make_draft(1)

        real_save = publish_state.save_publish_state
        state = {"failed_once": False}

        def flaky_save(job_dir: Path, job_id: str, job_arg: dict[str, Any], draft_arg: Any) -> Path:
            if (
                not state["failed_once"]
                and job_arg.get("step") == "continue_listing"
                and job_arg.get("money_click") != "clicked"
            ):
                state["failed_once"] = True
                raise OSError(28, "стенд: диск временно недоступен")
            return real_save(job_dir, job_id, job_arg, draft_arg)

        with mock.patch.object(publish_state, "save_publish_state", flaky_save):
            await self._run(job, draft, lambda: page)

        self.assertEqual(page.click_count, 0, "запись перед кликом не удалась — click() не вызван")
        disk_job = self._load_disk()
        self.assertEqual(disk_job.get("money_click"), "not_clicked")
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(plan)
        self.assertEqual(
            plan["mode"], "retry_item",
            "после разового сбоя записи resume должен ПОВТОРИТЬ объявление",
        )

    async def test_one_time_write_failure_of_clicked_marker_stops_without_retry(self) -> None:
        """П.5: click() прошёл, но однократная запись отметки «clicked» не
        удалась — итог должен быть «стоп без повтора» (skip/нет плана), но
        НИКОГДА retry_item (объявление уже могло быть создано).

        Пакет из 2: при items_total=1 план для money_click=="clicked" —
        None («пропускать последнее нельзя»), а `if plan is not None`
        тогда ничего не проверял бы (см. два теста выше)."""
        page = MoneyClickPage()
        job = _job_dict(items_total=2)
        draft = _make_draft(2)

        real_save = publish_state.save_publish_state
        state = {"failed_once": False}

        def flaky_save(job_dir: Path, job_id: str, job_arg: dict[str, Any], draft_arg: Any) -> Path:
            if not state["failed_once"] and job_arg.get("money_click") == "clicked":
                state["failed_once"] = True
                raise OSError(28, "стенд: диск временно недоступен после клика")
            return real_save(job_dir, job_id, job_arg, draft_arg)

        with mock.patch.object(publish_state, "save_publish_state", flaky_save):
            await self._run(job, draft, lambda: page)

        self.assertEqual(page.click_count, 1)
        self.assertNotEqual(job.get("status"), "done", "разовый сбой не должен смазаться до полного успеха")
        disk_job = self._load_disk()
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(
            plan, "запись отметки после клика не удалась — план не должен стать «пропускать нечего»"
        )
        self.assertEqual(
            plan["mode"], "skip_item",
            "запись отметки после клика не удалась — resume НЕ должен повторять "
            "потенциально уже созданное объявление",
        )
        self.assertEqual(plan["skipped_item"], 1)

    async def test_money_click_resets_between_items(self) -> None:
        """money_click сбрасывается в None для КАЖДОГО нового объявления:
        №1 успешно создаётся (money_click=clicked), №2 падает ДО клика
        (неизвестная подпись) — отметка №1 не должна просочиться в план №2.
        Пакет из 3 (не 2), чтобы №2 не был последним — иначе «пропускать
        нечего» (F09-ветка resume_plan) даёт None независимо от money_click
        и тест перестаёт что-либо различать."""
        page = MoneyClickPage()
        job = _job_dict(items_total=3)
        draft = _make_draft(3)

        # Считаем по job["item_index"] (а не по номеру вызова _step_open_form):
        # предполётная проверка (_run_publish_preflight) тоже дёргает
        # _step_open_form ОДИН раз ДО начала цикла по объявлениям — счётчик
        # вызовов сбился бы на единицу и «сбой №2» применился бы к №1.
        async def fake_open_form(p: Any, _profile: Any) -> str:
            if job.get("item_index") == 2:
                # Второе объявление: свежая форма (title ПУСТОЕ — иначе guard
                # от переоткрытой формы решит, что это дубль №1, и упадёт
                # раньше continue_listing), но с неизвестной подписью кнопки —
                # сбой ДО click().
                page.reset_for_new_item("")
                page.button_text = "Продолжить и оплатить"
            return "form"

        with _run_job_context(lambda: page, keep_real_continue_listing=True) as _ctx:
            with mock.patch.object(publisher, "_step_open_form", fake_open_form):
                await publisher.run_publish_job(
                    "job-money", job, draft,
                    cdp_url="http://127.0.0.1:9222", tmp_dir=str(self.tmp_dir),
                )

        self.assertEqual(job.get("items_published"), 1, "№1 должен был успешно уйти")
        disk_job = self._load_disk()
        self.assertEqual(disk_job.get("item_index"), 2)
        self.assertNotEqual(
            disk_job.get("money_click"), "clicked",
            "money_click от объявления №1 просочился в состояние объявления №2",
        )
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(plan)
        self.assertEqual(
            plan["mode"], "retry_item",
            "№2 не должен наследовать «clicked» от №1 — он ещё не дошёл до клика",
        )


# =============================================================================
# Раздел E. F36: промежуточная кнопка категории (button-next)
# =============================================================================

class _CategoryPageLoc:
    def __init__(self, page: "CategoryConfirmPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_CategoryPageLoc":
        return self

    async def count(self) -> int:
        return 1

    async def input_value(self, timeout: Optional[int] = None) -> str:
        values = self.page.hidden_values
        for name, value in values.items():
            if f"name='{name}'" in self.selector:
                return value
        if self.selector == psel.ADDRESS_HIDDEN:
            return self.page.address_value
        return ""

    async def is_visible(self, timeout: Optional[int] = None) -> bool:
        if self.selector == psel.TITLE_INPUT:
            return self.page.title_visible
        if self.selector == psel.CATEGORY_CONFIRM_CONTINUE_BUTTON:
            return self.page.button_visible
        return False

    async def is_enabled(self, timeout: Optional[int] = None) -> bool:
        return self.page.button_enabled

    async def inner_text(self, timeout: Optional[int] = None) -> str:
        if self.selector == psel.CATEGORY_CONFIRM_CONTINUE_BUTTON:
            return self.page.button_text
        return ""

    async def click(self, timeout: Optional[int] = None) -> None:
        await self.page._click()


class CategoryConfirmPage:
    """Аналог _CategoryConfirmationPage из test_category_confirmation.py,
    расширенный url/адресом для F36: строгие проверки требуют path==/additem
    и пустое поле адреса."""

    def __init__(self, *, title_visible: bool = False) -> None:
        self.url = "https://www.avito.ru/additem"
        self.title_visible = title_visible
        self.address_value = ""
        self.button_visible = True
        self.button_enabled = True
        self.button_text = "Продолжить\ntiming"
        self.continue_clicks = 0
        self.hidden_values = {
            "category_id": "27", "params[175]": "748", "params[176]": "756",
        }
        self.click_leads_to_cpxpromo = False
        self.item_id = "777001"
        self.click_error: Optional[BaseException] = None
        self.closed = False
        # Своей context.pages эта страница не использует (нужна только
        # _step_continue_listing/_resolve_cpxpromo_page, здесь их нет).
        self.context = SimpleNamespace(pages=[])

    def locator(self, selector: str) -> _CategoryPageLoc:
        return _CategoryPageLoc(self, selector)

    async def wait_for_selector(self, selector: str, *, timeout: int, state: str) -> _CategoryPageLoc:
        del timeout, state
        loc = self.locator(selector)
        if not await loc.is_visible():
            raise TimeoutError(selector)
        return loc

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def screenshot(self, **_kwargs: Any) -> None:
        return None

    async def content(self) -> str:
        return "<html></html>"

    async def _click(self) -> None:
        self.continue_clicks += 1
        if self.click_error is not None:
            raise self.click_error
        if self.click_leads_to_cpxpromo:
            self.url = f"https://www.avito.ru/cpxpromo/{self.item_id}"
            # title тоже "видим" — изолируем причину исключения: если бы
            # старая логика (только проверка видимости title) сочла это
            # успехом, тест ошибочно проходил бы НЕ из-за проверки URL.
            self.title_visible = True
        else:
            self.title_visible = True


class CategoryConfirmationF36Tests(unittest.IsolatedAsyncioTestCase):
    """F36: промежуточная кнопка должна проходить те же строгие проверки,
    что и финальная (путь /additem, точная подпись, пустые title/адрес)."""

    async def test_nonempty_title_blocks_click(self) -> None:
        """Черновик уже содержит title (видимый) — по текущей логике это
        обрабатывается веткой «форма уже открыта», а не денежным кликом;
        здесь фиксируем требование строгих проверок при СКРЫТОМ, но
        НЕПУСТОМ адресе — денежная зона."""
        page = CategoryConfirmPage(title_visible=False)
        page.address_value = "Москва, ул. Уже заполнена, 5"

        with self.assertRaises(publisher.UserActionRequired):
            await publisher._continue_category_confirmation(page, category_profiles.TSHIRTS)
        self.assertEqual(page.continue_clicks, 0, "click не должен был произойти при непустом адресе")

    async def test_wrong_path_blocks_click(self) -> None:
        page = CategoryConfirmPage(title_visible=False)
        page.url = "https://www.avito.ru/profile/pro/items"

        with self.assertRaises((publisher.UserActionRequired, publisher.StepError)):
            await publisher._continue_category_confirmation(page, category_profiles.TSHIRTS)
        self.assertEqual(page.continue_clicks, 0)

    async def test_unknown_button_label_blocks_click(self) -> None:
        page = CategoryConfirmPage(title_visible=False)
        page.button_text = "Продолжить и оплатить"

        with self.assertRaises(publisher.UserActionRequired):
            await publisher._continue_category_confirmation(page, category_profiles.TSHIRTS)
        self.assertEqual(page.continue_clicks, 0)

    async def test_disabled_button_blocks_click(self) -> None:
        page = CategoryConfirmPage(title_visible=False)
        page.button_enabled = False

        with self.assertRaises((publisher.UserActionRequired, publisher.StepError)):
            await publisher._continue_category_confirmation(page, category_profiles.TSHIRTS)
        self.assertEqual(page.continue_clicks, 0)

    async def test_click_leading_to_cpxpromo_stops_the_scenario(self) -> None:
        """П.10: если промежуточный клик всё же увёл на /cpxpromo — это
        денежная зона задним числом, сценарий обязан остановиться исключением
        (а не продолжать заполнение формы на чужой странице)."""
        page = CategoryConfirmPage(title_visible=False)
        page.click_leads_to_cpxpromo = True

        with self.assertRaises(Exception):
            await publisher._continue_category_confirmation(page, category_profiles.TSHIRTS)

        self.assertEqual(page.continue_clicks, 1, "клик должен был произойти ровно один раз")
        self.assertTrue(
            publisher.parse_cpxpromo_item_id(page.url),
            "страница действительно должна была перейти на /cpxpromo",
        )


class CategoryConfirmationEndToEndTests(_RunJobMixin, unittest.IsolatedAsyncioTestCase):
    """F36 п.10, сквозной прогон: настоящий `_continue_category_confirmation`
    вызывается внутри настоящего `run_publish_job` (через фейковый
    `_step_check_category`, который просто делегирует в неё) — денежная
    отметка и checkpoint проверяются не изолированно, а как их видит resume."""

    async def test_intermediate_click_to_cpxpromo_marks_clicked_and_skips_item(
        self,
    ) -> None:
        page = CategoryConfirmPage(title_visible=False)
        page.click_leads_to_cpxpromo = True
        job = _job_dict(items_total=2)
        draft = _make_draft(2)

        async def fake_check_category(p: Any, profile: Any) -> None:
            if p is page:
                await publisher._continue_category_confirmation(p, profile)
            # Иначе это предполётная вкладка — там проверять нечего.

        with _run_job_context(lambda: page, keep_real_continue_listing=False):
            with mock.patch.object(publisher, "_step_check_category", fake_check_category):
                await publisher.run_publish_job(
                    "job-money", job, draft,
                    cdp_url="http://127.0.0.1:9222", tmp_dir=str(self.tmp_dir),
                )

        self.assertEqual(page.continue_clicks, 1, "клик должен был произойти ровно один раз")
        self.assertEqual(job["status"], "needs_user_action")

        disk_job = self._load_disk()
        self.assertEqual(disk_job.get("step"), "check_category")
        self.assertEqual(
            disk_job.get("money_click"), "clicked",
            "промежуточный клик увёл на /cpxpromo — это денежный клик задним числом",
        )
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["mode"], "skip_item")
        self.assertEqual(plan["skipped_item"], 1)
        self.assertEqual(plan["start_index"], 2)

    async def test_intermediate_click_exception_on_additem_does_not_mark_and_retries(
        self,
    ) -> None:
        """Клик по промежуточной кнопке бросил исключение, но путь остался
        /additem — это НЕ денежный случай (F36), отметки быть не должно,
        план — обычный retry_item (check_category и так безопасный шаг)."""
        page = CategoryConfirmPage(title_visible=False)
        page.click_error = TimeoutError("Playwright: click timeout")
        job = _job_dict(items_total=2)
        draft = _make_draft(2)

        async def fake_check_category(p: Any, profile: Any) -> None:
            if p is page:
                await publisher._continue_category_confirmation(p, profile)

        with _run_job_context(lambda: page, keep_real_continue_listing=False):
            with mock.patch.object(publisher, "_step_check_category", fake_check_category):
                await publisher.run_publish_job(
                    "job-money", job, draft,
                    cdp_url="http://127.0.0.1:9222", tmp_dir=str(self.tmp_dir),
                )

        self.assertEqual(page.continue_clicks, 1)
        self.assertEqual(job["status"], "needs_user_action")
        self.assertEqual(publisher.parse_cpxpromo_item_id(page.url), None,
                          "путь должен был остаться /additem, а не уйти на /cpxpromo")

        disk_job = self._load_disk()
        self.assertNotEqual(disk_job.get("money_click"), "clicked")
        plan = publish_state.resume_plan(disk_job)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["mode"], "retry_item")
        self.assertEqual(plan["start_index"], 1)


# =============================================================================
# Раздел F. F27: защита от переоткрытой формы (№1 + retry_item)
# =============================================================================

class GuardAgainstReopenedDraftSignatureTests(unittest.IsolatedAsyncioTestCase):
    """Прямые тесты новой сигнатуры _guard_against_reopened_draft(...,
    own_draft_allowed=...) — интерфейс дан в брифе дословно."""

    class _TitlePage:
        def __init__(self, current_title: str) -> None:
            self._title = current_title

        def locator(self, selector: str) -> "_TitleLoc":
            return _TitleLoc(self, selector)

    async def test_draft_number_one_now_protected_against_reopened_form(self) -> None:
        """До фикса №1 никогда не проверялся (draft_index<=1 -> return).
        По брифу защита должна работать и для №1."""
        page = self._TitlePage("Наше название")
        with self.assertRaises(publisher.UserActionRequired):
            await publisher._guard_against_reopened_draft(
                page, "Наше название", 1, 3,
            )

    async def test_own_draft_allowed_suppresses_guard_for_resumed_item(self) -> None:
        page = self._TitlePage("Наше название")
        # Не должно бросить исключение — свой недосозданный черновик.
        await publisher._guard_against_reopened_draft(
            page, "Наше название", 2, 3, own_draft_allowed=True,
        )

    async def test_own_draft_allowed_false_by_default_still_blocks_duplicate(self) -> None:
        page = self._TitlePage("Наше название")
        with self.assertRaises(publisher.UserActionRequired):
            await publisher._guard_against_reopened_draft(
                page, "Наше название", 2, 3, own_draft_allowed=False,
            )

    async def test_own_draft_allowed_true_but_different_title_is_still_a_clean_form(
        self,
    ) -> None:
        """own_draft_allowed не должен глушить проверку, если название и правда
        другое (обычный чужой/старый черновик) — тут и без own_draft_allowed
        исключения не было бы, поведение не должно измениться."""
        page = self._TitlePage("Совсем другой чужой черновик")
        await publisher._guard_against_reopened_draft(
            page, "Наше название", 2, 3, own_draft_allowed=True,
        )


class _TitleLoc:
    def __init__(self, page: Any, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> "_TitleLoc":
        return self

    async def input_value(self, timeout: Optional[int] = None) -> str:
        return self.page._title


class RunSingleItemOwnDraftWiringTests(_RunJobMixin, unittest.IsolatedAsyncioTestCase):
    """F27 п.11: run_publish_job должен передавать own_draft_allowed =
    (job.get('resume_retry_item') == item_index) в guard — проверяем через
    реальный _run_single_item/run_publish_job, не гадая про сигнатуру guard
    изнутри."""

    async def test_resume_retry_item_matching_current_item_allows_own_draft(self) -> None:
        # Намеренно НЕ №1: draft_index<=1 у guard'а сегодня и без фикса
        # всегда «прозрачен» (return без проверки) — тест на №1 прошёл бы
        # «случайно», не проверяя own_draft_allowed вовсе. Берём №2 из 2,
        # возобновление сразу со второго (start_index=2, resume_retry_item=2).
        job = _job_dict(items_total=2)
        job["resume_retry_item"] = 2
        page = MoneyClickPage()
        page.title_value = "Денежный тест"
        draft = _make_draft(2)

        async def boom(*_a: Any, **_k: Any) -> None:
            raise _GuardPassed()

        with _run_job_context(lambda: page, keep_real_continue_listing=False):
            with mock.patch.object(publisher, "_step_upload_photos", boom):
                await publisher.run_publish_job(
                    "job-guard-allow", job, draft,
                    cdp_url="http://127.0.0.1:9222", tmp_dir=str(self.tmp_dir),
                    start_index=2,
                )

        # Guard должен был ПРОПУСТИТЬ (дошли до upload_photos -> failed с
        # текстом от _GuardPassed, а не needs_user_action от guard).
        self.assertEqual(job["status"], "failed")
        self.assertNotIn(
            "переоткрыл", (job.get("error") or ""),
            "guard заблокировал совпадение названия, хотя это retry_item того же номера",
        )

    async def test_fresh_start_without_resume_retry_item_still_blocks_duplicate(self) -> None:
        job = _job_dict(items_total=1)  # без resume_retry_item — свежий старт
        page = MoneyClickPage()
        page.title_value = "Денежный тест"
        draft = _make_draft(1)

        async def boom(*_a: Any, **_k: Any) -> None:
            raise _GuardPassed()

        with _run_job_context(lambda: page, keep_real_continue_listing=False):
            with mock.patch.object(publisher, "_step_upload_photos", boom):
                await publisher.run_publish_job(
                    "job-guard-block", job, draft,
                    cdp_url="http://127.0.0.1:9222", tmp_dir=str(self.tmp_dir),
                )

        self.assertEqual(job["status"], "needs_user_action")
        self.assertIn("переоткрыл", (job.get("error") or "").lower())


class ApiPublishResumeMarksRetryItemTests(unittest.IsolatedAsyncioTestCase):
    """F27 п.11: /api/publish/resume должен проставлять job["resume_retry_item"]
    ровно тогда, когда сохранённый item_index == start_index плана retry_item
    (объявление было В РАБОТЕ), и не проставлять его иначе."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._tmp_publish_dir = Path(self._tmp.name)
        patcher = mock.patch.object(app, "TMP_PUBLISH_DIR", self._tmp_publish_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.PUBLISH_JOBS.clear()
        app.ACTIVE_PUBLISH_TASKS.clear()
        self.addCleanup(app.PUBLISH_JOBS.clear)
        self.addCleanup(app.ACTIVE_PUBLISH_TASKS.clear)

    def _write(self, job_id: str, job: dict[str, Any]) -> None:
        publish_state.save_publish_state(
            self._tmp_publish_dir / job_id, job_id, job, _draft_for_disk(2),
        )

    async def test_retry_item_in_progress_sets_resume_retry_item(self) -> None:
        job_id = str(uuid.uuid4())
        # item_index == pending (2-е объявление В РАБОТЕ, безопасный шаг).
        self._write(job_id, {
            "status": "failed", "step": "fill_address",
            "items_total": 2, "item_index": 1, "items_published": 0,
        })

        with mock.patch.object(app, "_schedule_publish", lambda *a, **k: None):
            response = await app.api_publish_resume(job_id)
        self.assertEqual(response.status_code, 200, response.body)

        self.assertEqual(
            app.PUBLISH_JOBS[job_id].get("resume_retry_item"), 1,
            "retry_item того же номера должен пометить объявление как «своё»",
        )

    async def test_retry_item_fresh_next_does_not_set_resume_retry_item(self) -> None:
        job_id = str(uuid.uuid4())
        # №1 полностью отправлен, item_index отстаёт (fresh start №2).
        self._write(job_id, {
            "status": "failed", "step": "open_next_form",
            "items_total": 2, "item_index": 1, "items_published": 1,
        })

        with mock.patch.object(app, "_schedule_publish", lambda *a, **k: None):
            response = await app.api_publish_resume(job_id)
        self.assertEqual(response.status_code, 200, response.body)

        self.assertIsNone(
            app.PUBLISH_JOBS[job_id].get("resume_retry_item"),
            "свежий старт следующего объявления — не «свой недосозданный черновик»",
        )

    async def test_skip_item_does_not_set_resume_retry_item(self) -> None:
        job_id = str(uuid.uuid4())
        self._write(job_id, {
            "status": "failed", "step": "continue_listing", "money_click": "clicked",
            "items_total": 3, "item_index": 2, "items_published": 1,
        })

        with mock.patch.object(app, "_schedule_publish", lambda *a, **k: None):
            response = await app.api_publish_resume(job_id)
        self.assertEqual(response.status_code, 200, response.body)

        self.assertIsNone(app.PUBLISH_JOBS[job_id].get("resume_retry_item"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
