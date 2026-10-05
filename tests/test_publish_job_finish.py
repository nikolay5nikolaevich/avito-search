"""
Тесты-доказательства брифа 05 «Финал пакета: следы пропущенных и обработка
ошибок» (docs/_internal/briefs/resume-audit/05-job-finish-and-traces.md).

Написаны ДО реализации, от брифа и денежных правил, не от кода: там, где бриф
требует нового поведения, тесты сейчас красные — это приёмка для исполнителя.
Находки: F04/S4, F24/S3, F16, S2, F37, F21, F19, S5
(docs/reports/2026-09-23-resume-audit.md).

Стенд — tests/publish_money_harness.py (настоящие app.api_publish_start/
resume/status, app._run_publish_and_cleanup, publisher.run_publish_job/
_run_single_item, publish_state целиком; подменены только шаги Playwright,
CDP, драйвер и диск). Поверх него здесь:
  - «реалистичный» continue_listing: как настоящий шаг, отмечает трекер
    денежного клика (checks_started → click_called → mark_clicked) и
    ВОЗВРАЩАЕТ (item_id, page). Контракт шага не меняется (бриф: клики и их
    проверки не трогать), значит item_id сохраняет вызывающий код;
  - сбои по имени точки (а не по номеру события), чтобы новые записи
    checkpoint в реализации не сдвигали место сбоя;
  - поддельный драйвер для F21: его __aexit__ «убивает» страницы — после
    выхода из `async with async_playwright()` скриншот бросает, а close()
    становится молчаливым no-op (так ведёт себя настоящий Playwright, F21).

Ни сети, ни Chrome, ни боевых tmp/publish и debug/ (publish_test_isolation
импортируется первым через стенд; стенд дополнительно уводит всё во временный
каталог).

======================================================================
ОЖИДАЕМЫЕ ИМЕНА И ФОРМАТЫ (бриф их не фиксирует — исполнитель подстраивается
под этот блок или обсуждает его)
======================================================================
- job["skipped_items"] (checkpoint и /status): список записей-словарей.
  Номер объявления в пакете — ключ "item_index" (int, как в
  applied_view_prices/address_warnings). Проверка терпимая: номер ищется в
  "item_index", затем "index" / "number" / "item_number". Остальное —
  ПО СМЫСЛУ, через вхождение значения в JSON записи:
    * item_id объявления (строка из /cpxpromo/<id>) — ожидаемый ключ "item_id";
    * шаг остановки, например "fill_view_price" — ожидаемый ключ "step";
    * город/адрес варианта — ожидаемые ключи "city" и "address"
      (проверяется вхождение «улица Спековая, N»).
  Старый формат (список int) читается везде: resume_plan, /status, recover,
  /resume; смешанный список [2, {"item_index": 3, ...}] тоже.
- item_id созданного объявления в checkpoint до конца объявления: проверяется
  только вхождение строки id в JSON job (имя ключа любое; ожидаемо
  "item_id" / "current_item_id"). URL /cpxpromo/<id> не проверяется — бриф
  пишет «если есть».
- Номер в текстах (409 /resume, recover, стоп F37): формат «№N»
  (регулярка №\\s*N, как во всех текстах проекта).
- Статус «завершена с пропусками» после рестарта: строка, начинающаяся с
  "done" ("done" или, например, "done_with_skips").
- Терминальные статусы после сбоя/исключения: failed / needs_user_action /
  interrupted; CancelledError → именно "interrupted".
- Необработанное исключение run_publish_job → status "failed", непустой
  job["error"], задача asyncio завершается без исключения (его читает
  страхующий try), в лог app — запись уровня ERROR с traceback (exc_info).

Запуск (из tests/, pytest в .venv нет):
  ../.venv/Scripts/python.exe -m unittest test_publish_job_finish -v
"""

from __future__ import annotations

import os
import sys

_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import publish_test_isolation  # noqa: E402,F401 — первым: мёртвый CDP, temp dirs

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
import uuid  # noqa: E402
from collections import Counter  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402
from unittest import mock  # noqa: E402

import publish_money_harness as H  # noqa: E402
import app as backend_app  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402
from playwright.async_api import Error as PlaywrightError  # noqa: E402

TERMINAL_STOP_STATUSES = frozenset({"failed", "needs_user_action", "interrupted"})
MONEY_POINTS = frozenset({
    "fill_view_price", "continue_view_price:click", "skip_services:click",
})


# ---------------------------------------------------------------------------
# Разбор записей skipped_items «по смыслу»
# ---------------------------------------------------------------------------

