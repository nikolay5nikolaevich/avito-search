"""Отбор продавцов и последовательная рассылка через Chrome пользователя."""

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from playwright.async_api import Page, async_playwright

import avito_selectors as sel
from browser import connect_over_cdp
from cities import build_search_url, get_city_by_slug, is_local_listing
from outreach_store import (
    extract_seller_id, is_contacted, normalize_profile_url, record_contact, seller_key,
)
from parser import MAX_SEARCH_PAGES, _check_block, _random_delay, _wait_for_cards_and_scroll


BASE_URL = "https://www.avito.ru"
logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int, str], None]
# (step_key, item_index) — вызывается при входе в каждый шаг сценария для
# текущего кандидата; см. STEPS ниже и outreach_state.SAFE_PRECLICK_STEPS.
StepCallback = Callable[[str, int], None]

# Шаги сценария отправки одному кандидату — порядок и ключи зашиты во фронте
# и в outreach_state.SAFE_PRECLICK_STEPS. Граница безопасного повтора —
# ровно перед send_click (см. outreach_state.py).
STEPS: list[tuple[str, str]] = [
    ("connect_chrome", "Подключение к Chrome"),
    ("open_listing", "Открытие объявления"),
    ("check_seller", "Сверка продавца"),
    ("open_chat", "Открытие чата"),
    ("fill_message", "Ввод письма"),
    ("send_click", "Отправка сообщения"),
    ("confirm_sent", "Подтверждение отправки"),
    ("back_to_search", "Возврат к выдаче"),
]
STEP_LABELS: dict[str, str] = dict(STEPS)

# Каталог дампов остановки рассылки: debug/outreach_stop_<UTC-метка>/.
DEBUG_DIR = Path("debug")

# Сколько ждём поле ввода чата после клика по «Написать».
CHAT_OPEN_TIMEOUT_MS = 12_000
# Сколько ждём подтверждения отправки после единственного клика.
CHAT_CONFIRM_TIMEOUT_MS = 20_000
# Сколько ждём появления виджета мини-мессенджера в DOM (attached) перед
# поиском кнопки «Написать» — см. MINI_MESSENGER в avito_selectors.py.
# Живой прогон 22.09.2026 показал именно недогруженный блок (скелет-заглушка
# на месте кнопок контакта), а не полностью отсутствующий — подняли с 30 до 45 с.
CHAT_WIDGET_TIMEOUT_MS = 45_000

# Подтверждение отправки: в ленте прибавилось ИСХОДЯЩЕЕ сообщение И поле ввода
# опустело. Точный текст не сверяем — он уже сверен обратным чтением до клика,
# а в DOM к сообщению подмешивается время («Отменить тариф9:48»).
# getClientRects() вместо offsetParent: мини-мессенджер позиционируется fixed,
# и offsetParent у его потомков равен null даже когда они видимы.
_CONFIRM_SENT_JS = """
({outgoing, input, before}) =>
    document.querySelectorAll(outgoing).length > before &&
    Array.from(document.querySelectorAll(input)).some(
        el => el.getClientRects().length > 0 && el.value === '')
"""

# Диагностика для остановки: что именно не сошлось. Только чтение, без кликов.
_SEND_STATE_JS = """
({outgoing, input}) => ({
    outgoing: document.querySelectorAll(outgoing).length,
    empty: Array.from(document.querySelectorAll(input)).some(
        el => el.getClientRects().length > 0 && el.value === ''),
})
"""

# Диагностика для дампа остановки на шаге open_chat: различает «чанк мессенджера
# завис в этой вкладке» (маркеров нет вовсе) от «Авито придерживает чат» (см.
# HEADER_UNREAD_CHATS_COUNTER в avito_selectors.py). Только чтение, без кликов.
_WIDGET_STATE_JS = """
({miniMessenger, unreadCounter, chatButton}) => ({
    miniMessenger: document.querySelectorAll(miniMessenger).length,
    unreadCounter: document.querySelectorAll(unreadCounter).length,
    chatButton: document.querySelectorAll(chatButton).length,
})
"""


class OutreachStoppedError(RuntimeError):
    """Неизвестное состояние: пакет остановлен для ручной проверки."""

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result


