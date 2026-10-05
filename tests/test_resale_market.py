"""
Тесты чистой логики отчёта «Поиск под перепродажу» (backend/resale_market.py).

model_key и build_report — чистые функции без I/O (см. docs/specs/resale-finder.md,
раздел «backend/resale_market.py»). Никакого Авито и ИИ здесь нет — только
арифметика группировки, медианы и порога скидки.
"""

from __future__ import annotations

import os
import statistics
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import resale_market  # noqa: E402


# ── Фикстуры ─────────────────────────────────────────────────────────────────

def _item(item_id: str, price, *, title: str | None = None, url: str | None = None) -> dict:
    """Минимальный объявление-словарь в формате parser._item_from_json."""
    return {
        "item_id": item_id,
        "title": title or f"Ноутбук {item_id}",
        "price": price,
        "url": url or f"https://www.avito.ru/moskva/noutbuki/item_{item_id}",
        "category_slug": "noutbuki",
        "description": "",
    }


def _cls(
    *,
    brand="Acer",
    series="Nitro 5",
    gpu="RTX 3060",
    condition="working",
    is_laptop=True,
    reason="исправен",
    error=None,
) -> dict:
    """Классификация в формате resale_classifier.classify_items."""
    return {
        "is_laptop": is_laptop,
        "brand": brand,
        "series": series,
        "gpu": gpu,
        "condition": condition,
        "reason": reason,
        "error": error,
    }


# ── model_key ────────────────────────────────────────────────────────────────

class ModelKeyTests(unittest.TestCase):
    def test_normalizes_case_and_collapses_spaces(self) -> None:
        c1 = _cls(brand="Acer", series="Nitro 5", gpu="RTX 3060")
        c2 = _cls(brand="  ACER ", series="nitro   5", gpu=" rtx  3060 ")
        self.assertEqual(resale_market.model_key(c1), resale_market.model_key(c2))
        self.assertEqual(resale_market.model_key(c1), "acer nitro 5 | rtx 3060")

    def test_none_when_not_laptop(self) -> None:
        self.assertIsNone(resale_market.model_key(_cls(is_laptop=False)))

    def test_none_when_no_brand(self) -> None:
        self.assertIsNone(resale_market.model_key(_cls(brand=None)))

    def test_none_when_no_series(self) -> None:
        self.assertIsNone(resale_market.model_key(_cls(series=None)))

    def test_none_when_no_gpu(self) -> None:
        self.assertIsNone(resale_market.model_key(_cls(gpu=None)))


# ── build_report: медиана и sample ──────────────────────────────────────────

class MarketMedianTests(unittest.TestCase):
    def test_median_computed_only_over_working_condition(self) -> None:
        items = [_item("1", 50000), _item("2", 52000), _item("3", 999999)]
        classifications = {
            "1": _cls(condition="working"),
            "2": _cls(condition="working"),
            "3": _cls(condition="parts"),  # не должен попасть в рынок
        }
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=1)
        self.assertEqual(len(report["groups"]), 1)
        group = report["groups"][0]
        self.assertEqual(group["sample"], 2)
        self.assertEqual(group["market_price"], statistics.median([50000, 52000]))

    def test_min_max_price_over_working_sample(self) -> None:
        items = [_item("1", 50000), _item("2", 55000), _item("3", 60000)]
        classifications = {i: _cls(condition="working") for i in ("1", "2", "3")}
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=1)
        group = report["groups"][0]
        self.assertEqual(group["min_price"], 50000)
        self.assertEqual(group["max_price"], 60000)

    def test_sample_equal_to_min_sample_is_not_low_data(self) -> None:
        items = [_item(str(i), 50000 + i * 1000) for i in range(1, 6)]  # 5 штук
        classifications = {str(i): _cls(condition="working") for i in range(1, 6)}
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        self.assertFalse(report["groups"][0]["low_data"])

    def test_sample_below_min_sample_is_low_data(self) -> None:
        items = [_item(str(i), 50000 + i * 1000) for i in range(1, 5)]  # 4 штуки
        classifications = {str(i): _cls(condition="working") for i in range(1, 5)}
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        self.assertTrue(report["groups"][0]["low_data"])

    def test_group_with_zero_working_sample_has_no_market_and_no_deals(self) -> None:
        # ДОПУЩЕНИЕ: спека говорит «sample == 0 — рынка нет, лотов нет» —
        # трактую это как полное отсутствие такой группы в отчёте (не запись
        # с market_price=None), т.к. без рабочих объявлений сравнивать не с чем.
        items = [_item("1", 20000)]
        classifications = {"1": _cls(condition="unknown")}
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=1)
        self.assertEqual(report["groups"], [])
        self.assertEqual(report["deals"], [])


# ── build_report: выгодные лоты и порог ─────────────────────────────────────