def _number_of(record: Any) -> Optional[int]:
    if isinstance(record, bool):
        return None
    if isinstance(record, int):
        return record
    if isinstance(record, str) and record.strip().isdigit():
        return int(record)
    if isinstance(record, dict):
        for key in ("item_index", "index", "number", "item_number"):
            value = record.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value)
    return None


def _skipped_numbers(skipped: Any) -> set[Optional[int]]:
    return {_number_of(r) for r in (skipped or [])}


def _find_skipped(skipped: Any, number: int) -> Any:
    for record in skipped or []:
        if _number_of(record) == number:
            return record
    return None


def _mentions(obj: Any, text: Any) -> bool:
    return str(text) in json.dumps(obj, ensure_ascii=False)


def _names_number(text: Any, number: int) -> bool:
    return bool(re.search(rf"№\s*{number}(?!\d)", str(text or "")))


# ---------------------------------------------------------------------------
# Стенд брифа 05 поверх publish_money_harness
# ---------------------------------------------------------------------------

class FinishHarness(H.Harness):
    """Harness + реалистичный continue_listing + сбои по имени точки."""

    def __init__(self, n_items: int = 3, **kwargs: Any) -> None:
        super().__init__(n_items, **kwargs)
        # (имя точки, номер) -> (вид сбоя, на каком по счёту вхождении)
        self.name_faults: dict[tuple[str, Optional[int]], tuple[str, int]] = {}
        self._fault_hits: Counter = Counter()
        # номер объявления -> номер объявления, чей объект Авито «вернёт» (F37)
        self.reuse: dict[int, int] = {}
        self.reuse_at: Optional[int] = None
        # Хук «сразу после возврата continue_listing» (для сбоя записи).
        self.after_continue_listing: Optional[Any] = None

    def __enter__(self) -> "FinishHarness":
        super().__enter__()
        av = self.avito
        original_point = av.point

        def point(name: str, index: Optional[int] = None) -> None:
            original_point(name, index)
            key = (name, index)
            if key not in self.name_faults:
                return
            self._fault_hits[key] += 1
            kind, occurrence = self.name_faults[key]
            if self._fault_hits[key] != occurrence:
                return
            if kind == "kill":
                self.snapshot_disk()
                av.dead = True
                raise H.ServerKilled(name)
            if kind == "cancel":
                raise asyncio.CancelledError(f"стенд: отмена в точке {name}")
            raise PlaywrightError(f"Стенд: сбой Playwright в точке {name}")

        av.point = point  # type: ignore[method-assign]
        self._stack.enter_context(
            mock.patch.object(publisher, "_step_continue_listing", self._continue_listing)
        )
        return self

    def fail_at(self, name: str, index: Optional[int], kind: str = "exc",
                occurrence: int = 1) -> None:
        self.name_faults[(name, index)] = (kind, occurrence)

    async def _continue_listing(self, page: Any) -> tuple[str, Any]:
        av = self.avito
        tracker = publisher._MONEY_CLICK_TRACKER.get()
        if tracker is not None:
            tracker.listing_checks_started = True
        index = page.form.address_index if page.form else None
        await av.apoint("continue_listing:click", index)
        if tracker is not None:
            tracker.listing_click_called = True
        source = self.reuse.get(index) if index is not None else None
        existing = next(
            (x for x in av.listings if source is not None and x["index"] == source),
            None,
        )
        if existing is not None:
            # Авито «вернул» уже созданный объект: тот же id, форма перезаписана.
            av.clicks[("continue_listing", index)] += 1
            existing["title"] = page.form.title
            page.form.clicked = True
            page.form.listing = existing
            page.url = f"https://www.avito.ru/cpxpromo/{existing['id']}"
            listing = existing
            self.reuse_at = av.counter
        else:
            listing = av.register_continue_listing(page)
        if tracker is not None:
            tracker.mark_clicked()
        await av.apoint("continue_listing:after", index)
        if self.after_continue_listing is not None:
            self.after_continue_listing(index)
        return listing["id"], page

    # --- наблюдение -------------------------------------------------------
    @property
    def job_dir(self) -> Path:
        return self.publish_root / str(self.job_id)

    def disk_job(self) -> dict[str, Any]:
        job, _draft = publish_state.load_publish_state(self.job_dir)
        return job

    def listing_id(self, index: int) -> str:
        for listing in self.avito.listings:
            if listing["index"] == index:
                return str(listing["id"])
        raise LookupError(f"объявление №{index} на поддельном Авито не создано")

    def money_events_after(self, counter: int) -> list[tuple[int, str, Any]]:
        return [e for e in self.avito.trace if e[0] > counter and e[1] in MONEY_POINTS]

    def memory_job(self) -> dict[str, Any]:
        return backend_app.PUBLISH_JOBS.get(str(self.job_id)) or {}


