"""Контракт подтверждённого профиля «Демисезонные куртки»."""

import asyncio
import json
import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import category_profiles  # noqa: E402
import publisher  # noqa: E402
import app as app_module  # noqa: E402


class DemiJacketsProfileTest(unittest.TestCase):
    def test_profile_keeps_confirmed_category_path(self) -> None:
        profile = category_profiles.DEMI_JACKETS

        self.assertEqual(profile.key, "demi_jackets")
        self.assertEqual(profile.label, "Демисезонные куртки")
        self.assertEqual(profile.full_path, (
            "Личные вещи",
            "Одежда, обувь, аксессуары",
            "Мужская одежда",
            "Верхняя одежда",
            "Демисезонные куртки",
        ))
        self.assertEqual(profile.category_title_text, "Демисезонные куртки")

    def test_verified_profile_has_exact_dom_values(self) -> None:
        profile = category_profiles.DEMI_JACKETS

        self.assertTrue(category_profiles.DEMI_JACKETS_VERIFIED)
        self.assertIs(category_profiles.PROFILES["demi_jackets"], profile)
        self.assertIs(category_profiles.get_profile("demi_jackets"), profile)
        self.assertEqual(profile.expected_category, {
            "category_id": "27",
            "params[175]": "748",
            "params[176]": "751",
            "params[114145]": "908138",
        })
        self.assertIs(profile.size_options, category_profiles._JACKETS_SIZE_OPTIONS)
        self.assertEqual(profile.size_prefix, "razmer")
        self.assertIs(profile.color_options, category_profiles._COLOR_OPTIONS)
        self.assertEqual(profile.color_prefix, "cvet")
        self.assertIs(profile.trade_type_options, category_profiles._TRADE_TYPE_OPTIONS)
        self.assertEqual(profile.trade_type_prefix, "type_of_trade")
        self.assertIs(profile.condition_options, category_profiles._CONDITION_OPTIONS)
        self.assertEqual(profile.condition_param, 110385)
        self.assertEqual(profile.brand_param, 115634)

    def test_api_exposes_verified_category(self) -> None:
        response = asyncio.run(app_module.api_publish_categories())
        categories = {
            item["key"]: item
            for item in json.loads(response.body)["categories"]
        }
        self.assertEqual(categories["demi_jackets"]["label"], "Демисезонные куртки")
        self.assertEqual(categories["demi_jackets"]["path"], list(category_profiles.DEMI_JACKETS.full_path))
        self.assertEqual(categories["demi_jackets"]["sizes"], list(category_profiles._JACKETS_SIZE_OPTIONS))

    def test_valid_form_passes_validation_for_demi_jackets(self) -> None:
        fields = {
            "title": "Демисезонная куртка Lacoste",
            "trade_type": "Продаю своё",
            "condition": "Отличное",
            "size": "48 (M)",
            "brand": "Lacoste",
            "color": "Чёрный",
            "description": "Демисезонная куртка в отличном состоянии.",
            "price": "15000",
            "locations_json": json.dumps([{
                "city": "Москва", "address": "ул. Арбат, 1",
            }], ensure_ascii=False),
            "view_price_max": "0,5",
        }
        photos = [("jacket.jpg", "image/jpeg", 1024 * 1024)]

        self.assertEqual(
            publisher.validate_publish_form(fields, photos, category_profiles.DEMI_JACKETS),
            [],
        )


if __name__ == "__main__":
    unittest.main()