class DealThresholdTests(unittest.TestCase):
    def _market_items_and_cls(self):
        # Медиана по working: [50000, 52000, 54000, 56000, 58000] -> 54000
        items = [_item(f"w{i}", price) for i, price in enumerate(
            (50000, 52000, 54000, 56000, 58000), start=1
        )]
        classifications = {item["item_id"]: _cls(condition="working") for item in items}
        return items, classifications

    def test_deal_at_exact_threshold_boundary_is_included(self) -> None:
        items, classifications = self._market_items_and_cls()
        # cutoff = 54000 * (1 - 0.2) = 43200 — ровно на границе, лот включается.
        items.append(_item("deal", 43200))
        classifications["deal"] = _cls(condition="unknown")
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertIn("deal", deal_ids)

    def test_price_just_above_threshold_is_not_a_deal(self) -> None:
        items, classifications = self._market_items_and_cls()
        items.append(_item("no_deal", 43201))
        classifications["no_deal"] = _cls(condition="unknown")
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertNotIn("no_deal", deal_ids)

    def test_unknown_condition_is_eligible_deal_but_not_counted_in_median(self) -> None:
        items, classifications = self._market_items_and_cls()
        items.append(_item("unknown_deal", 30000))
        classifications["unknown_deal"] = _cls(condition="unknown")
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        group = report["groups"][0]
        # sample не вырос — unknown не участвует в медиане
        self.assertEqual(group["sample"], 5)
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertIn("unknown_deal", deal_ids)

    def test_discount_pct_rounded_to_one_decimal(self) -> None:
        # Медиана: [1000, 1100, 1200, 1300, 1400] -> 1200
        items = [_item(f"w{i}", price) for i, price in enumerate(
            (1000, 1100, 1200, 1300, 1400), start=1
        )]
        classifications = {item["item_id"]: _cls(condition="working") for item in items}
        items.append(_item("deal", 850))
        classifications["deal"] = _cls(condition="unknown")
        # discount = (1200 - 850) / 1200 * 100 = 29.1666... -> 29.2
        # threshold_pct=25 -> cutoff=900, 850 проходит порог (иначе лот не
        # попал бы в deals и тест проверял бы не то, что заявлено в имени).
        report = resale_market.build_report(items, classifications, threshold_pct=25, min_sample=5)
        deal = next(d for d in report["deals"] if d["item_id"] == "deal")
        self.assertEqual(deal["discount_pct"], 29.2)

    def test_deals_sorted_by_discount_desc(self) -> None:
        items, classifications = self._market_items_and_cls()
        items.append(_item("small_discount", 40000))  # скидка ~25.9%
        classifications["small_discount"] = _cls(condition="unknown")
        items.append(_item("big_discount", 10000))  # скидка ~81.5%
        classifications["big_discount"] = _cls(condition="unknown")
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        deal_ids = [d["item_id"] for d in report["deals"]]
        self.assertEqual(deal_ids, ["big_discount", "small_discount"])

    def test_price_none_ignored(self) -> None:
        items, classifications = self._market_items_and_cls()
        items.append(_item("no_price", None))
        classifications["no_price"] = _cls(condition="working")
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        group = report["groups"][0]
        self.assertEqual(group["sample"], 5)  # объявление с price=None не учтено
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertNotIn("no_price", deal_ids)

    def test_price_zero_ignored(self) -> None:
        items, classifications = self._market_items_and_cls()
        items.append(_item("zero_price", 0))
        classifications["zero_price"] = _cls(condition="working")
        report = resale_market.build_report(items, classifications, threshold_pct=20, min_sample=5)
        group = report["groups"][0]
        self.assertEqual(group["sample"], 5)
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertNotIn("zero_price", deal_ids)


# ── build_report: исключения (excluded) ─────────────────────────────────────