def _plan_retries_item(job: dict[str, Any], index: int) -> bool:
    plan = publish_state.resume_plan(job)
    return bool(plan and plan["mode"] == "retry_item" and plan["start_index"] == index)


# ---------------------------------------------------------------------------
# F04/S4: item_id созданного объявления в checkpoint
# ---------------------------------------------------------------------------

class ItemIdPersistedTests(unittest.IsolatedAsyncioTestCase):
    """Сбой после клика «Продолжить» → item_id созданного объявления на диске."""

    async def _stop_after_click(self, step_point: str) -> None:
        with FinishHarness(3) as h:
            h.fail_at(step_point, 2)
            code, payload = await h.start()
            self.assertEqual(code, 200, payload)
            self.assertEqual(await h.wait_task(), "ok")
            self.assertIn(h.memory_job().get("status"), TERMINAL_STOP_STATUSES)

            id2 = h.listing_id(2)
            saved = h.disk_job()
            self.assertTrue(
                _mentions(saved, id2),
                f"F04: item_id {id2} созданного №2 (сбой на {step_point}) не "
                f"записан в checkpoint",
            )
            # id уже отправленного №1 — не то же самое: проверяем именно №2.
            self.assertNotEqual(id2, h.listing_id(1))

    async def test_failure_at_fill_view_price_keeps_item_id(self) -> None:
        await self._stop_after_click("fill_view_price")

    async def test_failure_at_skip_services_keeps_item_id(self) -> None:
        await self._stop_after_click("skip_services:click")

    async def test_item_id_is_on_disk_before_view_price_step(self) -> None:
        """Процесс убит на fill_view_price (до обработчика ошибок): id уже
        должен лежать на диске — «вместе с отметкой клика или сразу после»."""
        with FinishHarness(3) as h:
            h.fail_at("fill_view_price", 2, kind="kill")
            await h.start()
            self.assertEqual(await h.wait_task(), "killed")
            id2 = h.listing_id(2)
            h.restart()
            saved = h.disk_job()
            self.assertTrue(
                _mentions(saved, id2),
                f"F04: после kill на fill_view_price в checkpoint нет item_id {id2}",
            )
            self.assertFalse(_plan_retries_item(saved, 2), "план повторит созданный №2")

    async def test_failed_post_click_write_stops_without_repeat_click(self) -> None:
        """Запись item_id после клика упала → стоп до цены просмотра, без
        повтора клика ни сейчас, ни при resume (граница брифа, как в 03)."""
        with FinishHarness(3) as h:
            armed = {"on": False}
            real_save = H.REAL_SAVE

            def arm(index: Optional[int]) -> None:
                if index == 2:
                    armed["on"] = True

            def flaky_save(job_dir: Path, job_id: str, job: dict, draft: Any) -> Path:
                if armed["on"]:
                    armed["on"] = False
                    raise OSError(28, "Стенд: нет места на диске (запись после клика)")
                return real_save(job_dir, job_id, job, draft)

            h.after_continue_listing = arm
            with mock.patch.object(H, "REAL_SAVE", flaky_save):
                await h.start()
                self.assertEqual(await h.wait_task(), "ok")

            names_for_2 = [(name, idx) for _n, name, idx in h.avito.trace if idx == 2]
            self.assertNotIn(
                ("fill_view_price", 2), names_for_2,
                "Запись после клика не удалась, а сценарий пошёл к цене просмотра "
                "(или записи item_id сразу после клика нет вовсе)",
            )
            self.assertIn(h.memory_job().get("status"), TERMINAL_STOP_STATUSES)
            self.assertFalse(_plan_retries_item(h.disk_job(), 2), "resume повторит №2")

            await H.resume_to_end(h)
        self.assertEqual(h.avito.created_indices()[2], 1, "№2 создан повторно")
        repeated = {k: v for k, v in h.avito.clicks.items() if v > 1}
        self.assertEqual(repeated, {}, "денежный клик повторён")


# ---------------------------------------------------------------------------
# skipped_items записями + /status + F24/S3 (done с пропусками) + F16
# ---------------------------------------------------------------------------

