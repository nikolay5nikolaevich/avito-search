"""Тесты реестра агентов и журнала событий (backend/agents_registry.py, backend/journal.py)."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import agents_registry  # noqa: E402
import journal  # noqa: E402


class JournalTestCase(unittest.TestCase):
    """Общая база: своя временная БД на каждый тест, а не agents.db."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp_dir.name) / "agents-test.db")
        journal.init_db(self.db_path)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()


class AgentsRegistryTests(unittest.TestCase):
    def test_list_agents_returns_all_five(self) -> None:
        agents = agents_registry.list_agents()
        self.assertEqual(len(agents), 5)
        self.assertEqual(
            {a.id for a in agents},
            {"scout", "preparer", "publisher", "stats_collector", "strategist"},
        )

    def test_get_agent_known_and_unknown(self) -> None:
        self.assertIsNotNone(agents_registry.get_agent("scout"))
        self.assertIsNone(agents_registry.get_agent("ghost"))


class LogAndListEventsTests(JournalTestCase):
    def test_round_trip_write_and_read(self) -> None:
        journal.log_event(
            "scout",
            "run_started",
            "Разведчик запущен",
            run_id="job-1",
            payload={"query": "диван", "cities": 3},
            db_path=self.db_path,
        )

        events = journal.list_events(db_path=self.db_path)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["actor"], "scout")
        self.assertEqual(event["type"], "run_started")
        self.assertEqual(event["summary"], "Разведчик запущен")
        self.assertEqual(event["run_id"], "job-1")
        # payload отдаётся уже разобранным dict, а не строкой
        self.assertEqual(event["payload"], {"query": "диван", "cities": 3})

    def test_default_payload_is_empty_dict(self) -> None:
        journal.log_event("scout", "run_started", "старт", db_path=self.db_path)

        events = journal.list_events(db_path=self.db_path)

        self.assertEqual(events[0]["payload"], {})

    def test_list_events_new_first(self) -> None:
        journal.log_event("scout", "run_started", "первое", db_path=self.db_path)
        journal.log_event("scout", "run_finished", "второе", db_path=self.db_path)

        events = journal.list_events(db_path=self.db_path)

        self.assertEqual([e["summary"] for e in events], ["второе", "первое"])

    def test_list_events_respects_limit(self) -> None:
        for i in range(5):
            journal.log_event("scout", "run_started", f"событие {i}", db_path=self.db_path)

        events = journal.list_events(limit=2, db_path=self.db_path)

        self.assertEqual(len(events), 2)


class FilterByActorTests(JournalTestCase):
    def test_filter_by_actor_returns_only_matching(self) -> None:
        journal.log_event("scout", "run_started", "разведка", db_path=self.db_path)
        journal.log_event("publisher", "run_started", "публикация", db_path=self.db_path)
        journal.log_event("human", "decision", "решение человека", db_path=self.db_path)

        scout_events = journal.list_events(actor="scout", db_path=self.db_path)
        human_events = journal.list_events(actor="human", db_path=self.db_path)

        self.assertEqual(len(scout_events), 1)
        self.assertEqual(scout_events[0]["summary"], "разведка")
        self.assertEqual(len(human_events), 1)
        self.assertEqual(human_events[0]["actor"], "human")


class ViolationTests(JournalTestCase):
    def test_disallowed_tool_is_recorded_as_violation(self) -> None:
        # scout не имеет права на avito_additem (это инструмент publisher)
        journal.log_event(
            "scout",
            "artifact",
            "разведчик пытается публиковать",
            tool="avito_additem",
            db_path=self.db_path,
        )

        events = journal.list_events(db_path=self.db_path)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["type"], "violation")
        self.assertEqual(event["payload"], {
            "attempted_type": "artifact",
            "tool": "avito_additem",
            "summary": "разведчик пытается публиковать",
        })

    def test_allowed_tool_is_not_a_violation(self) -> None:
        journal.log_event(
            "scout",
            "artifact",
            "отчёт готов",
            tool="sqlite_cache",
            db_path=self.db_path,
        )

        events = journal.list_events(db_path=self.db_path)

        self.assertEqual(events[0]["type"], "artifact")


