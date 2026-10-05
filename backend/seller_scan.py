"""
Разбор продавца: просмотры всех объявлений со страницы его профиля.

Публичный API:
    validate_profile_url(raw) -> str            — проверка и нормализация ссылки
    collect_item_urls(page, profile_url, limit)  -> (urls, found_total, duplicates_skipped)
    scan_seller(profile_url, limit, progress_cb) -> dict

Только чтение: сервис не кликает ни по одной кнопке Авито (написать, избранное,
подписка) — работаем под живым аккаунтом пользователя на чужом профиле, лишние
действия там рискуют баном. Единственное исключение — навигационная «Показать
все» под витриной продавца (см. _open_full_list): без неё видно только 12–15
объявлений из всех. Своя вкладка Chrome по CDP, открыть/закрыть —
браузер пользователя не трогаем (см. _owned_page, тот же приём, что в outreach.py).

Разметка страницы профиля ПОДТВЕРЖДЕНА живой разведкой 23.09.2026 — см.
avito_selectors.SELLER_PROFILE_CARD и debug/seller_profile_map_20260923T085405Z.txt:
карточек-контейнеров [data-marker='item'] на этой странице нет вообще, лента
подгружается бесконечной прокруткой (без пагинации), а Авито сам повторяет
часть объявлений в разных карточках ленты — сбор дедуплицирует по ID и считает
пропущенные повторы отдельно (см. collect_item_urls). Просмотры на странице
ОТДЕЛЬНОГО объявления подтверждены тем же дампом 21.09.2026 — см. avito_selectors.py
(ITEM_VIEWS_TOTAL/ITEM_VIEWS_TODAY/ITEM_TITLE_CONFIRMED) и _read_item ниже.
"""

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.async_api import Page, async_playwright

import avito_selectors as sel
from browser import connect_over_cdp
from parser import AvitoBlockedError, _check_block, _extract_int, _random_delay


BASE_URL = "https://www.avito.ru"
logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int, str], None]

# Каталог дампов «собрали меньше N» — материал для разбора сбоев сбора
# (короткая подгрузка, бан, неожиданная разметка).
DEBUG_DIR = Path("debug")

# Сколько подряд шагов прокрутки без новых уникальных ID считаем концом ленты.
# ПОДТВЕРЖДЕНО разведкой 23.09.2026: после последней порции карточек высота
# документа (и число уникальных ID) переставала расти уже на 3-м шаге подряд.
MAX_STAGNANT_SCROLLS = 3

# После каждого шага прокрутки ждём роста числа карточек в DOM опросом до
# этого времени, а не фиксированной паузой: разведка показала, что XHR
# подгрузки следующей порции не укладывается в фиксированные 0.8 с, и при
# фиксированной паузе прокрутка могла принять ещё не догрузившуюся ленту за
# конец списка.
SCROLL_GROWTH_TIMEOUT_SECONDS = 3.0
SCROLL_GROWTH_POLL_INTERVAL_SECONDS = 0.2

# Потолок шагов прокрутки — предохранитель, если лента не кончается или
# зависла. Разведка 23.09.2026: ~11 новых объявлений за шаг, 170 карточек
# (156 уникальных) за 14 шагов — с запасом под ~300 объявлений.
MAX_SCROLL_STEPS = 60

# Ссылка на объявление: /<город>/<категория>/<slug>_<ID>, ID — 6+ цифр.
# ПОДТВЕРЖДЕНО и на выдаче поиска (avito_selectors.SEARCH_CARD_TITLE), и на
# странице профиля живой разведкой 23.09.2026.
ITEM_URL_RE = re.compile(r"^/[^/]+/[^/]+/[^/]+_(\d{6,})/?$")

# «Найдено 170 объявлений» — сводка над лентой карточек профиля.
# ПОДТВЕРЖДЕНО живой разведкой 23.09.2026 (текст лежит в <h5>): если фраза
# всё же не найдена на другой раскладке, просто не показываем число
# (см. app.py) — не гадаем.
FOUND_TOTAL_RE = re.compile(
    r"[Нн]айдено\s+([0-9][0-9\s\xa0 ]*)\s+объявлени", re.UNICODE
)