class ChatUnavailableError(OutreachStoppedError):
    """Чат не доступен после безопасных попыток; можно перейти к следующему."""


def parse_review_count(text: str) -> int | None:
    """Распознаёт точное число отзывов, не рейтинг и не округлённые тысячи."""
    match = re.search(
        r"(?<![\d.,])([0-9]{1,3}(?:[ \xa0\u202f][0-9]{3})+|[0-9]+)"
        r"\s+отзыв(?:а|ов)?\b", text, re.IGNORECASE,
    )
    return int(re.sub(r"\s", "", match.group(1))) if match else None


def _avito_url(url: str) -> str:
    parsed = urlsplit(urljoin(BASE_URL, url))
    if (parsed.scheme != "https" or parsed.hostname not in {"avito.ru", "www.avito.ru"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ValueError("Ожидалась ссылка https://www.avito.ru")
    return urlunsplit(("https", "www.avito.ru", parsed.path, "", ""))


def extract_candidates_from_html(html: str, city: str, query: str) -> list[dict]:
    """Берёт местные карточки с 50+ отзывами, по одной на продавца."""
    candidates = []
    seen_ids: set[str] = set()
    seen_profiles: set[str] = set()
    for card in BeautifulSoup(html, "html.parser").select(sel.SEARCH_CARD):
        title = card.select_one(sel.SEARCH_CARD_TITLE)
        reviews = card.select_one(sel.SEARCH_SELLER_REVIEWS)
        count = parse_review_count(reviews.get_text(" ", strip=True)) if reviews else None
        if title is None or count is None or count < 50:
            continue
        try:
            listing_url = _avito_url(title.get("href", ""))
        except ValueError:
            continue
        if not is_local_listing(listing_url, city):
            continue
        for link in card.select(sel.SEARCH_SELLER_LINK):
            name = link.get_text(" ", strip=True)
            if not name:
                continue
            raw_profile = urljoin(BASE_URL, link.get("href", ""))
            try:
                _avito_url(raw_profile)
                profile = normalize_profile_url(raw_profile)
                identity = extract_seller_id(raw_profile)
                key = seller_key(raw_profile)
            except ValueError:
                continue
            if not profile or not key:
                continue
            if profile in seen_profiles or (identity and identity in seen_ids):
                break
            seen_profiles.add(profile)
            if identity:
                seen_ids.add(identity)
            candidates.append({
                "seller_key": key, "seller_id": identity, "profile_url": profile,
                "seller_name": name, "review_count": count, "listing_url": listing_url,
                "city": city, "query": query,
            })
            break
    return candidates


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
                logger.warning("Не удалось закрыть вкладку рассылки", exc_info=True)


async def _checked_html(page: Page) -> str:
    html = await page.content()
    _check_block(html, page.url, page_title=await page.title())
    _avito_url(page.url)
    if (urlsplit(page.url).path.startswith(("/profile/login", "/registration"))
            or await page.locator("[data-marker='auth-popup']:visible, form[action*='/login']:visible").count()):
        raise OutreachStoppedError("Сессия Авито потеряна. Войдите в Chrome вручную.")
    return html


def _progress(callback: ProgressCallback | None, done: int, total: int, label: str) -> None:
    if callback:
        try:
            callback(done, total, label)
        except Exception:
            logger.warning("Не удалось обновить прогресс рассылки", exc_info=True)


async def collect_candidates(
    city_slug: str, query: str, limit: int, progress_cb: ProgressCallback | None = None,
) -> list[dict]:
    """Обходит выдачу безопасными GET; не открывает чат и не отправляет письма."""
    if get_city_by_slug(city_slug) is None or not query.strip() or not 1 <= limit <= 150:
        raise ValueError("Нужны известный город, запрос и лимит 1–150")
    candidates: list[dict] = []
    seen_ids: set[str] = set()
    seen_profiles: set[str] = set()
    seen_listings: set[str] = set()
    async with _owned_page() as page:
        for number in range(1, MAX_SEARCH_PAGES + 1):
            url = build_search_url(city_slug, query)
            if number > 1:
                url += f"&p={number}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await _wait_for_cards_and_scroll(page, url)
            html = await _checked_html(page)
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select(sel.SEARCH_CARD)
            if not cards:
                if any(text in soup.get_text(" ", strip=True).lower() for text in (
                    "ничего не найдено", "нет объявлений", "не нашли объявления",
                )):
                    break
                raise OutreachStoppedError("Не распознана выдача Авито: сбор остановлен.")
            listings = {
                node.get("href", "").split("?", 1)[0]
                for node in soup.select(sel.SEARCH_CARD_TITLE)
                if is_local_listing(node.get("href", ""), city_slug)
            }
            if not listings - seen_listings:
                break
            seen_listings.update(listings)
            for candidate in extract_candidates_from_html(html, city_slug, query):
                identity, profile = candidate["seller_id"], candidate["profile_url"]
                if profile in seen_profiles or (identity and identity in seen_ids):
                    continue
                seen_profiles.add(profile)
                if identity:
                    seen_ids.add(identity)
                if is_contacted(identity, profile):
                    continue
                candidates.append(candidate)
                if len(candidates) >= limit:
                    break
            _progress(progress_cb, len(candidates), limit, f"Просмотрена страница {number}")
            if len(candidates) >= limit:
                break
            await _random_delay()
    return candidates


def _ui_length(text: str) -> int:
    """Длина в кодовых единицах UTF-16 — именно их считает maxlength в браузере."""
    return len(text.encode("utf-16-le")) // 2


def _check_message_fits(message: str) -> None:
    """
    Ловит шаблон длиннее maxlength поля Авито ещё до открытия Chrome.

    Playwright набирает текст в textarea через Input.insertText, то есть как
    человек, и браузер обрежет лишнее по maxlength=1000. Обратное чтение тогда
    не совпадёт с шаблоном и пакет встанет — но выглядеть это будет как сбой
    Авито. Поэтому проверяем длину заранее и говорим прямо.
    """
    length = _ui_length(message)
    if length > sel.CHAT_INPUT_MAXLENGTH:
        raise ValueError(
            f"Шаблон письма длиннее поля Авито: {length} символов при лимите "
            f"{sel.CHAT_INPUT_MAXLENGTH}. Сократите текст — иначе Авито обрежет его."
        )


def _item_path(url: str) -> str:
    """Путь объявления без хвостового слэша — для сверки «мы всё ещё здесь»."""
    return urlsplit(url).path.rstrip("/")


def _guard_same_item(page: Page, item_path: str) -> None:
    """
    Требует, чтобы чат открылся поверх того же объявления.

    Личность продавца проверена ИМЕННО на этой странице. Если Авито увёл нас
    на /profile/messenger или в отдельный диалог, получатель ничем не
    подтверждён — писать туда нельзя, пакет останавливается.
    """
    if _item_path(page.url) != item_path:
        raise OutreachStoppedError(
            f"Чат открылся вне страницы объявления ({page.url}): получатель не "
            "подтверждён, отправка остановлена."
        )


def _guard_still_on_listing(page: Page, item_path: str) -> None:
    """
    Останавливает сценарий ДО бессмысленного 30-секундного ожидания виджета,
    если Авито уже увёл со страницы объявления.

    Факт с боевого прогона 22.09.2026: остановка «виджет чата не загрузился за
    30 с» совпала с вкладкой «Сообщения | Авито» — похоже, Авито увёл сценарий
    на полную страницу мессенджера /profile/messenger, где плавающего
    мини-мессенджера нет в принципе (живьём пока не подтверждено). Если это
    произошло, ждать виджет там бессмысленно — говорим прямо и останавливаемся.
    """
    current_path = _item_path(page.url)
    if current_path == item_path:
        return
    if current_path.startswith("/profile/messenger"):
        raise OutreachStoppedError(
            "Авито увёл сценарий со страницы объявления на полную страницу "
            f"мессенджера ({page.url}) — там нет плавающего мини-мессенджера. "
            "Отправка остановлена до ожидания виджета."
        )
    raise OutreachStoppedError(
        f"Страница ушла со страницы объявления ({page.url}) до открытия чата: "
        "отправка остановлена."
    )


async def _wait_for_composer(page: Page, timeout: float = CHAT_OPEN_TIMEOUT_MS) -> bool:
    """Ждёт видимое поле ввода чата; молчит про неудачу — решает вызывающий."""
    try:
        await page.wait_for_selector(sel.CHAT_INPUT, state="visible", timeout=timeout)
        return True
    except Exception:
        logger.info("Поле ввода чата не появилось за %.0f мс", timeout, exc_info=True)
        return False


async def _wait_for_widget(page: Page, item_path: str, ctx: dict) -> None:
    """
    Ждёт мини-мессенджер в DOM (attached — виджет может быть свёрнут).

    См. MINI_MESSENGER в avito_selectors.py: это ленивый чанк, и пока он не
    подгрузился, обработчик клика по «Написать» не навешен — клик уходит
    в пустоту. Ждём именно attached, не visible.

    Повтор с безопасной перезагрузкой делает _open_chat: там одна общая
    граница для отсутствующего виджета, кнопки и поля ввода.

    Перед ожиданием сверяем, что мы всё ещё на странице объявления — см.
    _guard_still_on_listing.
    """
    _guard_still_on_listing(page, item_path)
    try:
        await page.locator(sel.MINI_MESSENGER).wait_for(
            state="attached", timeout=CHAT_WIDGET_TIMEOUT_MS
        )
        return
    except Exception as exc:
        raise ChatUnavailableError("Чат недоступен") from exc


async def _pick_chat_button(page: Page):
    """
    Выбирает осознанно, а не `.first`, одну из видимых копий кнопки «Написать».

    ПОДТВЕРЖДЕНО разведкой 21.09.2026 (debug/chat_existing_map_20260921T212054Z.txt,
    debug/chat_existing_map_20260921T212143Z.txt): копий на странице две, обе
    visible — из «липкой» панели (bounding box y < 0, вне вьюпорта) и из блока
    продавца (y > 0, в вьюпорте). Клик по копии в вьюпорте открывает чат, по
    «липкой» — нет. Берём копию с y > 0, при нескольких — с наибольшей площадью.
    Если таких нет вовсе, берём самую большую из оставшихся (лучше, чем ничего)
    и отмечаем это в логе как нештатную ситуацию.
    """
    candidates = await page.locator(sel.ITEM_CHAT_BUTTON).all()
    boxes = []
    for candidate in candidates:
        if not await candidate.is_visible():
            continue
        boxes.append((candidate, await candidate.bounding_box()))
    if not boxes:
        raise ChatUnavailableError("Чат недоступен")

    def area(box: dict | None) -> float:
        return (box["width"] * box["height"]) if box else 0.0

    in_view = [pair for pair in boxes if pair[1] and pair[1]["y"] > 0]
    pool = in_view if in_view else boxes
    if not in_view:
        logger.warning(
            "Среди %d видимых копий кнопки «Написать» нет ни одной с y > 0 — "
            "беру наибольшую по площади из оставшихся.", len(boxes),
        )
    opener, _ = max(pool, key=lambda pair: area(pair[1]))
    return opener


async def _open_chat_once(page: Page, item_path: str, ctx: dict) -> bool:
    """Одна попытка: дождаться виджета, выбрать кнопку, кликнуть, дождаться поле."""
    await _wait_for_widget(page, item_path, ctx)
    opener = await _pick_chat_button(page)
    label = " ".join((await opener.inner_text()).split())
    if not label:
        label = " ".join((await opener.get_attribute("aria-label") or "").split())
    if label not in sel.ITEM_CHAT_BUTTON_LABELS:
        raise OutreachStoppedError(f"Неизвестная кнопка открытия чата: {label!r}.")
    await opener.click(timeout=10_000)
    if await _wait_for_composer(page):
        return True
    _guard_same_item(page, item_path)
    expand = page.locator(sel.MINI_MESSENGER_EXPAND + ":visible").first
    if await expand.count():
        logger.info("Поле ввода не появилось — разворачиваю мини-мессенджер.")
        await expand.click(timeout=10_000)
    return await _wait_for_composer(page)


async def _open_chat(page: Page, item_path: str, ctx: dict) -> None:
    """
    Открывает чат кнопкой «Написать» и доводит мини-мессенджер до поля ввода.

    Штатный случай («переписки ещё нет») — мини-мессенджер разворачивается сам.
    В разведке 21.09.2026 был и второй случай: на том же объявлении, где уже
    существовал диалог, поле ввода не появилось. Повторная разведка в тот же
    день нашла причину (гонка с ленивой загрузкой виджета, см. _wait_for_widget)
    и лечение — но живьём оно разобрано не на 100%, поэтому здесь два
    безопасных повторных захода целиком (заново дождаться виджета, заново выбрать
    кнопку, кликнуть). Клик по «Написать» ничего не отправляет и не стоит денег —
    это не финансовый клик, ретрай здесь допустим.

    Между тремя заходами не больше двух безопасных перезагрузок страницы.
    """
    for attempt in range(3):
        try:
            if await _open_chat_once(page, item_path, ctx):
                _guard_same_item(page, item_path)
                return
        except ChatUnavailableError:
            # Перед ретраем и безопасной классификацией последней неудачи
            # проверяем страницу: чат мог увести на диалог, логин или капчу.
            await _checked_html(page)
            _guard_still_on_listing(page, item_path)
            if attempt == 2:
                raise
        if attempt < 2:
            # False от ожидания composer тоже мог прийти уже после redirect.
            await _checked_html(page)
            _guard_still_on_listing(page, item_path)
            logger.info("Чат не открылся — перезагружаю объявление и повторяю безопасно.")
            ctx["reloaded"] = True
            await page.reload(wait_until="domcontentloaded")
            await _checked_html(page)
            _guard_still_on_listing(page, item_path)
    # Последнее ожидание поля тоже могло завершиться переходом на логин,
    # капчу или отдельный диалог — это не безопасный автопропуск.
    await _checked_html(page)
    _guard_still_on_listing(page, item_path)
    raise ChatUnavailableError("Чат недоступен")


async def _count_outgoing(page: Page) -> int:
    """Считает исходящие сообщения в открытой ленте переписки."""
    return await page.evaluate(
        "selector => document.querySelectorAll(selector).length", sel.CHAT_OUTGOING_MESSAGE
    )


async def _describe_send_state(page: Page, before: int) -> str:
    """Читает состояние чата для внятной остановки. Только чтение, без кликов."""
    try:
        state = await page.evaluate(
            _SEND_STATE_JS, {"outgoing": sel.CHAT_OUTGOING_MESSAGE, "input": sel.CHAT_INPUT},
        )
        return (
            "Подтверждение отправки не получено: исходящих сообщений было "
            f"{before}, стало {state['outgoing']}; поле ввода "
            f"{'пустое' if state['empty'] else 'не пустое'}."
        )
    except Exception:
        logger.warning("Не удалось прочитать состояние чата", exc_info=True)
        return "Подтверждение отправки не получено, состояние чата прочитать не удалось."


async def _dump_stop_state(
    page: Page,
    *,
    item_index: int,
    candidate: dict | None,
    step: str,
    error_text: str,
    click_started: bool,
    total_candidates: int = 0,
    sent_so_far: int = 0,
    run_started_at: datetime | None = None,
    last_sent_at: datetime | None = None,
    reloaded: bool = False,
) -> None:
    """
    Сохраняет page.html/screenshot.png/state.txt при остановке OutreachStoppedError.

    Разбор таких остановок сейчас стоит отдельного живого прогона под РУ-VPN —
    это дорого, поэтому артефакты пишем сразу. Вызывающий код держит вызов в
    try/except: сбой дампа не должен ломать и подменять исходную ошибку.

    total_candidates/sent_so_far/run_started_at/last_sent_at/reloaded — сведения
    для различения двух гипотез остановки на open_chat (боевой прогон
    22.09.2026, см. CLAUDE.md): «чанк мессенджера завис в этой вкладке» против
    «Авито придерживает чат после нескольких писем подряд».
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump_dir = DEBUG_DIR / f"outreach_stop_{stamp}"
    dump_dir.mkdir(parents=True, exist_ok=True)
    try:
        html = await page.content()
        (dump_dir / "page.html").write_text(html, encoding="utf-8")
    except Exception:
        logger.warning("Дамп остановки рассылки: html не сохранён", exc_info=True)
    try:
        await page.screenshot(path=str(dump_dir / "screenshot.png"), full_page=True)
    except Exception:
        logger.warning("Дамп остановки рассылки: скриншот не снят", exc_info=True)
    # Состояние виджетов чата читаем отдельным блоком: сбой evaluate() не
    # должен обрушить остальные строки state.txt (URL, шаг, счётчики).
    widget_state: dict | None = None
    try:
        widget_state = await page.evaluate(
            _WIDGET_STATE_JS,
            {"miniMessenger": sel.MINI_MESSENGER, "unreadCounter": sel.HEADER_UNREAD_CHATS_COUNTER,
             "chatButton": sel.ITEM_CHAT_BUTTON},
        )
    except Exception:
        logger.warning("Дамп остановки рассылки: состояние виджета не прочитано", exc_info=True)
    try:
        seller_name = candidate.get("seller_name", "") if candidate else ""
        now = datetime.now(timezone.utc)
        since_start = f"{(now - run_started_at).total_seconds():.0f}" if run_started_at else "неизвестно"
        since_last_sent = (
            f"{(now - last_sent_at).total_seconds():.0f}" if last_sent_at else "писем ещё не было"
        )
        lines = [
            f"URL: {page.url}",
            f"Заголовок вкладки: {await page.title()}",
            f"Шаг: {step}",
            f"Кандидат: №{item_index} из {total_candidates} {seller_name}",
            f"Писем отправлено в этом прогоне: {sent_so_far}",
            f"Секунд с начала прогона: {since_start}",
            f"Секунд с момента отправки предыдущего письма: {since_last_sent}",
            "Контейнер мини-мессенджера в DOM: "
            + (f"{widget_state['miniMessenger']} шт." if widget_state else "не удалось проверить"),
            "Счётчик непрочитанных чатов (header/unread-chats-counter): "
            + (f"{widget_state['unreadCounter']} шт." if widget_state else "не удалось проверить"),
            "Копий кнопки «Написать» (messenger-button/button): "
            + (f"{widget_state['chatButton']} шт." if widget_state else "не удалось проверить"),
            f"Перезагрузка страницы на этом кандидате: {'да' if reloaded else 'нет'}",
            f"Клик отправки уже сделан: {'да' if click_started else 'нет'}",
            f"Ошибка: {error_text}",
        ]
        (dump_dir / "state.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        logger.warning("Дамп остановки рассылки: state.txt не сохранён", exc_info=True)
    logger.info("Остановка рассылки: дамп сохранён в %s", dump_dir)


async def _prepare_message(
    page: Page,
    candidate: dict,
    message: str,
    step_cb: StepCallback | None = None,
    item_index: int = 0,
    chat_ctx: dict | None = None,
):
    """Проверяет продавца и подписи перед необратимой отправкой.

    chat_ctx — словарь для _open_chat/_wait_for_widget (item_index,
    total_candidates, sent_so_far, reloaded); см. send_messages.
    """
    if step_cb:
        step_cb("check_seller", item_index)
    await page.wait_for_selector(sel.ITEM_SELLER_LINK, state="attached", timeout=15_000)
    html = await _checked_html(page)
    profile_links = BeautifulSoup(html, "html.parser").select(sel.ITEM_SELLER_LINK)
    identities = [urljoin(BASE_URL, link.get("href", "")) for link in profile_links]
    if not identities or not all(
        normalize_profile_url(url) == candidate["profile_url"]
        or (candidate.get("seller_id") and extract_seller_id(url) == candidate["seller_id"])
        for url in identities
    ):
        raise OutreachStoppedError("Продавец объявления не совпадает с выбранным кандидатом.")
    item_path = _item_path(page.url)
    if step_cb:
        step_cb("open_chat", item_index)
    await _open_chat(page, item_path, chat_ctx if chat_ctx is not None else {
        "item_index": item_index, "total_candidates": 1, "sent_so_far": 0, "reloaded": False,
    })
    await _checked_html(page)
    if step_cb:
        step_cb("fill_message", item_index)
    composer = page.locator(sel.CHAT_INPUT).filter(visible=True)
    if await composer.count() != 1:
        raise OutreachStoppedError("Не удалось однозначно определить поле ввода чата.")
    await composer.fill(message)
    typed = await composer.input_value()
    if typed != message:
        raise OutreachStoppedError(
            "Авито принял не тот текст письма: в поле "
            f"{_ui_length(typed)} символов вместо {_ui_length(message)}."
        )
    # Кнопка отправки существует в DOM только при непустом поле — ищем её
    # строго после ввода текста, иначе её там просто нет.
    send = page.locator(sel.CHAT_SEND).filter(visible=True)
    if await send.count() != 1:
        raise OutreachStoppedError("Не удалось однозначно определить кнопку отправки.")
    # У <span> нет видимого текста: подпись живёт только в aria-label,
    # а «доступность» — в aria-disabled (is_enabled() на span не равнозначен).
    label = " ".join((await send.get_attribute("aria-label") or "").split())
    if label not in sel.CHAT_SEND_ARIA_LABELS:
        raise OutreachStoppedError(f"Неизвестная кнопка отправки: {label!r}.")
    if (await send.get_attribute("aria-disabled") or "").strip().lower() == "true":
        raise OutreachStoppedError("Кнопка отправки помечена aria-disabled='true'.")
    return send, await _count_outgoing(page)


async def send_messages(
    candidates: list[dict],
    message: str,
    progress_cb: ProgressCallback | None = None,
    step_cb: StepCallback | None = None,
    start_index: int = 1,
) -> dict:
    """
    Отправляет подтверждённый список; sending/uncertain защищают от дублей.

    start_index (нумерация с 1) — с какого кандидата списка начинать; более
    ранние кандидаты пропускаются без всякой работы в браузере (checkpoint —
    см. outreach_state.py). step_cb(step_key, item_index) зовётся при входе
    в каждый шаг STEPS для текущего кандидата.
    """
    if not message.strip() or not candidates:
        raise ValueError("Выберите получателей и заполните шаблон письма")
    # До Chrome и до любых кликов: слишком длинный шаблон Авито обрежет молча.
    _check_message_fits(message)
    result = {"sent": 0, "failed": 0, "skipped": 0, "uncertain": 0, "results": []}
    current_step = ""
    # Для дампа остановки на open_chat (см. _dump_stop_state) — различить
    # «чанк завис в этой вкладке» от «Авито придерживает чат после нескольких
    # писем подряд» можно только по времени и числу уже ушедших писем.
    run_started_at = datetime.now(timezone.utc)
    last_sent_at: datetime | None = None

    def _step(name: str, idx: int) -> None:
        nonlocal current_step
        current_step = name
        if step_cb:
            try:
                step_cb(name, idx)
            except Exception:
                logger.warning("Не удалось обновить шаг рассылки", exc_info=True)

    async def _dump(
        item_index: int, candidate: dict | None, error_text: str, click_started: bool,
        reloaded: bool = False,
    ) -> None:
        try:
            await _dump_stop_state(
                page, item_index=item_index, candidate=candidate, step=current_step,
                error_text=error_text, click_started=click_started,
                total_candidates=len(candidates), sent_so_far=result["sent"],
                run_started_at=run_started_at, last_sent_at=last_sent_at, reloaded=reloaded,
            )
        except Exception:
            logger.warning("Не удалось сохранить дамп остановки рассылки", exc_info=True)

    async with _owned_page() as page:
        _step("connect_chrome", start_index)
        for index, candidate in enumerate(candidates):
            item_index = index + 1
            if item_index < start_index:
                # Возобновление: этот кандидат уже закрыт прошлым прогоном —
                # никакой работы в браузере для него не делаем.
                continue
            _progress(progress_cb, index, len(candidates), candidate["seller_name"])
            if is_contacted(candidate.get("seller_id"), candidate["profile_url"]):
                result["skipped"] += 1
                result["results"].append({**candidate, "status": "skipped"})
                continue
            _step("open_listing", item_index)
            listing_url = _avito_url(candidate["listing_url"])
            try:
                await page.goto(listing_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                logger.warning("Не загрузилось объявление %s: %s", listing_url, exc)
                record_contact(candidate, "failed", message=message, error=str(exc))
                result["failed"] += 1
                result["results"].append({**candidate, "status": "failed", "error": str(exc)})
                continue
            if await page.title() == "Ошибка 404. Страница не найдена":
                error = "Авито вернуло 404: страница объявления не найдена."
                logger.warning("Не найдено объявление %s", listing_url)
                record_contact(candidate, "failed", message=message, error=error)
                result["failed"] += 1
                result["results"].append({**candidate, "status": "failed", "error": error})
                continue
            click_started = False
            # Общий на кандидата контекст для _open_chat/_wait_for_widget:
            # честный текст остановки и признак «была перезагрузка» для дампа.
            chat_ctx = {
                "item_index": item_index, "total_candidates": len(candidates),
                "sent_so_far": result["sent"], "reloaded": False,
            }
            try:
                send, before = await _prepare_message(
                    page, candidate, message, step_cb=_step, item_index=item_index,
                    chat_ctx=chat_ctx,
                )
                # За время открытия чата пользователь мог импортировать этого продавца.
                if is_contacted(candidate.get("seller_id"), candidate["profile_url"]):
                    result["skipped"] += 1
                    result["results"].append({**candidate, "status": "skipped"})
                    continue
                # Сначала устойчивая запись, затем единственный необратимый клик.
                record_contact(candidate, "sending", message=message)
                click_started = True
                _step("send_click", item_index)
                await send.click(timeout=10_000)
                _step("confirm_sent", item_index)
                try:
                    await page.wait_for_function(
                        _CONFIRM_SENT_JS,
                        arg={"outgoing": sel.CHAT_OUTGOING_MESSAGE, "input": sel.CHAT_INPUT,
                             "before": before}, timeout=CHAT_CONFIRM_TIMEOUT_MS,
                    )
                except Exception as wait_error:
                    # Повторов нет: клик уже сделан. Говорим, что именно не сошлось.
                    raise OutreachStoppedError(
                        await _describe_send_state(page, before)
                    ) from wait_error
                await _checked_html(page)
                record_contact(candidate, "sent", message=message)
            except ChatUnavailableError as exc:
                logger.info("Чат недоступен у кандидата №%d, перехожу к следующему", item_index)
                record_contact(candidate, "failed", message=message, error=str(exc))
                result["failed"] += 1
                result["results"].append({**candidate, "status": "failed", "error": str(exc)})
                continue
            except (Exception, asyncio.CancelledError) as exc:
                status = "uncertain" if click_started else "failed"
                try:
                    record_contact(candidate, status, message=message, error=str(exc))
                except Exception:
                    logger.error("Не удалось записать результат отправки", exc_info=True)
                result[status] += 1
                result["results"].append({**candidate, "status": status, "error": str(exc)})
                if isinstance(exc, asyncio.CancelledError):
                    raise
                await _dump(item_index, candidate, str(exc), click_started, chat_ctx["reloaded"])
                raise OutreachStoppedError(
                    ("Результат отправки неизвестен; повтор заблокирован. " if click_started
                     else "Отправка остановлена до клика. ") + str(exc), result,
                ) from exc
            result["sent"] += 1
            result["results"].append({**candidate, "status": "sent"})
            last_sent_at = datetime.now(timezone.utc)
            _step("back_to_search", item_index)
            try:
                await page.goto(build_search_url(candidate["city"], candidate["query"]),
                                wait_until="domcontentloaded", timeout=30_000)
                await _checked_html(page)
            except Exception as exc:
                await _dump(item_index, candidate, str(exc), True, chat_ctx["reloaded"])
                raise OutreachStoppedError(
                    "Сообщение отправлено, но вернуться к выдаче не удалось. " + str(exc), result,
                ) from exc
            _progress(progress_cb, index + 1, len(candidates), candidate["seller_name"])
            if index + 1 < len(candidates):
                await _random_delay()
    return result
