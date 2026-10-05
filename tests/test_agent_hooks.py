"""
Тесты хука журнала в publisher.py (docs/_internal/briefs/agents-registry/03-agent-hooks.md).

Главное правило участка (деньги): запись в журнал агентов встаёт СТРОГО
после _save_checkpoint() и не может ни бросить исключение наружу, ни изменить
исход публикации — объявление уже отправлено и оплачено. Эти тесты проверяют
именно это: publisher.run_publish_job должен дойти до status="done" даже если
журнал полностью недоступен.
"""

from __future__ import annotations

import os
import sqlite3
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

import journal  # noqa: E402
import publisher  # noqa: E402


def make_draft() -> publisher.DraftData:
    """Черновик на одно объявление — минимум, нужный run_publish_job."""
    return publisher.DraftData(
        title="Кроссовки Heckel",
        trade_type="Продаю своё",
        condition="Новое",
        size="42",
        brand="Heckel",
        color="Чёрный",
        description="Описание",
        price=5000,
        locations=(publisher.LocationData("Москва", "Тверская, 10"),),
        view_price_max=Decimal("2"),
        photo_paths=("tmp/publish/job-hook/photo_01.jpg",),
        category="sneakers",
    )


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


async def _run_item_stub(
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
    """Заглушка одного объявления — без реального Chrome/Авито."""
    del active_page_ref, checkpoint_callback
    item_id = str(900 + item_index)
    return item_id, f"https://www.avito.ru/items/edit/{item_id}", Decimal("1.5"), None


class JournalHookFinancialSafetyTests(unittest.IsolatedAsyncioTestCase):
    """publisher.run_publish_job не должен зависеть от исправности журнала."""

    async def _run_one_item_job(self) -> dict[str, object]:
        job: dict[str, object] = {"status": "queued", "items_total": 1}
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
            mock.patch.object(publisher, "_run_single_item", new=_run_item_stub),
            mock.patch.object(publisher, "_pause", new=mock.AsyncMock()),
        ):
            await publisher.run_publish_job(
                "job-hook",
                job,
                make_draft(),
                cdp_url="http://127.0.0.1:9222",
            )
        return job

    async def test_sqlite_failure_does_not_block_publish(self) -> None:
        """Замокана ошибка SQLite (как в journal.DbFailureSafetyTests) — журнал
        сам глотает её, объявление всё равно доходит до status="done"."""
        with mock.patch.object(
            journal.sqlite3,
            "connect",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            job = await self._run_one_item_job()

        self.assertEqual(job["status"], "done")
        self.assertEqual(job["items_published"], 1)
        self.assertEqual(len(job["published_urls"]), 1)

    async def test_unexpected_journal_exception_does_not_block_publish(self) -> None:
        """Даже непредвиденное исключение журнала (не sqlite3.Error) не должно
        сорвать публикацию — hook в publisher.py заворачивает его сам."""
        with mock.patch.object(
            journal,
            "record_listing",
            side_effect=RuntimeError("непредвиденный сбой журнала"),
        ):
            job = await self._run_one_item_job()

        self.assertEqual(job["status"], "done")
        self.assertEqual(job["items_published"], 1)
        self.assertEqual(len(job["published_urls"]), 1)


class AppStartupJournalSafetyTests(unittest.TestCase):
    """Битая agents.db не должна мешать серверу стартовать (иначе после сбоя
    посреди пакета resume станет недоступен — publish_state.recover_publish_states
    идёт следующим шагом в on_startup)."""

    def test_broken_journal_db_does_not_block_startup(self) -> None:
        import app as app_module  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            broken_path = str(Path(tmp) / "broken-agents.db")
            # Файл-мусор вместо базы: sqlite3 открывает его без ошибок, но
            # выполнение CREATE TABLE упадёт с sqlite3.DatabaseError.
            Path(broken_path).write_bytes(b"not a sqlite database")

            with (
                mock.patch.object(journal, "DB_PATH", broken_path),
                mock.patch.object(app_module.cache_mod, "init_db"),
                mock.patch.object(
                    app_module.publish_state, "recover_publish_states", return_value={}
                ),
            ):
                try:
                    app_module.on_startup()
                except Exception as exc:  # noqa: BLE001 — тест именно на отсутствие исключений
                    self.fail(f"on_startup не должен падать при битой agents.db: {exc}")


if __name__ == "__main__":
    unittest.main()