def validate_profile_url(raw: str) -> str:
    """
    Проверяет и нормализует ссылку на страницу всех объявлений продавца.

    Хост — avito.ru/www.avito.ru, путь начинается с /brands/ или /user/.
    Голый /brands/<id> без продолжения дополняется до /brands/<id>/items/all.
    Остальное — ValueError с понятным текстом (эндпоинт app.py превращает
    его в 422).
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Ссылка на профиль продавца не заполнена")
    try:
        parsed = urlsplit(urljoin(BASE_URL, text))
    except ValueError as exc:
        raise ValueError(f"Не удалось разобрать ссылку: {exc}") from exc
    if parsed.scheme != "https" or parsed.hostname not in {"avito.ru", "www.avito.ru"}:
        raise ValueError(
            "Ожидалась ссылка вида https://www.avito.ru/brands/... или https://www.avito.ru/user/..."
        )
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[0] not in {"brands", "user"}:
        raise ValueError(
            "Ссылка должна вести на страницу продавца (/brands/... или /user/...)"
        )
    if len(parts) == 2 and parts[0] == "brands":
        parts = parts + ["items", "all"]
    path = "/" + "/".join(parts)
    return urlunsplit(("https", "www.avito.ru", path, parsed.query, ""))


def _item_id_from_path(path: str) -> Optional[str]:
    match = ITEM_URL_RE.match(path)
    return match.group(1) if match else None


def _extract_item_urls_from_html(html: str) -> list[str]:
    """
    Извлекает ссылки на объявления в порядке появления, без дублей по ID.

    Ищет ссылки только внутри карточек профиля (avito_selectors.SELLER_PROFILE_CARD);
    если карточек нет (разметка неожиданно другая) — по всей странице, как
    запасной путь. ПОДТВЕРЖДЕНО живой разведкой 23.09.2026: у каждой карточки
    внутри ровно две ссылки на один и тот же ID (item-photo-sliderLink,
    item-title), ссылок на чужие блоки внутри карточек нет.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(sel.SELLER_PROFILE_CARD)
    scope = cards if cards else [soup]

    urls: list[str] = []
    seen: set[str] = set()
    for scope_el in scope:
        for link in scope_el.select("a[href]"):
            href = link.get("href") or ""
            absolute = urljoin(BASE_URL, href)
            parsed = urlsplit(absolute)
            if parsed.hostname not in {"avito.ru", "www.avito.ru"}:
                continue
            item_id = _item_id_from_path(parsed.path)
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            urls.append(urlunsplit(("https", "www.avito.ru", parsed.path, "", "")))
    return urls


def _extract_found_total(html: str) -> Optional[int]:
    """«Найдено N объявлений» из текста страницы — не нашлось, значит None."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    match = FOUND_TOTAL_RE.search(text)
    if not match:
        return None
    # \xa0 — неразрывный пробел, \u202f — узкий неразрывный пробел: Авито
    # использует оба как разделитель тысяч в «Найдено N объявлений».
    digits = re.sub(r"[\s\xa0\u202f]", "", match.group(1))
    return int(digits) if digits.isdigit() else None


def sort_items_by_views(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """
    Сортирует по убыванию `key` (views_total/views_today).

    Объявления без прочитанного числа (None) — в конец, не выкидываются;
    порядок между ними — как пришли со сканирования (сортировка стабильна).
    """
    return sorted(items, key=lambda item: (item.get(key) is None, -(item.get(key) or 0)))


@asynccontextmanager
async def _owned_page():
    """Подключается по CDP; закрывает исключительно созданную здесь вкладку."""
    async with async_playwright() as pw:
        context = await connect_over_cdp(pw, os.getenv("AVITO_CDP_URL", "http://127.0.0.1:9222"))
        page = await context.new_page()
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:
                logger.warning("Не удалось закрыть вкладку разбора продавца", exc_info=True)


def _progress(callback: Optional[ProgressCallback], done: int, total: int, label: str) -> None:
    if callback:
        try:
            callback(done, total, label)
        except Exception:
            logger.warning("Не удалось обновить прогресс разбора продавца", exc_info=True)


async def _dump_short_collection(page: Page, profile_url: str, collected: int, limit: int) -> None:
    """Собрали меньше N — сохраняет HTML профиля в debug/ (как diag.py), это
    материал для разбора сбоя (капча, неожиданная разметка), не падение задачи."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump_dir = DEBUG_DIR / f"seller_scan_short_{stamp}"
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
        html = await page.content()
        (dump_dir / "profile.html").write_text(html, encoding="utf-8")
        logger.warning(
            "Разбор продавца: собрано %d ссылок из %d на %s — дамп в %s",
            collected, limit, profile_url, dump_dir,
        )
    except Exception:
        logger.warning("Разбор продавца: не удалось сохранить дамп короткого сбора", exc_info=True)


