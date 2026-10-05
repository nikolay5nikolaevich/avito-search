"""
Тесты Сборщика статистики (backend/stats_collector.py, брифа 05).

HTTP не выполняется. Фикстуры повторяют СТРУКТУРУ ответов Авито из
debug/stats_probe/*.json (разведка 15.09.2026), но со значениями, которых на
реальном аккаунте не было — id, заголовки, адреса выдуманы (debug/ с
персональными данными в git не идёт, tests/ — идёт).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import journal  # noqa: E402
import stats_collector  # noqa: E402


class DateRangeTests(unittest.TestCase):
    """date_to = вчера, date_from = date_to - (period_days - 1)."""

    def test_seven_days(self) -> None:
        date_from, date_to = stats_collector.compute_date_range(7, today=date(2026, 9, 15))
        self.assertEqual(date_to, "2026-09-14")
        self.assertEqual(date_from, "2026-09-08")

    def test_thirty_days(self) -> None:
        date_from, date_to = stats_collector.compute_date_range(30, today=date(2026, 9, 15))
        self.assertEqual(date_to, "2026-09-14")
        self.assertEqual(date_from, "2026-08-16")

    def test_invalid_period_raises(self) -> None:
        with self.assertRaises(ValueError):
            stats_collector.compute_date_range(14)


class MatchCityFromAddressTests(unittest.TestCase):
    def test_matches_known_city_by_exact_segment(self) -> None:
        city = stats_collector.match_city_from_address("Челябинская обл., Челябинск, Российская ул.")
        self.assertIsNotNone(city)
        self.assertEqual(city.slug, "chelyabinsk")

    def test_does_not_match_substring_false_positive(self) -> None:
        # «Мурино» — не «Мурманск» и не в CITIES; подстрочный поиск дал бы
        # ложное совпадение с «Муринское городское поселение» — точный не должен.
        city = stats_collector.match_city_from_address(
            "Ленинградская обл., Муринское городское поселение, Мурино, ул. Шувалова, 32"
        )
        self.assertIsNone(city)

    def test_no_match_for_city_outside_the_50(self) -> None:
        city = stats_collector.match_city_from_address("Мурманская обл., Мурманск, ул. Ленина")
        self.assertIsNone(city)

    def test_empty_address(self) -> None:
        self.assertIsNone(stats_collector.match_city_from_address(""))


# ── Фикстуры build_snapshot_data (структура — debug/stats_probe/03_items.json
# и 04_stats.json, значения — выдуманные) ──────────────────────────────────

API_ITEMS = [
    {"id": 111, "title": "Куртка А", "address": "Москва, ул. Тверская, 1", "status": "active"},
    {"id": 222, "title": "Куртка Б", "address": "Санкт-Петербург, Невский пр., 1", "status": "active"},
    {"id": 333, "title": "Куртка В без просмотров", "address": "Казань, ул. Баумана, 1", "status": "active"},
    {"id": 444, "title": "Куртка Г без записи в listings", "address": "Уфа, ул. Ленина, 1", "status": "active"},
]

STATS_BY_ID = {
    "111": [{"date": "2026-09-10", "uniqViews": 4, "uniqContacts": 1, "uniqFavorites": 0}],
    "222": [{"date": "2026-09-11", "uniqViews": 2, "uniqContacts": 0, "uniqFavorites": 1}],
    "333": [],  # объявление без единого просмотра — stats API не возвращает пустых дней вовсе
    "444": [{"date": "2026-09-12", "uniqViews": 6, "uniqContacts": 2, "uniqFavorites": 0}],
}

LISTINGS_BY_ID = {
    "111": {
        "item_id": "111", "prep_id": "prep-x", "variant_index": 1,
        "city_slug": "moskva", "city_name": "Москва",
    },
    "222": {
        "item_id": "222", "prep_id": "prep-x", "variant_index": 2,
        "city_slug": "sankt-peterburg", "city_name": "Санкт-Петербург",
    },
    "333": {
        "item_id": "333", "prep_id": "prep-x", "variant_index": 3,
        "city_slug": "kazan", "city_name": "Казань",
    },
    # "444" намеренно отсутствует — опубликовано не через этот сервис.
}


class BuildSnapshotDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = stats_collector.build_snapshot_data(
            API_ITEMS, STATS_BY_ID, LISTINGS_BY_ID, period_days=7,
        )

    def test_all_items_present(self) -> None:
        self.assertEqual(len(self.data["items"]), 4)

    def test_item_without_listing_falls_back_to_address_city(self) -> None:
        item = next(i for i in self.data["items"] if i["item_id"] == "444")
        self.assertEqual(item["city_slug"], "ufa")
        self.assertEqual(item["city_name"], "Уфа")
        self.assertIsNone(item["prep_id"])
        self.assertIsNone(item["variant_index"])
        self.assertEqual(item["views"], 6)
        self.assertAlmostEqual(item["views_per_day"], 6 / 7)

    def test_zero_views_item_has_zero_views_per_day(self) -> None:
        item = next(i for i in self.data["items"] if i["item_id"] == "333")
        self.assertEqual(item["views"], 0)
        self.assertEqual(item["views_per_day"], 0.0)

    def test_zero_views_gives_null_contact_rate_not_zero(self) -> None:
        variant = next(v for v in self.data["variants"] if v["variant_index"] == 3)
        self.assertEqual(variant["prep_id"], "prep-x")
        self.assertIsNone(variant["contact_rate"])

    def test_cities_aggregate_grouping_and_contact_rate(self) -> None:
        cities = {c["city_slug"]: c for c in self.data["cities"]}
        self.assertEqual(set(cities), {"moskva", "sankt-peterburg", "kazan", "ufa"})

        moskva = cities["moskva"]
        self.assertEqual(moskva["items"], 1)
        self.assertAlmostEqual(moskva["views_per_day_avg"], 4 / 7)
        self.assertAlmostEqual(moskva["contact_rate"], 1 / 4)

        kazan = cities["kazan"]
        self.assertEqual(kazan["items"], 1)
        self.assertIsNone(kazan["contact_rate"])  # views=0 у item-333

    def test_variants_aggregate_grouped_by_prep_and_index(self) -> None:
        variants = {v["variant_index"]: v for v in self.data["variants"]}
        # У item-444 нет prep_id/variant_index — в variants не попадает.
        self.assertEqual(set(variants), {1, 2, 3})
        for v in variants.values():
            self.assertEqual(v["prep_id"], "prep-x")
            self.assertEqual(v["items"], 1)


class RunOrchestrationTests(unittest.TestCase):
    """run() с замоканными сетевыми функциями — проверяем, что снимок реально
    доезжает до agents.db через journal.py."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp_dir.name) / "agents-test.db")
        journal.init_db(self.db_path)
        journal.record_listing(
            "111",
            prep_id="prep-x",
            variant_index=1,
            city_slug="moskva",
            city_name="Москва",
            db_path=self.db_path,
        )

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def test_run_saves_snapshot_and_returns_its_id(self) -> None:
        with (
            mock.patch.object(stats_collector, "_fetch_token", return_value="tok"),
            mock.patch.object(stats_collector, "_fetch_account_user_id", return_value="uid"),
            mock.patch.object(stats_collector, "_fetch_items", return_value=[API_ITEMS[0]]),
            mock.patch.object(
                stats_collector, "_fetch_stats", return_value={"111": STATS_BY_ID["111"]}
            ),
            mock.patch.dict(os.environ, {"AVITO_CLIENT_ID": "x", "AVITO_CLIENT_SECRET": "y"}),
        ):
            snapshot_id = stats_collector.run(7, db_path=self.db_path)

        snapshot = journal.get_stats_snapshot(snapshot_id, db_path=self.db_path)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["period_days"], 7)
        self.assertEqual(len(snapshot["data"]["items"]), 1)
        self.assertEqual(snapshot["data"]["items"][0]["city_slug"], "moskva")

        latest = journal.get_latest_stats_snapshot(db_path=self.db_path)
        self.assertEqual(latest["id"], snapshot_id)

    def test_run_without_credentials_raises(self) -> None:
        env_without_creds = {
            k: v for k, v in os.environ.items()
            if k not in ("AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET")
        }
        with mock.patch.dict(os.environ, env_without_creds, clear=True):
            with self.assertRaises(stats_collector.StatsCollectorError):
                stats_collector.run(7, db_path=self.db_path)

    def test_run_invalid_period_raises_before_any_network_call(self) -> None:
        with self.assertRaises(ValueError):
            stats_collector.run(14, db_path=self.db_path)


if __name__ == "__main__":
    unittest.main()