class UnknownActorTests(JournalTestCase):
    def test_unknown_actor_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            journal.log_event("ghost", "run_started", "кто это", db_path=self.db_path)

        # Событие не должно было записаться
        self.assertEqual(journal.list_events(db_path=self.db_path), [])

    def test_human_actor_is_allowed_without_registry_entry(self) -> None:
        # human не в реестре агентов, но валиден как actor
        journal.log_event("human", "decision", "принято", db_path=self.db_path)

        events = journal.list_events(db_path=self.db_path)
        self.assertEqual(len(events), 1)


class DbFailureSafetyTests(JournalTestCase):
    """Правило безопасности: сбой БД не должен ронять публикацию."""

    def test_log_event_swallows_sqlite_errors(self) -> None:
        with mock.patch.object(
            journal.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")
        ):
            try:
                journal.log_event("scout", "run_started", "старт", db_path=self.db_path)
            except Exception as exc:  # noqa: BLE001 — тест именно на отсутствие исключений
                self.fail(f"log_event не должен бросать исключения при сбое БД: {exc}")

    def test_list_events_returns_empty_list_on_sqlite_error(self) -> None:
        with mock.patch.object(
            journal.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")
        ):
            events = journal.list_events(db_path=self.db_path)
        self.assertEqual(events, [])

    def test_record_listing_swallows_sqlite_errors(self) -> None:
        with mock.patch.object(
            journal.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")
        ):
            try:
                journal.record_listing("item-1", db_path=self.db_path)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"record_listing не должен бросать исключения при сбое БД: {exc}")


class RecordListingIdempotencyTests(JournalTestCase):
    def test_record_listing_is_idempotent_by_item_id(self) -> None:
        journal.record_listing(
            "item-42",
            publish_job_id="job-1",
            prep_id="prep-1",
            variant_index=1,
            city_slug="moskva",
            city_name="Москва",
            category="sneakers",
            title="Кроссовки Heckel, арт.00001",
            db_path=self.db_path,
        )
        # Повторная запись того же item_id (например, после resume checkpoint) —
        # не должна создать дубль и не должна перезаписать первую запись.
        journal.record_listing(
            "item-42",
            publish_job_id="job-1",
            prep_id="prep-1",
            variant_index=1,
            city_slug="moskva",
            city_name="Москва",
            category="sneakers",
            title="ДРУГОЙ ЗАГОЛОВОК — не должен попасть в БД",
            db_path=self.db_path,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT title FROM listings WHERE item_id = ?", ("item-42",)
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Кроссовки Heckel, арт.00001")

    def test_record_listing_different_item_ids_both_stored(self) -> None:
        journal.record_listing("item-1", city_slug="moskva", db_path=self.db_path)
        journal.record_listing("item-2", city_slug="kazan", db_path=self.db_path)

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(count, 2)


class DisabledJournalTests(unittest.TestCase):
    """DB_PATH=None и вызовы без db_path — журнал выключен молча, без файлов."""

    def test_calls_without_db_path_are_noops_when_disabled(self) -> None:
        with mock.patch.object(journal, "DB_PATH", None):
            try:
                journal.init_db()
                journal.log_event("human", "decision", "тест без БД")
                journal.record_listing("item-disabled")
            except Exception as exc:  # noqa: BLE001 — тест именно на отсутствие исключений
                self.fail(f"Журнал не должен бросать исключения при DB_PATH=None: {exc}")

            self.assertEqual(journal.list_events(), [])

    def test_module_db_path_used_when_call_omits_db_path(self) -> None:
        """Без явного db_path вызовы попадают в модульную DB_PATH (как в app.py)."""
        with tempfile.TemporaryDirectory() as tmp:
            env_path = str(Path(tmp) / "agents-env.db")
            with mock.patch.object(journal, "DB_PATH", env_path):
                journal.init_db()
                journal.log_event("human", "decision", "через DB_PATH")
                events = journal.list_events()

            self.assertEqual(len(events), 1)
            self.assertTrue(os.path.exists(env_path))


if __name__ == "__main__":
    unittest.main()
