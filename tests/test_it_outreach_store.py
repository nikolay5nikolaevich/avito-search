"""Проверки SQLite-хранилища результатов IT-рассылки."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

import it_outreach_store  # noqa: E402


class ItOutreachStoreTests(unittest.TestCase):
    """Контракты дедупликации и сохранности найденных контактов."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.temp_dir.name) / "it_outreach.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_retry_error_updates_check_without_losing_found_email(self) -> None:
        """Ловит замену найденного email пустыми данными при ошибке повторного обхода."""
        it_outreach_store.upsert_company(
            {
                "domain": "example.ru",
                "company_name": "Пример",
                "city": "Москва",
                "site_url": "https://example.ru",
                "vacancy_url": "https://example.ru/jobs",
                "internship_url": "https://example.ru/internships",
                "email": "jobs@example.ru",
                "email_kind": "careers",
                "source_url": "https://example.ru/careers",
                "status": "found",
            },
            self.db_path,
        )
        first = it_outreach_store.list_companies(self.db_path)[0]

        it_outreach_store.upsert_company(
            {
                "domain": "example.ru",
                "status": "error",
                "error": "таймаут",
            },
            self.db_path,
        )
        companies = it_outreach_store.list_companies(self.db_path)

        self.assertEqual(len(companies), 1)
        self.assertEqual(companies[0]["company_name"], "Пример")
        self.assertEqual(companies[0]["city"], "Москва")
        self.assertEqual(companies[0]["site_url"], "https://example.ru")
        self.assertEqual(companies[0]["vacancy_url"], "https://example.ru/jobs")
        self.assertEqual(companies[0]["internship_url"], "https://example.ru/internships")
        self.assertEqual(companies[0]["email"], "jobs@example.ru")
        self.assertEqual(companies[0]["source_url"], "https://example.ru/careers")
        self.assertEqual(companies[0]["status"], "error")
        self.assertEqual(companies[0]["error"], "таймаут")
        self.assertNotEqual(companies[0]["checked_at"], first["checked_at"])

    def test_lists_newer_checks_first(self) -> None:
        """Ловит выдачу истории в порядке старых проверок."""
        it_outreach_store.upsert_company({"domain": "old.ru", "status": "found"}, self.db_path)
        it_outreach_store.upsert_company({"domain": "new.ru", "status": "found"}, self.db_path)

        self.assertEqual(
            [company["domain"] for company in it_outreach_store.list_companies(self.db_path)],
            ["new.ru", "old.ru"],
        )


if __name__ == "__main__":
    unittest.main()
