"""SQLite-история контактов для рассылки продавцам Авито."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
from typing import Any, Iterable, Iterator
from urllib.parse import parse_qs, urlsplit


SEED_URLS = (
    "https://www.avito.ru/brands/i146938103/all/bytovaya_elektronika?page_from=from_u2u_messenger&src=messenger&sellerId=fe94da6377a0793d0e9d0d70d633bbe6",
    "https://www.avito.ru/brands/i2359392/all?page_from=from_u2u_messenger&src=messenger&sellerId=e4304d869b38198bfa0d6883b846e7c0",
    "https://www.avito.ru/user/c6aaa5dfd61ea32ecba46f28bc6a9e7e/profile/all?page_from=from_u2u_messenger&src=messenger&sellerId=c6aaa5dfd61ea32ecba46f28bc6a9e7e",
    "https://www.avito.ru/brands/iphoneberry/all?page_from=from_u2u_messenger&src=messenger&sellerId=f099c22bdbb687dc0563573e12edf0d0",
    "https://www.avito.ru/user/10a1b8c9c72c9b1b20e63460571407c2/profile/all?page_from=from_u2u_messenger&src=messenger&sellerId=10a1b8c9c72c9b1b20e63460571407c2",
    "https://www.avito.ru/user/e712362f0bf30e0e3eb95d1c684d1d39/profile/all/telefony?page_from=from_u2u_messenger&src=messenger&sellerId=e712362f0bf30e0e3eb95d1c684d1d39",
    "https://www.avito.ru/brands/playshop/all?page_from=from_u2u_messenger&src=messenger&sellerId=26ffcf83a772c37447fd7de4f8209674",
    "https://www.avito.ru/user/23e0c2a6ba6d40d6559452559e853d2d/profile/all/bytovaya_elektronika?page_from=from_u2u_messenger&src=messenger&sellerId=23e0c2a6ba6d40d6559452559e853d2d",
    "https://www.avito.ru/user/6ca60bfce674cfbc68a2994d112e5fbf/profile/all/telefony?page_from=from_u2u_messenger&src=messenger&sellerId=6ca60bfce674cfbc68a2994d112e5fbf",
    "https://www.avito.ru/brands/ultrastore/all/bytovaya_elektronika?page_from=from_u2u_messenger&src=messenger&sellerId=023e58b4728f76be8e9623969e1bdc3f",
)

_BLOCKING_STATUSES = ("imported", "sending", "sent", "uncertain")


def normalize_profile_url(url: str) -> str:
    """Возвращает устойчивый профильный URL без поисковых и навигационных меток."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "avito.ru", "www.avito.ru",
    }:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "brands":
        return f"https://www.avito.ru/brands/{parts[1]}"
    if len(parts) >= 3 and parts[0] == "user" and parts[2] == "profile":
        return f"https://www.avito.ru/user/{parts[1]}/profile"
    return ""


def extract_seller_id(url: str) -> str:
    """Извлекает sellerId из URL или устойчивого адреса пользовательского профиля."""
    parsed = urlsplit(url.strip())
    seller_id = parse_qs(parsed.query).get("sellerId", [""])[0].strip()
    if seller_id:
        return seller_id
    normalized = normalize_profile_url(url)
    if normalized and "/user/" in normalized:
        return normalized.split("/")[4]
    return ""


def seller_key(url: str) -> str:
    """Выбирает более надёжный sellerId, иначе канонический профильный URL."""
    seller_id = extract_seller_id(url)
    if seller_id:
        return f"seller:{seller_id}"
    profile_url = normalize_profile_url(url)
    return f"profile:{profile_url}" if profile_url else ""


@contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
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
        CREATE TABLE IF NOT EXISTS contacts (
            seller_key TEXT PRIMARY KEY,
            seller_id TEXT,
            profile_url TEXT,
            seller_name TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL DEFAULT '',
            listing_url TEXT NOT NULL DEFAULT '',
            review_count INTEGER,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )


def _find_contact(
    connection: sqlite3.Connection, seller_id: str | None, profile_url: str | None
) -> sqlite3.Row | None:
    clauses: list[str] = []
    values: list[str] = []
    if seller_id:
        clauses.append("seller_id = ?")
        values.append(seller_id)
    if profile_url:
        clauses.append("profile_url = ?")
        values.append(profile_url)
    if not clauses:
        return None
    return connection.execute(
        f"SELECT * FROM contacts WHERE {' OR '.join(clauses)} LIMIT 1", values
    ).fetchone()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _import_one(connection: sqlite3.Connection, url: str) -> bool:
    profile_url = normalize_profile_url(url)
    seller_id = extract_seller_id(url)
    key = seller_key(url)
    if not key:
        return False
    existing = _find_contact(connection, seller_id, profile_url)
    if existing:
        if existing["status"] == "failed":
            connection.execute(
                "UPDATE contacts SET status = 'imported', error = '' WHERE seller_key = ?",
                (existing["seller_key"],),
            )
            return True
        return False
    connection.execute(
        """
        INSERT INTO contacts (seller_key, seller_id, profile_url, status, created_at)
        VALUES (?, ?, ?, 'imported', ?)
        """,
        (key, seller_id, profile_url, _now()),
    )
    return True


def init_db(db_path: str = "outreach.db") -> None:
    """Создаёт историю и однократно заносит переданный пользователем стоп-лист."""
    with _connect(db_path) as connection:
        _create_table(connection)
        for url in SEED_URLS:
            _import_one(connection, url)


def import_contact_urls(
    urls: Iterable[str], db_path: str = "outreach.db"
) -> dict[str, Any]:
    """Импортирует уже обработанных продавцов, не создавая дубликаты."""
    init_db(db_path)
    imported = 0
    skipped = 0
    invalid: list[str] = []
    with _connect(db_path) as connection:
        for url in urls:
            if not seller_key(url):
                invalid.append(url)
            elif _import_one(connection, url):
                imported += 1
            else:
                skipped += 1
    return {"imported": imported, "skipped": skipped, "invalid": invalid}


def is_contacted(
    seller_id: str | None, profile_url: str | None, db_path: str = "outreach.db"
) -> bool:
    """Проверяет блокирующую историю по sellerId или каноническому профилю."""
    init_db(db_path)
    normalized = normalize_profile_url(profile_url or "")
    with _connect(db_path) as connection:
        contact = _find_contact(connection, seller_id, normalized)
    return bool(contact and contact["status"] in _BLOCKING_STATUSES)


def record_contact(
    candidate: dict[str, Any],
    status: str,
    message: str = "",
    error: str = "",
    db_path: str = "outreach.db",
) -> None:
    """Сохраняет итог попытки отправки, объединяя записи одного продавца."""
    profile_url = normalize_profile_url(str(candidate.get("profile_url", "")))
    seller_id = candidate.get("seller_id") or extract_seller_id(str(candidate.get("profile_url", "")))
    key = candidate.get("seller_key") or (
        f"seller:{seller_id}" if seller_id else f"profile:{profile_url}" if profile_url else ""
    )
    if not key:
        raise ValueError("Нужен seller_id или корректный profile_url кандидата.")
    init_db(db_path)
    with _connect(db_path) as connection:
        existing = _find_contact(connection, seller_id, profile_url)
        values = {
            "seller_key": key,
            "seller_id": seller_id,
            "profile_url": profile_url,
            "seller_name": candidate.get("seller_name", ""),
            "city": candidate.get("city", ""),
            "query": candidate.get("query", ""),
            "listing_url": candidate.get("listing_url", ""),
            "review_count": candidate.get("review_count"),
            "status": status,
            "message": message,
            "error": error,
        }
        if existing:
            connection.execute(
                """
                UPDATE contacts SET seller_id = :seller_id, profile_url = :profile_url,
                    seller_name = :seller_name, city = :city, query = :query,
                    listing_url = :listing_url, review_count = :review_count,
                    status = :status, message = :message, error = :error
                WHERE seller_key = :existing_key
                """,
                values | {"existing_key": existing["seller_key"]},
            )
        else:
            connection.execute(
                """
                INSERT INTO contacts (
                    seller_key, seller_id, profile_url, seller_name, city, query,
                    listing_url, review_count, status, message, error, created_at
                ) VALUES (
                    :seller_key, :seller_id, :profile_url, :seller_name, :city, :query,
                    :listing_url, :review_count, :status, :message, :error, :created_at
                )
                """,
                values | {"created_at": _now()},
            )


def list_contacts(db_path: str = "outreach.db") -> list[dict[str, Any]]:
    """Возвращает историю от новых записей к старым."""
    init_db(db_path)
    with _connect(db_path) as connection:
        rows = connection.execute("SELECT * FROM contacts ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]
