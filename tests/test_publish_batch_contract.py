"""Контракт геолокаций и стоимости просмотра для publish/start.

Запуск:
    python -X utf8 tests/test_publish_batch_contract.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for path in (TESTS_DIR, PROJECT_ROOT, BACKEND_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import category_profiles  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402


GOOD_PHOTO = ("photo.jpg", "image/jpeg", 1024)


def valid_fields(*, count: int = 2) -> dict[str, str]:
    locations = [
        {"city": "Москва", "address": "ул. Тверская, 10"},
        {"city": "Одинцово", "address": "ул. Центральная, 7"},
    ][:count]
    return {
        "title": "Пиджак Hugo Boss",
        "trade_type": "Продаю своё",
        "condition": "Отличное",
        "size": "50 (L)",
        "brand": "Hugo Boss",
        "color": "Чёрный",
        "description": "Почти не носили",
        "price": "15000",
        "drafts_count": str(count),
        "locations_json": json.dumps(locations, ensure_ascii=False),
        "view_price_max": "2",
        "item_type": "",
    }


class ViewPriceTests(unittest.TestCase):
    def test_parse_view_price_preserves_decimal_precision(self) -> None:
        self.assertEqual(publisher.parse_view_price(" 0,5 "), Decimal("0.5"))
        self.assertEqual(publisher.parse_view_price("2.250"), Decimal("2.250"))
        self.assertEqual(publisher.parse_view_price("1"), Decimal("1"))

    def test_parse_view_price_rejects_invalid_or_non_positive_values(self) -> None:
        for raw in (None, "", "0", "0,00", "-1", "abc", "1e3", "NaN", "Infinity"):
            with self.subTest(raw=raw):
                self.assertIsNone(publisher.parse_view_price(raw))

    def test_missing_view_price_cap_is_rejected(self) -> None:
        fields = valid_fields(count=1)
        fields.pop("view_price_max")

        errors = publisher.validate_publish_form(
            fields, [GOOD_PHOTO], category_profiles.JACKETS,
        )

        self.assertIn("view_price_max", {error["field"] for error in errors})


class LocationContractTests(unittest.TestCase):
    def test_batch_size_accepts_twenty_and_rejects_twenty_one(self) -> None:
        self.assertEqual(publisher.parse_drafts_count("20"), 20)
        self.assertIsNone(publisher.parse_drafts_count("21"))

    def test_parse_locations_json_preserves_order_and_trims_fields(self) -> None:
        raw = json.dumps([
            {"city": " Москва ", "address": " Тверская, 10 "},
            {"city": "Одинцово", "address": "Центральная, 7"},
        ], ensure_ascii=False)

        self.assertEqual(
            publisher.parse_locations_json(raw),
            (
                publisher.LocationData("Москва", "Тверская, 10"),
                publisher.LocationData("Одинцово", "Центральная, 7"),
            ),
        )

    def test_validate_publish_form_rejects_wrong_location_count(self) -> None:
        fields = valid_fields(count=2)
        fields["locations_json"] = json.dumps([
            {"city": "Москва", "address": "Тверская, 10"},
        ], ensure_ascii=False)

        errors = publisher.validate_publish_form(
            fields, [GOOD_PHOTO], category_profiles.JACKETS,
        )

        self.assertIn("locations", {error["field"] for error in errors})

    def test_validate_publish_form_reports_one_based_blank_location_fields(self) -> None:
        fields = valid_fields(count=2)
        fields["locations_json"] = json.dumps([
            {"city": "Москва", "address": "Тверская, 10"},
            {"city": "  ", "address": ""},
        ], ensure_ascii=False)

        errors = publisher.validate_publish_form(
            fields, [GOOD_PHOTO], category_profiles.JACKETS,
        )

        self.assertEqual(
            {error["field"] for error in errors},
            {"locations.2.city", "locations.2.address"},
        )

    def test_validate_publish_form_rejects_unstructured_address(self) -> None:
        fields = valid_fields(count=2)
        fields["locations_json"] = json.dumps([
            {"city": "Санкт-Петербург", "address": "королева 26"},
            {"city": "Москва", "address": "Тверская, 10"},
        ], ensure_ascii=False)

        errors = publisher.validate_publish_form(
            fields, [GOOD_PHOTO], category_profiles.JACKETS,
        )

        self.assertIn("locations.1.address", {error["field"] for error in errors})
        self.assertNotIn("locations.2.address", {error["field"] for error in errors})

    def test_validate_publish_form_accepts_address_without_house_number(self) -> None:
        """Номер дома не обязателен — Авито принимает адрес до улицы."""
        fields = valid_fields(count=2)
        fields["locations_json"] = json.dumps([
            {"city": "Челябинск", "address": "Российская улица"},
            {"city": "Москва", "address": "Тверская,"},
        ], ensure_ascii=False)

        errors = publisher.validate_publish_form(
            fields, [GOOD_PHOTO], category_profiles.JACKETS,
        )

        address_errors = {
            error["field"] for error in errors if error["field"].endswith(".address")
        }
        self.assertEqual(address_errors, set())

    def test_validate_publish_form_rejects_non_array_and_non_object_json(self) -> None:
        cases = (
            ("{", "locations"),
            (json.dumps({"city": "Москва"}), "locations"),
            (json.dumps([{"city": "Москва", "address": "Тверская"}, "Одинцово"]), "locations.2"),
        )
        for locations_json, expected_field in cases:
            with self.subTest(locations_json=locations_json):
                fields = valid_fields(count=2)
                fields["locations_json"] = locations_json
                errors = publisher.validate_publish_form(
                    fields, [GOOD_PHOTO], category_profiles.JACKETS,
                )
                self.assertIn(expected_field, {error["field"] for error in errors})

    def test_legacy_city_and_address_do_not_replace_missing_locations(self) -> None:
        fields = valid_fields(count=1)
        fields.pop("locations_json")
        fields["city"] = "Москва"
        fields["address"] = "Тверская, 10"

        errors = publisher.validate_publish_form(
            fields, [GOOD_PHOTO], category_profiles.JACKETS,
        )

        self.assertIn("locations", {error["field"] for error in errors})

    def test_build_draft_data_keeps_locations_and_view_price_max_in_batch_order(self) -> None:
        fields = valid_fields(count=2)

        draft = publisher.build_draft_data(fields, ["photo.jpg"], category="jackets")

        self.assertEqual(draft.view_price_max, Decimal("2"))
        self.assertEqual(draft.locations[0].full_address(), "Москва, ул. Тверская, 10")
        self.assertEqual(draft.locations[1].full_address(), "Одинцово, ул. Центральная, 7")
        self.assertEqual(draft.location_for(1), draft.locations[0])
        self.assertEqual(draft.location_for(2), draft.locations[1])
        self.assertEqual(draft.summary()["view_price_max"], "2")
        self.assertEqual(draft.summary()["locations"], [
            {"city": "Москва", "address": "ул. Тверская, 10"},
            {"city": "Одинцово", "address": "ул. Центральная, 7"},
        ])


def make_draft(count: int = 3) -> publisher.DraftData:
    return publisher.DraftData(
        title="Пиджак Hugo Boss",
        trade_type="Продаю своё",
        condition="Отличное",
        size="50 (L)",
        brand="Hugo Boss",
        color="Чёрный",
        description="Почти не носили",
        price=15000,
        locations=tuple(
            publisher.LocationData(f"Город {index}", f"Улица {index}, 1")
            for index in range(1, count + 1)
        ),
        view_price_max=Decimal("0.5"),
        photo_paths=("photo.jpg",),
    )


class _LoopPage:
    url = "about:blank"

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url


class _LoopContext:
    def __init__(self) -> None:
        self.page = _LoopPage()

    async def new_page(self) -> _LoopPage:
        return self.page


class _PlaywrightManager:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


class PublishStateMachineTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_item_runs_publication_steps_in_order(self) -> None:
        calls: list[str] = []

        def step(name: str, result: object = None):
            async def run(*_args: object, **_kwargs: object) -> object:
                calls.append(name)
                return result
            return run

        draft = make_draft(1)
        job: dict[str, object] = {}
        profile = category_profiles.JACKETS
        loop_page = _LoopPage()
        with (
            mock.patch.object(publisher, "_pause", new=step("pause")),
            mock.patch.object(publisher, "_step_open_form", new=step("open_form", "form")),
            mock.patch.object(publisher, "_step_select_category", new=step("select_category")),
            mock.patch.object(publisher, "_step_check_category", new=step("check_category")),
            mock.patch.object(publisher, "_guard_against_reopened_draft", new=step("guard")),
            mock.patch.object(publisher, "_clear_and_type", new=step("fill_title")),
            mock.patch.object(publisher, "_step_upload_photos", new=step("upload_photos")),
            mock.patch.object(publisher, "_step_fill_fields", new=step("fill_fields")),
            mock.patch.object(publisher, "_step_fill_description", new=step("fill_description")),
            mock.patch.object(publisher, "_step_fill_price", new=step("fill_item_price")),
            mock.patch.object(publisher, "_step_fill_address", new=step("fill_address")),
            mock.patch.object(
                publisher,
                "_step_continue_listing",
                new=step("continue_listing", ("8330238411", loop_page)),
            ),
            mock.patch.object(
                publisher,
                "_step_fill_view_price",
                new=step("fill_view_price", Decimal("0.5")),
            ),
            mock.patch.object(publisher, "_step_continue_view_price", new=step("continue_view_price")),
            mock.patch.object(publisher, "_step_skip_services", new=step("skip_services")),
        ):
            result = await publisher._run_single_item(
                loop_page, job, draft, draft.location_for(1), 1, 1, profile,
                checkpoint_callback=lambda: calls.append(
                    f"checkpoint:{job.get('step')}"
                ),
            )

        self.assertEqual(result, (
            "8330238411",
            "https://www.avito.ru/items/edit/8330238411",
            Decimal("0.5"),
            None,
        ))
        meaningful = [name for name in calls if name != "pause"]
        self.assertEqual(meaningful, [
            "open_form",
            "select_category",
            "check_category",
            "guard",
            "fill_title",
            "upload_photos",
            "fill_fields",
            "fill_description",
            "fill_item_price",
            "fill_address",
            "checkpoint:continue_listing",
            "continue_listing",
            "fill_view_price",
            "continue_view_price",
            "skip_services",
        ])

    async def test_partial_failure_keeps_submitted_item_and_legacy_aliases(self) -> None:
        draft = make_draft(3)
        context = _LoopContext()
        seen: list[tuple[int, str, str, int]] = []

        async def run_item(
            _page: object,
            job: dict[str, object],
            _data: publisher.DraftData,
            location: publisher.LocationData,
            item_index: int,
            _items_total: int,
            _profile: object,
            *,
            active_page_ref: list[object] | None,
            checkpoint_callback: object,
        ) -> tuple[str, str, Decimal, dict[str, str] | None]:
            del active_page_ref, checkpoint_callback
            seen.append((
                item_index,
                location.city,
                _data.brand,
                int(job["items_published"]),
            ))
            if item_index == 2:
                raise publisher.StepError("Авито вернул неизвестный экран")
            item_id = f"833023841{item_index}"
            return (
                item_id,
                f"https://www.avito.ru/moskva/item_{item_id}",
                Decimal("1.7"),
                {
                    "requested": "Город 1, Улица 1, 1",
                    "applied": "Город 1",
                } if item_index == 1 else None,
            )

        job: dict[str, object] = {"items_total": 3}
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            mock.patch(
                "playwright.async_api.async_playwright",
                return_value=_PlaywrightManager(),
            ),
            mock.patch(
                "browser.connect_over_cdp",
                new=mock.AsyncMock(return_value=context),
            ),
            mock.patch.object(publisher, "_pause", new=mock.AsyncMock()),
            mock.patch.object(publisher, "_run_single_item", new=run_item),
            mock.patch.object(
                publisher,
                "_run_publish_preflight",
                new=mock.AsyncMock(return_value=publisher.NO_BRAND_LABEL),
            ),
            mock.patch.object(
                publisher, "_dump_failure", new=mock.AsyncMock(return_value=None),
            ),
            mock.patch.object(publisher, "DRAFT_PAUSE_MIN_S", 0.0),
            mock.patch.object(publisher, "DRAFT_PAUSE_MAX_S", 0.0),
        ):
            await publisher.run_publish_job(
                "partial-publication-test",
                job,
                draft,
                cdp_url="http://localhost:9222",
                tmp_dir=tmp_dir,
            )
            _restored_job, restored_draft = publish_state.load_publish_state(
                Path(tmp_dir)
            )

        self.assertEqual(seen, [
            (1, "Город 1", draft.brand, 0),
            (2, "Город 2", draft.brand, 1),
        ])
        self.assertEqual(restored_draft.brand, draft.brand)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["items_total"], 3)
        self.assertEqual(job["item_index"], 2)
        self.assertEqual(job["items_published"], 1)
        self.assertEqual(job["published_urls"], [
            "https://www.avito.ru/moskva/item_8330238411",
        ])
        self.assertEqual(job["applied_view_prices"], [{
            "item_index": 1,
            "price": "1.7",
        }])
        self.assertEqual(job["address_warnings"], [{
            "item_index": 1,
            "requested": "Город 1, Улица 1, 1",
            "applied": "Город 1",
        }])
        self.assertEqual(job["brand_selected"], publisher.NO_BRAND_LABEL)
        self.assertEqual(job["drafts_total"], 3)
        self.assertEqual(job["draft_index"], 2)
        self.assertEqual(job["drafts_saved"], 1)
        self.assertEqual(job["saved_urls"], job["published_urls"])
        self.assertIn("Отправлено 1 из 3", str(job["error"]))


if __name__ == "__main__":
    unittest.main()
