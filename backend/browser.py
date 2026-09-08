"""
Общий модуль работы с браузером (Playwright) для парсера и публикатора.

Содержит две стратегии получения браузерного контекста:

1. connect_over_cdp(pw, cdp_url) — ОСНОВНОЙ рабочий способ.
   Подключение к Chrome, запущенному пользователем вручную через
   start-chrome.bat (флаг --remote-debugging-port=9222). Такой Chrome
   не содержит следов автоматизации и не банится Авито.

2. launch_persistent_context(pw, headless) — fallback.
   Собственный браузер Playwright с антидетект-мерами и persistent-профилем
   (.pw-profile/). На практике Авито такой браузер банит, но способ оставлен
   для диагностики и на случай изменения политики Авито.

Модуль вынесен из parser.py, чтобы те же функции использовал publisher.py.
Поведение функций при выносе НЕ менялось (чистый рефакторинг).
"""

import asyncio
import json
import logging
import pathlib
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from playwright.async_api import (
    Browser,
    BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
)
from websockets.asyncio.client import connect as websocket_connect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

# User-Agent — актуальный Chrome 124 Desktop на Windows.
# При запуске реального Chrome (channel="chrome") этот UA не подставляется —
# Chrome сам отдаёт корректный UA. Используется только с Chromium-fallback.
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Папка для persistent-профиля браузера (куки, localStorage, сессия Авито).
# .pw-profile/ добавлена в .gitignore — личные данные не коммитим.
PROFILE_DIR: pathlib.Path = pathlib.Path(".pw-profile")

# Сетевые операции к локальному CDP должны завершаться быстро: если Chrome
# завис или запущен без remote debugging, нет смысла блокировать задачу минуты.
CDP_HTTP_TIMEOUT_SECONDS: float = 3.0
CDP_CONNECT_TIMEOUT_MS: int = 30_000
CDP_TARGET_PROBE_TIMEOUT_SECONDS: float = 3.0

# Chrome 150 создаёт этот служебный target для окна браузера. Его можно
# диагностировать через /json/list, но НЕЛЬЗЯ закрывать через /json/close:
# живая проверка показала, что это может завершить весь пользовательский
# Chrome вместе с обычными вкладками. Для browser_ui preflight всегда read-only;
# publisher закрывает только обычные вкладки Avito, не ответившие на CDP-проверку.
_OMNIBOX_TARGET_TYPE = "browser_ui"
_OMNIBOX_TARGET_TITLE = "Omnibox Popup"
_AVITO_HOSTS = {"avito.ru", "www.avito.ru"}


class CdpEndpointUnavailableError(RuntimeError):
    """Локальный HTTP endpoint Chrome недоступен или отвечает не как CDP."""


class CdpInitializationTimeoutError(RuntimeError):
    """CDP доступен, но Playwright не завершил инициализацию соединения."""


class CdpUnresponsivePageError(RuntimeError):
    """В Chrome есть зависшая вкладка, которую нельзя безопасно закрыть автоматически."""

# Init-скрипт для маскировки автоматизации Playwright.
# Выполняется в каждой новой странице ДО загрузки HTML.
STEALTH_SCRIPT: str = """
// Убираем флаг автоматизации
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true
});

// Русские языки — как у обычного пользователя из России
Object.defineProperty(navigator, 'languages', {
    get: () => ['ru-RU', 'ru'],
    configurable: true
});

// Эмулируем объект window.chrome, который есть в настоящем Chrome
if (!window.chrome) {
    window.chrome = {
        runtime: {},
        loadTimes: function() {},
        csi: function() {},
        app: {}
    };
}

// Эмулируем плагины — пустой массив выдаёт headless-режим
const pluginData = [
    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
];
const fakePlugins = Object.create(PluginArray.prototype);
Object.defineProperty(fakePlugins, 'length', { get: () => pluginData.length });
pluginData.forEach((p, i) => {
    const plugin = Object.create(Plugin.prototype);
    Object.defineProperty(plugin, 'name', { get: () => p.name });
    Object.defineProperty(plugin, 'filename', { get: () => p.filename });
    Object.defineProperty(plugin, 'description', { get: () => p.description });
    Object.defineProperty(fakePlugins, i, { get: () => plugin });
});
Object.defineProperty(navigator, 'plugins', { get: () => fakePlugins, configurable: true });

// Патч navigator.permissions.query — chrome headless возвращает 'denied' для notifications
const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
window.navigator.permissions.query = (parameters) => {
    if (parameters.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission });
    }
    return originalQuery(parameters);
};
"""