class ExclusionTests(unittest.TestCase):
    def test_parts_excluded_from_market_and_counted(self) -> None:
        items = [_item("1", 30000)]
        classifications = {"1": _cls(condition="parts")}
        report = resale_market.build_report(items, classifications)
        self.assertEqual(report["excluded"]["parts"], 1)
        self.assertEqual(report["excluded"]["not_laptop"], 0)
        self.assertEqual(report["excluded"]["unrecognized"], 0)
        self.assertEqual(report["excluded"]["errors"], 0)
        self.assertEqual(report["deals"], [])
        self.assertEqual(report["groups"], [])

    def test_not_laptop_excluded_and_counted(self) -> None:
        items = [_item("1", 500)]
        classifications = {"1": _cls(is_laptop=False, brand=None, series=None, gpu=None, condition="unknown")}
        report = resale_market.build_report(items, classifications)
        self.assertEqual(report["excluded"]["not_laptop"], 1)
        self.assertEqual(report["excluded"]["parts"], 0)
        self.assertEqual(report["excluded"]["unrecognized"], 0)
        self.assertEqual(report["excluded"]["errors"], 0)

    def test_unrecognized_excluded_and_counted_when_no_gpu(self) -> None:
        items = [_item("1", 40000)]
        classifications = {"1": _cls(gpu=None, condition="working")}
        report = resale_market.build_report(items, classifications)
        self.assertEqual(report["excluded"]["unrecognized"], 1)
        self.assertEqual(report["excluded"]["parts"], 0)
        self.assertEqual(report["excluded"]["not_laptop"], 0)
        self.assertEqual(report["excluded"]["errors"], 0)

    def test_error_excluded_and_counted(self) -> None:
        items = [_item("1", 40000)]
        classifications = {"1": _cls(
            is_laptop=None, brand=None, series=None, gpu=None,
            condition="unknown", reason="", error="Пачка не разобрана: таймаут CLI",
        )}
        report = resale_market.build_report(items, classifications)
        self.assertEqual(report["excluded"]["errors"], 1)
        self.assertEqual(report["excluded"]["parts"], 0)
        self.assertEqual(report["excluded"]["not_laptop"], 0)
        self.assertEqual(report["excluded"]["unrecognized"], 0)

    def test_item_without_classification_entry_is_excluded(self) -> None:
        # ДОПУЩЕНИЕ: спека не описывает явно случай, когда для item_id вообще
        # нет записи в classifications (например, оно не попало ни в одну
        # пачку ИИ). Ожидаю, что build_report не падает и не включает такое
        # объявление ни в рынок, ни в лоты — какая именно категория excluded
        # ему присваивается, спекой не зафиксировано, поэтому это не проверяю.
        items = [_item("1", 40000), _item("2", 50000)]
        classifications = {"2": _cls(condition="working")}
        report = resale_market.build_report(items, classifications, min_sample=1)
        deal_ids = {d["item_id"] for d in report["deals"]}
        self.assertNotIn("1", deal_ids)
        # Единственный рабочий образец ("2") не должен раздуться до двух —
        # неклассифицированное "1" не попало в sample.
        self.assertEqual(report["groups"][0]["sample"], 1)
        self.assertEqual(report["total_items"], 2)


# ── build_report: группы ────────────────────────────────────────────────────

class GroupTests(unittest.TestCase):
    def test_different_gpu_same_series_are_different_groups(self) -> None:
        items = [_item("1", 50000), _item("2", 40000)]
        classifications = {
            "1": _cls(gpu="RTX 3060", condition="working"),
            "2": _cls(gpu="GTX 1650", condition="working"),
        }
        report = resale_market.build_report(items, classifications, min_sample=1)
        keys = {g["model_key"] for g in report["groups"]}
        self.assertEqual(len(keys), 2)

    def test_groups_sorted_by_sample_desc(self) -> None:
        big_group = [_item(f"a{i}", 50000) for i in range(3)]
        small_group = [_item(f"b{i}", 40000) for i in range(1)]
        items = big_group + small_group
        classifications = {}
        for item in big_group:
            classifications[item["item_id"]] = _cls(gpu="RTX 3060", condition="working")
        for item in small_group:
            classifications[item["item_id"]] = _cls(gpu="GTX 1650", condition="working")
        report = resale_market.build_report(items, classifications, min_sample=1)
        samples = [g["sample"] for g in report["groups"]]
        self.assertEqual(samples, sorted(samples, reverse=True))
        self.assertEqual(report["groups"][0]["sample"], 3)

    def test_key_normalization_merges_groups_across_case_and_spaces(self) -> None:
        items = [_item("1", 50000), _item("2", 52000)]
        classifications = {
            "1": _cls(brand="Acer", series="Nitro 5", gpu="RTX 3060", condition="working"),
            "2": _cls(brand="ACER", series="nitro   5", gpu="rtx 3060", condition="working"),
        }
        report = resale_market.build_report(items, classifications, min_sample=1)
        self.assertEqual(len(report["groups"]), 1)
        self.assertEqual(report["groups"][0]["sample"], 2)

    def test_group_model_display_uses_first_item_wording(self) -> None:
        items = [_item("1", 50000), _item("2", 52000)]
        classifications = {
            "1": _cls(brand="Acer", series="Nitro 5", gpu="RTX 3060", condition="working"),
            "2": _cls(brand="ACER", series="NITRO 5", gpu="RTX 3060", condition="working"),
        }
        report = resale_market.build_report(items, classifications, min_sample=1)
        self.assertEqual(report["groups"][0]["model"], "Acer Nitro 5 · RTX 3060")


# ── build_report: общий счётчик ─────────────────────────────────────────────

class TotalItemsTests(unittest.TestCase):
    def test_total_items_counts_all_input_items_regardless_of_category(self) -> None:
        items = [_item("1", 50000), _item("2", 1000), _item("3", 2000), _item("4", 3000)]
        classifications = {
            "1": _cls(condition="working"),
            "2": _cls(condition="parts"),
            "3": _cls(is_laptop=False, brand=None, series=None, gpu=None),
            "4": _cls(error="сбой"),
        }
        report = resale_market.build_report(items, classifications, min_sample=1)
        self.assertEqual(report["total_items"], 4)

    def test_defaults_are_threshold_20_and_min_sample_5(self) -> None:
        items = [_item(str(i), 50000 + i * 100) for i in range(1, 6)]
        classifications = {str(i): _cls(condition="working") for i in range(1, 6)}
        report = resale_market.build_report(items, classifications)  # без kwargs
        self.assertFalse(report["groups"][0]["low_data"])  # sample == 5 == min_sample


if __name__ == "__main__":
    unittest.main()
