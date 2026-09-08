"""Контракт подтверждённого профиля «Жилеты»."""

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


class VestsProfileTest(unittest.TestCase):
    def test_profile_keeps_declared_category_path(self) -> None:
        profile = category_profiles.VESTS

        self.assertEqual(profile.key, "vests")
        self.assertEqual(profile.label, "Жилеты")
        self.assertEqual(profile.full_path, (
            "Личные вещи",
            "Одежда, обувь, аксессуары",
            "Мужская одежда",
            "Верхняя одежда",
            "Жилеты",
        ))
        self.assertEqual(profile.category_title_text, "Жилеты")

    @unittest.skipIf(
        category_profiles.VESTS_VERIFIED,
        "профиль подтверждён разведкой — этот тест только для непроверенной фазы",
    )
    def test_unverified_profile_stays_out_of_registry(self) -> None:
        self.assertNotIn("vests", category_profiles.PROFILES)
        self.assertIs(category_profiles.get_profile("vests"), category_profiles.JACKETS)

        response = asyncio.run(app_module.api_publish_categories())
        categories = {
            item["key"]: item
            for item in json.loads(response.body)["categories"]
        }
        self.assertNotIn("vests", categories)

    @unittest.skipUnless(
        category_profiles.VESTS_VERIFIED,
        "профиль ещё не подтверждён живой разведкой (debug/recon_vests.py)",
    )
    def test_verified_profile_has_exact_dom_values(self) -> None:
        profile = category_profiles.VESTS

        self.assertEqual(profile.expected_category, {
            "category_id": "27",
            "params[175]": "748",
            "params[176]": "751",
            "params[114145]": "908141",
        })
        self.assertIs(category_profiles.PROFILES.get("vests"), profile)
        self.assertIs(profile.size_options, category_profiles._JACKETS_SIZE_OPTIONS)
        self.assertEqual(profile.size_prefix, "razmer")
        self.assertIs(profile.color_options, category_profiles._COLOR_OPTIONS)
        self.assertEqual(profile.condition_param, 110385)
        self.assertEqual(profile.brand_param, 115634)
        self.assertEqual(len(profile.material_options), 54)
        self.assertEqual(len(profile.style_options), 5)
        self.assertEqual(profile.material_options["Нейлон"], 3263861)
        self.assertEqual(profile.style_options["Повседневный"], 3360005)

        response = asyncio.run(app_module.api_publish_categories())
        categories = {
            item["key"]: item
            for item in json.loads(response.body)["categories"]
        }
        self.assertIn("vests", categories)

    @unittest.skipUnless(
        category_profiles.VESTS_VERIFIED,
        "профиль ещё не подтверждён живой разведкой (debug/recon_vests.py)",
    )
    def test_api_exposes_verified_category(self) -> None:
        response = asyncio.run(app_module.api_publish_categories())
        categories = {
            item["key"]: item
            for item in json.loads(response.body)["categories"]
        }
        self.assertEqual(categories["vests"]["label"], "Жилеты")
        self.assertEqual(categories["vests"]["path"], list(category_profiles.VESTS.full_path))
        self.assertEqual(categories["vests"]["sizes"], list(category_profiles._JACKETS_SIZE_OPTIONS))

        self.assertEqual(len(categories["vests"]["materials"]), 54)
        self.assertEqual(len(categories["vests"]["styles"]), 5)
        self.assertEqual(categories["jackets"]["materials"], [])
        self.assertEqual(categories["jackets"]["styles"], [])
        self.assertEqual(categories["demi_jackets"]["materials"], [])
        self.assertEqual(categories["demi_jackets"]["styles"], [])

    def test_valid_form_passes_validation_for_vests(self) -> None:
        fields = {
            "title": "Жилет C.P. Company",
            "trade_type": "Продаю своё",
            "condition": "Отличное",
            "size": "50 (L)",
            "brand": "C.P. Company",
            "color": "Чёрный",
            "description": "Жилет в отличном состоянии.",
            "price": "15000",
            "locations_json": json.dumps([{
                "city": "Москва", "address": "ул. Арбат, 1",
            }], ensure_ascii=False),
            "view_price_max": "0,5",
            "material": "Нейлон",
            "style": "Повседневный",
        }
        photos = [("vest.jpg", "image/jpeg", 1024 * 1024)]

        self.assertEqual(
            publisher.validate_publish_form(fields, photos, category_profiles.VESTS),
            [],
        )

    def test_missing_material_and_style_are_rejected_for_vests(self) -> None:
        fields = {
            "title": "Жилет C.P. Company",
            "trade_type": "Продаю своё",
            "condition": "Отличное",
            "size": "50 (L)",
            "brand": "C.P. Company",
            "color": "Чёрный",
            "description": "Жилет в отличном состоянии.",
            "price": "15000",
            "locations_json": json.dumps([{
                "city": "Москва", "address": "ул. Арбат, 1",
            }], ensure_ascii=False),
            "view_price_max": "0,5",
            # material и style намеренно не заданы
        }
        photos = [("vest.jpg", "image/jpeg", 1024 * 1024)]

        errors = publisher.validate_publish_form(fields, photos, category_profiles.VESTS)
        self.assertGreaterEqual(
            {e["field"] for e in errors}, {"material", "style"},
        )


if __name__ == "__main__":
    unittest.main()