# ---------------------------------------------------------------------------
# Публичные функции
# ---------------------------------------------------------------------------


def _cdp_http_url(cdp_url: str, path: str) -> str:
    """Добавляет ``/json/*`` после path-prefix, сохраняя query endpoint-а."""
    parsed = urlsplit(cdp_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Некорректный CDP-адрес: {cdp_url}")
    prefix = parsed.path.rstrip("/")
    full_path = f"{prefix}/{path.lstrip('/')}"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, full_path, parsed.query, "")
    )


def _read_cdp_json(cdp_url: str, path: str) -> Any:
    request = Request(
        _cdp_http_url(cdp_url, path),
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=CDP_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _close_cdp_target(cdp_url: str, target_id: str) -> None:
    """Закрывает одну точно выбранную вкладку через локальный HTTP API Chrome."""
    safe_target_id = quote(target_id, safe="")
    request = Request(
        _cdp_http_url(cdp_url, f"/json/close/{safe_target_id}"),
        headers={"Accept": "application/json, text/plain"},
        method="GET",
    )
    with urlopen(request, timeout=CDP_HTTP_TIMEOUT_SECONDS) as response:
        response.read()


async def _probe_page_target(target: dict[str, Any]) -> bool:
    """Проверяет, отвечает ли renderer вкладки на короткую CDP-команду."""
    websocket_url = str(target.get("webSocketDebuggerUrl") or "").strip()
    if not websocket_url:
        logger.warning(
            "CDP: у вкладки %s нет webSocketDebuggerUrl; пропускаем проверку",
            target.get("id", "без id"),
        )
        return True

    async def evaluate() -> bool:
        async with websocket_connect(
            websocket_url,
            open_timeout=CDP_TARGET_PROBE_TIMEOUT_SECONDS,
            close_timeout=1,
        ) as websocket:
            command_id = 1
            await websocket.send(
                json.dumps(
                    {
                        "id": command_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": "document.readyState",
                            "returnByValue": True,
                        },
                    }
                )
            )
            while True:
                message = json.loads(await websocket.recv())
                if message.get("id") == command_id:
                    return "error" not in message

    try:
        return await asyncio.wait_for(
            evaluate(),
            timeout=CDP_TARGET_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "CDP: вкладка не ответила на проверку %s (%s): %s",
            target.get("title") or "без названия",
            target.get("url") or "без URL",
            exc,
        )
        return False


def _is_avito_page(target: dict[str, Any]) -> bool:
    parsed = urlsplit(str(target.get("url") or ""))
    return (
        parsed.scheme.lower() in {"http", "https"}
        and (parsed.hostname or "").lower() in _AVITO_HOSTS
    )


async def _repair_unresponsive_page_targets(
    cdp_url: str,
    targets: list[dict[str, Any]],
) -> None:
    """Закрывает только доказанно зависшие вкладки Авито перед публикацией."""
    page_targets = [target for target in targets if target.get("type") == "page"]
    if not page_targets:
        return

    responsive = await asyncio.gather(
        *(_probe_page_target(target) for target in page_targets)
    )
    unresponsive = [
        target
        for target, is_responsive in zip(page_targets, responsive, strict=True)
        if not is_responsive
    ]
    foreign_pages = [target for target in unresponsive if not _is_avito_page(target)]
    if foreign_pages:
        page = foreign_pages[0]
        raise CdpUnresponsivePageError(
            "Chrome содержит зависшую вкладку другого сайта: "
            f"«{page.get('title') or 'без названия'}» "
            f"({page.get('url') or 'без URL'}). Закрой или перезагрузи её вручную "
            "и повтори публикацию; сервис не закрывает чужие вкладки автоматически."
        )

    for target in unresponsive:
        target_id = str(target.get("id") or "").strip()
        if not target_id:
            raise CdpUnresponsivePageError(
                "Обнаружена зависшая вкладка Авито без target id; закрой её вручную "
                "и повтори публикацию."
            )
        await asyncio.to_thread(_close_cdp_target, cdp_url, target_id)
        logger.info(
            "CDP: закрыта зависшая вкладка Авито %s (%s)",
            target.get("url", "без URL"),
            target_id,
        )