class SkippedRecordsAndDoneTests(unittest.IsolatedAsyncioTestCase):
    """№2 из 3 упал после клика, resume пропускает его и доводит пакет до done."""

    async def test_skipped_record_has_number_item_id_step_and_address(self) -> None:
        with FinishHarness(3) as h:
            h.fail_at("fill_view_price", 2)
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")
            id2 = h.listing_id(2)

            code, body = await h.resume()
            self.assertEqual((code, body.get("mode")), (200, "skip_item"), body)
            await h.wait_task()

            _code, status = await h.status()
            record = _find_skipped(status.get("skipped_items"), 2)
            self.assertIsNotNone(record, f"№2 нет в skipped_items: {status.get('skipped_items')}")
            self.assertIsInstance(record, dict, "запись о пропуске — словарь, а не голый номер")
            self.assertTrue(_mentions(record, id2), f"в записи о №2 нет item_id {id2}: {record}")
            self.assertTrue(_mentions(record, "fill_view_price"), f"нет шага остановки: {record}")
            self.assertTrue(
                _mentions(record, "Спековая, 2"), f"нет адреса варианта №2: {record}"
            )

            # Адрес — только в checkpoint tmp/publish, не в дампах debug/.
            for dumped in h.debug_root.rglob("*") if h.debug_root.exists() else []:
                if dumped.is_file():
                    text = dumped.read_text(encoding="utf-8", errors="ignore")
                    self.assertNotIn("Спековая", text, f"адрес попал в дамп {dumped}")

    async def test_done_with_skips_survives_restart_and_visible_in_status(self) -> None:
        with FinishHarness(3) as h:
            h.fail_at("fill_view_price", 2)
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")
            id2 = h.listing_id(2)
            answers = await H.resume_to_end(h)
            self.assertEqual(answers[0][0], 200, answers)
            self.assertEqual(h.memory_job().get("status"), "done")

            state_file = h.job_dir / publish_state.STATE_FILENAME
            self.assertTrue(
                state_file.is_file(),
                "F24/S3: done с пропусками удалил publish-state.json вместе со следами №2",
            )
            saved = h.disk_job()
            self.assertEqual(saved.get("status"), "done", "F16: на диске не done")
            self.assertTrue(_mentions(_find_skipped(saved.get("skipped_items"), 2), id2))

            clicks_before = sum(h.avito.clicks.values())
            h.restart()
            code, status = await h.status()
            self.assertEqual(code, 200, "после рестарта задача с пропусками не видна (404)")
            self.assertTrue(
                str(status.get("status")).startswith("done"),
                f"после рестарта не «завершена с пропусками»: {status.get('status')}",
            )
            record = _find_skipped(status.get("skipped_items"), 2)
            self.assertIsNotNone(record, status.get("skipped_items"))
            self.assertTrue(_mentions(record, id2))
            self.assertFalse(status.get("resume_available"))

            # Завершённый пакет не запускается заново ни при каких условиях.
            code, _body = await h.resume()
            self.assertNotEqual(code, 200, "resume запустил завершённый пакет")
            await h.wait_task()
            self.assertEqual(sum(h.avito.clicks.values()), clicks_before)
        self.assertEqual(dict(h.avito.created_indices()), {1: 1, 2: 1, 3: 1})

    async def test_done_is_written_to_disk_before_cleanup(self) -> None:
        """F16: done попадает на диск ДО уборки; если уборка не удалась (kill в
        окне, антивирус держит файл), после рестарта нет ложного interrupted."""
        with FinishHarness(2) as h:
            events: list[tuple[str, Any]] = []
            real_save = H.REAL_SAVE

            def recording_save(job_dir: Path, job_id: str, job: dict, draft: Any) -> Path:
                path = real_save(job_dir, job_id, job, draft)
                events.append(("save", job.get("status")))
                return path

            def locked_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
                events.append(("rmtree", Path(path).name))  # файл «занят» — не удалено

            with (
                mock.patch.object(H, "REAL_SAVE", recording_save),
                mock.patch.object(shutil, "rmtree", locked_rmtree),
            ):
                await h.start()
                self.assertEqual(await h.wait_task(), "ok")
            self.assertEqual(h.memory_job().get("status"), "done")

            self.assertIn(("save", "done"), events, "F16: done ни разу не записан на диск")
            first_done = events.index(("save", "done"))
            cleanup = [i for i, e in enumerate(events) if e == ("rmtree", h.job_id)]
            if cleanup:
                self.assertLess(first_done, cleanup[0], "каталог задачи убран до записи done")

            self.assertEqual(h.disk_job().get("status"), "done")
            h.restart()
            after = backend_app.PUBLISH_JOBS.get(h.job_id)
            if after is not None:
                self.assertNotEqual(
                    after.get("status"), "interrupted",
                    f"полностью отправленный пакет выглядит прерванным: {after.get('error')}",
                )


# ---------------------------------------------------------------------------
# S2: пропуск последнего объявления
# ---------------------------------------------------------------------------

