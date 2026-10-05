"""
Сборщик статистики (Этап 2, docs/specs/agents-registry.md).

За один запуск: токен API Авито → список активных объявлений →
статистика показов/контактов/избранного за период → обогащение данными
из listings (prep_id, вариант, город) → агрегаты по городам и вариантам →
снимок в agents.db. С SQLite сам не работает — только через journal.py
(единая точка доступа к agents.db).

HTTP — stdlib urllib, новых зависимостей нет. run() — блокирующая функция
(синхронные сетевые вызовы), вызывающий код (app.py) обязан запускать её
через asyncio.to_thread, иначе event loop замрёт на время запросов к Авито.

Реальные имена полей и формат ответов — из debug/stats_probe/*.json
(разведка 15.09.2026, docs/_internal/briefs/agents-registry/01-stats-probe.md), а не
из памяти о документации Авито (там, где спека и ответы расходятся, правы
ответы — см. docs/_internal/briefs/agents-registry/05-stats-collector.md).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, timedelta
from typing import Any, Optional

import journal
from cities import CITIES, City

logger = logging.getLogger(__name__)

BASE_URL = "https://api.avito.ru"
ITEMS_PER_PAGE = 100

# Лимит itemIds на запрос статистики — НЕ подтверждён на живом API: разведка
# (Этап 0) проверила только 3 id. 200 — осторожная оценка по документации
# Авито, реальный лимит остаётся непроверенным (см. отчёт брифа 05).
STATS_BATCH_SIZE = 200

# Только запросы статистики помечены как безопасные для повтора (см.
# «Решения, которых нет в спеке» брифа 05: «Запросы статистики — только
# чтение, поэтому их можно повторять»). Токен и список объявлений — не ретраим.
RETRY_ATTEMPTS = 2
RETRY_PAUSE_S = 1.0
REQUEST_TIMEOUT_S = 30


class StatsCollectorError(RuntimeError):
    """Понятная ошибка Сборщика — оборачивается в run_failed журнала в app.py."""


# ── HTTP ──────────────────────────────────────────────────────────────────

def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    data: Optional[bytes] = None,
    retry: bool = False,
) -> Any:
    """Выполняет HTTP-запрос и разбирает JSON-тело. Бросает StatsCollectorError
    при сбое (после ретраев, если retry=True)."""
    attempts = RETRY_ATTEMPTS if retry else 1
    last_exc: Optional[StatsCollectorError] = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_exc = StatsCollectorError(f"HTTP {exc.code} {method} {url}: {body[:300]}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_exc = StatsCollectorError(f"Ошибка запроса {method} {url}: {exc}")
        if attempt < attempts:
            logger.warning("Повтор запроса %s %s после ошибки: %s", method, url, last_exc)
            time.sleep(RETRY_PAUSE_S)
    assert last_exc is not None
    raise last_exc


def _fetch_token(client_id: str, client_secret: str) -> str:
    """POST /token (client_credentials). Формат ответа — debug/stats_probe/01_token.json."""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    payload = _http_json(
        "POST", f"{BASE_URL}/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not token:
        raise StatsCollectorError("Авито не вернул access_token")
    return token


def _fetch_account_user_id(token: str) -> str:
    """GET /core/v1/accounts/self → user_id. Формат — debug/stats_probe/02_accounts_self.json."""
    payload = _http_json(
        "GET", f"{BASE_URL}/core/v1/accounts/self",
        headers={"Authorization": f"Bearer {token}"},
    )
    user_id: Optional[str] = None
    if isinstance(payload, dict):
        for key in ("id", "user_id", "account_id"):
            if key in payload:
                user_id = str(payload[key])
                break
    if not user_id:
        raise StatsCollectorError("Авито не вернул id аккаунта")
    return user_id


def _fetch_items(token: str) -> list[dict[str, Any]]:
    """
    GET /core/v1/items?per_page=100&status=active — свои активные объявления.

    Формат ответа — debug/stats_probe/03_items.json: {"meta": {...},
    "resources": [...]}. Разведка получила 32 объявления при per_page=100 без
    второй страницы, поэтому пагинация за пределами первой страницы НЕ
    проверена на живом API — цикл по page добавлен на будущее (остановится
    сам, как только страница вернёт меньше ITEMS_PER_PAGE записей), но имя
    параметра "page" не подтверждено ответом.
    """
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"{BASE_URL}/core/v1/items?" + urllib.parse.urlencode({
            "per_page": ITEMS_PER_PAGE, "status": "active", "page": page,
        })
        payload = _http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
        resources = payload.get("resources") if isinstance(payload, dict) else None
        if not isinstance(resources, list):
            break
        page_items = [r for r in resources if isinstance(r, dict)]
        items.extend(page_items)
        if len(page_items) < ITEMS_PER_PAGE:
            break
        page += 1
    return items


def _fetch_stats(
    token: str,
    user_id: str,
    item_ids: list[Any],
    date_from: str,
    date_to: str,
) -> dict[str, list[dict[str, Any]]]:
    """
    POST /stats/v1/accounts/{user_id}/items, пачками по STATS_BATCH_SIZE.

    Формат тела и ответа — debug/stats_probe/04_stats.json: тело
    {dateFrom, dateTo, fields, itemIds, periodGrouping}, ответ
    {"result": {"items": [{"itemId", "stats": [{"date", "uniqViews", ...}]}]}}.
    Дни без активности в stats API не отдаёт вовсе (не нули) — это учтено
    в build_snapshot_data через простое суммирование по факту присутствия.

    Возвращает {item_id (str): stats_list}.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(item_ids), STATS_BATCH_SIZE):
        batch = item_ids[start:start + STATS_BATCH_SIZE]
        body = json.dumps({
            "dateFrom": date_from,
            "dateTo": date_to,
            "fields": ["uniqViews", "uniqContacts", "uniqFavorites"],
            "itemIds": batch,
            "periodGrouping": "day",
        }).encode("utf-8")
        payload = _http_json(
            "POST", f"{BASE_URL}/stats/v1/accounts/{user_id}/items",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=body,
            retry=True,
        )
        entries = payload.get("result", {}).get("items", []) if isinstance(payload, dict) else []
        for entry in entries:
            if isinstance(entry, dict) and "itemId" in entry:
                result[str(entry["itemId"])] = entry.get("stats") or []
    return result