def _is_stale_publish_page(target: dict[str, Any]) -> bool:
    """True только для точных маршрутов, оставшихся от publish-flow Авито."""
    if target.get("type") != "page" or not _is_avito_page(target):
        return False
    path = urlsplit(str(target.get("url") or "")).path.rstrip("/") or "/"
    return (
        path == "/additem"
        or path == "/pro/performance"
        or path == "/profile/pro/items"
        or (
            path.startswith("/cpxpromo/")
            and path.removeprefix("/cpxpromo/").isdigit()
        )
    )


async def _cleanup_stale_publish_targets(
    cdp_url: str,
    targets: list[dict[str, Any]],
) -> set[str]:
    """Закрывает только точные остаточные вкладки публикации в профиле Авито."""
    closed_ids: set[str] = set()
    for target in targets:
        if not _is_stale_publish_page(target):
            continue
        target_id = str(target.get("id") or "").strip()
        if not target_id:
            continue
        await asyncio.to_thread(_close_cdp_target, cdp_url, target_id)
        closed_ids.add(target_id)
        logger.info(
            "CDP: закрыта остаточная publish-вкладка Авито %s (%s)",
            target.get("url", "без URL"),
            target_id,
        )
    return closed_ids


async def _prepare_cdp_endpoint(
    cdp_url: str,
    *,
    repair_unresponsive_avito_pages: bool = False,
    cleanup_stale_publish_pages: bool = False,
) -> None:
    """Проверяет HTTP endpoint Chrome и при публикации лечит зависшие вкладки Авито."""
    await asyncio.to_thread(_read_cdp_json, cdp_url, "/json/version")

    try:
        targets = await asyncio.to_thread(_read_cdp_json, cdp_url, "/json/list")
    except Exception as exc:
        logger.warning("CDP: не удалось прочитать /json/list: %s", exc)
        return

    if not isinstance(targets, list):
        logger.warning("CDP: /json/list вернул неожиданный формат")
        return

    typed_targets = [target for target in targets if isinstance(target, dict)]
    page_targets = [target for target in typed_targets if target.get("type") == "page"]
    logger.info(
        "CDP: целей всего %d, вкладок type=page %d",
        len(typed_targets),
        len(page_targets),
    )
    closed_ids: set[str] = set()
    if cleanup_stale_publish_pages:
        closed_ids = await _cleanup_stale_publish_targets(cdp_url, typed_targets)
    if repair_unresponsive_avito_pages:
        remaining_targets = [
            target
            for target in typed_targets
            if str(target.get("id") or "") not in closed_ids
        ]
        await _repair_unresponsive_page_targets(cdp_url, remaining_targets)

    for target in typed_targets:
        if (
            target.get("type") != _OMNIBOX_TARGET_TYPE
            or target.get("title") != _OMNIBOX_TARGET_TITLE
        ):
            continue
        logger.warning(
            "CDP: обнаружен служебный target %s (%s); не закрываем его, "
            "поскольку /json/close может завершить пользовательский Chrome",
            _OMNIBOX_TARGET_TITLE,
            target.get("id", "без id"),
        )


async def launch_persistent_context(
    pw: Any,
    headless: bool,
) -> BrowserContext:
    """
    Запускает браузер как persistent context с антидетект-настройками.

    Сначала пытается использовать реальный установленный Chrome
    (channel="chrome"). Если Chrome не найден — fallback на встроенный
    Chromium с логом WARNING.

    Persistent context (.pw-profile/) сохраняет куки и сессию между
    запусками — Авито реже гоняет проверки у «знакомого» браузера.
    """
    # Убеждаемся, что папка профиля существует
    PROFILE_DIR.mkdir(exist_ok=True)
    profile_path = str(PROFILE_DIR.resolve())

    # Общие kwargs для launch_persistent_context
    ctx_kwargs: dict[str, Any] = dict(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        # Реалистичный viewport — самый популярный у десктоп-пользователей
        viewport={"width": 1366, "height": 768},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        extra_http_headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
        },
    )

    # Попытка 1: реальный Chrome — он сам даёт корректный UA, не переопределяем
    try:
        context = await pw.chromium.launch_persistent_context(
            profile_path,
            channel="chrome",
            **ctx_kwargs,
        )
        logger.info("Браузер запущен: реальный Chrome (channel='chrome')")
    except Exception as chrome_err:
        # Chrome не установлен или не найден Playwright — используем Chromium
        logger.warning(
            "Реальный Chrome не найден (%s) — fallback на встроенный Chromium",
            chrome_err,
        )
        # При Chromium устанавливаем UA явно, чтобы не светить headless-строкой
        ctx_kwargs["user_agent"] = USER_AGENT
        context = await pw.chromium.launch_persistent_context(
            profile_path,
            **ctx_kwargs,
        )
        logger.info("Браузер запущен: встроенный Chromium (fallback)")

    # Регистрируем stealth-скрипт — выполнится в каждой новой странице
    await context.add_init_script(STEALTH_SCRIPT)
    logger.debug("Stealth init-скрипт зарегистрирован на контексте")

    return context