class LastItemSkipTests(unittest.IsolatedAsyncioTestCase):
    async def _last_item(self, step_point: str) -> None:
        with FinishHarness(3) as h:
            h.fail_at(step_point, 3)
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")
            id3 = h.listing_id(3)
            clicks_before = sum(h.avito.clicks.values())

            code, body = await h.resume()
            self.assertNotEqual(code, 200, "resume после создания последнего запустил пакет")
            await h.wait_task()
            self.assertEqual(sum(h.avito.clicks.values()), clicks_before)
            self.assertTrue(
                _names_number(body.get("error"), 3),
                f"S2: текст 409 не называет №3: {body.get('error')!r}",
            )

            _code, status = await h.status()
            record = _find_skipped(status.get("skipped_items"), 3)
            self.assertIsNotNone(
                record, f"S2: последнего №3 нет в skipped_items: {status.get('skipped_items')}"
            )
            self.assertTrue(_mentions(record, id3), f"в записи о №3 нет item_id {id3}")
            self.assertIn(3, _skipped_numbers(h.disk_job().get("skipped_items")),
                          "№3 не попал в skipped_items на диске")

            h.restart()
            recovered = h.memory_job()
            self.assertTrue(
                _names_number(recovered.get("error"), 3),
                f"S2: текст recover не называет №3: {recovered.get('error')!r}",
            )
            _code, status = await h.status()
            self.assertIn(3, _skipped_numbers(status.get("skipped_items")))

    async def test_last_item_failed_at_fill_view_price(self) -> None:
        await self._last_item("fill_view_price")

    async def test_last_item_failed_at_skip_services(self) -> None:
        await self._last_item("skip_services:click")


# ---------------------------------------------------------------------------
# F37: повторный item_id в пакете → стоп до fill_view_price
# ---------------------------------------------------------------------------

class RepeatedItemIdTests(unittest.IsolatedAsyncioTestCase):
    def _assert_stopped_before_price(self, h: FinishHarness, repeated_id: str) -> None:
        self.assertIsNotNone(h.reuse_at, "стенд: повторный id не был выдан")
        self.assertEqual(
            h.money_events_after(h.reuse_at), [],
            "F37: после повторного item_id сценарий дошёл до цены/оплаты",
        )
        job = h.memory_job()
        self.assertIn(job.get("status"), TERMINAL_STOP_STATUSES, "повторный id — не стоп")
        error = str(job.get("error") or "")
        self.assertIn(repeated_id, error, f"текст стопа не называет id: {error!r}")
        self.assertRegex(error, r"№\s*\d", f"текст стопа не называет номер: {error!r}")
        self.assertIn("кабинет", error.lower(), f"текст не отправляет в кабинет: {error!r}")

    async def test_id_of_sent_item_returned_again(self) -> None:
        with FinishHarness(3) as h:
            h.reuse = {2: 1}
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")
            id1 = h.listing_id(1)
            self._assert_stopped_before_price(h, id1)
            self.assertEqual(h.avito.listings[0]["view_paid"], 1, "объект №1 оплачен дважды")
            self.assertNotIn(("continue_listing:click", 3),
                             [(n, i) for _c, n, i in h.avito.trace],
                             "стоп пакета, а не пропуск: №3 запускаться не должен")
            self.assertEqual(h.memory_job().get("items_published"), 1)

    async def test_id_of_skipped_item_returned_after_resume(self) -> None:
        """Сценарий аудита F37: №2 создан и пропущен, при resume /additem для
        №3 «переоткрывает» объект №2."""
        with FinishHarness(4) as h:
            h.fail_at("fill_view_price", 2)
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")
            id2 = h.listing_id(2)
            h.reuse = {3: 2}
            code, body = await h.resume()
            self.assertEqual((code, body.get("mode")), (200, "skip_item"), body)
            self.assertEqual(await h.wait_task(), "ok")
            self._assert_stopped_before_price(h, id2)
            self.assertEqual(h.avito.listings[1]["view_paid"], 0)
            self.assertNotIn(("continue_listing:click", 4),
                             [(n, i) for _c, n, i in h.avito.trace])


# ---------------------------------------------------------------------------
# Старый формат skipped_items (список чисел) и новый — записями
# ---------------------------------------------------------------------------

def _draft(n_items: int) -> publisher.DraftData:
    return publisher.DraftData(
        title="Формат", trade_type="tt", condition="c", size="s", brand="",
        color="col", description="d", price=100,
        locations=tuple(
            publisher.LocationData(city="Москва", address=f"улица Спековая, {k}")
            for k in range(1, n_items + 1)
        ),
        view_price_max=Decimal("1"), photo_paths=(), category="jackets",
    )


