"""Проверки постоянной истории контактов рассылки."""

from __future__ import annotations

import pathlib
import os
import sys
import tempfile
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

import outreach_store  # noqa: E402


BRAND_URL = (
    "https://www.avito.ru/brands/test-store/all?src=messenger"
    "&sellerId=00000000000000000000000000000001"
)
USER_URL = (
    "https://www.avito.ru/user/c6aaa5dfd61ea32ecba46f28bc6a9e7e/"
    "profile/all?page_from=from_u2u_messenger"
)


class OutreachStoreTests(unittest.TestCase):
    """Контракты нормализации и дедупликации продавцов."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.temp_dir.name) / "outreach.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_normalizes_brand_and_user_profiles_without_navigation_marks(self) -> None:
        """Сломается, если URL с параметрами или хвостом снова считают разными."""
        self.assertEqual(
            outreach_store.normalize_profile_url(BRAND_URL),
            "https://www.avito.ru/brands/test-store",
        )
        self.assertEqual(
            outreach_store.normalize_profile_url(USER_URL),
            "https://www.avito.ru/user/c6aaa5dfd61ea32ecba46f28bc6a9e7e/profile",
        )

    def test_extracts_seller_id_from_query_or_user_profile(self) -> None:
        """Сломается, если одно из двух надёжных представлений ID не распознаётся."""
        self.assertEqual(
            outreach_store.extract_seller_id(BRAND_URL),
            "00000000000000000000000000000001",
        )
        self.assertEqual(
            outreach_store.extract_seller_id(USER_URL),
            "c6aaa5dfd61ea32ecba46f28bc6a9e7e",
        )
        self.assertEqual(outreach_store.normalize_profile_url("https://example.com/no"), "")
        self.assertEqual(outreach_store.seller_key("https://example.com/no"), "")
        self.assertEqual(
            outreach_store.seller_key(USER_URL),
            "seller:c6aaa5dfd61ea32ecba46f28bc6a9e7e",
        )

    def test_import_is_idempotent_and_reports_invalid_urls(self) -> None:
        """Сломается, если второй импорт создаёт ещё одну блокирующую запись."""
        first = outreach_store.import_contact_urls([BRAND_URL, "https://example.com/no"], self.db_path)
        second = outreach_store.import_contact_urls([BRAND_URL], self.db_path)

        self.assertEqual(first, {"imported": 1, "skipped": 0, "invalid": ["https://example.com/no"]})
        self.assertEqual(second, {"imported": 0, "skipped": 1, "invalid": []})

    def test_matches_contact_by_seller_id_or_normalized_profile(self) -> None:
        """Сломается, если новый URL того же продавца попадёт в рассылку."""
        outreach_store.init_db(self.db_path)
        outreach_store.record_contact(
            {
                "seller_key": "same-seller",
                "seller_id": "same-seller",
                "profile_url": "https://www.avito.ru/brands/store-one/all?src=search",
            },
            "sent",
            db_path=self.db_path,
        )

        self.assertTrue(
            outreach_store.is_contacted(
                "same-seller", "https://www.avito.ru/user/same-seller/profile", self.db_path
            )
        )
        self.assertTrue(
            outreach_store.is_contacted(
                None, "https://www.avito.ru/brands/store-one", self.db_path
            )
        )

    def test_failed_contact_remains_eligible_but_in_progress_and_uncertain_block(self) -> None:
        """Сломается, если retry безопасной ошибки заблокирован или риск дубля пропущен."""
        candidate = {
            "seller_key": "retry-seller",
            "seller_id": "retry-seller",
            "profile_url": "https://www.avito.ru/user/retry-seller/profile",
        }
        outreach_store.init_db(self.db_path)
        outreach_store.record_contact(candidate, "failed", error="чат не открылся", db_path=self.db_path)
        self.assertFalse(outreach_store.is_contacted("retry-seller", candidate["profile_url"], self.db_path))

        outreach_store.record_contact(candidate, "sending", db_path=self.db_path)
        self.assertTrue(outreach_store.is_contacted("retry-seller", candidate["profile_url"], self.db_path))
        outreach_store.record_contact(candidate, "uncertain", db_path=self.db_path)
        self.assertTrue(outreach_store.is_contacted("retry-seller", candidate["profile_url"], self.db_path))

    def test_init_db_seeds_the_supplied_sellers_only_once(self) -> None:
        """Сломается, если повторная инициализация дублирует исходный стоп-лист."""
        outreach_store.init_db(self.db_path)
        once = outreach_store.list_contacts(self.db_path)
        outreach_store.init_db(self.db_path)

        self.assertEqual(len(once), 10)
        self.assertEqual(len(outreach_store.list_contacts(self.db_path)), 10)

    def test_public_functions_use_the_default_local_database(self) -> None:
        """Сломается, если worker без db_path падает до проверки стоп-листа."""
        previous_dir = os.getcwd()
        os.chdir(self.temp_dir.name)
        try:
            self.assertEqual(outreach_store.import_contact_urls([BRAND_URL])["imported"], 1)
            self.assertTrue(outreach_store.is_contacted("00000000000000000000000000000001", BRAND_URL))
            self.assertEqual(len(outreach_store.list_contacts()), 11)
        finally:
            os.chdir(previous_dir)


if __name__ == "__main__":
    unittest.main()