def _count_profile_cards(html: str) -> int:
    """
    Общее число карточек на странице профиля, включая повторы одного и того
    же ID в разных карточках ленты — Авито сам их повторяет (ПОДТВЕРЖДЕНО
    живой разведкой 23.09.2026: 170 карточек, 156 уникальных ID). Разница
    между этим числом и количеством уникальных ID — duplicates_skipped
    (см. collect_item_urls).
    """
    return len(BeautifulSoup(html, "html.parser").select(sel.SELLER_PROFILE_CARD))


async def _card_count(page: Page) -> int:
    """Быстрый подсчёт карточек профиля в DOM — только для опроса роста ленты
    между шагами прокрутки (см. _wait_for_more_cards), без парсинга HTML."""
    try:
        return await page.evaluate(
            "(selector) => document.querySelectorAll(selector).length",
            sel.SELLER_PROFILE_CARD,
        )
    except Exception:
        return -1


async def _wait_for_more_cards(page: Page, prev_count: int) -> None:
    """
    После шага прокрутки ждёт роста числа карточек в DOM опросом до
    SCROLL_GROWTH_TIMEOUT_SECONDS, а не фиксированной паузой — живая разведка
    23.09.2026 показала, что XHR подгрузки следующей порции не всегда
    укладывается в фиксированные 0.8 с, и тогда прокрутка ошибочно принимает
    ещё не догрузившуюся ленту за конец списка.
    """
    deadline = asyncio.get_event_loop().time() + SCROLL_GROWTH_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(SCROLL_GROWTH_POLL_INTERVAL_SECONDS)
        count = await _card_count(page)
        if count < 0 or count > prev_count:
            return


def _show_all_href(html: str) -> Optional[str]:
    """
    Абсолютная ссылка «Показать все» с витрины продавца или None.

    None — кнопки нет (уже полная лента) или она без href (раскладка-кнопка,
    её обрабатывает клик в _open_full_list). Ссылка не на avito.ru или с
    чужой подписью — тоже None: не уходим по неизвестной ссылке.
    """
    soup = BeautifulSoup(html, "html.parser")
    button = soup.select_one(sel.SELLER_SHOW_ALL_BUTTON)
    if button is None or button.name != "a":
        return None
    if sel.SELLER_SHOW_ALL_LABEL.lower() not in button.get_text(" ", strip=True).lower():
        return None
    href = button.get("href") or ""
    if not href:
        return None
    absolute = urljoin(BASE_URL, href)
    if urlsplit(absolute).hostname not in {"avito.ru", "www.avito.ru"}:
        return None
    return absolute