def _stopped_job(skipped: list[Any], *, total: int, published: int, index: int) -> dict[str, Any]:
    return {
        "status": "failed", "step": "fill_view_price", "items_total": total,
        "item_index": index, "items_published": published,
        "published_urls": [f"https://www.avito.ru/items/edit/{100 + k}" for k in range(published)],
        "skipped_items": skipped, "money_click": "clicked", "prep_id": None,
        "error": "стенд",
    }


NEW_RECORD_2 = {
    "item_index": 2, "item_id": "7730002", "step": "fill_view_price",
    "city": "Москва", "address": "улица Спековая, 2",
}


class SkippedItemsFormatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(backend_app, "TMP_PUBLISH_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        for registry in (backend_app.PUBLISH_JOBS, backend_app.ACTIVE_PUBLISH_TASKS):
            registry.clear()
            self.addCleanup(registry.clear)

    def _plan(self, skipped: list[Any]) -> Optional[dict[str, Any]]:
        return publish_state.resume_plan(_stopped_job(skipped, total=4, published=1, index=3))

    def test_old_int_list_resume_plan(self) -> None:
        self.assertEqual(self._plan([2]), {
            "mode": "skip_item", "start_index": 4, "skipped_item": 3, "items_total": 4,
        })

    def test_new_record_format_resume_plan(self) -> None:
        self.assertEqual(self._plan([dict(NEW_RECORD_2)]), {
            "mode": "skip_item", "start_index": 4, "skipped_item": 3, "items_total": 4,
        }, "resume_plan не читает skipped_items записями")

    def test_mixed_old_and_new_format_resume_plan(self) -> None:
        job = _stopped_job(
            [2, {**NEW_RECORD_2, "item_index": 3, "item_id": "7730003"}],
            total=5, published=1, index=4,
        )
        self.assertEqual(publish_state.resume_plan(job), {
            "mode": "skip_item", "start_index": 5, "skipped_item": 4, "items_total": 5,
        })

    async def _old_format_on_disk_flow(self, skipped: list[Any]) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        job = _stopped_job(skipped, total=4, published=1, index=3)
        publish_state.save_publish_state(self.root / job_id, job_id, job, _draft(4))

        recovered = publish_state.recover_publish_states(self.root)
        self.assertIn(job_id, recovered)
        rec_job, _draft_obj = recovered[job_id]
        self.assertEqual(rec_job["status"], "interrupted")
        self.assertTrue(rec_job["user_action"]["resumable"])
        backend_app.PUBLISH_JOBS[job_id] = rec_job

        status = backend_app._serialize_publish_status(job_id, rec_job)
        self.assertIn(2, _skipped_numbers(status["skipped_items"]))
        self.assertEqual(status["resume_plan"]["start_index"], 4)

        with mock.patch.object(backend_app, "_schedule_publish") as schedule:
            response = await backend_app.api_publish_resume(job_id)
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200, payload)
        self.assertEqual((payload["mode"], payload["skipped_item"]), ("skip_item", 3))
        self.assertEqual(schedule.call_args.kwargs["start_index"], 4)
        return backend_app.PUBLISH_JOBS[job_id]

    async def test_old_format_checkpoint_recover_status_resume(self) -> None:
        job = await self._old_format_on_disk_flow([2])
        self.assertEqual(_skipped_numbers(job["skipped_items"]), {2, 3})

    async def test_new_format_checkpoint_recover_status_resume(self) -> None:
        job = await self._old_format_on_disk_flow([dict(NEW_RECORD_2)])
        self.assertEqual(_skipped_numbers(job["skipped_items"]), {2, 3})
        self.assertTrue(_mentions(_find_skipped(job["skipped_items"], 2), "7730002"),
                        "resume потерял item_id ранее пропущенного №2")

    def test_recover_keeps_done_with_old_format_skips(self) -> None:
        job_id = str(uuid.uuid4())
        job = {
            "status": "done", "step": "done", "items_total": 3, "item_index": 3,
            "items_published": 2, "skipped_items": [2], "prep_id": None,
            "published_urls": ["https://www.avito.ru/items/edit/1",
                               "https://www.avito.ru/items/edit/3"],
        }
        publish_state.save_publish_state(self.root / job_id, job_id, job, _draft(3))
        recovered = publish_state.recover_publish_states(self.root)
        self.assertIn(job_id, recovered, "done с пропусками (старый формат) не восстановлен")
        self.assertTrue(str(recovered[job_id][0]["status"]).startswith("done"))
        self.assertIn(2, _skipped_numbers(recovered[job_id][0]["skipped_items"]))


# ---------------------------------------------------------------------------
# F21: дамп и закрытие вкладки — пока драйвер жив
# ---------------------------------------------------------------------------