# ── Даты ──────────────────────────────────────────────────────────────────

def compute_date_range(period_days: int, today: Optional[date] = None) -> tuple[str, str]:
    """date_to = вчера, date_from = date_to - (period_days - 1); ISO-строки."""
    if period_days not in (7, 30):
        raise ValueError(f"period_days должен быть 7 или 30, получено {period_days!r}")
    base_today = today or date.today()
    date_to = base_today - timedelta(days=1)
    date_from = date_to - timedelta(days=period_days - 1)
    return date_from.isoformat(), date_to.isoformat()


# ── Сопоставление города по адресу ─────────────────────────────────────────

def match_city_from_address(address: str) -> Optional[City]:
    """
    Ищет известный город (cities.CITIES) среди сегментов адреса API.

    Адрес разбивается по запятой, каждый сегмент сравнивается с названием
    города ТОЧНЫМ совпадением (после strip) — подстрочный поиск давал бы
    ложные срабатывания (например, «Мурино» внутри «Муринское городское
    поселение»). Не находит город — возвращает None, это не ошибка запуска
    (см. «Решения, которых нет в спеке» брифа 05).
    """
    if not address:
        return None
    by_name = {c.name: c for c in CITIES}
    for segment in address.split(","):
        city = by_name.get(segment.strip())
        if city is not None:
            return city
    return None


# ── Обогащение и агрегаты (чистые функции — тестируются без HTTP/SQLite) ──