async def _open_full_list(page: Page, profile_url: str) -> None:
    """
    С витрины продавца (/brands/<id>/all) переходит на полную ленту
    объявлений по «Показать все». Без этого витрина отдаёт только первые
    12–15 карточек, а прокрутка вниз уводит к отзывам (сбой 27.09.2026:
    12 объявлений из 68).

    Ссылка (<a href>) — открываем её адрес обычным переходом, без клика.
    Кнопка без href — кликаем, но только при точной подписи «Показать все»;
    чужая подпись — лог и работаем с тем, что есть, не угадываем.
    Кнопки нет — уже полная лента (/brands/<id>/items/all), ничего не делаем.
    """
    html = await page.content()
    href = _show_all_href(html)
    if href:
        logger.info("Разбор продавца: витрина %s → полная лента %s", profile_url, href)
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning("Разбор продавца: не удалось открыть полную ленту %s: %s", href, exc)
            return
    else:
        button = page.locator(sel.SELLER_SHOW_ALL_BUTTON).first
        try:
            if await button.count() == 0:
                return
            label = (await button.inner_text()).strip()
        except Exception:
            return
        if sel.SELLER_SHOW_ALL_LABEL.lower() not in label.lower():
            logger.warning(
                "Разбор продавца: неизвестная подпись кнопки полной ленты %r на %s — не нажимаю",
                label, profile_url,
            )
            return
        logger.info("Разбор продавца: нажимаю «%s» на %s", label, profile_url)
        prev_count = await _card_count(page)
        try:
            await button.scroll_into_view_if_needed(timeout=5_000)
            await button.click(timeout=10_000)
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.warning("Разбор продавца: не удалось нажать «Показать все» на %s: %s", profile_url, exc)
            return
        await _wait_for_more_cards(page, prev_count)

    try:
        await page.wait_for_selector(sel.SELLER_PROFILE_CARD, timeout=15_000, state="attached")
    except Exception:
        logger.info("Разбор продавца: карточки полной ленты не появились за 15 с — читаю как есть")


async def collect_item_urls(
    page: Page, profile_url: str, limit: int,
) -> tuple[list[str], Optional[int], int]:
    """
    Собирает первые `limit` уникальных ссылок на объявления со страницы
    профиля продавца, в порядке показа. Если открыта витрина с «Показать
    все» — сперва переходит на полную ленту (_open_full_list).

    Лента одна, бесконечная и без пагинации (ПОДТВЕРЖДЕНО живой разведкой
    23.09.2026 — ни p=, ни маркеров pagination на странице профиля нет),
    поэтому функция просто прокручивает вниз, пока не наберёт limit уникальных
    ID или несколько прокруток подряд не перестанут добавлять новые
    (MAX_STAGNANT_SCROLLS), с потолком MAX_SCROLL_STEPS шагов на случай
    зависшей подгрузки.

    Авито сам повторяет часть объявлений в разных карточках ленты (одно и то
    же ID встречается в нескольких карточках) — такие повторы отбрасывает
    дедупликация по ID, а их количество возвращается отдельно, чтобы не
    молчать перед пользователем о разнице с «Найдено N».

    Возвращает (urls, found_total, duplicates_skipped):
      - found_total — «Найдено N объявлений» с первой загрузки или None,
        если фраза не распознана;
      - duplicates_skipped — сколько карточек на итоговой странице оказались
        повтором уже встреченного ID.
    """
    urls: list[str] = []
    seen_ids: set[str] = set()
    found_total: Optional[int] = None
    html = ""

    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        logger.warning("Разбор продавца: не удалось загрузить %s: %s", profile_url, exc)
        return [], None, 0

    try:
        await page.wait_for_selector(sel.SELLER_PROFILE_CARD, timeout=15_000, state="attached")
    except Exception:
        logger.info(
            "Разбор продавца: карточки не появились за 15 с на %s — читаю страницу как есть",
            profile_url,
        )

    await _open_full_list(page, profile_url)

    stagnant = 0
    for step in range(1, MAX_SCROLL_STEPS + 1):
        html = await page.content()
        _check_block(html, page.url, page_title=await page.title())
        if found_total is None:
            found_total = _extract_found_total(html)

        new_found = False
        for url in _extract_item_urls_from_html(html):
            item_id = _item_id_from_path(urlsplit(url).path)
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            urls.append(url)
            new_found = True
            if len(urls) >= limit:
                break

        stagnant = 0 if new_found else stagnant + 1
        if len(urls) >= limit or stagnant >= MAX_STAGNANT_SCROLLS:
            break

        prev_count = await _card_count(page)
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        except Exception:
            break
        await _wait_for_more_cards(page, prev_count)
    else:
        logger.warning(
            "Разбор продавца: достигнут потолок %d шагов прокрутки на %s — лента могла быть длиннее",
            MAX_SCROLL_STEPS, profile_url,
        )

    total_cards = _count_profile_cards(html) if html else 0
    duplicates_skipped = max(total_cards - len(seen_ids), 0)

    if len(urls) < limit:
        await _dump_short_collection(page, profile_url, len(urls), limit)

    return urls[:limit], found_total, duplicates_skipped


