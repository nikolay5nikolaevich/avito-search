"""SQLite-хранилище результатов поиска IT-контактов."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
from typing import Iterator


_FIELDS = (
    "domain",
    "company_name",
    "city",
    "site_url",
    "vacancy_url",
    "internship_url",
    "email",
    "email_kind",
    "source_url",
    "status",
    "error",
)


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Открывает одно SQLite-подключение на публичный вызов."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _create_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            domain TEXT PRIMARY KEY,
            company_name TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            site_url TEXT NOT NULL DEFAULT '',
            vacancy_url TEXT NOT NULL DEFAULT '',
            internship_url TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            email_kind TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL
        )
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_company(record: dict, db_path: str = "it_outreach.db") -> None:
    """Сохраняет результат проверки компании, обновляя запись того же домена."""
    values = {field: str(record.get(field) or "") for field in _FIELDS}
    if not values["domain"]:
        raise ValueError("Нужен domain компании.")
    values["checked_at"] = _now()
    with _connect(db_path) as connection:
        _create_table(connection)
        connection.execute(
            """
            INSERT INTO companies (
                domain, company_name, city, site_url, vacancy_url, internship_url,
                email, email_kind, source_url, status, error, checked_at
            ) VALUES (
                :domain, :company_name, :city, :site_url, :vacancy_url, :internship_url,
                :email, :email_kind, :source_url, :status, :error, :checked_at
            )
            ON CONFLICT(domain) DO UPDATE SET
                company_name = CASE WHEN excluded.status = 'error' AND companies.company_name != ''
                    THEN companies.company_name ELSE excluded.company_name END,
                city = CASE WHEN excluded.status = 'error' AND companies.city != ''
                    THEN companies.city ELSE excluded.city END,
                site_url = CASE WHEN excluded.status = 'error' AND companies.site_url != ''
                    THEN companies.site_url ELSE excluded.site_url END,
                vacancy_url = CASE WHEN excluded.status = 'error' AND companies.vacancy_url != ''
                    THEN companies.vacancy_url ELSE excluded.vacancy_url END,
                internship_url = CASE WHEN excluded.status = 'error' AND companies.internship_url != ''
                    THEN companies.internship_url ELSE excluded.internship_url END,
                email = CASE WHEN excluded.status = 'error' AND companies.email != ''
                    THEN companies.email ELSE excluded.email END,
                email_kind = CASE WHEN excluded.status = 'error' AND companies.email != ''
                    THEN companies.email_kind ELSE excluded.email_kind END,
                source_url = CASE WHEN excluded.status = 'error' AND companies.email != ''
                    THEN companies.source_url ELSE excluded.source_url END,
                status = excluded.status,
                error = excluded.error,
                checked_at = excluded.checked_at
            """,
            values,
        )


def list_companies(db_path: str = "it_outreach.db") -> list[dict]:
    """Возвращает результаты от новых проверок к старым."""
    with _connect(db_path) as connection:
        _create_table(connection)
        rows = connection.execute(
            "SELECT * FROM companies ORDER BY checked_at DESC, rowid DESC"
        ).fetchall()
    return [dict(row) for row in rows]
