"""
Публикатор черновиков объявлений Авито (категория «Пиджаки и костюмы»).

Стейт-машина из 12 шагов (ТЗ §9): connect_chrome → open_form → select_category →
check_category → fill_title → upload_photos → fill_fields → fill_description →
fill_price → fill_address → save_draft → done.

Пакетный режим (ТЗ §16): одна форма → N одинаковых черновиков (1–10).
Черновики делаются ПОСЛЕДОВАТЕЛЬНО в одной задаче: connect_chrome один раз,
затем полный цикл open_form → save_draft на каждый черновик, пауза 5–15 с
между черновиками. step/step_label/done/total описывают ТЕКУЩИЙ черновик;
прогресс пакета — в draft_index/drafts_total/drafts_saved/saved_urls.

Работает ТОЛЬКО через Chrome, запущенный пользователем (start-chrome.bat + CDP).
Логин/пароль/куки не хранятся; закрываем только собственную вкладку.

╔══════════════════════════════════════════════════════════════════════════╗
║ ЖЁСТКОЕ ПРАВИЛО БЕЗОПАСНОСТИ (ТЗ §2):                                     ║
║ Кнопка «Продолжить» (data-marker «item-edit/button-next») НЕ кликается    ║
║ НИКОГДА — она публикует объявление, у аккаунта платный тариф с оплатой    ║
║ просмотров: случайная публикация = списание реальных денег.               ║
║ Единственная разрешённая финальная кнопка — «Сохранить и выйти»           ║
║ (avito_publish_selectors.SAVE_AND_EXIT_BUTTON).                           ║
╚══════════════════════════════════════════════════════════════════════════╝

Статусы задачи: queued / running / needs_user_action / done / failed.
needs_user_action — ТЕРМИНАЛЬНЫЙ (v1 без resume): пользователь получает
инструкцию, делает действие руками и перезапускает задачу заново.

Самотесты валидации: python backend/publisher.py
"""

import asyncio
import datetime
import json
import logging
import pathlib
import random
import shutil
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Optional

import avito_publish_selectors as psel
import category_profiles
from category_profiles import CategoryProfile

# ---------------------------------------------------------------------------
# Константы сценария
# ---------------------------------------------------------------------------

ADDITEM_URL: str = "https://www.avito.ru/additem"

# Шаги стейт-машины: (имя шага, человекочитаемая подпись для фронта)
STEPS: list[tuple[str, str]] = [
    ("connect_chrome", "Подключение к Chrome"),
    ("open_form", "Открытие формы Avito"),
    ("select_category", "Выбор категории"),
    ("check_category", "Проверка категории"),
    ("fill_title", "Название объявления"),
    ("upload_photos", "Загрузка фотографий"),
    ("fill_fields", "Заполнение характеристик"),
    ("fill_description", "Описание"),
    ("fill_price", "Цена"),
    ("fill_address", "Адрес"),
    ("save_draft", "Сохранение черновика"),
    ("done", "Готово"),
]
TOTAL_STEPS: int = len(STEPS)
STEP_LABELS: dict[str, str] = dict(STEPS)
_STEP_INDEX: dict[str, int] = {name: i + 1 for i, (name, _) in enumerate(STEPS)}

# Паузы между действиями — человекоподобность (у Авито антибот)
PAUSE_MIN: float = 0.5
PAUSE_MAX: float = 1.5

# Таймауты, мс
WAIT_FORM_TIMEOUT_MS: int = 30_000        # ожидание формы после goto
WAIT_SELECTOR_TIMEOUT_MS: int = 10_000    # обычное ожидание элемента
PHOTO_UPLOAD_TIMEOUT_S: float = 120.0     # фото грузятся на сервер — с запасом
GEO_SUGGEST_TIMEOUT_MS: int = 10_000      # ожидание гео-саджеста
GEO_RETRIES: int = 3                      # ретраи ввода адреса
SAVE_EXIT_TIMEOUT_S: float = 30.0         # ожидание ухода со страницы после save_draft

# Пакетный режим (ТЗ §16): сколько одинаковых черновиков делаем за задачу
DRAFTS_MIN: int = 1
DRAFTS_MAX: int = 10
DRAFTS_DEFAULT: int = 1
DRAFT_PAUSE_MIN_S: float = 5.0            # пауза между черновиками, с
DRAFT_PAUSE_MAX_S: float = 15.0

# Ограничения валидации (ТЗ §8)
MIN_PHOTOS: int = 1
MAX_PHOTOS: int = 10
MAX_PHOTO_SIZE_BYTES: int = 25 * 1024 * 1024  # 25 МБ на файл
ALLOWED_PHOTO_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".heic"}
)
ALLOWED_PHOTO_MIME: frozenset[str] = frozenset(
    {"image/jpeg", "image/pjpeg", "image/png", "image/gif", "image/heic"}
)

# Корень проекта: файловый хэндлер логгера создаётся при ИМПОРТЕ модуля,
# то есть ДО os.chdir(PROJECT_ROOT) в app.py — путь к logs/ должен быть
# абсолютным, иначе при запуске сервера из backend/ лог уезжает в backend/logs/.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Каталоги артефактов (относительные пути резолвятся после chdir в app.py)
DEBUG_PUBLISH_DIR = pathlib.Path("debug") / "publish"
LOGS_DIR = PROJECT_ROOT / "logs"
# База папок подготовки вариантов (ТЗ §17): tmp/publish/prep_{prep_id}/.
# Должна совпадать с app.TMP_PUBLISH_DIR (импорт из app.py невозможен — цикл).
TMP_PUBLISH_DIR = pathlib.Path("tmp") / "publish"

# ---------------------------------------------------------------------------
# Логгер 'publisher' → logs/publisher.log (+ наследование консоли от root)
# ---------------------------------------------------------------------------

logger = logging.getLogger("publisher")