def _enrich_item(
    api_item: dict[str, Any],
    listings_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Город/prep_id/вариант — из listings по item_id, а если записи нет —
    город определяется по адресу API, prep_id и вариант остаются null."""
    item_id = str(api_item.get("id"))
    listing = listings_by_id.get(item_id)
    if listing is not None:
        city_slug = listing.get("city_slug")
        city_name = listing.get("city_name")
        prep_id = listing.get("prep_id")
        variant_index = listing.get("variant_index")
    else:
        city = match_city_from_address(str(api_item.get("address") or ""))
        city_slug = city.slug if city else None
        city_name = city.name if city else None
        prep_id = None
        variant_index = None
    return {
        "item_id": item_id,
        "title": api_item.get("title") or "",
        "city_slug": city_slug,
        "city_name": city_name,
        "prep_id": prep_id,
        "variant_index": variant_index,
    }


def _sum_stats(stats_list: list[dict[str, Any]]) -> tuple[int, int, int]:
    views = sum(int(e.get("uniqViews") or 0) for e in stats_list)
    contacts = sum(int(e.get("uniqContacts") or 0) for e in stats_list)
    favorites = sum(int(e.get("uniqFavorites") or 0) for e in stats_list)
    return views, contacts, favorites


def _contact_rate(contacts: int, views: int) -> Optional[float]:
    """contacts / views; при views = 0 — None, а не 0 (граница брифа 05)."""
    return (contacts / views) if views > 0 else None


def build_snapshot_data(
    api_items: list[dict[str, Any]],
    stats_by_id: dict[str, list[dict[str, Any]]],
    listings_by_id: dict[str, dict[str, Any]],
    period_days: int,
) -> dict[str, Any]:
    """
    Собирает data_json снимка (docs/specs/agents-registry.md, Этап 2):
    items / cities / variants. Чистая функция без HTTP и SQLite — вся логика
    тестов брифа 05 (агрегаты, views=0, объявление без listings) идёт сюда.
    """
    items_out: list[dict[str, Any]] = []
    for api_item in api_items:
        enriched = _enrich_item(api_item, listings_by_id)
        views, contacts, favorites = _sum_stats(stats_by_id.get(enriched["item_id"], []))
        items_out.append({
            **enriched,
            "views": views,
            "contacts": contacts,
            "favorites": favorites,
            "views_per_day": views / period_days,
        })

    return {
        "items": items_out,
        "cities": _aggregate_by(
            items_out,
            key_fn=lambda i: i["city_slug"],
            make_row=lambda key, i: {"city_slug": key, "city_name": i["city_name"]},
        ),
        "variants": _aggregate_by(
            items_out,
            key_fn=lambda i: (i["prep_id"], i["variant_index"]) if i["prep_id"] is not None and i["variant_index"] is not None else None,
            make_row=lambda key, i: {"prep_id": key[0], "variant_index": key[1]},
        ),
    }


def _aggregate_by(items: list[dict[str, Any]], *, key_fn, make_row) -> list[dict[str, Any]]:
    """
    Общая группировка по ключу (город или (prep_id, вариант)): считает
    items/views_per_day_avg/contact_rate. key_fn возвращает None для строк вне
    группировки (город не определён / вариант вне пакета) — они просто не
    попадают в агрегат, оставаясь в data_json.items.
    """
    groups: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for item in items:
        key = key_fn(item)
        if key is None:
            continue
        if key not in groups:
            groups[key] = {
                **make_row(key, item),
                "items": 0,
                "_views_per_day_sum": 0.0,
                "_views_sum": 0,
                "_contacts_sum": 0,
            }
            order.append(key)
        group = groups[key]
        group["items"] += 1
        group["_views_per_day_sum"] += item["views_per_day"]
        group["_views_sum"] += item["views"]
        group["_contacts_sum"] += item["contacts"]

    result: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        result.append({
            k: v for k, v in group.items() if not k.startswith("_")
        } | {
            "views_per_day_avg": group["_views_per_day_sum"] / group["items"],
            "contact_rate": _contact_rate(group["_contacts_sum"], group["_views_sum"]),
        })
    return result


# ── Оркестрация ───────────────────────────────────────────────────────────

def run(period_days: int, db_path: Optional[str] = None) -> str:
    """
    Полный цикл Сборщика: токен → user_id → объявления → статистика →
    обогащение из listings → агрегаты → снимок в agents.db.

    Блокирующая функция (stdlib urllib, синхронный SQLite через journal.py) —
    вызывающий код (app.py) обязан запускать её через asyncio.to_thread.

    Args:
        period_days: 7 или 30
        db_path:     путь к agents.db для journal.get_listings/save_stats_snapshot;
                     None — берётся модульная journal.DB_PATH

    Returns:
        id созданного снимка статистики (stats_snapshots.id).

    Raises:
        ValueError:          period_days не 7 и не 30
        StatsCollectorError: не заданы AVITO_CLIENT_ID/SECRET или сбой запроса к Авито
        sqlite3.Error:       сбой записи снимка (см. journal.save_stats_snapshot)
    """
    date_from, date_to = compute_date_range(period_days)  # валидирует period_days первым

    client_id = os.environ.get("AVITO_CLIENT_ID")
    client_secret = os.environ.get("AVITO_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise StatsCollectorError(
            "Не заданы AVITO_CLIENT_ID / AVITO_CLIENT_SECRET: впишите их в файл .env "
            "в корне проекта и перезапустите сервер"
        )

    token = _fetch_token(client_id, client_secret)
    user_id = _fetch_account_user_id(token)
    api_items = _fetch_items(token)

    item_ids = [item["id"] for item in api_items if "id" in item]
    stats_by_id = _fetch_stats(token, user_id, item_ids, date_from, date_to) if item_ids else {}

    listings_by_id = journal.get_listings([str(i) for i in item_ids], db_path=db_path)

    data = build_snapshot_data(api_items, stats_by_id, listings_by_id, period_days)

    snapshot_id = str(uuid.uuid4())
    journal.save_stats_snapshot(
        snapshot_id,
        period_days=period_days,
        date_from=date_from,
        date_to=date_to,
        data=data,
        db_path=db_path,
    )
    return snapshot_id
