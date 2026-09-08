"""Контракт API-статуса полной публикации."""

import os
import sys
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import app  # noqa: E402


class PublishStatusContractTests(unittest.TestCase):
    def test_new_publish_fields_are_authoritative_and_aliases_match(self) -> None:
        job = {
            "status": "running",
            "step": "open_next_form",
            "step_label": "Переход к следующему объявлению",
            "done": 15,
            "total": 16,
            "error": None,
            "debug_dir": None,
            "items_total": 3,
            "item_index": 2,
            "items_published": 1,
            "published_urls": ["https://www.avito.ru/test_100"],
            "user_action": None,
            "brand_selected": "Без бренда",
            "applied_view_prices": [{"item_index": 1, "price": "1.7"}],
            "address_warnings": [{
                "item_index": 1,
                "requested": "Мурманск, улица Воровского, 11",
                "applied": "Мурманск",
            }],
            # Намеренно устаревшие алиасы: сериализатор не должен им верить.
            "drafts_total": 99,
            "draft_index": 99,
            "drafts_saved": 99,
            "saved_urls": ["stale"],
        }

        payload = app._serialize_publish_status("job-1", job)

        self.assertEqual(payload["items_total"], 3)
        self.assertEqual(payload["item_index"], 2)
        self.assertEqual(payload["items_published"], 1)
        self.assertEqual(payload["published_urls"], ["https://www.avito.ru/test_100"])
        self.assertEqual(payload["drafts_total"], 3)
        self.assertEqual(payload["draft_index"], 2)
        self.assertEqual(payload["drafts_saved"], 1)
        self.assertEqual(payload["saved_urls"], payload["published_urls"])
        self.assertEqual(payload["brand_selected"], "Без бренда")
        self.assertEqual(payload["applied_view_prices"], [
            {"item_index": 1, "price": "1.7"},
        ])
        self.assertEqual(payload["address_warnings"][0]["applied"], "Мурманск")

    def test_structured_user_action_is_exposed(self) -> None:
        action = {
            "code": "view_price_too_low",
            "item_index": 2,
            "requested": "0.5",
            "minimum": "26",
            "message": "Увеличьте стоимость просмотра и запустите пакет заново.",
        }
        payload = app._serialize_publish_status(
            "job-2",
            {
                "status": "needs_user_action",
                "items_total": 3,
                "item_index": 2,
                "items_published": 1,
                "published_urls": ["https://www.avito.ru/test_100"],
                "user_action": action,
            },
        )

        self.assertEqual(payload["user_action"], action)

    def test_resume_availability_is_derived_from_the_safe_checkpoint_boundary(self) -> None:
        # До continue_listing — retry_item: можно спокойно повторить текущее.
        safe = app._serialize_publish_status(
            "job-safe",
            {
                "status": "needs_user_action",
                "step": "open_form",
                "items_total": 13,
                "item_index": 10,
                "items_published": 9,
            },
        )
        # После continue_listing, но не последнее объявление — skip_item:
        # текущее уже создано на Авито, продолжаем со следующего.
        skippable = app._serialize_publish_status(
            "job-skippable",
            {
                "status": "needs_user_action",
                "step": "continue_listing",
                "items_total": 13,
                "item_index": 10,
                "items_published": 9,
            },
        )
        # После continue_listing на ПОСЛЕДНЕМ объявлении — пропускать нечего,
        # продолжения не будет.
        nothing_left = app._serialize_publish_status(
            "job-nothing-left",
            {
                "status": "needs_user_action",
                "step": "continue_listing",
                "items_total": 13,
                "item_index": 13,
                "items_published": 12,
            },
        )

        self.assertIs(safe["resume_available"], True)
        self.assertEqual(safe["resume_plan"]["mode"], "retry_item")
        self.assertEqual(safe["resume_plan"]["start_index"], 10)

        self.assertIs(skippable["resume_available"], True)
        self.assertEqual(skippable["resume_plan"]["mode"], "skip_item")
        self.assertEqual(skippable["resume_plan"]["start_index"], 11)
        self.assertEqual(skippable["resume_plan"]["skipped_item"], 10)

        self.assertIs(nothing_left["resume_available"], False)
        self.assertIsNone(nothing_left["resume_plan"])

    def test_skipped_items_are_exposed_and_default_to_empty(self) -> None:
        without = app._serialize_publish_status(
            "job-no-skips",
            {"status": "running", "items_total": 3, "item_index": 1, "items_published": 0},
        )
        with_skips = app._serialize_publish_status(
            "job-with-skips",
            {
                "status": "failed",
                "items_total": 13,
                "item_index": 8,
                "items_published": 6,
                "skipped_items": [7],
            },
        )

        self.assertEqual(without["skipped_items"], [])
        self.assertEqual(with_skips["skipped_items"], [7])


if __name__ == "__main__":
    unittest.main()