async def connect_over_cdp(
    pw: Any,
    cdp_url: str,
    *,
    repair_unresponsive_avito_pages: bool = False,
    cleanup_stale_publish_pages: bool = False,
) -> BrowserContext:
    """
    Подключается к уже запущенному пользовательскому Chrome через CDP.

    Пользователь должен запустить Chrome ВРУЧНУЮ командой:
        chrome.exe --remote-debugging-port=9222 --user-data-dir=<путь>

    Затем зайти на avito.ru вручную (прогреть сессию) и только потом
    вызывать этот метод.

    Возвращает context — существующий контекст браузера (contexts[0]) или новый.
    Объект browser наружу не отдаётся: контекст держит ссылку на него внутри
    Playwright, GC соединение не закроет.

    ВАЖНО: не закрывай context (и тем более browser) — это Chrome пользователя.
    Закрывай только страницы (page), которые сам открыл.

    ``repair_unresponsive_avito_pages=True`` разрешено только publisher: перед
    новым пакетом он проверяет renderer каждой вкладки и закрывает только
    доказанно зависшие страницы Авито, блокирующие весь CDP-сеанс.

    При неудаче подключения поднимает RuntimeError с инструкцией.
    """
    logger.info("CDP: подключаемся к браузеру по адресу %s", cdp_url)
    endpoint_scheme = urlsplit(cdp_url).scheme.lower()
    http_preflight_done = False
    if endpoint_scheme in {"http", "https"}:
        try:
            await _prepare_cdp_endpoint(
                cdp_url,
                repair_unresponsive_avito_pages=repair_unresponsive_avito_pages,
                cleanup_stale_publish_pages=cleanup_stale_publish_pages,
            )
            http_preflight_done = True
        except CdpUnresponsivePageError:
            raise
        except Exception as exc:
            raise CdpEndpointUnavailableError(
                f"CDP endpoint недоступен или вернул некорректный ответ ({cdp_url}). "
                "Останови текущий Chrome для Авито, перезапусти start-chrome.bat "
                "и снова открой avito.ru в появившемся окне. "
                f"Исходная ошибка: {exc}"
            ) from exc
    try:
        browser: Browser = await pw.chromium.connect_over_cdp(
            cdp_url,
            timeout=CDP_CONNECT_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        websocket_connected = "<ws connected>" in str(exc)
        if websocket_connected:
            detail = "WebSocket подключён, но Playwright не завершил инициализацию"
        elif http_preflight_done:
            detail = "CDP endpoint доступен, но Playwright не завершил подключение"
        else:
            detail = "Playwright не завершил подключение к CDP WebSocket"
        raise CdpInitializationTimeoutError(
            f"{detail} за 30 секунд ({cdp_url}). Останови текущий Chrome для "
            "Авито, перезапусти start-chrome.bat и снова открой avito.ru. "
            f"Исходная ошибка: {exc}"
        ) from exc
    except Exception as exc:
        if http_preflight_done:
            detail = "CDP endpoint доступен, но Playwright не смог подключиться"
        else:
            detail = "Playwright не смог подключиться к CDP WebSocket"
        raise RuntimeError(
            f"{detail} ({cdp_url}). "
            "Останови текущий Chrome для Авито, перезапусти start-chrome.bat "
            "и снова открой avito.ru. "
            f"Исходная ошибка: {exc}"
        ) from exc

    # Берём существующий контекст (вкладки пользователя) или создаём новый
    if browser.contexts:
        context: BrowserContext = browser.contexts[0]
        logger.info("CDP: используем существующий контекст (contexts[0])")
    else:
        context = await browser.new_context()
        logger.info("CDP: создан новый контекст (contexts[0] не было)")

    # Stealth-скрипт — дополнительная мера, хотя Chrome пользователя уже «чистый»
    try:
        await context.add_init_script(STEALTH_SCRIPT)
        logger.debug("CDP: stealth init-скрипт добавлен в контекст")
    except Exception as exc:
        # В режиме CDP add_init_script может не поддерживаться — не критично
        logger.debug("CDP: add_init_script не удалось применить: %s", exc)

    return context