class _DyingDriver:
    """async_playwright(), чей выход «убивает» все страницы."""

    def __init__(self, h: FinishHarness, events: list[tuple[str, Any]]) -> None:
        self.h = h
        self.events = events

    async def __aenter__(self) -> object:
        self.h.driver_alive = True  # type: ignore[attr-defined]
        self.events.append(("pw_enter", None))
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        self.events.append(("pw_exit", None))
        self.h.driver_alive = False  # type: ignore[attr-defined]


class DriverLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _install(self, h: FinishHarness, *, close_raises: bool = False) -> list[tuple[str, Any]]:
        events: list[tuple[str, Any]] = []
        h.driver_alive = False  # type: ignore[attr-defined]

        def alive(page: Any) -> bool:
            return bool(page.avito.h.driver_alive)

        async def screenshot(page: Any, path: Optional[str] = None, **_kw: Any) -> None:
            if not alive(page):
                events.append(("screenshot_dead", id(page)))
                raise PlaywrightError("Target page, context or browser has been closed")
            events.append(("screenshot", id(page)))
            if path:
                Path(path).write_bytes(b"\x89PNG fake")

        async def content(page: Any) -> str:
            if not alive(page):
                raise PlaywrightError("Target page, context or browser has been closed")
            return "<html>стенд</html>"

        async def close(page: Any) -> None:
            if not alive(page):
                # Настоящий Playwright после остановки драйвера: тихий no-op,
                # вкладка в Chrome остаётся открытой.
                events.append(("close_noop", id(page)))
                return None
            if close_raises:
                events.append(("close_failed", id(page)))
                raise PlaywrightError("Стенд: вкладку закрыть не удалось")
            events.append(("close", id(page)))
            page.closed = True

        for name, fn in (("screenshot", screenshot), ("content", content), ("close", close)):
            h._stack.enter_context(mock.patch.object(H.FakePage, name, fn))
        h._stack.enter_context(
            mock.patch("playwright.async_api.async_playwright", lambda: _DyingDriver(h, events))
        )
        return events

    def _assert_all_pages_closed_before_exit(self, h: FinishHarness,
                                             events: list[tuple[str, Any]]) -> None:
        exit_at = events.index(("pw_exit", None))
        pages = h.avito.context.pages
        self.assertGreaterEqual(len(pages), 2, "стенд: нет рабочей вкладки")
        for number, page in enumerate(pages):
            closes = [i for i, e in enumerate(events) if e == ("close", id(page))]
            self.assertTrue(
                closes and closes[0] < exit_at,
                f"F21: вкладка #{number} не закрыта до остановки драйвера "
                f"(события: {[e[0] for e in events]})",
            )
            self.assertTrue(page.closed, f"вкладка #{number} осталась открытой")

    async def test_failure_dump_and_close_happen_while_driver_alive(self) -> None:
        with FinishHarness(2) as h:
            events = self._install(h)
            h.fail_at("fill_view_price", 2)
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")

            work_page = h.avito.context.pages[-1]
            exit_at = events.index(("pw_exit", None))
            shots = [i for i, e in enumerate(events) if e == ("screenshot", id(work_page))]
            self.assertTrue(
                shots and shots[0] < exit_at,
                f"F21: скриншот сбоя не снят с живой страницы "
                f"(события: {[e[0] for e in events]})",
            )
            pngs = list((h.debug_root / h.job_id).glob("*.png"))
            self.assertTrue(pngs, "в дампе сбоя нет скриншота")
            self._assert_all_pages_closed_before_exit(h, events)

    async def test_success_closes_own_tab_while_driver_alive(self) -> None:
        with FinishHarness(1) as h:
            events = self._install(h)
            await h.start()
            self.assertEqual(await h.wait_task(), "ok")
            self.assertEqual(h.memory_job().get("status"), "done")
            self._assert_all_pages_closed_before_exit(h, events)

    async def test_tab_closed_log_only_when_close_succeeded(self) -> None:
        """close() либо бросает (драйвер жив), либо — после остановки драйвера —
        молча ничего не делает. Ни в одном случае лог не смеет писать «закрыта»."""
        with FinishHarness(1) as h:
            events = self._install(h, close_raises=True)
            h.fail_at("fill_view_price", 1)
            with _capture_logs(publisher.logger) as records:
                await h.start()
                self.assertEqual(await h.wait_task(), "ok")
            claimed = [r.getMessage() for r in records
                       if "вкладка закрыта" in r.getMessage().lower()]
            self.assertEqual(
                claimed, [],
                f"лог утверждает, что вкладка закрыта, хотя close() не прошёл "
                f"(события: {[e[0] for e in events]})",
            )


