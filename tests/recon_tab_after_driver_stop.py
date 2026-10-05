"""
Разведка №6 (аудит 2026-09-23, находка F21; live-recon-checklist, пункт 3):
остаётся ли вкладка Chrome открытой, если page.close() вызвать ПОСЛЕ
остановки драйвера Playwright (выход из `async with async_playwright()`).

Авито не нужен, VPN не важен. Скрипт сам запускает ОТДЕЛЬНЫЙ Chrome:
  - временный профиль (не avito-chrome-profile и не ваш обычный);
  - свой порт 9333 (рабочий Chrome для публикации сидит на 9222 — не трогаем);
  - только about:blank.

Два сценария на одном Chrome:
  A. «как было до брифа 05»: new_page() → выход из async with → page.close();
  B. «как стало»: new_page() → page.close() внутри async with → выход.
Число вкладок считается по http://127.0.0.1:9333/json/list (type == "page")
до и после каждого сценария. Итог печатается и пишется в
debug/recon-resume/tab_after_driver_stop.txt.

Запуск из корня проекта (PowerShell):
    .venv\\Scripts\\python.exe tests\\recon_tab_after_driver_stop.py

Если Chrome стоит не в стандартном месте:
    $env:CHROME_PATH = "D:\\путь\\к\\chrome.exe"
    .venv\\Scripts\\python.exe tests\\recon_tab_after_driver_stop.py

Имя файла намеренно не test_*.py: unittest discover его не запускает.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

PORT = 9333
CDP_URL = f"http://127.0.0.1:{PORT}"
DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "debug" / "recon-resume"
OUT_FILE = OUT_DIR / "tab_after_driver_stop.txt"

logger = logging.getLogger("recon_tab_after_driver_stop")


def _page_targets() -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=5) as response:
        targets = json.loads(response.read().decode("utf-8"))
    return [t for t in targets if t.get("type") == "page"]


def _wait_cdp(timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"Chrome не открыл CDP на порту {PORT} за {timeout_s:.0f} с")


def _start_chrome(profile_dir: Path) -> subprocess.Popen:
    chrome = os.environ.get("CHROME_PATH") or DEFAULT_CHROME
    if not Path(chrome).is_file():
        raise RuntimeError(
            f"Не найден chrome.exe: {chrome}. Укажите путь в переменной CHROME_PATH."
        )
    return subprocess.Popen([
        chrome,
        f"--remote-debugging-port={PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ])


async def _scenario(close_inside: bool) -> dict[str, Any]:
    """Одна вкладка about:blank; close() внутри или после async with."""
    from playwright.async_api import async_playwright

    before = len(_page_targets())
    page = None
    close_result = "не вызывался"
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto("about:blank")
        opened = len(_page_targets())
        if close_inside:
            try:
                await page.close()
                close_result = "внутри async with: вернул без ошибки"
            except Exception as exc:  # noqa: BLE001 — фиксируем как есть
                close_result = f"внутри async with: {type(exc).__name__}: {exc}"
    # Драйвер остановлен.
    if not close_inside:
        try:
            await page.close()
            close_result = "после выхода из async with: вернул без ошибки"
        except Exception as exc:  # noqa: BLE001
            close_result = f"после выхода из async with: {type(exc).__name__}: {exc}"
    await asyncio.sleep(1.0)
    after = len(_page_targets())
    return {
        "scenario": "B (close внутри)" if close_inside else "A (close после остановки)",
        "tabs_before": before,
        "tabs_with_our_page": opened,
        "tabs_after": after,
        "tab_left_open": after > before,
        "close_result": close_result,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    profile_dir = Path(tempfile.mkdtemp(prefix="recon-tab-profile-"))
    chrome = None
    lines: list[str] = [
        f"Разведка №6 (F21), {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Chrome: {os.environ.get('CHROME_PATH') or DEFAULT_CHROME}, порт {PORT}",
    ]
    code = 0
    try:
        chrome = _start_chrome(profile_dir)
        _wait_cdp()
        for close_inside in (False, True):
            result = asyncio.run(_scenario(close_inside))
            verdict = "ВКЛАДКА ОСТАЛАСЬ" if result["tab_left_open"] else "вкладка закрыта"
            lines.append(
                f"{result['scenario']}: вкладок до={result['tabs_before']}, "
                f"с нашей={result['tabs_with_our_page']}, после={result['tabs_after']} "
                f"— {verdict}; close(): {result['close_result']}"
            )
    except Exception as exc:  # noqa: BLE001 — итог разведки пишем в любом случае
        lines.append(f"Разведка не завершилась: {type(exc).__name__}: {exc}")
        code = 1
    finally:
        if chrome is not None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
        # Временный профиль создан этим же скриптом вне проекта.
        shutil.rmtree(profile_dir, ignore_errors=True)

    text = "\n".join(lines) + "\n"
    for line in lines:
        logger.info(line)
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        OUT_FILE.write_text(text, encoding="utf-8")
        logger.info("Итог записан: %s", OUT_FILE)
    except OSError as exc:
        logger.error("Итог не записан в %s: %s", OUT_FILE, exc)
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