async def _read_item(page: Page, url: str) -> dict[str, Any]:
    """
    Открывает страницу объявления, читает название и просмотры.

    Возвращает {"url", "title", "views_total", "views_today", "error"}.
    error не None — просмотры (и/или название) не прочитаны, строка идёт в
    конец таблиц с пометкой, а не выбрасывается (см. scan_seller).
    Поднимает AvitoBlockedError — вызывающий код обязан остановить весь пакет.
    """
    item: dict[str, Any] = {
        "url": url, "title": "", "views_total": None, "views_today": None, "error": None,
    }
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        logger.warning("Разбор продавца: не удалось загрузить объявление %s: %s", url, exc)
        item["error"] = f"Не удалось загрузить объявление: {exc}"
        return item

    for candidate_selector in (sel.ITEM_VIEWS_TOTAL, sel.ITEM_VIEWS_TODAY, sel.ITEM_TITLE_CONFIRMED):
        try:
            await page.wait_for_selector(candidate_selector, timeout=10_000, state="attached")
            break
        except Exception:
            continue
    else:
        logger.info("Разбор продавца: ключевые элементы объявления не появились: %s", url)

    # Пауза для завершения подзагрузки счётчиков просмотров — как в parser._parse_item_page.
    await asyncio.sleep(1.0)

    html = await page.content()
    try:
        page_title = await page.title()
    except Exception:
        page_title = ""
    _check_block(html, url, page_title=page_title)  # поднимет AvitoBlockedError при бане

    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(sel.ITEM_TITLE_CONFIRMED)
    item["title"] = title_el.get_text(strip=True) if title_el else ""

    total_el = soup.select_one(sel.ITEM_VIEWS_TOTAL)
    item["views_total"] = _extract_int(total_el.get_text()) if total_el else None

    today_el = soup.select_one(sel.ITEM_VIEWS_TODAY)
    item["views_today"] = _extract_int(today_el.get_text()) if today_el else None

    if item["views_total"] is None and item["views_today"] is None:
        item["error"] = "Не удалось прочитать просмотры на странице объявления"

    return item


async def scan_seller(
    profile_url: str, limit: int, progress_cb: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """
    Обходит первые `limit` объявлений продавца в своей вкладке Chrome по CDP.

    Только чтение (см. docstring модуля). Ошибка одного объявления — лог и
    строка со статусом ошибки, пакет идёт дальше; AvitoBlockedError
    останавливает весь обход (не ловится здесь — уходит вызывающему в app.py).
    """
    async with _owned_page() as page:
        urls, found_total, duplicates_skipped = await collect_item_urls(page, profile_url, limit)
        items: list[dict[str, Any]] = []
        for index, url in enumerate(urls, start=1):
            _progress(progress_cb, index - 1, len(urls), url)
            try:
                item = await _read_item(page, url)
            except AvitoBlockedError:
                raise
            except Exception as exc:
                logger.warning("Разбор продавца: ошибка объявления %s: %s", url, exc)
                item = {
                    "url": url, "title": "", "views_total": None, "views_today": None,
                    "error": f"Не удалось прочитать объявление: {exc}",
                }
            items.append(item)
            _progress(progress_cb, index, len(urls), url)
            if index < len(urls):
                await _random_delay()

    errors = sum(1 for item in items if item.get("error"))
    return {
        "found_total": found_total,
        "scanned": len(items),
        "errors": errors,
        "duplicates": duplicates_skipped,
        "items": items,
    }