# ---------------------------------------------------------------------------
# F19: исключение в обработчике / в run_publish_job
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _capture_logs(logger: logging.Logger) -> Any:
    """Собирает записи логгера, не падая, если их нет (в отличие от assertLogs)."""
    handler = _ListHandler()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        yield handler.records
    finally:
        logging.disable(logging.CRITICAL)
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


class HandlerExceptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exception_inside_error_handler_does_not_leave_running(self) -> None:
        async def exploding_dump(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("стенд: обработчик ошибок сам упал")

        with FinishHarness(3) as h:
            h.fail_at("fill_view_price", 2)
            with mock.patch.object(publisher, "_dump_failure", exploding_dump):
                await h.start()
                await h.wait_task()
            job = h.memory_job()
            self.assertIn(job.get("status"), TERMINAL_STOP_STATUSES,
                          f"F19: задача осталась {job.get('status')!r}")
            saved = h.disk_job()
            self.assertIn(saved.get("status"), TERMINAL_STOP_STATUSES,
                          f"F19: на диске {saved.get('status')!r}")
            self.assertFalse(_plan_retries_item(saved, 2), "финальная запись потеряла клик №2")

    async def test_unhandled_exception_in_run_publish_job_marks_failed(self) -> None:
        async def crashing_job(job_id: str, job: dict[str, Any], _data: Any,
                               **_kwargs: Any) -> None:
            # Как будто объявление №1 уже создано, и тут всё рухнуло.
            job.update({
                "status": "running", "step": "fill_view_price", "item_index": 1,
                "money_click": "clicked", "items_total": 3, "items_published": 0,
            })
            raise RuntimeError("стенд: необработанное исключение публикации")

        with FinishHarness(3) as h:
            with (
                mock.patch.object(publisher, "run_publish_job", crashing_job),
                _capture_logs(backend_app.logger) as records,
            ):
                await h.start()
                outcome = await h.wait_task()

            job = h.memory_job()
            self.assertEqual(job.get("status"), "failed", f"F19: статус {job.get('status')!r}")
            self.assertTrue(str(job.get("error") or "").strip(), "failed без текста")
            self.assertEqual(outcome, "ok", f"исключение задачи никто не прочитал: {outcome}")
            saved = h.disk_job()
            self.assertEqual(saved.get("status"), "failed", "итог не записан в checkpoint")
            self.assertFalse(_plan_retries_item(saved, 1), "запись потеряла клик №1")
            self.assertTrue(
                any(r.exc_info and r.levelno >= logging.ERROR for r in records),
                "traceback необработанного исключения не попал в лог app (ERROR + exc_info)",
            )


# ---------------------------------------------------------------------------
# S5: CancelledError → interrupted на диске и проброшен
# ---------------------------------------------------------------------------

class CancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_mid_item_writes_interrupted_and_propagates(self) -> None:
        with FinishHarness(3) as h:
            reached = asyncio.Event()
            original = h.avito.fill_view_price

            async def hanging_price(page: Any, *args: Any, **kwargs: Any) -> Decimal:
                listing = page.form.listing if page.form else None
                if listing and listing["index"] == 2 and not reached.is_set():
                    reached.set()
                    await asyncio.sleep(3600)
                return await original(page, *args, **kwargs)

            with mock.patch.object(publisher, "_step_fill_view_price", hanging_price):
                await h.start()
                await asyncio.wait_for(reached.wait(), timeout=10)
                task = backend_app.ACTIVE_PUBLISH_TASKS[h.job_id]
                task.cancel()
                outcome = await h.wait_task()

            self.assertEqual(outcome, "cancelled", f"CancelledError не проброшен: {outcome}")
            self.assertEqual(h.memory_job().get("status"), "interrupted",
                             f"S5: в памяти {h.memory_job().get('status')!r}")
            saved = h.disk_job()
            self.assertEqual(saved.get("status"), "interrupted",
                             f"S5: на диске {saved.get('status')!r}")
            self.assertEqual(int(saved.get("items_published") or 0), 1)
            self.assertFalse(_plan_retries_item(saved, 2), "отмена потеряла клик №2")

            # Не сломать: после рестарта resume доводит пакет без дублей.
            h.restart()
            answers = await H.resume_to_end(h)
            self.assertEqual(answers[0][0], 200, answers)
        self.assertEqual(dict(h.avito.created_indices()), {1: 1, 2: 1, 3: 1})
        repeated = {k: v for k, v in h.avito.clicks.items() if v > 1}
        self.assertEqual(repeated, {}, "денежный клик повторён")


if __name__ == "__main__":
    unittest.main(verbosity=2)