def _setup_publisher_logger() -> None:
    """Добавляет файловый хэндлер logs/publisher.log (однократно)."""
    if logger.handlers:
        return
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        handler = logging.FileHandler(
            LOGS_DIR / "publisher.log", mode="a", encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    except Exception as exc:  # логгер не должен ронять сервер
        logging.getLogger(__name__).warning(
            "Не удалось настроить logs/publisher.log: %s", exc
        )


_setup_publisher_logger()


# ---------------------------------------------------------------------------
# Исключения стейт-машины
# ---------------------------------------------------------------------------

class StepError(Exception):
    """Ошибка шага: задача завершается статусом failed."""


class UserActionRequired(Exception):
    """Нужно действие пользователя: статус needs_user_action (терминальный).

    args[0] — человекочитаемая инструкция для пользователя.
    """


# ---------------------------------------------------------------------------
# Данные черновика и валидация (ТЗ §8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DraftData:
    """Нормализованные данные черновика, прошедшие валидацию."""

    title: str
    trade_type: str        # ключ profile.trade_type_options
    condition: str         # ключ profile.condition_options
    size: str              # ключ profile.size_options
    brand: str
    color: str             # ключ profile.color_options
    description: str
    price: int             # рубли, целое > 0
    city: str
    address: str
    photo_paths: tuple[str, ...]
    category: str = "jackets"  # ключ профиля категории (category_profiles)

    def full_address(self) -> str:
        """Строка для гео-саджеста Авито: «Город, адрес»."""
        return f"{self.city}, {self.address}"

    def summary(self) -> dict[str, Any]:
        """Сводка введённых данных для /api/publish/result (без содержимого фото)."""
        return {
            "title": self.title,
            "trade_type": self.trade_type,
            "condition": self.condition,
            "size": self.size,
            "brand": self.brand,
            "color": self.color,
            "description": self.description,
            "price": self.price,
            "city": self.city,
            "address": self.address,
            "category": self.category,
            "photos_count": len(self.photo_paths),
        }


def parse_drafts_count(raw: Any) -> Optional[int]:
    """
    Нормализует поле «Сколько черновиков» (ТЗ §16).

    Отсутствие/пустая строка → DRAFTS_DEFAULT (обратная совместимость:
    запрос без drafts_count работает как раньше — 1 черновик).
    Целое число в диапазоне [DRAFTS_MIN, DRAFTS_MAX] → это число.
    Всё остальное (нечисловое, 0, 11, ...) → None (невалидно).
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return DRAFTS_DEFAULT
    try:
        value = int(text)
    except (ValueError, TypeError):
        return None
    if not (DRAFTS_MIN <= value <= DRAFTS_MAX):
        return None
    return value


def validate_publish_form(
    fields: dict[str, Any],
    photos: list[tuple[str, Optional[str], int]],
    profile: CategoryProfile,
) -> list[dict[str, str]]:
    """
    Серверная валидация формы черновика (ТЗ §8). Backend не доверяет фронту.

    Аргументы:
        fields: сырые текстовые поля формы:
                title, trade_type, condition, size, brand, color,
                description, price, city, address,
                drafts_count (необязательное, 1–10, дефолт 1 — ТЗ §16).
        photos: метаданные файлов — список кортежей
                (имя_файла, content_type или None, размер_в_байтах).
        profile: профиль категории (category_profiles.CategoryProfile) —
                 источник словарей допустимых значений size/color/
                 trade_type/condition для выбранной категории.

    Возвращает список ошибок [{"field": ..., "error": ...}];
    пустой список = форма валидна. Никогда не бросает исключений.
    """
    errors: list[dict[str, str]] = []

    def _text(name: str) -> str:
        return str(fields.get(name) or "").strip()

    # --- Обязательные текстовые поля ---
    for field_name, label in (
        ("title", "Название"),
        ("description", "Описание"),
        ("brand", "Бренд"),
        ("city", "Город"),
        ("address", "Адрес"),
    ):
        if not _text(field_name):
            errors.append({"field": field_name, "error": f"{label}: поле не заполнено"})

    # --- Цена: целое число > 0 ---
    price_raw = str(fields.get("price") if fields.get("price") is not None else "").strip()
    try:
        price = int(price_raw)
        if price <= 0:
            errors.append({"field": "price", "error": "Цена должна быть больше нуля"})
    except (ValueError, TypeError):
        errors.append({"field": "price", "error": "Цена должна быть целым числом"})

    # --- Сколько черновиков: 1–10, дефолт 1 (ТЗ §16) ---
    if parse_drafts_count(fields.get("drafts_count")) is None:
        errors.append({
            "field": "drafts_count",
            "error": f"Сколько черновиков: целое число от {DRAFTS_MIN} до {DRAFTS_MAX}",
        })

    # --- Select/radio: только ключи словарей профиля категории ---
    for field_name, options, label in (
        ("trade_type", profile.trade_type_options, "Вид объявления"),
        ("condition", profile.condition_options, "Состояние"),
        ("size", profile.size_options, "Размер"),
        ("color", profile.color_options, "Цвет"),
    ):
        value = _text(field_name)
        if value not in options:
            errors.append({
                "field": field_name,
                "error": f"{label}: недопустимое значение {value!r}",
            })

    # --- Фотографии: 1–10, форматы, размер ---
    if not (MIN_PHOTOS <= len(photos) <= MAX_PHOTOS):
        errors.append({
            "field": "photos",
            "error": f"Фотографий должно быть от {MIN_PHOTOS} до {MAX_PHOTOS}, "
                     f"передано {len(photos)}",
        })
    else:
        for filename, content_type, size in photos:
            ext = pathlib.Path(filename or "").suffix.lower()
            mime = (content_type or "").lower()
            if ext not in ALLOWED_PHOTO_EXTENSIONS and mime not in ALLOWED_PHOTO_MIME:
                errors.append({
                    "field": "photos",
                    "error": f"Файл {filename!r}: недопустимый формат "
                             f"(разрешены jpeg/png/gif/heic)",
                })
            if size > MAX_PHOTO_SIZE_BYTES:
                errors.append({
                    "field": "photos",
                    "error": f"Файл {filename!r}: больше 25 МБ",
                })

    return errors


def build_draft_data(
    fields: dict[str, Any],
    photo_paths: list[str],
    category: str = "jackets",
) -> DraftData:
    """Собирает DraftData из ПРОВАЛИДИРОВАННЫХ полей формы."""
    return DraftData(
        title=str(fields["title"]).strip(),
        trade_type=str(fields["trade_type"]).strip(),
        condition=str(fields["condition"]).strip(),
        size=str(fields["size"]).strip(),
        brand=str(fields["brand"]).strip(),
        color=str(fields["color"]).strip(),
        description=str(fields["description"]).strip(),
        price=int(str(fields["price"]).strip()),
        city=str(fields["city"]).strip(),
        address=str(fields["address"]).strip(),
        photo_paths=tuple(photo_paths),
        category=str(category or "jackets").strip(),
    )


# ---------------------------------------------------------------------------
# Вспомогательные функции стейт-машины
# ---------------------------------------------------------------------------

async def _pause() -> None:
    """Человекоподобная пауза между шагами/действиями: 0.5–1.5 с."""
    await asyncio.sleep(random.uniform(PAUSE_MIN, PAUSE_MAX))


def _set_step(job: dict[str, Any], step_name: str) -> None:
    """Переводит задачу на шаг step_name (обновляет step/step_label/done)."""
    job["step"] = step_name
    job["step_label"] = STEP_LABELS.get(step_name, step_name)
    job["done"] = _STEP_INDEX.get(step_name, 0)
    logger.info("Шаг %d/%d: %s", job["done"], TOTAL_STEPS, step_name)


def _page_is_closed(page: Any) -> bool:
    """Безопасно проверяет, закрыта ли вкладка."""
    if page is None:
        return True
    try:
        return bool(page.is_closed())
    except Exception:
        return True


async def _dump_failure(
    page: Any,
    job_id: str,
    step_name: str,
    error_text: str,
) -> Optional[str]:
    """
    Сохраняет артефакты сбоя в debug/publish/{job_id}/.

    Возвращает относительный путь каталога дампа вида «debug/publish/<job_id>»
    (для job["debug_dir"] и статуса API) или None, если ничего не записалось.
    """
    dump_dir = DEBUG_PUBLISH_DIR / job_id
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Не удалось создать %s: %s", dump_dir, exc)
        return None

    written_files = 0
    error_lines = [
        f"Время: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Шаг: {step_name}",
        f"Ошибка: {error_text}",
    ]
    if page is None:
        error_lines.append("Страница отсутствует, скриншот недоступен.")
    elif _page_is_closed(page):
        error_lines.append("Вкладка закрыта, скриншот недоступен.")

    try:
        (dump_dir / "error.txt").write_text(
            "\n".join(error_lines) + "\n",
            encoding="utf-8",
        )
        written_files += 1
    except Exception as exc:
        logger.warning("error.txt для шага %s не сохранён: %s", step_name, exc)

    if page is None or _page_is_closed(page):
        if written_files > 0:
            logger.info("Дамп шага %s сохранён в %s", step_name, dump_dir)
            return dump_dir.as_posix()
        return None

    try:
        await page.screenshot(path=str(dump_dir / f"step_{step_name}.png"), full_page=True)
        written_files += 1
    except Exception as exc:
        logger.warning("Скриншот шага %s не снят: %s", step_name, exc)
    try:
        html = await page.content()
        (dump_dir / f"step_{step_name}.html").write_text(html, encoding="utf-8")
        written_files += 1
    except Exception as exc:
        logger.warning("HTML шага %s не сохранён: %s", step_name, exc)
    if written_files > 0:
        logger.info("Дамп шага %s сохранён в %s", step_name, dump_dir)
        return dump_dir.as_posix()
    return None


async def _selector_visible(page: Any, selector: str, timeout_ms: int) -> bool:
    """True, если селектор появился за timeout_ms; промахи не бросают исключений."""
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        return True
    except Exception:
        return False


async def _selector_now_visible(page: Any, selector: str) -> bool:
    """True, если селектор видим прямо сейчас."""
    try:
        return await page.locator(selector).first.is_visible()
    except Exception:
        return False


def _norm_label(s: str) -> str:
    """Нормализует текст кнопки мастера: &nbsp; → пробел, схлопывание пробелов."""
    return " ".join((s or "").replace("\xa0", " ").split())


async def _find_wizard_button(page: Any, name: str) -> Optional[Any]:
    """Первая ВИДИМАЯ кнопка мастера категории с текстом == name (нормализ.) или None."""
    target = _norm_label(name)
    try:
        count = await page.locator(psel.CATEGORY_WIZARD_BUTTON).count()
    except Exception:
        return None
    for i in range(count):
        el = page.locator(psel.CATEGORY_WIZARD_BUTTON).nth(i)
        try:
            if await el.is_visible() and _norm_label(await el.inner_text()) == target:
                return el
        except Exception:
            continue
    return None


async def _wizard_button_visible(page: Any, name: str) -> bool:
    """Видна ли сейчас кнопка мастера категории с текстом == name (нормализ.)."""
    return await _find_wizard_button(page, name) is not None


async def _click_wizard_button(page: Any, name: str) -> bool:
    """Кликает видимую кнопку мастера категории с текстом == name (нормализ.)."""
    el = await _find_wizard_button(page, name)
    if el is None:
        return False
    try:
        await el.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception:
        return False
    logger.info("Категория: клик по пункту мастера %r", _norm_label(name))
    return True


async def _category_is_target(page: Any, profile: CategoryProfile) -> bool:
    """
    True, если hidden-поля категории совпали с profile.expected_category.

    Пустой profile.expected_category (например, у кроссовок до разведки
    hidden-ID) → возвращаем False: форсируем досверливание мастера, а
    финальный контроль выполняет _step_check_category по тексту крошки.
    """
    if not profile.expected_category:
        return False  # без hidden-ID полагаемся на крошку в _step_check_category
    for name, expected in profile.expected_category.items():
        selector = f"{psel.CATEGORY_HIDDEN_INPUTS}[name='{name}']"
        if (await _hidden_value(page, selector)) != expected:
            return False
    return True


def _manual_category_instruction(prefix: str, profile: CategoryProfile) -> str:
    """Единая инструкция пользователю для ручного выбора категории."""
    path_tail = " → ".join(profile.full_path[2:])  # «Мужская обувь → Кроссовки»
    return (
        f"{prefix} Открой avito.ru/additem в своём Chrome, выбери категорию "
        f"«{profile.category_title_text}» ({path_tail}) вручную и перезапусти задачу."
    )


async def _wait_until(
    condition: Callable[[], Awaitable[bool]],
    *,
    timeout_s: Optional[float] = None,
    attempts: Optional[int] = None,
    interval_s: float = 0.5,
) -> bool:
    """
    Общий poll-цикл: ждёт, пока condition() не вернёт True.

    НАМЕРЕННО самописный поллинг со sleep вместо page.wait_for_selector
    (антидетект); интервалы и таймауты задаёт вызывающий код — НЕ унифицировать.

    Режимы (ровно один из двух, повторяют исходные формы циклов):
        timeout_s — «проверка → sleep» до дедлайна time.monotonic()
                    (ожидание состояния страницы);
        attempts  — фиксированное число итераций «sleep → проверка»
                    (пауза ПЕРЕД первой проверкой — ожидание после клика).
    """
    if (timeout_s is None) == (attempts is None):
        raise ValueError("_wait_until: нужен ровно один из timeout_s/attempts")
    if timeout_s is not None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await condition():
                return True
            await asyncio.sleep(interval_s)
        return False
    for _ in range(attempts or 0):
        await asyncio.sleep(interval_s)
        if await condition():
            return True
    return False


async def _wait_additem_state(page: Any, timeout_ms: int) -> str:
    """
    Ждёт одно из состояний /additem (разведка 2026-06-10):
      - form:   мы на форме объявления (есть кликабельные крошки category-title);
      - picker: полноэкранный выбор категории «Новое объявление»
                (есть кнопки мастера category-wizard/button, крошек ещё нет);
      - unknown: за timeout не распознано ни одно из состояний (капча/иной экран).
    Сначала проверяем крошки: если открыт миллер-мастер ПОВЕРХ формы — там есть
    и крошки, и кнопки мастера, но это уже «form».
    """
    state = "unknown"

    async def _detect() -> bool:
        nonlocal state
        if await _selector_now_visible(page, psel.CATEGORY_TITLE):
            state = "form"
            return True
        if await _selector_now_visible(page, psel.CATEGORY_WIZARD_BUTTON):
            state = "picker"
            return True
        return False

    await _wait_until(_detect, timeout_s=timeout_ms / 1000, interval_s=0.5)
    return state


async def _wait_category_target(
    page: Any, profile: CategoryProfile, timeout_ms: int
) -> bool:
    """True, если за timeout_ms hidden-поля категории совпали с профилем."""
    return await _wait_until(
        lambda: _category_is_target(page, profile),
        timeout_s=timeout_ms / 1000, interval_s=0.5,
    )


async def _drill_category_path(page: Any, profile: CategoryProfile) -> None:
    """
    Досверливает миллер-мастер по profile.full_path кликами по
    category-wizard/button. Правило (разведка): НЕ кликать уровень, чей
    подуровень уже виден — повторный клик по выбранному пункту его схлопывает.
    Последний пункт (целевая категория) кликаем всегда — он грузит форму.
    """
    path = profile.full_path
    for idx, name in enumerate(path):
        is_last = idx == len(path) - 1
        if not is_last and await _wizard_button_visible(page, path[idx + 1]):
            logger.info("Категория: уровень %r уже выбран (виден %r) — пропуск",
                        name, path[idx + 1])
            continue
        if not await _click_wizard_button(page, name):
            raise UserActionRequired(_manual_category_instruction(
                f"Не нашёл пункт категории «{name}» в мастере.", profile
            ))
        # Ждём появления следующего уровня (или целевой формы для последнего)
        if is_last:
            await _wait_until(
                lambda: _category_is_target(page, profile),
                attempts=20, interval_s=0.4,
            )
        else:
            next_name = path[idx + 1]
            await _wait_until(
                lambda: _wizard_button_visible(page, next_name),
                attempts=20, interval_s=0.4,
            )


async def _keyboard_clear(page: Any) -> None:
    """Очищает сфокусированное поле с клавиатуры: Ctrl+A → Delete."""
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")


def _typed_value_matches(current: str, expected: str) -> bool:
    """
    Проверяет, совпадает ли значение в поле с ожидаемым.

    Нормализует оба аргумента: убирает обычные пробелы (U+0020) и
    неразрывные пробелы (U+00A0) — Авито форматирует цены с разделителем
    тысяч и ставит именно такие пробелы («2990» → «2 990» или «2 990»).
    Без нормализации контроль после ввода всегда давал несовпадение для
    цен ≥ 1000 и запускал хрупкий повторный ввод.
    """
    def _norm(s: str) -> str:
        return s.replace(" ", "").replace(" ", "").strip()

    return _norm(current) == _norm(expected)


async def _clear_and_type(page: Any, selector: str, value: str) -> None:
    """
    Очищает текстовое поле (Ctrl+A → Delete) и вводит значение.

    Сначала пробует .fill(); если React «не увидел» значение — fallback
    на посимвольный клавиатурный ввод (page.keyboard.type).
    Поднимает StepError с пометкой SELECTOR_MISS, если поля нет на странице.
    """
    locator = page.locator(selector).first
    try:
        await locator.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        logger.error("SELECTOR_MISS: поле %s не найдено/некликабельно: %s", selector, exc)
        raise StepError(f"Поле не найдено: {selector}") from exc

    # Очистка возможного старого черновика
    await _keyboard_clear(page)

    try:
        await locator.fill(value, timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        logger.warning(".fill() не сработал для %s (%s) — печатаю с клавиатуры", selector, exc)
        await locator.click()  # фокус мог уйти — вернуть перед посимвольным вводом
        await page.keyboard.type(value, delay=random.randint(30, 60))

    # Контроль: значение реально применилось
    try:
        current = await locator.input_value()
    except Exception:
        current = None
    if current is not None and not _typed_value_matches(current, value):
        logger.warning(
            "Значение %r не применилось к %s (сейчас %r) — повторный клавиатурный ввод",
            value, selector, current,
        )
        await locator.click()
        await _keyboard_clear(page)
        await page.keyboard.type(value, delay=random.randint(30, 60))


async def _click_visible_text(page: Any, text: str, timeout_s: float = 4.0) -> bool:
    """
    Кликает по ВИДИМОМУ элементу с точным текстом == text (за timeout_s).

    Нужен для пунктов раскрытого комбобокса: это видимые <div> с текстом опции
    без data-marker (скрытые <option> с тем же текстом игнорируются — невидимы).
    """
    target = _norm_label(text)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        loc = page.get_by_text(text, exact=True)
        try:
            count = await loc.count()
        except Exception:
            count = 0
        for i in range(count):
            el = loc.nth(i)
            try:
                if await el.is_visible() and _norm_label(await el.inner_text()) == target:
                    await el.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return False


async def _native_select_value(page: Any, prefix: str) -> str:
    """Текущее value скрытого нативного <select> комбобокса (для верификации)."""
    selector = psel.COMBOBOX_SELECT_TMPL.format(prefix=prefix)
    try:
        return await page.locator(selector).first.evaluate("el => el.value") or ""
    except Exception:
        return ""


async def _select_combobox(page: Any, prefix: str, label: str, option_id: int) -> None:
    """
    Выбирает опцию в кастомном комбобоксе Авито (разведка 2026-06-10):
      1) клик по контейнеру role=combobox — раскрыть список;
      2) клик по ВИДИМОМУ пункту с текстом label;
      3) верификация: нативный <select> получил value == option_id.
    Фолбэк при неудаче: ввод label в поле поиска комбобокса → клик пункта.
    """
    container_sel = psel.COMBOBOX_CONTAINER_TMPL.format(prefix=prefix)
    expected_value = str(option_id)

    async def _open() -> None:
        container = page.locator(container_sel).first
        try:
            await container.scroll_into_view_if_needed(timeout=WAIT_SELECTOR_TIMEOUT_MS)
            await container.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
        except Exception as exc:
            logger.error("SELECTOR_MISS: комбобокс %s не открылся: %s", container_sel, exc)
            raise StepError(f"Комбобокс не найден: {container_sel}") from exc
        await asyncio.sleep(random.uniform(0.3, 0.6))  # анимация списка

    # Попытка 1: открыть и кликнуть пункт по тексту
    await _open()
    clicked = await _click_visible_text(page, label)

    # Попытка 2 (фолбэк): через поле поиска внутри комбобокса
    if not clicked or await _native_select_value(page, prefix) != expected_value:
        search_sel = psel.COMBOBOX_SEARCH_TMPL.format(prefix=prefix)
        if await _selector_now_visible(page, search_sel):
            logger.info("Комбобокс %s: пробую через поле поиска", prefix)
            try:
                await page.locator(search_sel).first.fill(label, timeout=WAIT_SELECTOR_TIMEOUT_MS)
                await asyncio.sleep(0.5)
            except Exception:
                pass
            clicked = await _click_visible_text(page, label)
        else:
            # список мог закрыться — открываем заново и пробуем ещё раз
            await _open()
            clicked = await _click_visible_text(page, label)

    await asyncio.sleep(0.3)
    actual = await _native_select_value(page, prefix)
    if actual != expected_value:
        logger.error(
            "SELECTOR_MISS: комбобокс %s не принял %r (value=%r, ожидалось %r)",
            prefix, label, actual, expected_value,
        )
        raise StepError(
            f"Не удалось выбрать «{label}» в комбобоксе {prefix} "
            f"(value={actual!r}, ожидалось {expected_value!r})"
        )
    logger.info("Комбобокс %s: выбрано %r (value=%s)", prefix, label, expected_value)


async def _hidden_value(page: Any, selector: str) -> str:
    """Значение hidden-input (пустая строка, если элемента нет/ошибка чтения)."""
    try:
        return (await page.locator(selector).first.input_value(timeout=3_000)) or ""
    except Exception:
        return ""


def _normalize_saved_url(url: str) -> str:
    """
    Нормализует URL сохранённого черновика для API/фронта.

    Если последний известный URL остался на /additem, значит Авито закрыло вкладку
    раньше, чем можно было прочитать адрес страницы результата — возвращаем пустую
    строку как «URL неизвестен».
    """
    clean = (url or "").strip()
    if not clean:
        return ""
    return "" if "additem" in clean else clean


def _context_page_urls(page: Any) -> list[str]:
    """Текущие URL всех вкладок контекста для диагностики таймаута save_draft."""
    if page is None:
        return []
    try:
        context_pages = list(getattr(page.context, "pages", []) or [])
    except Exception:
        return []

    urls: list[str] = []
    for ctx_page in context_pages:
        try:
            urls.append(str(getattr(ctx_page, "url", "") or ""))
        except Exception:
            urls.append("<url-unavailable>")
    return urls


async def _wait_form_exited(
    page: Any,
    url_before: str,
    *,
    timeout_s: float = SAVE_EXIT_TIMEOUT_S,
    interval_s: float = 0.5,
    selector_visible_probe: Optional[Callable[[Any, str], Awaitable[bool]]] = None,
) -> tuple[str, str]:
    """
    Ждёт успешного ухода со страницы формы после «Сохранить и выйти».

    Возвращает (исход, итоговый_URL), где исход один из:
      - navigated  — URL реально сменился и ушёл с /additem;
      - page_closed — Авито закрыло/подменило вкладку;
      - form_gone  — кнопка save и крошки формы исчезли без смены URL.
    """
    visible_probe = selector_visible_probe or _selector_now_visible
    last_known_url = str(url_before or "")
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        closed = _page_is_closed(page)
        logger.debug("save_draft wait tick: url=%r is_closed=%s", last_known_url, closed)
        if closed:
            final_url = _normalize_saved_url(last_known_url)
            logger.info("save_draft: успех, исход=page_closed, final_url=%r", final_url)
            return "page_closed", final_url

        try:
            current_url = str(getattr(page, "url", "") or "")
            if current_url:
                last_known_url = current_url
        except Exception:
            if _page_is_closed(page):
                final_url = _normalize_saved_url(last_known_url)
                logger.info("save_draft: успех, исход=page_closed, final_url=%r", final_url)
                return "page_closed", final_url
            current_url = last_known_url

        if current_url != url_before and "additem" not in current_url:
            final_url = _normalize_saved_url(current_url)
            logger.info("save_draft: успех, исход=navigated, final_url=%r", final_url)
            return "navigated", final_url

        try:
            save_visible = await visible_probe(page, psel.SAVE_AND_EXIT_BUTTON)
            category_visible = await visible_probe(page, psel.CATEGORY_TITLE)
        except Exception:
            if _page_is_closed(page):
                final_url = _normalize_saved_url(last_known_url)
                logger.info("save_draft: успех, исход=page_closed, final_url=%r", final_url)
                return "page_closed", final_url
            save_visible = True
            category_visible = True

        if not save_visible and not category_visible:
            final_url = _normalize_saved_url(last_known_url)
            logger.info("save_draft: успех, исход=form_gone, final_url=%r", final_url)
            return "form_gone", final_url

        await asyncio.sleep(interval_s)

    context_urls = _context_page_urls(page)
    logger.warning(
        "save_draft: таймаут %.1f с, вкладки контекста: %s",
        timeout_s,
        context_urls,
    )
    raise StepError(
        "После «Сохранить и выйти» не произошло подтверждённого ухода со страницы формы "
        f"за {timeout_s:.1f} с."
    )


async def _guard_against_reopened_draft(
    page: Any, title: str, draft_index: int, drafts_total: int
) -> None:
    """
    Защита от перезаписи в пакетном режиме (ТЗ §16).

    Перед заполнением названия черновика i>1 читаем текущее значение
    input[name='title']:
      - пусто → нормальная свежая форма, заполняем;
      - непусто и СОВПАДАЕТ с нашим названием → Авито переоткрыл уже
        сохранённый черновик; заполнять нельзя (затрём сохранённое) →
        UserActionRequired (терминальный, дамп снимет обработчик);
      - непусто, но ДРУГОЙ текст (старый черновик пользователя) → норма,
        _clear_and_type очистит поле как обычно.
    """
    if draft_index <= 1:
        return
    try:
        current = (
            await page.locator(psel.TITLE_INPUT).first.input_value(
                timeout=WAIT_SELECTOR_TIMEOUT_MS
            )
        ) or ""
    except Exception as exc:
        # Не смогли прочитать — не блокируем: дальше упрётся в _clear_and_type
        logger.warning(
            "Черновик %d/%d: не удалось прочитать текущее название: %s",
            draft_index, drafts_total, exc,
        )
        return

    current = current.strip()
    if current and current == title.strip():
        logger.error(
            "Черновик %d/%d: поле названия уже содержит наше название %r — "
            "Авито переоткрыл уже сохранённый черновик",
            draft_index, drafts_total, current,
        )
        raise UserActionRequired(
            f"Авито переоткрывает существующий черновик — "
            f"сохранено {draft_index - 1} из {drafts_total}. Дальше пока вручную."
        )
    if current:
        logger.info(
            "Черновик %d/%d: в поле названия чужой текст %r — будет очищен",
            draft_index, drafts_total, current,
        )


# ---------------------------------------------------------------------------
# Реализация шагов
# ---------------------------------------------------------------------------

async def _step_open_form(page: Any, profile: CategoryProfile) -> str:
    """Шаг open_form: goto /additem и распознавание состояния экрана (ТЗ §6)."""
    await page.goto(ADDITEM_URL, wait_until="domcontentloaded", timeout=WAIT_FORM_TIMEOUT_MS)
    state = await _wait_additem_state(page, WAIT_FORM_TIMEOUT_MS)
    if state in ("form", "picker"):
        logger.info("Состояние /additem: %s", state)
        return state

    logger.error(
        "Не удалось распознать состояние /additem за %d мс (нет ни крошек %s, "
        "ни кнопок мастера %s)",
        WAIT_FORM_TIMEOUT_MS, psel.CATEGORY_TITLE, psel.CATEGORY_WIZARD_BUTTON,
    )
    raise UserActionRequired(_manual_category_instruction(
        "Форма объявления не открылась в ожидаемом виде (возможно, капча или другой экран).",
        profile,
    ))


async def _step_select_category(
    page: Any, form_state: str, profile: CategoryProfile
) -> None:
    """
    Шаг select_category: доводит форму до целевой категории профиля
    (ТЗ §6, алгоритм подтверждён live-разведкой 2026-06-10, debug/recon_full.py).

    1. Если категория уже целевая (profile.expected_category) — выходим
       (черновик переоткрыт). Пустой expected_category → всегда досверливаем.
    2. Если picker (полноэкранный выбор) — клик по верхнему разделу
       (profile.full_path[0]) уводит на форму с крошками.
    3. Клик по крошкам category-title раскрывает миллер-мастер.
    4. Досверливаем полный путь (_drill_category_path).
    5. Ждём появления целевой категории; иначе needs_user_action.
    """
    # 1) Уже на целевой форме?
    if await _category_is_target(page, profile):
        logger.info("Категория уже целевая (%s) — выбор не нужен", profile.label)
        return

    # 2) Полноэкранный picker: выбрать верхний раздел, дождаться формы с крошками
    if form_state == "picker":
        logger.info("Экран выбора категории (picker) — выбираю верхний раздел %r",
                    profile.full_path[0])
        if not await _click_wizard_button(page, profile.full_path[0]):
            raise UserActionRequired(_manual_category_instruction(
                "Не нашёл верхний раздел категории на экране выбора.", profile
            ))
        if not await _selector_visible(page, psel.CATEGORY_TITLE, WAIT_FORM_TIMEOUT_MS):
            raise UserActionRequired(_manual_category_instruction(
                "После выбора раздела форма с категорией не открылась.", profile
            ))
        await _pause()
        if await _category_is_target(page, profile):
            logger.info("После выбора верхнего раздела категория уже целевая")
            return

    # 3) Раскрываем миллер-мастер кликом по хлебным крошкам
    try:
        await page.locator(psel.CATEGORY_TITLE).first.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        logger.error("SELECTOR_MISS: крошки %s не кликабельны: %s", psel.CATEGORY_TITLE, exc)
        raise UserActionRequired(_manual_category_instruction(
            "Не удалось открыть мастер выбора категории (клик по крошкам).", profile
        )) from exc
    await asyncio.sleep(1.0)
    if not await _selector_now_visible(page, psel.CATEGORY_WIZARD_BUTTON):
        raise UserActionRequired(_manual_category_instruction(
            "Мастер выбора категории не раскрылся.", profile
        ))

    # 4) Досверливаем путь до целевой категории
    await _drill_category_path(page, profile)

    # 5) Контроль достижения цели
    if await _wait_category_target(page, profile, WAIT_FORM_TIMEOUT_MS):
        logger.info("Категория «%s» выбрана через мастер", profile.category_title_text)
        return
    raise UserActionRequired(_manual_category_instruction(
        f"Автовыбор категории не довёл до «{profile.category_title_text}».", profile
    ))


async def _step_check_category(page: Any, profile: CategoryProfile) -> None:
    """
    Шаг check_category: контроль hidden-ID профиля либо текста хлебных крошек.

    Пустой profile.expected_category (нет точных hidden-ID, например у
    кроссовок до разведки) → hidden-проверку пропускаем и подтверждаем
    категорию только по тексту крошки (profile.category_title_text).
    """
    if profile.expected_category:
        hidden_ok = True
        for name, expected in profile.expected_category.items():
            selector = f"{psel.CATEGORY_HIDDEN_INPUTS}[name='{name}']"
            actual = await _hidden_value(page, selector)
            if actual != expected:
                logger.warning(
                    "Категория: hidden %s = %r, ожидалось %r", name, actual, expected
                )
                hidden_ok = False

        if hidden_ok:
            logger.info("Категория подтверждена hidden-полями профиля %s", profile.key)
            return

    # Дублирующий контроль (или единственный при пустом expected_category) —
    # текст хлебных крошек.
    try:
        crumbs = await page.locator(psel.CATEGORY_TITLE).first.inner_text(timeout=5_000)
    except Exception:
        crumbs = ""
    if profile.category_title_text in crumbs:
        logger.info("Категория подтверждена хлебными крошками: %r", crumbs)
        return

    raise UserActionRequired(_manual_category_instruction(
        "На форме выбрана другая категория.", profile
    ))


# ---------------------------------------------------------------------------
# Диагностическая инструментация загрузки фото (гипотезы A/B)
# ---------------------------------------------------------------------------

# Эвристика photo_related: ключевые слова URL, типы ресурсов, CDN-хосты
_DIAG_URL_KEYWORDS: frozenset[str] = frozenset(
    {"upload", "photo", "image", "img", "file"}
)
_DIAG_CDN_HOSTS: frozenset[str] = frozenset(
    {"avito.st", "avatars.avito", "cdn.avito"}
)


def _diag_is_photo_request(url: str, resource_type: str) -> bool:
    """True, если запрос, вероятно, относится к загрузке/отдаче фото."""
    url_l = url.lower()
    if resource_type == "image":
        return True
    kw_hit = any(kw in url_l for kw in _DIAG_URL_KEYWORDS)
    if kw_hit and ("avito" in url_l or resource_type in ("xhr", "fetch")):
        return True
    if any(h in url_l for h in _DIAG_CDN_HOSTS):
        return True
    return False


def _attach_photo_network_probe(page: Any) -> Optional[dict]:
    """
    Навешивает page.on("request") / page.on("response") и возвращает probe-словарь.

    Если page не имеет метода .on (mock в тестах) — тихо возвращает None.

    probe = {
        "n_files": 0,          # заполняется вызывающим кодом
        "requests":  [{"ts", "event", "method", "url", "resource_type",
                        "photo_related"}, ...],
        "responses": [{"ts", "event", "url", "status", "resource_type",
                        "photo_related"}, ...]
    }
    """
    if not hasattr(page, "on"):
        return None

    probe: dict = {"n_files": 0, "requests": [], "responses": []}

    def _on_request(req: Any) -> None:
        try:
            url = getattr(req, "url", "") or ""
            method = getattr(req, "method", "") or ""
            rtype = getattr(req, "resource_type", "") or ""
            probe["requests"].append({
                "ts": datetime.datetime.utcnow().isoformat(),
                "event": "request",
                "method": method,
                "url": url,
                "resource_type": rtype,
                "photo_related": _diag_is_photo_request(url, rtype),
            })
        except Exception:
            pass

    def _on_response(resp: Any) -> None:
        try:
            url = getattr(resp, "url", "") or ""
            status = getattr(resp, "status", -1)
            req = getattr(resp, "request", None)
            rtype = (getattr(req, "resource_type", "") or "") if req else ""
            probe["responses"].append({
                "ts": datetime.datetime.utcnow().isoformat(),
                "event": "response",
                "url": url,
                "status": status,
                "resource_type": rtype,
                "photo_related": _diag_is_photo_request(url, rtype),
            })
        except Exception:
            pass

    try:
        page.on("request", _on_request)
        page.on("response", _on_response)
        logger.info("Диагностика фото: сетевые слушатели подключены")
    except Exception as exc:
        logger.warning("Диагностика фото: не удалось навесить слушатели: %s", exc)
        return None

    return probe


async def _diag_snap_previews(
    page: Any, label: str, dst_dir: pathlib.Path
) -> None:
    """
    Фиксирует состояние превью и <img>-тегов, дописывает строку в previews.log.
    Каждый вызов снабжён UTC-меткой и именем момента (label).
    Не бросает исключений.
    """
    try:
        ts = datetime.datetime.utcnow().isoformat()
        lines: list[str] = [f"--- {ts} [{label}] ---"]

        # Счётчик по каждому кандидату-селектору превью
        for candidate in psel.PHOTO_PREVIEW_CANDIDATES:
            try:
                count = await page.locator(candidate).count()
                lines.append(f"  sel {candidate!r}: {count} эл.")
            except Exception as exc_sel:
                lines.append(f"  sel {candidate!r}: ошибка ({exc_sel})")

        # Сканируем все <img>: blob: (кэш браузера) vs CDN avito.st
        try:
            img_srcs: list[str] = await page.evaluate(
                "() => Array.from(document.querySelectorAll('img'))"
                ".map(i => i.src || i.getAttribute('src') || '')"
                ".filter(Boolean).slice(0, 60)"
            )
            blob_c = sum(1 for s in img_srcs if s.startswith("blob:"))
            cdn_c = sum(
                1 for s in img_srcs
                if "avito" in s.lower() and not s.startswith("blob:")
            )
            lines.append(
                f"  img: всего={len(img_srcs)}, blob={blob_c}, CDN(avito)={cdn_c}"
            )
            for src in img_srcs[:25]:
                if "blob:" in src or "avito" in src.lower():
                    lines.append(f"    {src[:120]!r}")
        except Exception as exc_img:
            lines.append(f"  img-scan ошибка: {exc_img}")

        lines.append("")
        dst_dir.mkdir(parents=True, exist_ok=True)
        with (dst_dir / "previews.log").open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:
        logger.warning("Диагностика фото: снапшот [%s] не удался: %s", label, exc)


async def _dump_photo_diag(
    page: Any, probe: dict, label: str, dst_dir: pathlib.Path
) -> None:
    """
    Выгружает network.jsonl и summary.txt в dst_dir.
    Читает previews.log из того же dst_dir для сводки.
    Аргумент page зарезервирован для совместимости (скриншоты делаются отдельно).
    Не бросает исключений.
    """
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)

        # network.jsonl — все перехваченные события в хронологическом порядке
        all_events = sorted(
            probe.get("requests", []) + probe.get("responses", []),
            key=lambda e: e.get("ts", ""),
        )
        with (dst_dir / "network.jsonl").open("w", encoding="utf-8") as fh:
            for ev in all_events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")

        # Статистика из probe
        n_files = probe.get("n_files", 0)
        total_req = len(probe.get("requests", []))
        total_resp = len(probe.get("responses", []))
        photo_req = sum(1 for e in probe.get("requests", []) if e.get("photo_related"))
        photo_resp = sum(1 for e in probe.get("responses", []) if e.get("photo_related"))
        upload_resps = [
            e for e in probe.get("responses", [])
            if e.get("photo_related") and "upload" in e.get("url", "").lower()
        ]
        upload_statuses = [e.get("status", -1) for e in upload_resps]

        # Финальный blob/CDN-счёт из последней строки previews.log
        last_blob = "?"
        last_cdn = "?"
        prev_log = dst_dir / "previews.log"
        if prev_log.exists():
            try:
                for line in reversed(prev_log.read_text(encoding="utf-8").splitlines()):
                    if "blob=" in line and "CDN(avito)=" in line:
                        # Строка вида: «  img: всего=N, blob=B, CDN(avito)=C»
                        for part in line.split(","):
                            p = part.strip()
                            if p.startswith("blob="):
                                last_blob = p[len("blob="):]
                            elif p.startswith("CDN(avito)="):
                                last_cdn = p[len("CDN(avito)="):]
                        break
            except Exception:
                pass

        (dst_dir / "summary.txt").write_text(
            f"=== Диагностика загрузки фото ({label}) ===\n"
            f"Файлов передано: {n_files}\n"
            f"Сетевых запросов (всего): {total_req}\n"
            f"Сетевых ответов (всего): {total_resp}\n"
            f"Запросов photo_related: {photo_req}\n"
            f"Ответов photo_related: {photo_resp}\n"
            f"Upload-подобных ответов: {len(upload_resps)}\n"
            f"  статусы upload: {upload_statuses}\n"
            f"img на blob: перед сохранением: {last_blob}\n"
            f"img на CDN (avito.st) перед сохранением: {last_cdn}\n"
            f"\n"
            f"--- Интерпретация ---\n"
            f"Гипотеза A (гонка): upload XHR вернули 200 для ВСЕХ файлов,\n"
            f"  но img ещё на blob: перед сохранением → финализация на сервере\n"
            f"  Авито ещё не завершена, кнопка Save нажата слишком рано.\n"
            f"Гипотеза B (антидубль): upload XHR = 200 для всех {n_files} файлов,\n"
            f"  но img на CDN меньше {n_files} → Авито молча отбросил похожие фото.\n",
            encoding="utf-8",
        )
        logger.info(
            "Диагностика фото: дамп в %s (%d событий сети)", dst_dir, len(all_events)
        )
    except Exception as exc:
        logger.warning("Диагностика фото: дамп не удался: %s", exc)


async def _step_upload_photos(page: Any, photo_paths: tuple[str, ...]) -> None:
    """Шаг upload_photos: set_input_files + ожидание загрузки на сервер."""

    # ── Диагностика A/B: инициализация ────────────────────────────────────
    # Создаём папку артефактов и навешиваем сетевые слушатели ДО отправки файлов,
    # чтобы поймать upload-XHR с первого байта. Ошибки диагностики не роняют шаг.
    _d_dir: Optional[pathlib.Path] = None
    _d_probe: Optional[dict] = None
    try:
        _d_ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        _d_dir = PROJECT_ROOT / "debug" / f"photo_diag_{_d_ts}"
        _d_dir.mkdir(parents=True, exist_ok=True)
        _d_probe = _attach_photo_network_probe(page)
        if _d_probe is not None:
            _d_probe["n_files"] = len(photo_paths)
        # Сохраняем ссылки на page-объекте: _step_save_draft прочитает их
        try:
            page._photo_diag_probe = _d_probe   # type: ignore[attr-defined]
            page._photo_diag_dir = _d_dir        # type: ignore[attr-defined]
        except Exception:
            pass
        logger.info("Диагностика фото: папка %s, зондирование %s",
                    _d_dir, "активно" if _d_probe else "недоступно (mock)")
    except Exception as exc:
        logger.warning("Диагностика фото: инициализация не удалась: %s", exc)
    # ──────────────────────────────────────────────────────────────────────

    try:
        await page.set_input_files(
            psel.PHOTO_INPUT, list(photo_paths), timeout=WAIT_SELECTOR_TIMEOUT_MS
        )
    except Exception as exc:
        logger.error("SELECTOR_MISS: input фото %s: %s", psel.PHOTO_INPUT, exc)
        raise StepError(f"Не найден input загрузки фото: {psel.PHOTO_INPUT}") from exc

    # ── Диагностика: скриншот + снапшот сразу после set_input_files ───────
    if _d_dir is not None:
        try:
            if hasattr(page, "screenshot"):
                await page.screenshot(
                    path=str(_d_dir / "screenshot_after_set_files.png")
                )
        except Exception as exc_ss:
            logger.warning("Диагностика фото: скриншот after_set_files: %s", exc_ss)
        await _diag_snap_previews(page, "after_set_files", _d_dir)
    # ──────────────────────────────────────────────────────────────────────

    logger.info("Отдано на загрузку %d фото, ждём превью…", len(photo_paths))

    # Маркер превью живьём не подтверждён — пробуем кандидатов по очереди.
    expected = len(photo_paths)
    deadline = asyncio.get_event_loop().time() + PHOTO_UPLOAD_TIMEOUT_S
    working_selector: Optional[str] = None

    for candidate in psel.PHOTO_PREVIEW_CANDIDATES:
        try:
            count = await page.locator(candidate).count()
        except Exception:
            count = 0
        if count > 0:
            working_selector = candidate
            break
        # Даём время на появление первого превью у первого кандидата
        if await _selector_visible(page, candidate, 5_000):
            working_selector = candidate
            break
        logger.info("SELECTOR_MISS: кандидат превью фото %r не найден", candidate)

    if working_selector is None:
        # Превью не нашли ни по одному кандидату — щедрая фиксированная пауза,
        # чтобы загрузка успела завершиться, и продолжаем (не валим шаг:
        # Авито автосохраняет черновик, фото скорее всего долетят).
        fallback_wait = min(10.0 + 5.0 * expected, 60.0)
        logger.warning(
            "Превью фото не обнаружены ни по одному селектору — жду %.0f с вслепую",
            fallback_wait,
        )
        await asyncio.sleep(fallback_wait)
        return

    # Ждём, пока число превью достигнет числа файлов (или истечёт таймаут)
    logger.info("Превью фото отслеживаю по селектору %r", working_selector)
    last_count = 0
    _d_snap_itr = 0  # счётчик итераций для нечастых снапшотов превью (~каждые 3 с)
    while asyncio.get_event_loop().time() < deadline:
        try:
            last_count = await page.locator(working_selector).count()
        except Exception:
            last_count = 0
        if last_count >= expected:
            logger.info("Все %d фото загружены (превью на месте)", expected)
            # Небольшой довесок — дать серверной загрузке финализироваться
            await asyncio.sleep(2.0)
            return
        # ── Диагностика: снапшот превью через итерацию (~каждые 3 с) ─────
        _d_snap_itr += 1
        if _d_dir is not None and _d_snap_itr % 2 == 0:
            await _diag_snap_previews(
                page, f"waiting_{last_count}of{expected}", _d_dir
            )
        # ──────────────────────────────────────────────────────────────────
        await asyncio.sleep(1.5)

    logger.warning(
        "За %.0f с появилось %d/%d превью — продолжаю с тем, что есть",
        PHOTO_UPLOAD_TIMEOUT_S, last_count, expected,
    )


def _norm_suggest(text: str) -> str:
    """Нормализует текст пункта подсказки для сравнения: схлопывает пробелы + casefold."""
    return " ".join(text.split()).casefold()


async def _click_suggest_option(
    page: Any,
    option_selector: str,
    prefer_text: Optional[str] = None,
    timeout_ms: int = GEO_SUGGEST_TIMEOUT_MS,
) -> Optional[str]:
    """
    Ждёт пункты автокомплита по option_selector и кликает подходящий.

    prefer_text задан → кликает пункт с совпадающим (без учёта регистра/пробелов)
    текстом; если такого нет — первый. Возвращает текст кликнутого пункта или
    None, если пункты не появились (вызывающий решает, что делать).

    Зачем (живая разведка 2026-06-28): и бренд, и адрес — автокомплиты, значение
    «прилипает» только при КЛИКЕ пункта. Закрытие списка через Escape очищает поле
    бренда, а клик по контейнеру гео-саджеста адрес не выбирает.
    """
    if not await _selector_visible(page, option_selector, timeout_ms):
        return None
    options = page.locator(option_selector)
    try:
        count = await options.count()
    except Exception:
        count = 0
    if count == 0:
        return None

    target_idx = 0
    if prefer_text:
        want = _norm_suggest(prefer_text)
        for i in range(count):
            try:
                txt = await options.nth(i).inner_text()
            except Exception:
                continue
            if _norm_suggest(txt) == want:
                target_idx = i
                break

    chosen = options.nth(target_idx)
    try:
        text = (await chosen.inner_text()).strip()
    except Exception:
        text = ""
    await chosen.click(timeout=5_000)
    return text or "(пункт без текста)"


async def _step_fill_fields(
    page: Any, data: DraftData, profile: CategoryProfile
) -> None:
    """Шаг fill_fields: вид объявления, состояние, размер, бренд, цвет."""
    # Вид объявления — комбобокс
    await _select_combobox(
        page, profile.trade_type_prefix, data.trade_type,
        profile.trade_type_options[data.trade_type],
    )
    await _pause()

    # Состояние — радио, кликаем по label
    radio_sel = profile.condition_radio(profile.condition_options[data.condition])
    try:
        await page.click(radio_sel, timeout=WAIT_SELECTOR_TIMEOUT_MS)
        logger.info("Состояние: выбрано %r", data.condition)
    except Exception as exc:
        logger.error("SELECTOR_MISS: радио состояния %s: %s", radio_sel, exc)
        raise StepError(f"Радио состояния не найдено: {radio_sel}") from exc
    await _pause()

    # Размер — комбобокс
    await _select_combobox(
        page, profile.size_prefix, data.size, profile.size_options[data.size]
    )
    await _pause()

    # Бренд — автокомплит. ВАЖНО (разведка 2026-06-28): бренд сохраняется ТОЛЬКО
    # при клике пункта подсказки; Escape ОЧИЩАЕТ поле — поэтому его НЕ жмём, а
    # выбираем пункт brand_option (совпадающий по тексту, иначе первый).
    await _clear_and_type(page, profile.brand_input, data.brand)
    chosen_brand = await _click_suggest_option(page, profile.brand_option, prefer_text=data.brand)
    if chosen_brand is not None:
        logger.info("Бренд: выбран пункт подсказки %r", chosen_brand)
    else:
        logger.warning(
            "Бренд: подсказки не появились — оставляю введённый текст %r "
            "(Escape не жму, он очищает поле)", data.brand,
        )
    await _pause()

    # Цвет — комбобокс
    await _select_combobox(
        page, profile.color_prefix, data.color, profile.color_options[data.color]
    )


async def _step_fill_description(page: Any, description: str) -> None:
    """
    Шаг fill_description: contenteditable-редактор.

    НЕ .fill(): кликаем в редактор и вводим с клавиатуры.
    Контроль: hidden input[name='description_html'] должен стать непустым.
    """
    editor = page.locator(psel.DESCRIPTION_EDITOR).first
    try:
        await editor.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        logger.error("SELECTOR_MISS: редактор описания %s: %s", psel.DESCRIPTION_EDITOR, exc)
        raise StepError(f"Редактор описания не найден: {psel.DESCRIPTION_EDITOR}") from exc

    # Очистка возможного старого описания
    await _keyboard_clear(page)
    await asyncio.sleep(0.3)

    # Клавиатурный ввод (contenteditable не принимает .fill())
    await page.keyboard.type(description, delay=random.randint(10, 25))
    await asyncio.sleep(0.5)

    # Контроль через hidden description_html
    hidden = await _hidden_value(page, psel.DESCRIPTION_HIDDEN)
    if not hidden.strip():
        logger.warning("description_html пуст после type — пробую insert_text")
        await editor.click()
        await page.keyboard.insert_text(description)
        await asyncio.sleep(0.5)
        hidden = await _hidden_value(page, psel.DESCRIPTION_HIDDEN)

    if not hidden.strip():
        raise StepError(
            "Описание не применилось: hidden input[name='description_html'] пуст"
        )
    logger.info("Описание введено (%d символов, hidden непустой)", len(description))


async def _step_fill_price(page: Any, price: int) -> None:
    """Шаг fill_price: видимый input цены (+ запасной селектор)."""
    price_value = str(price)
    try:
        await _clear_and_type(page, psel.PRICE_INPUT, price_value)
    except StepError:
        logger.info("Пробую запасной селектор цены: %s", psel.PRICE_INPUT_FALLBACK)
        await _clear_and_type(page, psel.PRICE_INPUT_FALLBACK, price_value)
    logger.info("Цена введена: %s ₽", price_value)


async def _step_fill_address(page: Any, full_address: str) -> None:
    """
    Шаг fill_address: гео-саджест — самое хрупкое место (ТЗ §14).

    Посимвольный «живой» ввод с задержкой, ожидание саджеста, клик по первому
    пункту, контроль hidden address/locationId. До GEO_RETRIES попыток.
    """
    geo_input = page.locator(psel.GEO_SEARCH_INPUT).first

    async def _address_applied() -> bool:
        """True, когда hidden address и locationId непустые (адрес применился)."""
        address_val = await _hidden_value(page, psel.ADDRESS_HIDDEN)
        location_val = await _hidden_value(page, psel.LOCATION_ID_HIDDEN)
        if address_val.strip() and location_val.strip():
            logger.info(
                "Адрес применён: address=%r, locationId=%r", address_val, location_val
            )
            return True
        return False

    for attempt in range(1, GEO_RETRIES + 1):
        logger.info("Адрес, попытка %d/%d: %r", attempt, GEO_RETRIES, full_address)
        try:
            await geo_input.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
        except Exception as exc:
            logger.error("SELECTOR_MISS: гео-поле %s: %s", psel.GEO_SEARCH_INPUT, exc)
            raise StepError(f"Гео-поле не найдено: {psel.GEO_SEARCH_INPUT}") from exc

        # Очистка и «живой» посимвольный ввод
        await _keyboard_clear(page)
        await asyncio.sleep(0.4)
        await page.keyboard.type(full_address, delay=random.randint(80, 140))

        # Ждём появления контейнера саджеста
        if not await _selector_visible(page, psel.GEO_SUGGEST, GEO_SUGGEST_TIMEOUT_MS):
            logger.warning("Гео-саджест не появился (попытка %d)", attempt)
            await asyncio.sleep(1.0)
            continue

        # Клик по ПЕРВОМУ реальному пункту-адресу (button custom-option(N)).
        # ВАЖНО (разведка 2026-06-28): раньше кликали контейнер geo/field/suggest —
        # это не выбирало адрес, и он терялся при сохранении черновика.
        chosen_addr = await _click_suggest_option(
            page, psel.GEO_SUGGEST_OPTION, prefer_text=None, timeout_ms=5_000
        )
        if chosen_addr is None:
            logger.warning("Гео: пункты-адреса (custom-option) не найдены (попытка %d)", attempt)
            continue
        logger.info("Гео: выбран адрес %r", chosen_addr)

        # Контроль: hidden address и locationId непустые (даём React время)
        if await _wait_until(_address_applied, attempts=10, interval_s=0.5):
            return
        logger.warning("hidden address/locationId не заполнились (попытка %d)", attempt)

    # Все ретраи исчерпаны — стоп с инструкцией (дамп снимет обработчик)
    raise UserActionRequired(
        "Не удалось выбрать адрес через подсказку Авито (гео-саджест не сработал). "
        "Открой черновик на avito.ru (Мои объявления → Черновики), укажи адрес "
        "вручную и при необходимости перезапусти задачу."
    )


async def _step_save_draft(page: Any) -> str:
    """
    Шаг save_draft: клик «Сохранить и выйти», успех = подтверждённый уход с формы.

    ВНИМАНИЕ (ТЗ §2): здесь и только здесь кликается финальная кнопка.
    Кнопка «Продолжить» (data-marker «item-edit/button-next») публикует
    объявление и списывает деньги — её НЕ кликать НИ ПРИ КАКИХ УСЛОВИЯХ.
    """
    url_before = page.url

    # ── Диагностика: финальный снапшот + дамп ПЕРЕД кликом «Сохранить» ────
    # Ключевой момент для гипотезы A: видим, сколько img ещё на blob: в момент
    # нажатия кнопки. Любой сбой — только WARNING, публикация не прерывается.
    try:
        _d_dir: Optional[pathlib.Path] = getattr(page, "_photo_diag_dir", None)
        _d_probe: Optional[dict] = getattr(page, "_photo_diag_probe", None)
        if _d_dir is not None:
            await _diag_snap_previews(page, "before_save_click", _d_dir)
            try:
                if hasattr(page, "screenshot"):
                    await page.screenshot(
                        path=str(_d_dir / "screenshot_before_save.png")
                    )
            except Exception as exc_ss:
                logger.warning("Диагностика фото: скриншот before_save: %s", exc_ss)
            if _d_probe is not None:
                await _dump_photo_diag(page, _d_probe, "before_save", _d_dir)
    except Exception as exc:
        logger.warning("Диагностика фото: снапшот before_save не удался: %s", exc)
    # ──────────────────────────────────────────────────────────────────────

    # Кнопка «Сохранить и выйти» появляется ТОЛЬКО после ввода данных (разведка
    # 2026-06-10): на пустой форме внизу «Выйти». Форма уже заполнена — ждём кнопку.
    if not await _selector_visible(page, psel.SAVE_AND_EXIT_BUTTON, WAIT_SELECTOR_TIMEOUT_MS):
        logger.error(
            "SELECTOR_MISS: кнопка «Сохранить и выйти» %s не появилась "
            "(данные не введены? на форме только «Выйти»?)",
            psel.SAVE_AND_EXIT_BUTTON,
        )
        raise StepError(
            f"Кнопка «Сохранить и выйти» не найдена: {psel.SAVE_AND_EXIT_BUTTON}"
        )
    try:
        await page.click(psel.SAVE_AND_EXIT_BUTTON, timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        logger.error(
            "SELECTOR_MISS: кнопка «Сохранить и выйти» %s: %s",
            psel.SAVE_AND_EXIT_BUTTON, exc,
        )
        raise StepError(
            f"Кнопка «Сохранить и выйти» не найдена: {psel.SAVE_AND_EXIT_BUTTON}"
        ) from exc

    logger.info("Клик «Сохранить и выйти», ждём ухода со страницы формы…")
    _, final_url = await _wait_form_exited(page, url_before)
    logger.info("Черновик сохранён, конечный URL: %s", final_url)
    return final_url


# ---------------------------------------------------------------------------
# Главная функция: прогон стейт-машины
# ---------------------------------------------------------------------------

async def _run_single_draft(
    page: Any,
    job: dict[str, Any],
    data: DraftData,
    draft_index: int,
    drafts_total: int,
    profile: CategoryProfile,
) -> str:
    """
    Полный цикл шагов open_form → save_draft для ОДНОГО черновика (ТЗ §9, §16).

    step/step_label/done в job описывают текущий черновик. profile —
    профиль категории (category_profiles): источник категорийных данных
    (путь/крошка/hidden-ID, словари полей, префиксы комбобоксов, бренд).
    Возвращает конечный URL после «Сохранить и выйти». Исключения шагов
    уходят наверх — их обрабатывает run_publish_job.
    """
    # ── Шаг 2: open_form (заново для каждого черновика, §16) ─────────────
    _set_step(job, "open_form")
    form_state = await _step_open_form(page, profile)
    await _pause()

    # ── Шаг 3: select_category ────────────────────────────────────────────
    _set_step(job, "select_category")
    await _step_select_category(page, form_state, profile)
    await _pause()

    # ── Шаг 4: check_category ─────────────────────────────────────────────
    _set_step(job, "check_category")
    await _step_check_category(page, profile)
    await _pause()

    # ── Шаг 5: fill_title (+ защита от перезаписи для i>1, ТЗ §16) ───────
    _set_step(job, "fill_title")
    await _guard_against_reopened_draft(page, data.title, draft_index, drafts_total)
    await _clear_and_type(page, psel.TITLE_INPUT, data.title)
    await _pause()

    # ── Шаг 6: upload_photos ──────────────────────────────────────────────
    _set_step(job, "upload_photos")
    await _step_upload_photos(page, data.photo_paths)
    await _pause()

    # ── Шаг 7: fill_fields ────────────────────────────────────────────────
    _set_step(job, "fill_fields")
    await _step_fill_fields(page, data, profile)
    await _pause()

    # ── Шаг 8: fill_description ───────────────────────────────────────────
    _set_step(job, "fill_description")
    await _step_fill_description(page, data.description)
    await _pause()

    # ── Шаг 9: fill_price ─────────────────────────────────────────────────
    _set_step(job, "fill_price")
    await _step_fill_price(page, data.price)
    await _pause()

    # ── Шаг 10: fill_address ──────────────────────────────────────────────
    _set_step(job, "fill_address")
    await _step_fill_address(page, data.full_address())
    await _pause()

    # ── Шаг 11: save_draft ────────────────────────────────────────────────
    _set_step(job, "save_draft")
    return await _step_save_draft(page)


def _format_partial_error(message: str, drafts_saved: int, drafts_total: int) -> str:
    """
    Дополняет текст ошибки/инструкции сводкой частичного успеха (ТЗ §16):
    «сохранено k-1 из N». В одиночном режиме (N=1) текст не трогаем;
    если сводка уже есть в сообщении (защита от перезаписи) — не дублируем.
    """
    if drafts_total <= 1 or "сохранено" in message.lower():
        return message
    return f"{message} Сохранено {drafts_saved} из {drafts_total}."


def _load_prep_variant(
    prep_dir: pathlib.Path, draft_index: int
) -> tuple[str, str, list[str]]:
    """
    Читает title/description/фото варианта draft_index из папки подготовки
    tmp/publish/prep_{prep_id}/draft_{i:02d}/ (ТЗ §17; пишет preparation.py).

    Возвращает (title, description, отсортированный список путей фото).
    Нет папки/файлов/фото или пустые тексты → StepError с понятным русским
    текстом (какой именно путь не найден/пуст). Чистая функция без Playwright.
    """
    draft_dir = prep_dir / f"draft_{draft_index:02d}"
    if not draft_dir.is_dir():
        raise StepError(f"Папка варианта черновика не найдена: {draft_dir}")

    title_path = draft_dir / "title.txt"
    text_path = draft_dir / "text.txt"
    for path, label in ((title_path, "названием"), (text_path, "описанием")):
        if not path.is_file():
            raise StepError(f"Файл с {label} варианта не найден: {path}")

    try:
        title = title_path.read_text(encoding="utf-8").strip()
        description = text_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StepError(
            f"Не удалось прочитать тексты варианта из {draft_dir}: {exc}"
        ) from exc
    if not title:
        raise StepError(f"Название варианта пустое: {title_path}")
    if not description:
        raise StepError(f"Описание варианта пустое: {text_path}")

    photos_dir = draft_dir / "photos"
    if not photos_dir.is_dir():
        raise StepError(f"Папка фото варианта не найдена: {photos_dir}")
    photo_paths = [str(p) for p in sorted(photos_dir.iterdir()) if p.is_file()]
    if not photo_paths:
        raise StepError(f"В папке фото варианта нет файлов: {photos_dir}")
    return title, description, photo_paths


async def run_publish_job(
    job_id: str,
    job: dict[str, Any],
    data: DraftData,
    *,
    cdp_url: Optional[str],
    tmp_dir: Optional[str] = None,
) -> None:
    """
    Выполняет сценарий сохранения черновиков, обновляя job (dict из PUBLISH_JOBS).

    Пакетный режим (ТЗ §16): число черновиков читается из job["drafts_total"]
    (кладёт app.py; отсутствует → 1, обратная совместимость). connect_chrome —
    один раз, затем полный цикл шагов на каждый черновик, пауза 5–15 с между
    черновиками. Прогресс пакета — job["draft_index"/"drafts_saved"/"saved_urls"].

    Вариативность (ТЗ §17): если в job есть "prep_id" (кладёт app.py), на каждый
    черновик i подставляются title/description/фото варианта из
    tmp/publish/prep_{prep_id}/draft_{i:02d}/ (_load_prep_variant); без prep_id
    поведение прежнее — данные data одинаковы для всех черновиков.

    Аргументы:
        job_id:  идентификатор задачи
        job:     состояние задачи (status/step/step_label/done/total/error/...)
        data:    провалидированные данные черновика (одинаковые для всех N)
        cdp_url: адрес CDP Chrome пользователя (None → сразу failed)
        tmp_dir: каталог tmp/publish/{job_id} с фото; удаляется при done,
                 при failed/needs_user_action — остаётся для разбора.

    Никогда не бросает исключений наружу — итог только в job["status"].
    """
    # Ленивый импорт: playwright нужен только при реальном прогоне
    from playwright.async_api import async_playwright

    from browser import connect_over_cdp

    # Сколько черновиков: из состояния задачи; мусор/отсутствие → 1
    try:
        drafts_total = int(job.get("drafts_total") or DRAFTS_DEFAULT)
    except (TypeError, ValueError):
        drafts_total = DRAFTS_DEFAULT
    drafts_total = max(DRAFTS_MIN, min(DRAFTS_MAX, drafts_total))

    # Профиль категории: из data.category; неизвестный/пустой ключ → JACKETS
    profile = category_profiles.get_profile(getattr(data, "category", None))
    logger.info("Категория задачи: %s (%s)", profile.key, profile.label)

    job["status"] = "running"
    job["total"] = TOTAL_STEPS
    job["drafts_total"] = drafts_total
    job["draft_index"] = 0
    job["drafts_saved"] = 0
    job["saved_urls"] = []
    page: Any = None

    try:
        async with async_playwright() as pw:
            # ── Шаг 1: connect_chrome (один раз на всю задачу) ────────────
            _set_step(job, "connect_chrome")
            if not cdp_url:
                raise StepError(
                    "CDP-адрес не задан. Сначала запусти start-chrome.bat."
                )
            try:
                context = await connect_over_cdp(pw, cdp_url)
            except RuntimeError as exc:
                raise StepError(
                    f"Chrome не найден по адресу {cdp_url}. "
                    f"Сначала запусти start-chrome.bat. ({exc})"
                ) from exc
            # Открываем СВОЮ вкладку; браузер пользователя не трогаем
            page = await context.new_page()
            await _pause()

            # ── Черновики 1..N: полный цикл шагов на каждый (ТЗ §16) ─────
            for draft_index in range(1, drafts_total + 1):
                job["draft_index"] = draft_index

                if page is None or _page_is_closed(page):
                    page = await context.new_page()
                    logger.info(
                        "Черновик %d/%d: вкладка пересоздана после закрытия Авито",
                        draft_index, drafts_total,
                    )

                if draft_index > 1:
                    # Пауза 5–15 с между черновиками (антибот, ТЗ §16)
                    pause_s = random.uniform(DRAFT_PAUSE_MIN_S, DRAFT_PAUSE_MAX_S)
                    logger.info(
                        "Пауза %.1f с перед черновиком %d/%d",
                        pause_s, draft_index, drafts_total,
                    )
                    await asyncio.sleep(pause_s)

                logger.info("Черновик %d/%d: начат", draft_index, drafts_total)

                # Вариативность (ТЗ §17): при наличии prep_id подставляем
                # title/description/фото варианта i из папки подготовки.
                # Без prep_id — прежнее поведение: data без изменений.
                data_i = data
                if job.get("prep_id"):
                    title_i, desc_i, photos_i = _load_prep_variant(
                        TMP_PUBLISH_DIR / f"prep_{job['prep_id']}", draft_index
                    )
                    data_i = replace(
                        data,
                        title=title_i,
                        description=desc_i,
                        photo_paths=tuple(photos_i),
                    )
                    logger.info(
                        "Черновик %d/%d: взят вариант из подготовки (%d фото)",
                        draft_index, drafts_total, len(photos_i),
                    )

                final_url = await _run_single_draft(
                    page, job, data_i, draft_index, drafts_total, profile
                )
                job["drafts_saved"] = draft_index
                job["saved_urls"].append(final_url)
                job["result_url"] = final_url  # последний сохранённый URL
                logger.info(
                    "Черновик %d/%d: сохранён, URL=%s",
                    draft_index, drafts_total, final_url,
                )

            # ── Шаг 12: done ──────────────────────────────────────────────
            _set_step(job, "done")
            job["status"] = "done"
            job["done"] = TOTAL_STEPS
            logger.info(
                "Задача %s: сохранено черновиков %d/%d (последний URL: %s)",
                job_id, job["drafts_saved"], drafts_total, job.get("result_url"),
            )

    except Exception as exc:  # любой сбой шага — сервер не падает, итог в job
        current_step = str(job.get("step") or "unknown")
        if isinstance(exc, UserActionRequired):
            # Терминальный статус: нужна ручная помощь пользователя
            status = "needs_user_action"
            message = _format_partial_error(
                str(exc), job.get("drafts_saved", 0), drafts_total
            )
            logger.warning("Задача %s, шаг %s: needs_user_action — %s",
                           job_id, current_step, message)
        elif isinstance(exc, StepError):
            status = "failed"
            message = _format_partial_error(
                str(exc), job.get("drafts_saved", 0), drafts_total
            )
            logger.error("Задача %s, шаг %s: failed — %s", job_id, current_step, message)
        else:  # непредвиденный сбой
            status = "failed"
            logger.exception("Задача %s, шаг %s: непредвиденная ошибка: %s",
                             job_id, current_step, exc)
            message = _format_partial_error(
                f"Непредвиденная ошибка на шаге {current_step}: {exc}",
                job.get("drafts_saved", 0), drafts_total,
            )
        # Путь дампа отдаём фронту в статусе (debug_dir = debug/publish/<job_id>)
        job["debug_dir"] = await _dump_failure(page, job_id, current_step, message)
        job["status"] = status
        job["error"] = message

    finally:
        # Закрываем ТОЛЬКО свою вкладку — браузер пользователя не трогаем
        if page is not None:
            try:
                await page.close()
                logger.info("Задача %s: своя вкладка закрыта", job_id)
            except Exception as exc:
                logger.debug("Не удалось закрыть вкладку: %s", exc)

        # Фото: при done подчищаем tmp; при failed/needs_user_action оставляем
        if tmp_dir and job.get("status") == "done":
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.info("Задача %s: временные фото удалены (%s)", job_id, tmp_dir)
            except Exception as exc:
                logger.warning("Не удалось удалить %s: %s", tmp_dir, exc)


# ---------------------------------------------------------------------------
# Самотесты валидации (запуск: python backend/publisher.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    _GOOD_FIELDS: dict[str, Any] = {
        "title": "Мужской классический костюм",
        "trade_type": "Продаю своё",
        "condition": "Отличное",
        "size": "48 (M)",
        "brand": "Hugo Boss",
        "color": "Чёрный",
        "description": "Отличный костюм, надевался один раз.",
        "price": "15000",
        "city": "Москва",
        "address": "ул. Арбат, 1",
    }
    _GOOD_PHOTO = ("suit.jpg", "image/jpeg", 1024 * 1024)
    # Профиль по умолчанию для базовых тестов валидации — «Пиджаки и костюмы»
    _PROFILE = category_profiles.JACKETS

    # ── Тест 1: валидная форма → ошибок нет ──────────────────────────────────
    errs = validate_publish_form(_GOOD_FIELDS, [_GOOD_PHOTO], _PROFILE)
    assert errs == [], f"Валидная форма не должна давать ошибок: {errs}"
    print("[OK] Тест 1: валидная форма проходит")

    # ── Тест 2: пустые название/описание/бренд/город/адрес ──────────────────
    empty = dict(_GOOD_FIELDS, title="", description="  ", brand="", city="", address="")
    errs = validate_publish_form(empty, [_GOOD_PHOTO], _PROFILE)
    bad_fields = {e["field"] for e in errs}
    for f in ("title", "description", "brand", "city", "address"):
        assert f in bad_fields, f"Нет ошибки для пустого поля {f}: {errs}"
    print("[OK] Тест 2: пустые обязательные поля ловятся")

    # ── Тест 3: цена 0 / отрицательная / нечисловая ──────────────────────────
    for bad_price in ("0", "-100", "abc", ""):
        errs = validate_publish_form(dict(_GOOD_FIELDS, price=bad_price), [_GOOD_PHOTO], _PROFILE)
        assert any(e["field"] == "price" for e in errs), (
            f"Цена {bad_price!r} должна быть отклонена: {errs}"
        )
    print("[OK] Тест 3: цена 0/-100/abc/'' отклоняется")

    # ── Тест 4: select/radio вне словарей ────────────────────────────────────
    for field_name, bad_value in (
        ("trade_type", "Дарю"),
        ("condition", "Норм"),
        ("size", "99 (XXXXL)"),
        ("color", "Хаки"),
    ):
        errs = validate_publish_form(dict(_GOOD_FIELDS, **{field_name: bad_value}), [_GOOD_PHOTO], _PROFILE)
        assert any(e["field"] == field_name for e in errs), (
            f"{field_name}={bad_value!r} должно быть отклонено: {errs}"
        )
    print("[OK] Тест 4: значения вне словарей отклоняются")

    # ── Тест 5: 0 фото и 11 фото ─────────────────────────────────────────────
    errs = validate_publish_form(_GOOD_FIELDS, [], _PROFILE)
    assert any(e["field"] == "photos" for e in errs), "0 фото должно быть отклонено"
    errs = validate_publish_form(_GOOD_FIELDS, [_GOOD_PHOTO] * 11, _PROFILE)
    assert any(e["field"] == "photos" for e in errs), "11 фото должно быть отклонено"
    print("[OK] Тест 5: 0 и 11 фото отклоняются")

    # ── Тест 6: недопустимый формат и превышение 25 МБ ───────────────────────
    errs = validate_publish_form(_GOOD_FIELDS, [("doc.pdf", "application/pdf", 1000)], _PROFILE)
    assert any(e["field"] == "photos" for e in errs), "PDF должен быть отклонён"
    errs = validate_publish_form(
        _GOOD_FIELDS, [("big.jpg", "image/jpeg", 26 * 1024 * 1024)], _PROFILE
    )
    assert any(e["field"] == "photos" for e in errs), ">25 МБ должно быть отклонено"
    # heic допустим
    errs = validate_publish_form(_GOOD_FIELDS, [("photo.heic", "image/heic", 1000)], _PROFILE)
    assert errs == [], f"heic должен проходить: {errs}"
    print("[OK] Тест 6: форматы и размер фото проверяются")

    # ── Тест 7: build_draft_data и full_address ─────────────────────────────
    draft = build_draft_data(_GOOD_FIELDS, ["tmp/publish/x/suit.jpg"])
    assert draft.price == 15000
    assert draft.full_address() == "Москва, ул. Арбат, 1"
    assert draft.category == "jackets", draft.category  # дефолт категории
    assert draft.summary()["photos_count"] == 1
    assert draft.summary()["category"] == "jackets", draft.summary()
    assert "photo_paths" not in draft.summary(), "В сводке не должно быть путей к фото"
    # Явная категория переносится в DraftData
    draft_sn = build_draft_data(_GOOD_FIELDS, ["x.jpg"], category="sneakers")
    assert draft_sn.category == "sneakers", draft_sn.category
    print("[OK] Тест 7: build_draft_data / full_address / summary / category")

    # ── Тест 8: стейт-машина — 12 шагов, метки по-русски ─────────────────────
    assert TOTAL_STEPS == 12, f"Шагов должно быть 12, есть {TOTAL_STEPS}"
    assert STEPS[0][0] == "connect_chrome" and STEPS[-1][0] == "done"
    assert STEP_LABELS["select_category"] == "Выбор категории"
    assert STEP_LABELS["upload_photos"] == "Загрузка фотографий"
    assert _STEP_INDEX["save_draft"] == 11
    print("[OK] Тест 8: стейт-машина из 12 шагов")

    # ── Тест 9: drafts_count — валидация пакетного режима (ТЗ §16) ──────────
    # 0 и 11 — вне диапазона [1, 10] → ошибка поля drafts_count
    for bad_count in ("0", "11", "-1", "abc", "1.5"):
        errs = validate_publish_form(
            dict(_GOOD_FIELDS, drafts_count=bad_count), [_GOOD_PHOTO], _PROFILE
        )
        assert any(e["field"] == "drafts_count" for e in errs), (
            f"drafts_count={bad_count!r} должно быть отклонено: {errs}"
        )
    # «3» строкой → ок, парсится в 3
    errs = validate_publish_form(dict(_GOOD_FIELDS, drafts_count="3"), [_GOOD_PHOTO], _PROFILE)
    assert errs == [], f"drafts_count='3' должно проходить: {errs}"
    assert parse_drafts_count("3") == 3
    # Отсутствие поля → ок, дефолт 1 (обратная совместимость)
    errs = validate_publish_form(_GOOD_FIELDS, [_GOOD_PHOTO], _PROFILE)
    assert errs == [], f"Форма без drafts_count должна проходить: {errs}"
    assert parse_drafts_count(None) == 1
    assert parse_drafts_count("") == 1
    assert parse_drafts_count("  ") == 1
    # Граничные значения диапазона
    assert parse_drafts_count("1") == 1 and parse_drafts_count("10") == 10
    assert parse_drafts_count("0") is None and parse_drafts_count("11") is None
    print("[OK] Тест 9: drafts_count - 0/11 ошибка, '3' -> 3, отсутствие -> 1")

    # ── Тест 10: сводка частичного успеха в тексте ошибки ────────────────────
    # N=1 — текст не трогаем (одиночный режим как раньше)
    assert _format_partial_error("Ошибка X.", 0, 1) == "Ошибка X."
    # N>1 — дописываем «Сохранено k-1 из N»
    assert _format_partial_error("Ошибка X.", 2, 5) == "Ошибка X. Сохранено 2 из 5."
    # Сводка уже есть (защита от перезаписи) — не дублируем
    msg = "Авито переоткрывает существующий черновик — сохранено 1 из 3. Дальше пока вручную."
    assert _format_partial_error(msg, 1, 3) == msg
    print("[OK] Тест 10: частичный успех дописывается в ошибку без дублей")

    # ── Тест 11: _wait_form_exited — navigated / page_closed / form_gone / timeout ──
    class _FakeContext:
        def __init__(self) -> None:
            self.pages: list[Any] = []

    class _FakeExitPage:
        def __init__(self, url: str) -> None:
            self.url = url
            self._closed = False
            self.context = _FakeContext()
            self.context.pages = [self]
            self.visible: dict[str, bool] = {
                psel.SAVE_AND_EXIT_BUTTON: True,
                psel.CATEGORY_TITLE: True,
            }

        def is_closed(self) -> bool:
            return self._closed

        async def close(self) -> None:
            self._closed = True

    async def _fake_visible_probe(page: Any, selector: str) -> bool:
        return bool(page.visible.get(selector, False))

    async def _flip_after(delay_s: float, action: Callable[[], None]) -> None:
        await asyncio.sleep(delay_s)
        action()

    async def _test_wait_form_exited_navigated() -> None:
        page = _FakeExitPage(ADDITEM_URL)
        task = asyncio.create_task(_flip_after(
            0.005,
            lambda: setattr(page, "url", "https://www.avito.ru/profile/items"),
        ))
        outcome, final_url = await _wait_form_exited(
            page,
            ADDITEM_URL,
            timeout_s=0.05,
            interval_s=0.001,
            selector_visible_probe=_fake_visible_probe,
        )
        await task
        assert outcome == "navigated", outcome
        assert final_url == "https://www.avito.ru/profile/items", final_url

    async def _test_wait_form_exited_page_closed() -> None:
        page = _FakeExitPage(ADDITEM_URL)
        task = asyncio.create_task(_flip_after(
            0.005,
            lambda: setattr(page, "_closed", True),
        ))
        outcome, final_url = await _wait_form_exited(
            page,
            ADDITEM_URL,
            timeout_s=0.05,
            interval_s=0.001,
            selector_visible_probe=_fake_visible_probe,
        )
        await task
        assert outcome == "page_closed", outcome
        assert final_url == "", final_url

    async def _test_wait_form_exited_form_gone() -> None:
        page = _FakeExitPage(ADDITEM_URL)

        def _hide_form() -> None:
            page.visible[psel.SAVE_AND_EXIT_BUTTON] = False
            page.visible[psel.CATEGORY_TITLE] = False

        task = asyncio.create_task(_flip_after(0.005, _hide_form))
        outcome, final_url = await _wait_form_exited(
            page,
            ADDITEM_URL,
            timeout_s=0.05,
            interval_s=0.001,
            selector_visible_probe=_fake_visible_probe,
        )
        await task
        assert outcome == "form_gone", outcome
        assert final_url == "", final_url

    async def _test_wait_form_exited_timeout() -> None:
        page = _FakeExitPage(ADDITEM_URL)
        try:
            await _wait_form_exited(
                page,
                ADDITEM_URL,
                timeout_s=0.01,
                interval_s=0.001,
                selector_visible_probe=_fake_visible_probe,
            )
        except StepError as exc:
            assert "Сохранить и выйти" in str(exc) or "страницы формы" in str(exc), str(exc)
            return
        raise AssertionError("Ожидали StepError по таймауту _wait_form_exited")

    asyncio.run(_test_wait_form_exited_navigated())
    asyncio.run(_test_wait_form_exited_page_closed())
    asyncio.run(_test_wait_form_exited_form_gone())
    asyncio.run(_test_wait_form_exited_timeout())
    print("[OK] Тест 11: _wait_form_exited покрывает navigated/page_closed/form_gone/timeout")

    # ── Тест 12: _load_prep_variant — чтение варианта из папки подготовки ────
    import tempfile

    with tempfile.TemporaryDirectory() as _tmpdir:
        _prep_dir = pathlib.Path(_tmpdir) / "prep_test"
        _d1 = _prep_dir / "draft_01"
        (_d1 / "photos").mkdir(parents=True)
        (_d1 / "title.txt").write_text("Пиджак Hugo Boss, вариант 1", encoding="utf-8")
        (_d1 / "text.txt").write_text("Описание варианта 1.", encoding="utf-8")
        (_d1 / "photos" / "2.jpg").write_bytes(b"fake-jpeg-2")
        (_d1 / "photos" / "1.jpg").write_bytes(b"fake-jpeg-1")

        # Корректная папка → правильные значения, фото отсортированы
        _t, _desc, _photos = _load_prep_variant(_prep_dir, 1)
        assert _t == "Пиджак Hugo Boss, вариант 1", _t
        assert _desc == "Описание варианта 1.", _desc
        assert _photos == [
            str(_d1 / "photos" / "1.jpg"),
            str(_d1 / "photos" / "2.jpg"),
        ], _photos

        # Отсутствие папки draft_02 → StepError с путём в тексте
        try:
            _load_prep_variant(_prep_dir, 2)
        except StepError as exc:
            assert "draft_02" in str(exc), str(exc)
        else:
            raise AssertionError("Ожидали StepError для отсутствующей папки draft_02")

        # Папка есть, но фото нет → StepError
        _d3 = _prep_dir / "draft_03"
        (_d3 / "photos").mkdir(parents=True)
        (_d3 / "title.txt").write_text("Вариант 3", encoding="utf-8")
        (_d3 / "text.txt").write_text("Описание 3.", encoding="utf-8")
        try:
            _load_prep_variant(_prep_dir, 3)
        except StepError as exc:
            assert "фото" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("Ожидали StepError для варианта без фото")
    print("[OK] Тест 12: _load_prep_variant - чтение варианта, нет папки/фото -> StepError")

    # ── Тест: лог publisher пишется в logs/ КОРНЯ проекта (не зависит от cwd) ──
    _file_handlers = [
        h for h in logger.handlers if isinstance(h, logging.FileHandler)
    ]
    assert _file_handlers, "У логгера 'publisher' нет файлового хэндлера"
    _log_path = pathlib.Path(_file_handlers[0].baseFilename).resolve()
    _expected = (pathlib.Path(__file__).resolve().parent.parent / "logs" / "publisher.log").resolve()
    assert _log_path == _expected, (
        f"Лог publisher пишется в {_log_path}, ожидали {_expected}"
    )
    print("[OK] Тест: logs/publisher.log в корне проекта независимо от cwd")

    # ── Тест 13: validate_publish_form учитывает профиль категории ──────────
    import category_profiles as cp
    # size пиджаков валиден для jackets, но не для sneakers (другой словарь обуви)
    base = {
        "title": "t", "trade_type": "Продаю своё", "condition": "Отличное",
        "size": "48 (M)", "brand": "b", "color": "Чёрный",
        "description": "d", "price": "100", "city": "Москва", "address": "ул. 1",
    }
    photos = [("a.jpg", "image/jpeg", 1000)]
    assert validate_publish_form(base, photos, cp.JACKETS) == []
    # тот же размер «48 (M)» не из словаря кроссовок → ошибка по полю size
    errs = validate_publish_form(base, photos, cp.SNEAKERS)
    assert any(e["field"] == "size" for e in errs), errs
    print("[OK] validate_publish_form учитывает профиль категории")

    print("\n=== Все самотесты publisher.py пройдены ===")
