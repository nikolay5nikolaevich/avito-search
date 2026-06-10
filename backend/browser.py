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

import logging
import pathlib
from typing import Any

from playwright.async_api import Browser, BrowserContext

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

    При неудаче подключения поднимает RuntimeError с инструкцией.
    """
    logger.info("CDP: подключаемся к браузеру по адресу %s", cdp_url)
    try:
        browser: Browser = await pw.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        # Понятное сообщение, если Chrome не запущен с нужным флагом
        raise RuntimeError(
            f"Не удалось подключиться к Chrome по CDP ({cdp_url}).\n"
            "Убедитесь, что Chrome запущен с флагом --remote-debugging-port.\n"
            "Пример команды:\n"
            '  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            "--remote-debugging-port=9222 "
            '--user-data-dir="C:\\Users\\TBG\\avito-chrome-profile"\n'
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
