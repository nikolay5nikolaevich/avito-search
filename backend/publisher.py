"""
Публикатор объявлений Авито.

Стейт-машина полной публикации: connect_chrome → заполнение формы →
continue_listing → fill_view_price → continue_view_price → skip_services →
open_next_form → done.

Пакетный режим: объявления отправляются СТРОГО ПОСЛЕДОВАТЕЛЬНО в одной задаче.
После перехода со страницы услуг в кабинет начинается следующее объявление. Наличие
карточки во вкладке «Активные» намеренно не проверяется. Прогресс пакета хранится в
item_index/items_total/items_published/published_urls; названия этих полей сохранены
для совместимости API, хотя теперь они означают передачу объявления Авито.

Работает ТОЛЬКО через Chrome, запущенный пользователем (start-chrome.bat + CDP).
Логин/пароль/куки не хранятся; закрываем только собственную вкладку.

Опасные переходы выполняются только после проверки контекста экрана,
точного текста управляющей кнопки и всех финансовых инвариантов. Любое
неизвестное состояние останавливает задачу до клика.

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
import re
import shutil
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

import avito_publish_selectors as psel
import category_profiles
import publish_state
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
    ("fill_item_price", "Цена товара"),
    ("fill_address", "Геолокация объявления"),
    ("continue_listing", "Переход к публикации"),
    ("fill_view_price", "Стоимость просмотра"),
    ("continue_view_price", "Подтверждение стоимости просмотра"),
    ("skip_services", "Отказ от дополнительных услуг"),
    ("open_next_form", "Переход к следующему объявлению"),
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
PUBLISH_TRANSITION_TIMEOUT_S: float = 30.0
OPEN_FORM_RETRY_DELAYS_S: tuple[float, ...] = (5.0, 15.0, 30.0)
TRANSIENT_NAVIGATION_ERRORS: tuple[str, ...] = (
    "net::ERR_ABORTED",
    "net::ERR_NAME_NOT_RESOLVED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_INTERNET_DISCONNECTED",
    "net::ERR_CONNECTION_TIMED_OUT",
    "net::ERR_TIMED_OUT",
)

# Штатный пункт справочника брендов Авито для марок, которых там нет
# (разведка 16.08.2026: пункт присутствует в списке кроссовок).
NO_BRAND_LABEL: str = "Без бренда"

VIEW_PRICE_ACTION_TEXTS: frozenset[str] = frozenset({
    "Сохранить",
    "Продолжить с минимальной ценой",
})

# Пакетный режим (ТЗ §16): сколько одинаковых черновиков делаем за задачу
DRAFTS_MIN: int = 1
DRAFTS_MAX: int = 20
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

    def __init__(
        self,
        message: str,
        *,
        user_action: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.user_action = user_action


# ---------------------------------------------------------------------------
# Данные черновика и валидация (ТЗ §8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LocationData:
    """Одна геолокация объявления, отдельными городом и адресом."""

    city: str
    address: str

    def full_address(self) -> str:
        """Строка для гео-саджеста Авито: «Город, адрес»."""
        return f"{self.city}, {self.address}"


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
    locations: tuple[LocationData, ...]
    # Жёсткий финансовый потолок стоимости просмотра. Движок всегда берёт
    # минимальную цену, которую предлагает Авито; выше потолка не поднимается
    # никогда — превышение останавливает публикацию (view_price_cap_exceeded).
    view_price_max: Decimal
    photo_paths: tuple[str, ...]
    category: str = "jackets"  # ключ профиля категории (category_profiles)
    # Ключ profile.item_type_options. Пустая строка для категорий без поля
    # «Вид товара» (пиджаки, кроссовки) — движок его тогда не заполняет.
    item_type: str = ""
    # Ключ profile.material_options (мультикомбобокс, выбираем одно значение).
    # Пустая строка для категорий без поля «Материал основной части».
    material: str = ""
    # Ключ profile.style_options (радио). Пустая строка для категорий без поля «Стиль».
    style: str = ""

    def location_for(self, item_index: int) -> LocationData:
        """Возвращает геолокацию объявления по 1-based индексу."""
        if not (1 <= item_index <= len(self.locations)):
            raise IndexError(f"Нет геолокации для объявления №{item_index}")
        return self.locations[item_index - 1]

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
            "locations": [
                {"city": location.city, "address": location.address}
                for location in self.locations
            ],
            "view_price_max": format(self.view_price_max, "f"),
            "category": self.category,
            "item_type": self.item_type,
            "material": self.material,
            "style": self.style,
            "photos_count": len(self.photo_paths),
        }


def parse_view_price(raw: Any) -> Optional[Decimal]:
    """Разбирает положительную десятичную стоимость просмотра без float."""
    text = str(raw if raw is not None else "").strip()
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        return None
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


_ADDRESS_TYPE_WORDS: tuple[str, ...] = (
    "улица",
    "ул.",
    "проспект",
    "пр-т",
    "переулок",
    "пер.",
    "шоссе",
    "набережная",
    "наб.",
    "бульвар",
    "бул.",
    "площадь",
    "пл.",
    "проезд",
    "аллея",
    "линия",
    "микрорайон",
    "мкр.",
    "тракт",
)


def is_structured_address(address: str) -> bool:
    """Отсекает строки вроде «королева 26», которые Авито сводит к городу.

    Номер дома не обязателен: адрес до улицы («Российская улица») Авито
    принимает, поэтому требуем только тип улицы либо запятую.
    """
    normalized = " ".join(str(address or "").casefold().split())
    return "," in normalized or any(word in normalized for word in _ADDRESS_TYPE_WORDS)


def parse_cpxpromo_item_id(url: str) -> Optional[str]:
    """Извлекает item id только из точного пути страницы стоимости просмотра."""
    try:
        path = urlsplit(str(url or "")).path
    except ValueError:
        return None
    match = re.fullmatch(r"/cpxpromo/(\d+)/?", path)
    return match.group(1) if match else None


def parse_ruble_decimal(text: Any) -> Optional[Decimal]:
    """Разбирает единственную денежную величину из текста Авито без float."""
    normalized = str(text if text is not None else "")
    tokens = re.findall(
        r"(?<!\d)\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?(?!\d)",
        normalized,
    )
    if len(tokens) != 1:
        return None
    number = re.sub(r"[ \u00a0\u202f]", "", tokens[0]).replace(",", ".")
    try:
        return Decimal(number)
    except InvalidOperation:
        return None


ALLOWED_SERVICE_SWITCHES: frozenset[str] = frozenset(
    psel.SERVICE_SWITCH_MARKERS
)


def validate_service_switch_states(
    states: list[dict[str, Any]],
) -> Optional[str]:
    """Fail-closed проверяет полный allowlist выключенных платных услуг."""
    markers = [str(state.get("marker") or "") for state in states]
    marker_set = set(markers)

    unknown = sorted(marker_set - ALLOWED_SERVICE_SWITCHES)
    if unknown:
        return f"Обнаружена неизвестная платная услуга: {', '.join(unknown)}"
    if len(markers) != len(marker_set):
        return "Обнаружен повтор переключателя платной услуги"

    missing = sorted(ALLOWED_SERVICE_SWITCHES - marker_set)
    if missing:
        return f"Отсутствует переключатель платной услуги: {', '.join(missing)}"

    for state in states:
        marker = str(state.get("marker") or "")
        aria_checked = state.get("aria_checked")
        checked = state.get("checked")
        if aria_checked not in {"true", "false"} or not isinstance(checked, bool):
            return f"Не удалось определить состояние услуги {marker}"
        aria_enabled = aria_checked == "true"
        if aria_enabled != checked:
            return f"Состояния переключателя услуги {marker} расходятся"
        if checked:
            return f"Платная услуга {marker} включена"
    return None


def parse_locations_json(raw: Any) -> Optional[tuple[LocationData, ...]]:
    """Разбирает JSON-массив геолокаций, сохраняя исходный порядок."""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, list):
        return None

    locations: list[LocationData] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        city = item.get("city")
        address = item.get("address")
        if not isinstance(city, str) or not isinstance(address, str):
            return None
        locations.append(LocationData(city=city.strip(), address=address.strip()))
    return tuple(locations)


def parse_drafts_count(raw: Any) -> Optional[int]:
    """
    Нормализует поле «Сколько черновиков» (ТЗ §16).

    Отсутствие/пустая строка → DRAFTS_DEFAULT (обратная совместимость:
    запрос без drafts_count работает как раньше — 1 черновик).
    Целое число в диапазоне [DRAFTS_MIN, DRAFTS_MAX] → это число.
    Всё остальное (нечисловое, 0, 21, ...) → None (невалидно).
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
                description, price, locations_json, view_price_max,
                drafts_count (необязательное, 1–20, дефолт 1 — ТЗ §16).
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
    # Бренд намеренно НЕ обязателен: Авито знает не все марки (например,
    # спецобувь Heckel отсутствует в подсказках). Пустой бренд — штатный
    # режим «без бренда», см. _select_brand.
    for field_name, label in (
        ("title", "Название"),
        ("description", "Описание"),
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

    # --- Сколько черновиков: 1–20, дефолт 1 (ТЗ §16) ---
    drafts_count = parse_drafts_count(fields.get("drafts_count"))
    if drafts_count is None:
        errors.append({
            "field": "drafts_count",
            "error": f"Количество объявлений: целое число от {DRAFTS_MIN} до {DRAFTS_MAX}",
        })

    # --- Геолокации: JSON-массив, ровно по одной на объявление ---
    locations_raw = str(fields.get("locations_json") or "").strip()
    try:
        locations_payload = json.loads(locations_raw) if locations_raw else None
    except (json.JSONDecodeError, TypeError):
        locations_payload = None

    if not isinstance(locations_payload, list):
        errors.append({
            "field": "locations",
            "error": "Геолокации: требуется корректный JSON-массив",
        })
    else:
        if drafts_count is not None and len(locations_payload) != drafts_count:
            errors.append({
                "field": "locations",
                "error": (
                    "Количество геолокаций должно совпадать с количеством "
                    f"объявлений: ожидалось {drafts_count}, передано {len(locations_payload)}"
                ),
            })
        for index, location in enumerate(locations_payload, start=1):
            if not isinstance(location, dict):
                errors.append({
                    "field": f"locations.{index}",
                    "error": f"Геолокация №{index}: требуется объект с городом и адресом",
                })
                continue
            for key, label in (("city", "Город"), ("address", "Адрес")):
                value = location.get(key)
                if not isinstance(value, str) or not value.strip():
                    errors.append({
                        "field": f"locations.{index}.{key}",
                        "error": f"{label} объявления №{index}: поле не заполнено",
                    })
            address = location.get("address")
            if (
                isinstance(address, str)
                and address.strip()
                and not is_structured_address(address)
            ):
                errors.append({
                    "field": f"locations.{index}.address",
                    "error": (
                        "Добавьте тип улицы (например, «Тверская улица») или отделите дом запятой"
                    ),
                })

    # --- Стоимость просмотра: единственное число — жёсткий потолок.
    # Движок всегда берёт минимальную цену, которую предложит Авито; поле
    # "view_price" (желаемая цена), если пришло от старого фронта или
    # сохранённого шаблона, молча игнорируется — сравнивать потолок не с чем.
    if parse_view_price(fields.get("view_price_max")) is None:
        errors.append({
            "field": "view_price_max",
            "error": "Укажите потолок стоимости просмотра — число больше нуля",
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

    # --- Вид товара: обязателен только у категорий, где поле есть ---
    # У остальных (пиджаки, кроссовки) присланное значение молча игнорируем:
    # фронт мог не очистить поле при переключении категории.
    if profile.has_item_type:
        item_type = _text("item_type")
        if item_type not in profile.item_type_options:
            errors.append({
                "field": "item_type",
                "error": f"Вид товара: недопустимое значение {item_type!r}",
            })

    # --- Материал основной части: обязателен только у категорий, где поле есть ---
    if profile.has_material:
        material = _text("material")
        if material not in profile.material_options:
            errors.append({
                "field": "material",
                "error": f"Материал: недопустимое значение {material!r}",
            })

    # --- Стиль: обязателен только у категорий, где поле есть ---
    if profile.has_style:
        style = _text("style")
        if style not in profile.style_options:
            errors.append({
                "field": "style",
                "error": f"Стиль: недопустимое значение {style!r}",
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
    locations = parse_locations_json(fields.get("locations_json"))
    view_price_max = parse_view_price(fields.get("view_price_max"))
    if locations is None or view_price_max is None:
        raise ValueError("Невалидные геолокации или потолок стоимости просмотра")
    return DraftData(
        title=str(fields["title"]).strip(),
        trade_type=str(fields["trade_type"]).strip(),
        condition=str(fields["condition"]).strip(),
        size=str(fields["size"]).strip(),
        brand=str(fields["brand"]).strip(),
        color=str(fields["color"]).strip(),
        description=str(fields["description"]).strip(),
        price=int(str(fields["price"]).strip()),
        locations=locations,
        photo_paths=tuple(photo_paths),
        view_price_max=view_price_max,
        category=str(category or "jackets").strip(),
        # Есть не у всех категорий: отсутствие поля — норма, не ошибка
        item_type=str(fields.get("item_type") or "").strip(),
        material=str(fields.get("material") or "").strip(),
        style=str(fields.get("style") or "").strip(),
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
    Кликает по ВИДИМОМУ элементу с точным текстом без учёта регистра.

    Нужен для пунктов раскрытого комбобокса: это видимые <div> с текстом опции
    без data-marker (скрытые <option> с тем же текстом игнорируются — невидимы).
    """
    target = _norm_label(text).casefold()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        loc = page.get_by_text(text, exact=False)
        try:
            count = await loc.count()
        except Exception:
            count = 0
        for i in range(count):
            el = loc.nth(i)
            try:
                if (
                    await el.is_visible()
                    and _norm_label(await el.inner_text()).casefold() == target
                ):
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


async def _multi_selected_values(page: Any, prefix: str) -> list[str]:
    """Текущие value выбранных <option> мультиселекта (для верификации)."""
    selector = psel.COMBOBOX_SELECT_TMPL.format(prefix=prefix)
    try:
        return await page.locator(selector).first.evaluate(
            "el => Array.from(el.selectedOptions).map(o => o.value)"
        )
    except Exception:
        return []


async def _select_multi_combobox(page: Any, prefix: str, label: str, option_id: int) -> None:
    """
    Выбирает ОДНО значение в мультикомбоксе Авито (<select multiple>,
    Авито разрешает до 5 значений — мы всегда выбираем ровно одно).

    Проверка идёт по selectedOptions (у <select multiple> свойство .value
    ненадёжно — не отражает реально выбранные пункты). Список после выбора
    остаётся раскрытым — его обязательно закрываем, иначе оверлей перехватит
    клики следующих полей формы.

    В отличие от _select_combobox: повторный клик по пункту разрешён ТОЛЬКО
    если проверка показала, что он ещё НЕ выбран (иначе повторный клик по
    уже выбранному пункту в мультиселекте СНИМАЕТ выбор).
    """
    container_sel = psel.COMBOBOX_CONTAINER_TMPL.format(prefix=prefix)
    expected_value = str(option_id)

    async def _open() -> None:
        container = page.locator(container_sel).first
        try:
            await container.scroll_into_view_if_needed(timeout=WAIT_SELECTOR_TIMEOUT_MS)
            # Клик по контейнеру — переключатель: если список уже раскрыт
            # (в мультиселекте он остаётся открытым после выбора), клик его
            # ЗАКРОЕТ и повторная попытка выбора сгорит впустую.
            if await container.get_attribute("aria-expanded") != "true":
                await container.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
        except Exception as exc:
            logger.error("SELECTOR_MISS: мультикомбобокс %s не открылся: %s", container_sel, exc)
            raise StepError(f"Комбобокс не найден: {container_sel}") from exc
        await asyncio.sleep(random.uniform(0.3, 0.6))  # анимация списка

    async def _search_and_click() -> bool:
        # Список большой (54 пункта) — сначала печатаем в поле поиска, если оно видимо
        search_sel = psel.COMBOBOX_SEARCH_TMPL.format(prefix=prefix)
        if await _selector_now_visible(page, search_sel):
            try:
                await page.locator(search_sel).first.fill(label, timeout=WAIT_SELECTOR_TIMEOUT_MS)
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return await _click_visible_text(page, label)

    await _open()
    await _search_and_click()

    selected = await _multi_selected_values(page, prefix)
    if expected_value not in selected:
        # Одна повторная попытка: список мог закрыться/не среагировать
        await _open()
        await _search_and_click()
        selected = await _multi_selected_values(page, prefix)

    if expected_value not in selected:
        logger.error(
            "SELECTOR_MISS: мультикомбобокс %s не принял %r (выбрано %r, ожидалось %r)",
            prefix, label, selected, expected_value,
        )
        raise StepError(
            f"Не удалось выбрать «{label}» в мультикомбоксе {prefix} (выбрано: {selected!r})"
        )

    # Закрыть раскрытый список — иначе оверлей перехватит клики следующих полей
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.3)
        if await _selector_now_visible(page, container_sel):
            container = page.locator(container_sel).first
            aria_expanded = await container.get_attribute("aria-expanded")
            if aria_expanded == "true":
                await container.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        logger.warning("Мультикомбобокс %s: не удалось штатно закрыть список: %s", prefix, exc)

    # Обязательная перепроверка: Escape в проекте уже известен способностью
    # сбрасывать выбор в некоторых полях Авито (автокомплиты бренда/адреса).
    selected_after_close = await _multi_selected_values(page, prefix)
    if expected_value not in selected_after_close:
        logger.error(
            "SELECTOR_MISS: закрытие списка мультикомбокса %s сбросило выбор %r (осталось %r)",
            prefix, label, selected_after_close,
        )
        raise StepError(
            f"Закрытие списка сбросило выбор «{label}» в мультикомбоксе {prefix}"
        )

    logger.info("Мультикомбокс %s: выбрано %r (value=%s)", prefix, label, option_id)


async def _hidden_value(page: Any, selector: str) -> str:
    """Значение hidden-input (пустая строка, если элемента нет/ошибка чтения)."""
    try:
        return (await page.locator(selector).first.input_value(timeout=3_000)) or ""
    except Exception:
        return ""


async def _guard_against_reopened_draft(
    page: Any, title: str, draft_index: int, drafts_total: int
) -> None:
    """
    Защита от перезаписи в пакетном режиме (ТЗ §16).

    Перед заполнением названия объявления i>1 читаем текущее значение
    input[name='title']:
      - пусто → нормальная свежая форма, заполняем;
      - непусто и СОВПАДАЕТ с нашим названием → Авито переоткрыл уже
        обработанную форму; заполнять нельзя (можно создать дубль) →
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
            "Объявление %d/%d: поле названия уже содержит наше название %r — "
            "Авито переоткрыл уже обработанную форму",
            draft_index, drafts_total, current,
        )
        raise UserActionRequired(
            "Авито переоткрыло уже обработанную форму. "
            "Автоматическая публикация остановлена, чтобы не создать дубль."
        )
    if current:
        logger.info(
            "Объявление %d/%d: в поле названия чужой текст %r — будет очищен",
            draft_index, drafts_total, current,
        )


# ---------------------------------------------------------------------------
# Реализация шагов
# ---------------------------------------------------------------------------

async def _goto_additem_with_dns_retry(page: Any) -> None:
    """Повторяет безопасный GET формы при временном сетевом сбое Chrome."""
    attempts = len(OPEN_FORM_RETRY_DELAYS_S) + 1
    for attempt in range(attempts):
        try:
            await page.goto(
                ADDITEM_URL,
                wait_until="domcontentloaded",
                timeout=WAIT_FORM_TIMEOUT_MS,
            )
            return
        except Exception as exc:
            error_text = str(exc)
            is_transient = any(
                marker in error_text for marker in TRANSIENT_NAVIGATION_ERRORS
            )
            if not is_transient or attempt == attempts - 1:
                raise
            delay_s = OPEN_FORM_RETRY_DELAYS_S[attempt]
            logger.warning(
                "Сеть Авито временно недоступна при открытии формы (%s); "
                "повтор %d/%d через %.1f с",
                next(
                    marker
                    for marker in TRANSIENT_NAVIGATION_ERRORS
                    if marker in error_text
                ),
                attempt + 2,
                attempts,
                delay_s,
            )
            await asyncio.sleep(delay_s)


async def _step_open_form(page: Any, profile: CategoryProfile) -> str:
    """Шаг open_form: goto /additem и распознавание состояния экрана (ТЗ §6)."""
    await _goto_additem_with_dns_retry(page)
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
        actual_values: dict[str, str] = {}

        async def _known_hidden_ids_ready() -> bool:
            actual_values.clear()
            for name in profile.expected_category:
                selector = f"{psel.CATEGORY_HIDDEN_INPUTS}[name='{name}']"
                actual_values[name] = await _hidden_value(page, selector)
            return actual_values == profile.expected_category

        if await _wait_until(
            _known_hidden_ids_ready,
            attempts=10,
            interval_s=0.25,
        ):
            logger.info("Категория подтверждена hidden-полями профиля %s", profile.key)
            await _continue_category_confirmation(page, profile)
            return

        for name, expected in profile.expected_category.items():
            logger.warning(
                "Категория: hidden %s = %r, ожидалось %r",
                name,
                actual_values.get(name, ""),
                expected,
            )
        raise UserActionRequired(_manual_category_instruction(
            "Авито не подтвердил выбранную категорию служебными полями.", profile
        ))

    # Для профиля без известных hidden-ID единственный доступный контроль —
    # точный текст хлебных крошек.
    try:
        crumbs = await page.locator(psel.CATEGORY_TITLE).first.inner_text(timeout=5_000)
    except Exception:
        crumbs = ""
    if profile.category_title_text in crumbs:
        logger.info("Категория подтверждена хлебными крошками: %r", crumbs)
        await _continue_category_confirmation(page, profile)
        return

    raise UserActionRequired(_manual_category_instruction(
        "На форме выбрана другая категория.", profile
    ))


async def _continue_category_confirmation(
    page: Any, profile: CategoryProfile
) -> None:
    """
    Открывает форму с промежуточного экрана «Черновик → Продолжить».

    Авито переиспользует item-edit/button-next и на более позднем опасном шаге,
    поэтому функция вызывается только ПОСЛЕ проверки категории и кликает лишь
    когда title уже присутствует в DOM, но остаётся невидимым. Если форма уже
    открыта, ничего не делает.
    """
    title = page.locator(psel.TITLE_INPUT).first

    # Сначала даём React шанс открыть форму без дополнительного подтверждения.
    if await _selector_visible(page, psel.TITLE_INPUT, 2_000):
        logger.info("Форма категории уже открыта — промежуточный клик не нужен")
        return

    try:
        title_exists = await title.count() > 0
    except Exception:
        title_exists = False
    if not title_exists:
        raise UserActionRequired(_manual_category_instruction(
            "Категория выбрана, но поле названия отсутствует в DOM.", profile
        ))

    button = page.locator(psel.CATEGORY_CONFIRM_CONTINUE_BUTTON).first
    try:
        button_visible = await button.is_visible()
        button_text = _norm_label(await button.inner_text(timeout=3_000))
    except Exception:
        button_visible = False
        button_text = ""

    if not button_visible or not button_text.startswith("Продолжить"):
        raise UserActionRequired(_manual_category_instruction(
            "Категория выбрана, но промежуточная кнопка «Продолжить» не найдена.",
            profile,
        ))

    logger.info(
        "Категория %s подтверждена: нажимаю промежуточную кнопку «Продолжить»",
        profile.key,
    )
    try:
        await button.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        raise UserActionRequired(_manual_category_instruction(
            "Не удалось нажать промежуточную кнопку «Продолжить».", profile
        )) from exc

    if not await _selector_visible(page, psel.TITLE_INPUT, WAIT_FORM_TIMEOUT_MS):
        raise UserActionRequired(_manual_category_instruction(
            "После подтверждения категории форма объявления не открылась.", profile
        ))
    logger.info("Промежуточный экран категории пройден, поле title стало видимым")


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
            f"img на blob после загрузки: {last_blob}\n"
            f"img на CDN (avito.st) после загрузки: {last_cdn}\n"
            f"\n"
            f"--- Интерпретация ---\n"
            f"Гипотеза A (гонка): upload XHR вернули 200 для ВСЕХ файлов,\n"
            f"  но img ещё на blob → финализация на сервере Авито не завершена.\n"
            f"Гипотеза B (антидубль): upload XHR = 200 для всех {n_files} файлов,\n"
            f"  но img на CDN меньше {n_files} → Авито молча отбросил похожие фото.\n",
            encoding="utf-8",
        )
        logger.info(
            "Диагностика фото: дамп в %s (%d событий сети)", dst_dir, len(all_events)
        )
    except Exception as exc:
        logger.warning("Диагностика фото: дамп не удался: %s", exc)


async def _wait_for_photo_previews(
    page: Any,
    expected: int,
    *,
    timeout_s: float,
    diagnostic_dir: Optional[pathlib.Path],
) -> tuple[str, int]:
    """Ждёт полный набор превью, повторно опрашивая все селекторы-кандидаты."""

    deadline = asyncio.get_event_loop().time() + timeout_s
    best_selector = psel.PHOTO_PREVIEW_CANDIDATES[0]
    best_count = 0
    iteration = 0

    while True:
        for candidate in psel.PHOTO_PREVIEW_CANDIDATES:
            try:
                count = await page.locator(candidate).count()
            except Exception:
                count = 0
            if count > best_count:
                best_selector = candidate
                best_count = count
                logger.info(
                    "Превью фото: найдено %d/%d по селектору %r",
                    count,
                    expected,
                    candidate,
                )
            if count >= expected:
                return candidate, count

        now = asyncio.get_event_loop().time()
        if now >= deadline:
            break

        iteration += 1
        if diagnostic_dir is not None and iteration % 2 == 0:
            await _diag_snap_previews(
                page,
                f"waiting_{best_count}of{expected}",
                diagnostic_dir,
            )
        await asyncio.sleep(min(1.5, max(0.0, deadline - now)))

    raise StepError(
        "Загрузка фотографий не подтверждена: за "
        f"{timeout_s:.0f} с появилось {best_count}/{expected} превью. "
        f"Лучший селектор: {best_selector!r}. Объявление не отправлено."
    )


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
        logger.info("Диагностика фото: папка %s, зондирование %s",
                    _d_dir, "активно" if _d_probe else "недоступно (mock)")
    except Exception as exc:
        logger.warning("Диагностика фото: инициализация не удалась: %s", exc)
    # ──────────────────────────────────────────────────────────────────────

    diagnostic_label = "failed"
    try:
        try:
            await page.set_input_files(
                psel.PHOTO_INPUT,
                list(photo_paths),
                timeout=int(PHOTO_UPLOAD_TIMEOUT_S * 1000),
            )
        except Exception as exc:
            logger.error("SELECTOR_MISS: input фото %s: %s", psel.PHOTO_INPUT, exc)
            raise StepError(
                f"Не найден input загрузки фото: {psel.PHOTO_INPUT}"
            ) from exc

        # ── Диагностика: скриншот + снапшот сразу после set_input_files ───
        if _d_dir is not None:
            try:
                if hasattr(page, "screenshot"):
                    await page.screenshot(
                        path=str(_d_dir / "screenshot_after_set_files.png")
                    )
            except Exception as exc_ss:
                logger.warning(
                    "Диагностика фото: скриншот after_set_files: %s", exc_ss
                )
            await _diag_snap_previews(page, "after_set_files", _d_dir)

        expected = len(photo_paths)
        logger.info("Отдано на загрузку %d фото, ждём превью…", expected)
        working_selector, _ = await _wait_for_photo_previews(
            page,
            expected,
            timeout_s=PHOTO_UPLOAD_TIMEOUT_S,
            diagnostic_dir=_d_dir,
        )
        logger.info(
            "Все %d фото загружены (превью по селектору %r)",
            expected,
            working_selector,
        )
        # Небольшой довесок — дать серверной загрузке финализироваться.
        await asyncio.sleep(2.0)
        diagnostic_label = "confirmed"
    finally:
        if _d_dir is not None:
            await _diag_snap_previews(page, diagnostic_label, _d_dir)
        if _d_dir is not None and _d_probe is not None:
            await _dump_photo_diag(page, _d_probe, diagnostic_label, _d_dir)


def _norm_suggest(text: str) -> str:
    """Нормализует текст пункта подсказки для сравнения: схлопывает пробелы + casefold."""
    return " ".join(text.split()).casefold().replace("ё", "е")


def _address_house_number(text: str) -> Optional[str]:
    """Извлекает дом после последнего сегмента типа улицы, игнорируя подъезд/квартиру."""
    segments = [
        segment.strip()
        for segment in re.split(r"[,\n]+", _norm_suggest(text))
        if segment.strip()
    ]
    address_type_words = {
        _norm_suggest(marker).rstrip(".") for marker in _ADDRESS_TYPE_WORDS
    }
    road_indexes = [
        index
        for index, segment in enumerate(segments)
        if address_type_words
        & set(re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", segment))
    ]
    if not road_indexes:
        return None

    road_index = road_indexes[-1]
    trailing = re.search(r"(?<!\w)(\d+[\w/-]*)\s*$", segments[road_index])
    if trailing:
        return trailing.group(1)
    for segment in segments[road_index + 1:]:
        leading = re.match(r"(\d+[\w/-]*)", segment)
        if leading:
            return leading.group(1)
    return None


def _house_numbers_match(requested: str, applied: str) -> bool:
    if requested == applied:
        return True
    suffix = applied[len(requested):] if applied.startswith(requested) else ""
    return bool(suffix) and suffix[0].isalpha()


def _suggest_contains(needle: str, text: str) -> bool:
    """Сопоставляет гео-подсказку; другой дом запрещён, улица без дома допустима."""
    wanted = set(re.findall(r"\w+", _norm_suggest(needle)))
    actual = set(re.findall(r"\w+", _norm_suggest(text)))
    if not wanted:
        return False
    wanted_house = _address_house_number(needle)
    actual_house = _address_house_number(text)
    if wanted_house:
        wanted.discard(wanted_house)
    if actual_house:
        actual.discard(actual_house)
    return (
        wanted <= actual
        and not (
            wanted_house
            and actual_house
            and not _house_numbers_match(wanted_house, actual_house)
        )
    )


def _address_matches_requested(requested: str, applied: str) -> bool:
    """Проверяет город, улицу и номер дома с допустимым каноническим суффиксом.

    Дом в запросе не обязателен: если пользователь указал адрес только до
    улицы, применённый Авито дом (любой) считаем допустимым — сверяем лишь
    город и улицу.
    """
    if not _suggest_contains(requested, applied):
        return False
    requested_house = _address_house_number(requested)
    if not requested_house:
        return True
    applied_house = _address_house_number(applied)
    return bool(
        applied_house and _house_numbers_match(requested_house, applied_house)
    )


def _brand_text_for_avito(brand: str) -> str:
    """Поднимает первую букву каждого слова бренда, не ломая его остальное написание."""
    normalized = " ".join(brand.split())
    result: list[str] = []
    capitalize_next = True
    for char in normalized:
        if capitalize_next and char.isalpha():
            result.append(char.upper())
            capitalize_next = False
        else:
            result.append(char)
            if char.isalnum():
                capitalize_next = False
        if char.isspace() or char in "&/-":
            capitalize_next = True
    return "".join(result)


async def _click_suggest_option(
    page: Any,
    option_selector: str,
    prefer_text: Optional[str] = None,
    timeout_ms: int = GEO_SUGGEST_TIMEOUT_MS,
) -> Optional[str]:
    """
    Ждёт пункты автокомплита по option_selector и кликает подходящий.

    prefer_text задан → ждёт пункт со всеми заданными словами без
    учёта регистра и порядка. Пока видны только старые подсказки другого
    города, ничего не нажимает. Без prefer_text кликает первый видимый
    пункт. Возвращает текст кликнутого пункта или None.

    Зачем (живая разведка 2026-06-28): и бренд, и адрес — автокомплиты, значение
    «прилипает» только при КЛИКЕ пункта. Закрытие списка через Escape очищает поле
    бренда, а клик по контейнеру гео-саджеста адрес не выбирает.
    """
    if not await _selector_visible(page, option_selector, timeout_ms):
        return None
    want = _norm_suggest(prefer_text or "")
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while True:
        options = page.locator(option_selector)
        try:
            count = await options.count()
        except Exception:
            count = 0

        visible_options: list[tuple[Any, str]] = []
        for i in range(count):
            option = options.nth(i)
            try:
                if not await option.is_visible():
                    continue
                text = (await option.inner_text()).strip()
            except Exception:
                continue
            visible_options.append((option, text))

        chosen: Optional[Any] = None
        chosen_text = ""
        if want:
            for option, text in visible_options:
                if _suggest_contains(want, text):
                    chosen = option
                    chosen_text = text
                    break
        elif visible_options:
            chosen, chosen_text = visible_options[0]

        if chosen is not None:
            await chosen.click(timeout=5_000)
            return chosen_text or "(пункт без текста)"

        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.2)


async def _select_brand(
    page: Any,
    profile: CategoryProfile,
    brand: str,
) -> str:
    """
    Вводит бренд с заглавных букв и обязательно выбирает видимую подсказку.

    Пустой или явный «Без бренда» → штатный пункт справочник Авито (разведка
    16.08.2026: пункт есть в списке кроссовок, форма сама подсказывает
    «Выберите "Без бренда", если марка не указана»). Просто оставить поле
    пустым нельзя — Авито не пропускает форму дальше.
    """
    requested_brand = _norm_label(brand)
    use_no_brand = (
        not requested_brand
        or requested_brand.casefold() == NO_BRAND_LABEL.casefold()
    )
    brand_text = NO_BRAND_LABEL if use_no_brand else _brand_text_for_avito(brand)

    await _clear_and_type(page, profile.brand_input, brand_text)

    # В текущей форме (2026-08-14) видимый пункт автокомплита больше не имеет
    # прежнего data-marker params[...]/option. Как и для комбобоксов, выбираем
    # реально видимый элемент по точному тексту, иначе введённая строка не
    # считается выбранным брендом и Авито не пропускает форму дальше.
    if not await _click_visible_text(page, brand_text, timeout_s=6.0):
        if brand_text != NO_BRAND_LABEL:
            raise StepError(
                f"Бренд «{brand_text}» отсутствует в подсказках Авито. "
                "Публикация остановлена до загрузки фото."
            )
        raise StepError(
            f"Не удалось выбрать «{NO_BRAND_LABEL}» в подсказках Авито "
            "для этой категории. Публикация остановлена до загрузки фото."
        )

    logger.info("Бренд: выбран видимый пункт подсказки %r", brand_text)
    return brand_text


async def _step_fill_fields(
    page: Any, data: DraftData, profile: CategoryProfile
) -> str:
    """Шаг fill_fields: вид объявления, [вид товара], состояние, размер, бренд,
    цвет, [материал], [стиль]."""
    # Вид объявления — комбобокс
    await _select_combobox(
        page, profile.trade_type_prefix, data.trade_type,
        profile.trade_type_options[data.trade_type],
    )
    await _pause()

    # Вид товара (футболка/поло/худи/…) — только у категорий, где поле есть
    # (см. CategoryProfile.has_item_type). У пиджаков и кроссовок пропускается.
    # Заполняется рано: на Авито подтип может менять состав остальных полей.
    if profile.has_item_type:
        await _select_combobox(
            page, profile.item_type_prefix, data.item_type,
            profile.item_type_options[data.item_type],
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

    # Бренд — автокомплит: одной введённой строки недостаточно, пункт должен
    # быть явно выбран. Старый params[...]/option в текущем DOM отсутствует.
    selected_brand = await _select_brand(page, profile, data.brand)
    await _pause()

    # Цвет — комбобокс
    await _select_combobox(
        page, profile.color_prefix, data.color, profile.color_options[data.color]
    )

    # Материал основной части — мультикомбобокс, есть не у всех категорий
    # (см. CategoryProfile.has_material). Выбираем ровно одно значение.
    if profile.has_material:
        await _pause()
        await _select_multi_combobox(
            page, profile.material_prefix, data.material,
            profile.material_options[data.material],
        )

    # Стиль — радио, как «Состояние»; есть не у всех категорий
    if profile.has_style:
        await _pause()
        style_sel = profile.style_radio(profile.style_options[data.style])
        try:
            await page.click(style_sel, timeout=WAIT_SELECTOR_TIMEOUT_MS)
            logger.info("Стиль: выбрано %r", data.style)
        except Exception as exc:
            logger.error("SELECTOR_MISS: радио стиля %s: %s", style_sel, exc)
            raise StepError(f"Радио стиля не найдено: {style_sel}") from exc

    return selected_brand


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


async def _step_fill_address(
    page: Any,
    full_address: str,
) -> Optional[dict[str, str]]:
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
            if not _address_matches_requested(full_address, address_val):
                logger.warning(
                    "Авито подменил адрес: запрошено %r, применено %r — повторяю выбор",
                    full_address,
                    address_val,
                )
                return False
            return True
        return False

    for attempt in range(1, GEO_RETRIES + 1):
        logger.info("Адрес, попытка %d/%d: %r", attempt, GEO_RETRIES, full_address)
        try:
            await geo_input.click(
                timeout=WAIT_SELECTOR_TIMEOUT_MS,
                no_wait_after=True,
            )
        except Exception as exc:
            logger.warning(
                "Не удалось кликнуть по гео-полю (возможна гонка с навигацией), "
                "попытка %d/%d: %s",
                attempt,
                GEO_RETRIES,
                exc,
            )
            if attempt == GEO_RETRIES:
                raise StepError(
                    "Не удалось кликнуть по гео-полю после "
                    f"{GEO_RETRIES} попыток (возможна гонка с навигацией)"
                ) from exc
            await asyncio.sleep(1.0)
            continue

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
        # это не выбирало адрес, и геолокация терялась перед публикацией.
        chosen_addr = await _click_suggest_option(
            page,
            psel.GEO_SUGGEST_OPTION,
            prefer_text=full_address,
            timeout_ms=5_000,
        )
        if chosen_addr is None:
            logger.warning("Гео: пункты-адреса (custom-option) не найдены (попытка %d)", attempt)
            continue
        logger.info("Гео: выбран адрес %r", chosen_addr)

        # Контроль: hidden address и locationId непустые (даём React время)
        if await _wait_until(_address_applied, attempts=10, interval_s=0.5):
            return None
        logger.warning("hidden address/locationId не заполнились (попытка %d)", attempt)

    # Все ретраи исчерпаны — стоп с инструкцией (дамп снимет обработчик)
    raise UserActionRequired(
        "Не удалось выбрать адрес через подсказку Авито (гео-саджест не сработал). "
        "Проверь адрес в форме сервиса и запусти публикацию заново."
    )


def _page_path(page: Any) -> str:
    """Текущий URL path страницы без query; при ошибке возвращает пустую строку."""
    try:
        return urlsplit(str(getattr(page, "url", "") or "")).path.rstrip("/") or "/"
    except ValueError:
        return ""


def _normalized_visible_value(value: str) -> str:
    """Нормализация коротких DOM-значений для точного смыслового сравнения."""
    return " ".join(str(value or "").replace("\xa0", " ").split()).casefold()


async def _resolve_cpxpromo_page(page: Any, item_id: str) -> Any:
    """Ждёт новую вкладку цены и не доверяет мигающему URL закрываемой формы."""
    deadline = time.monotonic() + 2.0
    while True:
        context = getattr(page, "context", None)
        pages = list(getattr(context, "pages", []) or []) if context is not None else []
        for candidate in reversed(pages):
            if candidate is page or _page_is_closed(candidate):
                continue
            if parse_cpxpromo_item_id(getattr(candidate, "url", "")) == item_id:
                logger.info(
                    "Авито перенёс настройку цены в новую вкладку; продолжаю в ней"
                )
                return candidate
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.1)
    if not _page_is_closed(page) and parse_cpxpromo_item_id(
        getattr(page, "url", "")
    ) == item_id:
        return page
    raise StepError("Рабочая вкладка стоимости просмотра не найдена")


async def _step_continue_listing(page: Any) -> tuple[str, Any]:
    """Один раз переводит заполненную форму на экран стоимости просмотра."""
    if _page_path(page) != "/additem":
        raise StepError("Кнопка публикации разрешена только на форме /additem")
    if not await _selector_now_visible(page, psel.SAVE_AND_EXIT_BUTTON):
        raise StepError(
            "Заполненная форма не подтверждена: кнопка «Сохранить и выйти» не видна"
        )

    title = page.locator(psel.TITLE_INPUT).first
    try:
        if not await title.is_visible() or not (await title.input_value()).strip():
            raise StepError("Заполненная форма не подтверждена: название пусто")
    except StepError:
        raise
    except Exception as exc:
        raise StepError("Не удалось проверить название на заполненной форме") from exc

    button = page.locator(psel.FORM_CONTINUE_BUTTON).first
    try:
        if not await button.is_visible():
            raise StepError("Кнопка «Продолжить» на заполненной форме не видна")
        button_text = _norm_label(await button.inner_text())
        # Авито добавляет внутрь подписи служебный
        # <span elementtiming="sx.additem.forward-button">timing</span>.
        # Разрешаем только два точных подтверждённых DOM-варианта: произвольный
        # суффикс у финансово значимой кнопки по-прежнему блокирует клик.
        if button_text not in {"Продолжить", "Продолжить timing"}:
            raise StepError(
                f"Финальная кнопка формы имеет неизвестное название: {button_text!r}"
            )
        if not await button.is_enabled():
            raise StepError("Кнопка «Продолжить» на заполненной форме недоступна")
        await button.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except StepError:
        raise
    except Exception as exc:
        raise StepError("Не удалось нажать «Продолжить» на заполненной форме") from exc

    reached = await _wait_until(
        lambda: _async_bool(parse_cpxpromo_item_id(getattr(page, "url", ""))),
        timeout_s=PUBLISH_TRANSITION_TIMEOUT_S,
        interval_s=0.5,
    )
    item_id = parse_cpxpromo_item_id(getattr(page, "url", ""))
    if not reached or not item_id:
        raise StepError(
            "После «Продолжить» не подтверждён экран стоимости просмотра"
        )
    return item_id, await _resolve_cpxpromo_page(page, item_id)


async def _async_bool(value: Any) -> bool:
    """Маленький адаптер для чистой проверки внутри `_wait_until`."""
    return bool(value)


async def _slider_is_at(slider: Any, expected_index: int) -> bool:
    return await slider.get_attribute("aria-valuenow") == str(expected_index)


async def _move_view_price_slider(
    slider: Any,
    key: str,
    expected_index: int,
) -> None:
    await slider.press(key)
    reached = await _wait_until(
        lambda: _slider_is_at(slider, expected_index),
        timeout_s=2.0,
        interval_s=0.05,
    )
    if not reached:
        raise StepError(
            f"Slider стоимости просмотра не перешёл в позицию {expected_index}"
        )


async def _price_input_differs(price_input: Any, previous: Decimal) -> bool:
    current = parse_ruble_decimal(await price_input.input_value())
    return current is not None and current != previous


async def _price_input_is_readable(price_input: Any) -> bool:
    """True, когда React уже отрисовал в поле разбираемую сумму."""
    return parse_ruble_decimal(await price_input.input_value()) is not None


async def _wait_view_price_change(price_input: Any, previous: Decimal) -> None:
    changed = await _wait_until(
        lambda: _price_input_differs(price_input, previous),
        timeout_s=2.0,
        interval_s=0.05,
    )
    if not changed:
        raise StepError("Slider изменил позицию, но стоимость просмотра не обновилась")


async def _select_minimum_view_price(
    page: Any,
    price_input: Any,
    view_price_max: Decimal,
    item_index: int,
    items_total: int,
) -> str:
    """Ставит slider в минимальную позицию и принимает цену Авито;
    выше минимума не поднимаемся никогда."""
    slider = page.locator(psel.VIEW_PRICE_SLIDER).first
    if not await slider.is_visible() or not await slider.is_enabled():
        raise StepError("Slider стоимости просмотра недоступен")

    try:
        slider_min = int(await slider.get_attribute("aria-valuemin") or "")
        slider_max = int(await slider.get_attribute("aria-valuemax") or "")
    except (TypeError, ValueError) as exc:
        raise StepError("Не удалось прочитать диапазон slider стоимости просмотра") from exc
    if slider_min != 0 or slider_max < slider_min:
        raise StepError("Авито вернул неизвестный диапазон slider стоимости просмотра")

    # Исходную цену читаем ТОЛЬКО после того, как React её отрисовал. Пустое
    # (нечитаемое) поле лишает нас точки отсчёта: тогда ожидание изменения
    # пропускается, и первый же read после Home может вернуть догидратированное
    # СТАРОЕ значение прежней позиции — цену ВЫШЕ минимума, — а мы примем его
    # за минимум и заплатим больше нужного.
    await _wait_until(
        lambda: _price_input_is_readable(price_input),
        timeout_s=2.0,
        interval_s=0.05,
    )
    initial_text = await price_input.input_value()
    initial_price = parse_ruble_decimal(initial_text)
    try:
        initial_index = int(await slider.get_attribute("aria-valuenow") or "")
    except (TypeError, ValueError) as exc:
        raise StepError("Не удалось прочитать позицию slider стоимости просмотра") from exc
    if not slider_min <= initial_index <= slider_max:
        raise StepError("Авито вернул неизвестную позицию slider стоимости просмотра")

    await _move_view_price_slider(slider, "Home", slider_min)
    if initial_index != slider_min:
        if initial_price is None:
            # Slider пришлось двигать, а исходной цены мы так и не увидели:
            # проверить, что поле догнало минимум, нечем. Стоп до денег.
            raise StepError(
                "Стоимость просмотра не отрисовалась до перевода slider "
                f"в минимальную позицию: {initial_text!r}"
            )
        await _wait_view_price_change(price_input, initial_price)
    current_text = await price_input.input_value()
    current = parse_ruble_decimal(current_text)
    if current is None:
        raise StepError(
            f"Не удалось прочитать минимальную стоимость просмотра: {current_text!r}"
        )

    if current > view_price_max:
        message = (
            f"Для объявления №{item_index} минимум Авито "
            f"{format(current, 'f')} ₽ превышает потолок "
            f"{format(view_price_max, 'f')} ₽"
        )
        raise UserActionRequired(
            message,
            user_action={
                "type": "view_price_cap_exceeded",
                "resumable": False,
                "item_index": item_index,
                "items_total": items_total,
                "maximum_view_price": format(view_price_max, "f"),
                "minimum_view_price": format(current, "f"),
                "message": message,
            },
        )

    logger.info(
        "Объявление №%d: беру минимальную стоимость просмотра %s ₽ (потолок %s ₽)",
        item_index, format(current, "f"), format(view_price_max, "f"),
    )
    return current_text


async def _dismiss_view_price_onboarding(page: Any) -> bool:
    """Закрывает чёрную onboarding-подсказку на экране цены.

    Клик разрешён только по точному data-marker подсказки и SVG close,
    чтобы не задеть финансовые кнопки экрана.
    """
    tooltip = page.locator(psel.VIEW_PRICE_ONBOARDING_TOOLTIP).first
    try:
        if not await tooltip.is_visible():
            return False
    except Exception as exc:
        raise StepError(
            "Не удалось проверить onboarding-подсказку на экране цены"
        ) from exc

    close_button = page.locator(psel.VIEW_PRICE_ONBOARDING_CLOSE).first
    try:
        if not await close_button.is_visible():
            raise StepError(
                "На onboarding-подсказке стоимости не найдена кнопка закрытия"
            )
        await close_button.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)

        async def tooltip_is_hidden() -> bool:
            return not await tooltip.is_visible()

        if not await _wait_until(
            tooltip_is_hidden,
            timeout_s=3.0,
            interval_s=0.1,
        ):
            raise StepError(
                "Onboarding-подсказка стоимости не закрылась"
            )
    except StepError:
        raise
    except Exception as exc:
        raise StepError(
            "Не удалось закрыть onboarding-подсказку стоимости"
        ) from exc

    logger.info("Onboarding-подсказка на экране стоимости закрыта")
    return True


async def _ensure_view_price_screen_rendered(page: Any) -> None:
    """Один раз перезагружает пустую React-оболочку /cpxpromo без финансовых действий."""
    if await _selector_visible(page, psel.VIEW_PRICE_CITY_INPUT, 5_000):
        return
    logger.warning("Экран стоимости не отрисовался — перезагружаю один раз")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        raise StepError("Не удалось перезагрузить пустой экран стоимости") from exc
    if not await _selector_visible(page, psel.VIEW_PRICE_CITY_INPUT, 10_000):
        raise StepError("Экран стоимости остался пустым после перезагрузки")


async def _step_fill_view_price(
    page: Any,
    location: LocationData,
    item_index: int,
    items_total: int,
    *,
    view_price_max: Decimal,
) -> Decimal:
    """Вводит минимальную стоимость Авито и дословно проверяет её, ничего
    финансового не подтверждая."""
    if not parse_cpxpromo_item_id(getattr(page, "url", "")):
        raise StepError("Стоимость просмотра разрешено вводить только на /cpxpromo/{item_id}")

    await _ensure_view_price_screen_rendered(page)
    await _dismiss_view_price_onboarding(page)

    city_input = page.locator(psel.VIEW_PRICE_CITY_INPUT).first
    try:
        shown_city = await city_input.input_value()
    except Exception as exc:
        raise StepError("Не удалось прочитать город показа объявления") from exc
    if _normalized_visible_value(shown_city) != _normalized_visible_value(location.city):
        raise StepError(
            f"Город показа Авито «{shown_city}» не совпадает с городом "
            f"объявления «{location.city}»"
        )

    manual_option = page.locator(psel.VIEW_PRICE_MANUAL_OPTION).first
    manual_radio = page.locator(psel.VIEW_PRICE_MANUAL_RADIO).first
    try:
        current_manual_visible = await manual_option.is_visible()
        if current_manual_visible:
            manual_confirmed = await manual_radio.is_checked()
        else:
            # Fallback только для старой A/B-версии Авито.
            mode = page.locator(psel.VIEW_PRICE_MODE_SWITCH).first
            manual_text = page.get_by_text("Вручную", exact=True).first
            manual_confirmed = (
                await manual_text.is_visible()
                and await mode.get_attribute("aria-checked") == "false"
            )
    except Exception as exc:
        raise StepError("Не удалось определить ручной режим стоимости просмотра") from exc
    if not manual_confirmed:
        raise StepError(
            "Не подтверждён ручной режим стоимости просмотра; автоматический режим запрещён"
        )

    price_input = page.locator(psel.VIEW_PRICE_INPUT).first
    try:
        if not await price_input.is_visible() or not await price_input.is_enabled():
            raise StepError("Поле стоимости просмотра недоступно для ручного ввода")
        actual_text = await _select_minimum_view_price(
            page,
            price_input,
            view_price_max,
            item_index,
            items_total,
        )
    except (StepError, UserActionRequired):
        raise
    except Exception as exc:
        raise StepError("Не удалось ввести стоимость просмотра") from exc

    actual = parse_ruble_decimal(actual_text)
    if actual is None:
        raise StepError(
            f"Не удалось прочитать применённую стоимость просмотра: {actual_text!r}"
        )
    # Подмена молча выше потолка после нашего выбора — стоп, не платим.
    if actual > view_price_max:
        raise StepError(
            f"Авито поднял стоимость просмотра выше потолка {format(view_price_max, 'f')}: "
            f"прочитано {actual_text!r}"
        )

    budget_input = page.locator(psel.VIEW_PRICE_DAILY_BUDGET_INPUT).first
    try:
        budget = await budget_input.input_value()
    except Exception as exc:
        raise StepError("Не удалось проверить дневной бюджет") from exc
    if str(budget or "").strip():
        raise StepError("Дневной бюджет уже заполнен; автоматизация не будет его менять")

    container = page.locator(psel.VIEW_PRICE_INPUT_CONTAINER).first
    invalid = await container.get_attribute("aria-invalid")
    if invalid == "true":
        minimum_locator = page.locator(psel.VIEW_PRICE_MINIMUM_TEXT).first
        try:
            minimum_text = await minimum_locator.inner_text()
        except Exception as exc:
            raise StepError("Авито отклонил стоимость, но minimum не удалось прочитать") from exc
        minimum = parse_ruble_decimal(minimum_text)
        if minimum is None:
            raise StepError(
                f"Авито отклонил стоимость, но minimum не распознан: {minimum_text!r}"
            )
        # Поле отмечено невалидным, хотя slider уже стоял на минимуме: значит
        # выставленное значение всё же ниже требуемого. Ставить цену «руками»
        # в обход slider нельзя — это финансовый ввод без обратной сверки,
        # поэтому останавливаемся и показываем требуемый минимум.
        if minimum > view_price_max:
            message = (
                f"Для объявления №{item_index} Авито требует минимум "
                f"{format(minimum, 'f')} ₽, а slider не дал этого значения"
            )
            raise UserActionRequired(
                message,
                user_action={
                    "type": "view_price_too_low",
                    "resumable": False,
                    "item_index": item_index,
                    "items_total": items_total,
                    "minimum_view_price": format(minimum, "f"),
                    "message": message,
                },
            )
        raise StepError(f"Авито не принял стоимость просмотра: {minimum_text}")
    if invalid not in {None, "false"}:
        raise StepError("Неизвестное состояние валидации стоимости просмотра")

    action = await _find_view_price_action(page)
    if action is None:
        raise StepError(
            "Не найдена кнопка «Продолжить с минимальной ценой» или «Сохранить»"
        )
    try:
        if await action.get_attribute("aria-disabled") == "true":
            raise StepError("Кнопка подтверждения стоимости просмотра недоступна")
        if not await action.is_enabled():
            raise StepError("Кнопка подтверждения стоимости просмотра выключена")
    except StepError:
        raise
    except Exception as exc:
        raise StepError(
            "Не удалось проверить кнопку подтверждения стоимости просмотра"
        ) from exc
    return actual


async def _find_view_price_action(
    page: Any,
    *,
    timeout_s: float = 5.0,
) -> Optional[Any]:
    """Находит видимую финансовую кнопку только по точному разрешённому тексту."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        buttons = page.locator(psel.VIEW_PRICE_ACTION_BUTTONS)
        try:
            count = await buttons.count()
        except Exception:
            count = 0
        for index in range(count):
            button = buttons.nth(index)
            try:
                if not await button.is_visible():
                    continue
                if _norm_label(await button.inner_text()) in VIEW_PRICE_ACTION_TEXTS:
                    return button
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return None


async def _step_continue_view_price(page: Any, item_id: str) -> None:
    """At-most-once подтверждает проверенную стоимость и ждёт экран услуг."""
    if parse_cpxpromo_item_id(getattr(page, "url", "")) != item_id:
        raise StepError("Экран стоимости не соответствует текущему объявлению")

    button = await _find_view_price_action(page)
    if button is None:
        raise StepError(
            "Не найдена кнопка «Продолжить с минимальной ценой» или «Сохранить»"
        )
    try:
        text = _norm_label(await button.inner_text())
        if text not in VIEW_PRICE_ACTION_TEXTS:
            raise StepError(f"Неизвестная кнопка подтверждения стоимости: {text!r}")
        if await button.get_attribute("aria-disabled") == "true":
            raise StepError("Кнопка подтверждения стоимости просмотра недоступна")
        if not await button.is_enabled():
            raise StepError("Кнопка подтверждения стоимости просмотра выключена")
        await button.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except StepError:
        raise
    except Exception as exc:
        raise StepError("Не удалось подтвердить стоимость просмотра") from exc

    reached = await _wait_until(
        lambda: _async_bool(_page_path(page) == "/pro/performance"),
        timeout_s=PUBLISH_TRANSITION_TIMEOUT_S,
        interval_s=0.5,
    )
    if not reached and _page_path(page) != "/pro/performance":
        raise StepError(
            "Результат подтверждения стоимости не подтверждён; повторный клик запрещён"
        )


def _service_widget_root(marker: str) -> Optional[str]:
    """Сворачивает marker вложенного узла услуги до `<prefix>/widget/<name>`."""
    parts = str(marker or "").split("/")
    try:
        widget_index = parts.index("widget")
    except ValueError:
        return None
    if len(parts) <= widget_index + 1:
        return None
    return "/".join(parts[: widget_index + 2])


async def _services_screen_ready(page: Any) -> bool:
    """True, когда React отрисовал полный ожидаемый экран выключенных услуг."""
    expected_roots = {
        marker.removesuffix("/switcher")
        for marker in ALLOWED_SERVICE_SWITCHES
    }
    try:
        widgets = page.locator(psel.SERVICES_ALL_WIDGETS)
        widget_roots: set[str] = set()
        for index in range(await widgets.count()):
            marker = await widgets.nth(index).get_attribute("data-marker")
            root = _service_widget_root(marker or "")
            if root:
                widget_roots.add(root)
        if not expected_roots.issubset(widget_roots):
            return False

        switches = page.locator(psel.SERVICES_ALL_SWITCHES)
        switch_markers = {
            str(await switches.nth(index).get_attribute("data-marker") or "")
            for index in range(await switches.count())
        }
        if not ALLOWED_SERVICE_SWITCHES.issubset(switch_markers):
            return False

        button = page.locator(psel.SERVICES_CONTINUE_WITHOUT_BUTTON).first
        return (
            await button.is_visible()
            and _norm_label(await button.inner_text()) == "Продолжить без услуг"
        )
    except Exception:
        return False


async def _step_skip_services(page: Any) -> None:
    """Проверяет полный allowlist выключенных услуг и кликает ровно один раз."""
    if _page_path(page) != "/pro/performance":
        raise StepError("Отказ от услуг разрешён только на /pro/performance")

    screen_ready = await _wait_until(
        lambda: _services_screen_ready(page),
        timeout_s=WAIT_SELECTOR_TIMEOUT_MS / 1000,
        interval_s=0.25,
    )
    if not screen_ready:
        logger.warning(
            "Экран услуг не отрисовался за %.0f с — перезагружаю один раз",
            WAIT_SELECTOR_TIMEOUT_MS / 1000,
        )
        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=WAIT_FORM_TIMEOUT_MS,
            )
        except Exception as exc:
            raise StepError("Не удалось перезагрузить пустой экран услуг") from exc
        await _wait_until(
            lambda: _services_screen_ready(page),
            timeout_s=PUBLISH_TRANSITION_TIMEOUT_S,
            interval_s=0.25,
        )

    widget_locator = page.locator(psel.SERVICES_ALL_WIDGETS)
    widget_roots: set[str] = set()
    for index in range(await widget_locator.count()):
        marker = await widget_locator.nth(index).get_attribute("data-marker")
        root = _service_widget_root(marker or "")
        if root:
            widget_roots.add(root)
    expected_roots = {
        marker.removesuffix("/switcher")
        for marker in ALLOWED_SERVICE_SWITCHES
    }
    unknown_roots = sorted(widget_roots - expected_roots)
    if unknown_roots:
        raise StepError(
            f"Обнаружена неизвестная платная услуга: {', '.join(unknown_roots)}"
        )
    missing_roots = sorted(expected_roots - widget_roots)
    if missing_roots:
        raise StepError(
            f"Не найдена ожидаемая платная услуга: {', '.join(missing_roots)}"
        )

    switches = page.locator(psel.SERVICES_ALL_SWITCHES)
    states: list[dict[str, Any]] = []
    for index in range(await switches.count()):
        switch = switches.nth(index)
        checkbox = switch.locator("input[type='checkbox']").first
        states.append({
            "marker": await switch.get_attribute("data-marker"),
            "aria_checked": await switch.get_attribute("aria-checked"),
            "checked": await checkbox.is_checked(),
        })
    service_error = validate_service_switch_states(states)
    if service_error:
        raise StepError(service_error)

    button = page.locator(psel.SERVICES_CONTINUE_WITHOUT_BUTTON).first
    try:
        if _norm_label(await button.inner_text()) != "Продолжить без услуг":
            raise StepError("Точная кнопка «Продолжить без услуг» не найдена")
        if not await button.is_enabled() or await button.get_attribute("aria-disabled") == "true":
            raise StepError("Кнопка «Продолжить без услуг» недоступна")
        await button.click(timeout=WAIT_SELECTOR_TIMEOUT_MS)
    except StepError:
        raise
    except Exception as exc:
        raise StepError("Не удалось продолжить без дополнительных услуг") from exc

    reached = await _wait_until(
        lambda: _async_bool(_page_path(page) == "/profile/pro/items"),
        timeout_s=PUBLISH_TRANSITION_TIMEOUT_S,
        interval_s=0.5,
    )
    if not reached and _page_path(page) != "/profile/pro/items":
        raise StepError(
            "Результат отказа от услуг не подтверждён; повторный клик запрещён"
        )


# ---------------------------------------------------------------------------
# Главная функция: прогон стейт-машины
# ---------------------------------------------------------------------------

async def _run_single_item(
    page: Any,
    job: dict[str, Any],
    data: DraftData,
    location: LocationData,
    item_index: int,
    items_total: int,
    profile: CategoryProfile,
    *,
    active_page_ref: Optional[list[Any]] = None,
    checkpoint_callback: Optional[Callable[[], None]] = None,
) -> tuple[str, str, Decimal, Optional[dict[str, str]]]:
    """
    Полный цикл от формы до передачи ОДНОГО объявления Авито.

    step/step_label/done в job описывают текущее объявление. profile —
    профиль категории (category_profiles): источник категорийных данных
    (путь/крошка/hidden-ID, словари полей, префиксы комбобоксов, бренд).
    Возвращает `(item_id, edit_url)` после перехода со страницы услуг в кабинет.
    Наличие карточки во вкладке «Активные» не проверяется. Исключения шагов
    обрабатывает run_publish_job.
    """
    # ── Шаг 2: open_form (заново для каждого объявления) ─────────────────
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
    await _guard_against_reopened_draft(page, data.title, item_index, items_total)
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

    # ── Шаг 9: fill_item_price ────────────────────────────────────────────
    _set_step(job, "fill_item_price")
    await _step_fill_price(page, data.price)
    await _pause()

    # ── Шаг 10: fill_address ──────────────────────────────────────────────
    _set_step(job, "fill_address")
    address_warning = await _step_fill_address(page, location.full_address())
    await _pause()

    # ── Шаг 11: continue_listing ──────────────────────────────────────────
    _set_step(job, "continue_listing")
    # Сначала фиксируем на диске начало неоднозначной финансовой зоны. Если
    # Chrome или сервер оборвётся после клика, resume не повторит объявление.
    if checkpoint_callback is not None:
        checkpoint_callback()
    item_id, page = await _step_continue_listing(page)
    if active_page_ref is not None:
        active_page_ref[:] = [page]

    # ── Шаг 12: fill_view_price ───────────────────────────────────────────
    _set_step(job, "fill_view_price")
    actual_view_price = await _step_fill_view_price(
        page,
        location,
        item_index,
        items_total,
        view_price_max=data.view_price_max,
    )

    # ── Шаг 13: continue_view_price ───────────────────────────────────────
    _set_step(job, "continue_view_price")
    await _step_continue_view_price(page, item_id)

    # ── Шаг 14: skip_services ─────────────────────────────────────────────
    _set_step(job, "skip_services")
    await _step_skip_services(page)

    # После отказа от услуг Авито уже вернул кабинет. По требованию пользователя
    # не проверяем вкладку «Активные»: она обновляется с задержкой и могла
    # останавливать весь пакет. Сохраняем стабильную ссылку редактирования по id.
    edit_url = f"https://www.avito.ru/items/edit/{item_id}"
    return item_id, edit_url, actual_view_price, address_warning


async def _run_publish_preflight(
    context: Any,
    data: DraftData,
    profile: CategoryProfile,
) -> str:
    """До фото и денег проверяет сеть, категорию и фактический бренд Авито."""
    preflight_page = await context.new_page()
    try:
        form_state = await _step_open_form(preflight_page, profile)
        await _step_select_category(preflight_page, form_state, profile)
        await _step_check_category(preflight_page, profile)
        selected_brand = await _step_fill_fields(preflight_page, data, profile)
        logger.info(
            "Предполётная проверка пройдена: категория=%s, бренд=%r",
            profile.key,
            selected_brand,
        )
        return selected_brand
    finally:
        try:
            await preflight_page.close()
        except Exception as exc:
            logger.debug("Не удалось закрыть предполётную вкладку: %s", exc)


def _format_partial_error(message: str, items_published: int, items_total: int) -> str:
    """
    Дополняет ошибку сводкой уже переданных Авито объявлений пакета.
    """
    if items_total <= 1 or "отправлено" in message.lower():
        return message
    return f"{message} Отправлено {items_published} из {items_total}."


def _sync_legacy_publish_aliases(job: dict[str, Any]) -> None:
    """Временно синхронизирует старые draft-поля из новых item-полей."""
    job["drafts_total"] = job["items_total"]
    job["draft_index"] = job["item_index"]
    job["drafts_saved"] = job["items_published"]
    job["saved_urls"] = list(job["published_urls"])


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
    start_index: Optional[int] = None,
) -> None:
    """
    Последовательно отправляет пакет объявлений Авито и обновляет job.

    Число объявлений читается из job["items_total"], а старое drafts_total
    остаётся fallback для совместимости. Chrome подключается один раз; каждое
    объявление проходит полный цикл до возврата в кабинет, после чего начинается
    следующее. Вкладка «Активные» не проверяется.

    Вариативность (ТЗ §17): если в job есть "prep_id" (кладёт app.py), на каждый
    черновик i подставляются title/description/фото варианта из
    tmp/publish/prep_{prep_id}/draft_{i:02d}/ (_load_prep_variant); без prep_id
    поведение прежнее — данные data одинаковы для всех черновиков.

    Аргументы:
        job_id:  идентификатор задачи
        job:     состояние задачи (status/step/step_label/done/total/error/...)
        data:    провалидированные данные объявления (общие поля пакета)
        cdp_url: адрес CDP Chrome пользователя (None → сразу failed)
        tmp_dir: каталог tmp/publish/{job_id} с фото; удаляется при done,
                 при failed/needs_user_action — остаётся для разбора.

    Никогда не бросает исключений наружу — итог только в job["status"].
    """
    # Ленивый импорт: playwright нужен только при реальном прогоне
    from playwright.async_api import async_playwright

    from browser import connect_over_cdp

    # Сколько объявлений: новое поле — источник правды, старое остаётся fallback.
    try:
        items_total = int(
            job.get("items_total")
            or job.get("drafts_total")
            or DRAFTS_DEFAULT
        )
    except (TypeError, ValueError):
        items_total = DRAFTS_DEFAULT
    items_total = max(DRAFTS_MIN, min(DRAFTS_MAX, items_total))

    # Профиль категории: из data.category; неизвестный/пустой ключ → JACKETS
    profile = category_profiles.get_profile(getattr(data, "category", None))
    logger.info("Категория задачи: %s (%s)", profile.key, profile.label)

    is_resume = start_index is not None
    if start_index is None:
        start_index = 1
    start_index = max(1, min(items_total, int(start_index)))

    job["status"] = "running"
    job["total"] = TOTAL_STEPS
    job["items_total"] = items_total
    if not is_resume:
        job["item_index"] = 0
        job["items_published"] = 0
        job["published_urls"] = []
        job["applied_view_prices"] = []
        job["address_warnings"] = []
        job["brand_selected"] = None
    else:
        job.setdefault("published_urls", [])
        job.setdefault("applied_view_prices", [])
        job.setdefault("address_warnings", [])
        job.setdefault("items_published", start_index - 1)
    job["user_action"] = None
    _sync_legacy_publish_aliases(job)
    page: Any = None
    active_page_ref: list[Any] = [None]

    def _save_checkpoint() -> None:
        if not tmp_dir:
            return
        try:
            publish_state.save_publish_state(
                pathlib.Path(tmp_dir),
                job_id,
                job,
                data,
            )
        except Exception as exc:
            logger.error("Checkpoint задачи %s не сохранён: %s", job_id, exc)
            raise StepError("Не удалось безопасно сохранить прогресс публикации") from exc

    try:
        _save_checkpoint()
        # Выставляем шаг ДО запуска Node-драйвера Playwright. Если дочерний
        # процесс не смог стартовать, пользователь увидит понятный этап вместо
        # misleading `unknown`, а дамп будет правильно назван.
        _set_step(job, "connect_chrome")
        async with async_playwright() as pw:
            # ── Шаг 1: connect_chrome (один раз на всю задачу) ────────────
            if not cdp_url:
                raise StepError(
                    "CDP-адрес не задан. Сначала запусти start-chrome.bat."
                )
            try:
                context = await connect_over_cdp(
                    pw,
                    cdp_url,
                    repair_unresponsive_avito_pages=True,
                    cleanup_stale_publish_pages=True,
                )
            except RuntimeError as exc:
                # browser.connect_over_cdp уже различает недоступный endpoint,
                # timeout инициализации и прочие ошибки протокола. Не затираем
                # эту диагностику неточным «Chrome не найден».
                raise StepError(str(exc)) from exc
            selected_brand = await _run_publish_preflight(context, data, profile)
            job["brand_selected"] = selected_brand
            _save_checkpoint()

            # Открываем СВОЮ рабочую вкладку; предполётная уже закрыта
            page = await context.new_page()
            await _pause()

            # ── Объявления 1..N: строго последовательная публикация ───────
            for item_index in range(start_index, items_total + 1):
                job["item_index"] = item_index
                _set_step(job, "open_form")
                _sync_legacy_publish_aliases(job)
                _save_checkpoint()

                if page is None or _page_is_closed(page):
                    page = await context.new_page()
                    logger.info(
                        "Объявление %d/%d: вкладка пересоздана после закрытия Авито",
                        item_index, items_total,
                    )

                if item_index > 1:
                    # Пауза 5–15 с между объявлениями (антибот, ТЗ §16)
                    pause_s = random.uniform(DRAFT_PAUSE_MIN_S, DRAFT_PAUSE_MAX_S)
                    logger.info(
                        "Пауза %.1f с перед объявлением %d/%d",
                        pause_s, item_index, items_total,
                    )
                    await asyncio.sleep(pause_s)

                logger.info("Объявление %d/%d: начато", item_index, items_total)

                # Вариативность (ТЗ §17): при наличии prep_id подставляем
                # title/description/фото варианта i из папки подготовки.
                # Без prep_id — прежнее поведение: data без изменений.
                data_i = data
                if job.get("prep_id"):
                    title_i, desc_i, photos_i = _load_prep_variant(
                        TMP_PUBLISH_DIR / f"prep_{job['prep_id']}", item_index
                    )
                    data_i = replace(
                        data,
                        title=title_i,
                        description=desc_i,
                        photo_paths=tuple(photos_i),
                    )
                    logger.info(
                        "Объявление %d/%d: взят вариант из подготовки (%d фото)",
                        item_index, items_total, len(photos_i),
                    )

                active_page_ref[:] = [page]
                (
                    item_id,
                    submitted_url,
                    actual_view_price,
                    address_warning,
                ) = await _run_single_item(
                    page,
                    job,
                    data_i,
                    data.location_for(item_index),
                    item_index,
                    items_total,
                    profile,
                    active_page_ref=active_page_ref,
                    checkpoint_callback=_save_checkpoint,
                )
                page = active_page_ref[0]
                # Имена полей оставлены для совместимости существующего API.
                job["items_published"] += 1
                job["published_urls"].append(submitted_url)
                job["result_url"] = submitted_url
                job["applied_view_prices"].append({
                    "item_index": item_index,
                    "price": format(actual_view_price, "f"),
                })
                if address_warning:
                    job["address_warnings"].append({
                        "item_index": item_index,
                        **address_warning,
                    })
                _sync_legacy_publish_aliases(job)
                # Это граница возобновления: только после подтверждённой отправки
                # объявления фиксируем, что следующий запуск может начать с i + 1.
                _save_checkpoint()
                logger.info(
                    "Объявление %d/%d отправлено Авито: item_id=%s, URL=%s",
                    item_index, items_total, item_id, submitted_url,
                )

                if item_index < items_total:
                    _set_step(job, "open_next_form")
                    # Следующий цикл сам открывает чистую /additem через общий
                    # безопасный сетевой retry. Второй прямой goto здесь создавал
                    # лишнюю гонку и обходил обработку временных ошибок.

            # ── Финальный done ────────────────────────────────────────────
            _set_step(job, "done")
            job["status"] = "done"
            job["done"] = TOTAL_STEPS
            _sync_legacy_publish_aliases(job)
            logger.info(
                "Задача %s: отправлено Авито %d/%d (последний URL: %s)",
                job_id, job["items_published"], items_total, job.get("result_url"),
            )

    except Exception as exc:  # любой сбой шага — сервер не падает, итог в job
        page = active_page_ref[0] or page
        current_step = str(job.get("step") or "unknown")
        if isinstance(exc, UserActionRequired):
            # Терминальный статус: нужна ручная помощь пользователя
            status = "needs_user_action"
            message = _format_partial_error(
                str(exc), job.get("items_published", 0), items_total
            )
            job["user_action"] = exc.user_action
            logger.warning("Задача %s, шаг %s: needs_user_action — %s",
                           job_id, current_step, message)
        elif isinstance(exc, StepError):
            status = "failed"
            job["user_action"] = None
            message = _format_partial_error(
                str(exc), job.get("items_published", 0), items_total
            )
            logger.error("Задача %s, шаг %s: failed — %s", job_id, current_step, message)
        elif (
            current_step == "connect_chrome"
            and "Connection closed while reading from the driver" in str(exc)
        ):
            status = "failed"
            job["user_action"] = None
            message = _format_partial_error(
                "Не удалось запустить драйвер Playwright: текущий сервер не имеет "
                "доступа к файлам драйвера. Остановите сервер и запустите "
                "`python backend/app.py` из обычного PowerShell в папке проекта, "
                "затем повторите публикацию объявлений.",
                job.get("items_published", 0),
                items_total,
            )
            logger.exception(
                "Задача %s, шаг %s: Node-драйвер Playwright завершился при старте",
                job_id,
                current_step,
            )
        else:  # непредвиденный сбой
            status = "failed"
            job["user_action"] = None
            logger.exception("Задача %s, шаг %s: непредвиденная ошибка: %s",
                             job_id, current_step, exc)
            message = _format_partial_error(
                f"Непредвиденная ошибка на шаге {current_step}: {exc}",
                job.get("items_published", 0), items_total,
            )
        # Путь дампа отдаём фронту в статусе (debug_dir = debug/publish/<job_id>)
        job["debug_dir"] = await _dump_failure(page, job_id, current_step, message)
        job["status"] = status
        job["error"] = message
        _sync_legacy_publish_aliases(job)
        try:
            _save_checkpoint()
        except StepError:
            logger.exception("Финальный checkpoint задачи %s не сохранён", job_id)

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
        "locations_json": json.dumps(
            [{"city": "Москва", "address": "ул. Арбат, 1"}],
            ensure_ascii=False,
        ),
        "view_price_max": "0,5",
    }
    _GOOD_PHOTO = ("suit.jpg", "image/jpeg", 1024 * 1024)
    # Профиль по умолчанию для базовых тестов валидации — «Пиджаки и костюмы»
    _PROFILE = category_profiles.JACKETS

    # ── Тест 1: валидная форма → ошибок нет ──────────────────────────────────
    errs = validate_publish_form(_GOOD_FIELDS, [_GOOD_PHOTO], _PROFILE)
    assert errs == [], f"Валидная форма не должна давать ошибок: {errs}"
    print("[OK] Тест 1: валидная форма проходит")

    # ── Тест 2: пустые название/описание/город/адрес ────────────────────────
    # Бренд намеренно исключён: пустой бренд — штатный режим «без бренда»
    # (Авито знает не все марки), проверяется отдельно в тесте 2б.
    empty = dict(
        _GOOD_FIELDS,
        title="",
        description="  ",
        brand="",
        locations_json='[{"city": "", "address": ""}]',
    )
    errs = validate_publish_form(empty, [_GOOD_PHOTO], _PROFILE)
    bad_fields = {e["field"] for e in errs}
    for f in (
        "title",
        "description",
        "locations.1.city",
        "locations.1.address",
    ):
        assert f in bad_fields, f"Нет ошибки для пустого поля {f}: {errs}"
    assert "brand" not in bad_fields, (
        f"Пустой бренд не должен быть ошибкой (режим «без бренда»): {errs}"
    )

    # ── Тест 2б: форма без бренда валидна целиком ───────────────────────────
    errs = validate_publish_form(dict(_GOOD_FIELDS, brand=""), [_GOOD_PHOTO], _PROFILE)
    assert errs == [], f"Форма без бренда должна проходить валидацию: {errs}"
    print("[OK] Тест 2б: пустой бренд допустим")
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
    assert draft.location_for(1).full_address() == "Москва, ул. Арбат, 1"
    assert draft.view_price_max == Decimal("0.5")
    assert draft.category == "jackets", draft.category  # дефолт категории
    assert draft.summary()["photos_count"] == 1
    assert draft.summary()["category"] == "jackets", draft.summary()
    assert "photo_paths" not in draft.summary(), "В сводке не должно быть путей к фото"
    # Явная категория переносится в DraftData
    draft_sn = build_draft_data(_GOOD_FIELDS, ["x.jpg"], category="sneakers")
    assert draft_sn.category == "sneakers", draft_sn.category
    # Вид товара: у категорий без поля пусто, значение из формы переносится
    assert draft.item_type == "", draft.item_type
    assert draft.summary()["item_type"] == ""
    draft_it = build_draft_data(
        {**_GOOD_FIELDS, "item_type": "Футболка"}, ["x.jpg"], category="jackets"
    )
    assert draft_it.item_type == "Футболка", draft_it.item_type
    # Материал/стиль: у категорий без полей пусто, значения из формы переносятся
    assert draft.material == "" and draft.style == ""
    assert draft.summary()["material"] == "" and draft.summary()["style"] == ""
    draft_ms = build_draft_data(
        {**_GOOD_FIELDS, "material": "Нейлон", "style": "Повседневный"},
        ["x.jpg"], category="jackets",
    )
    assert draft_ms.material == "Нейлон" and draft_ms.style == "Повседневный"
    print("[OK] Тест 7: build_draft_data / full_address / summary / category / item_type / material / style")

    # ── Тест 7b: валидация «Вида товара»/материала/стиля зависит от профиля ──
    import category_profiles as _cp
    # У пиджаков поля нет → лишнее значение игнорируется, ошибок не добавляет
    assert validate_publish_form(
        {**_GOOD_FIELDS, "item_type": "Чепуха"}, [_GOOD_PHOTO], _cp.JACKETS
    ) == [], "категория без item_type не должна валидировать это поле"
    assert validate_publish_form(
        {**_GOOD_FIELDS, "material": "Чепуха", "style": "Чепуха"}, [_GOOD_PHOTO], _cp.JACKETS
    ) == [], "категория без material/style не должна валидировать эти поля"
    # Синтетический профиль с полем: пустое и чужое значение — ошибка, своё — ок
    _p_it = replace(
        _cp.JACKETS, key="tshirts_test",
        item_type_options={"Футболка": 111, "Худи": 222}, item_type_prefix="vid_tovara",
    )
    assert _p_it.has_item_type
    _errs_empty = validate_publish_form(_GOOD_FIELDS, [_GOOD_PHOTO], _p_it)
    assert [e["field"] for e in _errs_empty] == ["item_type"], _errs_empty
    assert validate_publish_form(
        {**_GOOD_FIELDS, "item_type": "Худи"}, [_GOOD_PHOTO], _p_it
    ) == []
    print("[OK] Тест 7b: «Вид товара» обязателен только там, где поле есть")

    # ── Тест 7c: валидация материала/стиля зависит от профиля (как item_type) ─
    _p_ms = replace(
        _cp.JACKETS, key="vests_test",
        material_options={"Нейлон": 3263861}, material_prefix="material_osnovnoi_chasti",
        style_options={"Повседневный": 3360005}, style_param=192478,
    )
    assert _p_ms.has_material and _p_ms.has_style
    # Пустые material/style у профиля, где поля есть, → ошибки обоих полей
    _errs_ms_empty = validate_publish_form(_GOOD_FIELDS, [_GOOD_PHOTO], _p_ms)
    assert {e["field"] for e in _errs_ms_empty} >= {"material", "style"}, _errs_ms_empty
    # Корректные значения проходят без ошибок
    assert validate_publish_form(
        {**_GOOD_FIELDS, "material": "Нейлон", "style": "Повседневный"}, [_GOOD_PHOTO], _p_ms
    ) == []
    print("[OK] Тест 7c: материал/стиль обязательны только там, где поле есть")

    # ── Тест 8: стейт-машина полной публикации, метки по-русски ──────────────
    assert TOTAL_STEPS == 16, f"Шагов должно быть 16, есть {TOTAL_STEPS}"
    assert STEPS[0][0] == "connect_chrome" and STEPS[-1][0] == "done"
    assert STEP_LABELS["select_category"] == "Выбор категории"
    assert STEP_LABELS["upload_photos"] == "Загрузка фотографий"
    assert _STEP_INDEX["continue_listing"] == 11
    assert _STEP_INDEX["open_next_form"] == 15
    assert "verify_published" not in STEP_LABELS
    print("[OK] Тест 8: стейт-машина отправки без проверки «Активных» из 16 шагов")

    # ── Тест 9: drafts_count — валидация пакетного режима (ТЗ §16) ──────────
    # 0 и 21 — вне диапазона [1, 20] → ошибка поля drafts_count
    for bad_count in ("0", "21", "-1", "abc", "1.5"):
        errs = validate_publish_form(
            dict(_GOOD_FIELDS, drafts_count=bad_count), [_GOOD_PHOTO], _PROFILE
        )
        assert any(e["field"] == "drafts_count" for e in errs), (
            f"drafts_count={bad_count!r} должно быть отклонено: {errs}"
        )
    # «3» строкой → ок, парсится в 3
    three_locations = json.dumps([
        {"city": "Москва", "address": "ул. Арбат, 1"},
        {"city": "Одинцово", "address": "ул. Центральная, 7"},
        {"city": "Тула", "address": "ул. Ленина, 2"},
    ], ensure_ascii=False)
    errs = validate_publish_form(
        dict(_GOOD_FIELDS, drafts_count="3", locations_json=three_locations),
        [_GOOD_PHOTO],
        _PROFILE,
    )
    assert errs == [], f"drafts_count='3' должно проходить: {errs}"
    assert parse_drafts_count("3") == 3
    # Отсутствие поля → ок, дефолт 1 (обратная совместимость)
    errs = validate_publish_form(_GOOD_FIELDS, [_GOOD_PHOTO], _PROFILE)
    assert errs == [], f"Форма без drafts_count должна проходить: {errs}"
    assert parse_drafts_count(None) == 1
    assert parse_drafts_count("") == 1
    assert parse_drafts_count("  ") == 1
    # Граничные значения диапазона
    assert parse_drafts_count("1") == 1 and parse_drafts_count("20") == 20
    assert parse_drafts_count("0") is None and parse_drafts_count("21") is None
    print("[OK] Тест 9: drafts_count - 0/21 ошибка, '20' -> 20, отсутствие -> 1")

    # ── Тест 10: сводка частичного успеха в тексте ошибки ────────────────────
    # N=1 — текст не трогаем (одиночный режим как раньше)
    assert _format_partial_error("Ошибка X.", 0, 1) == "Ошибка X."
    # N>1 — дописываем число уже отправленных объявлений
    assert _format_partial_error("Ошибка X.", 2, 5) == "Ошибка X. Отправлено 2 из 5."
    # Сводка уже есть — не дублируем
    msg = "Не удалось отправить объявление. Отправлено 1 из 3."
    assert _format_partial_error(msg, 1, 3) == msg
    print("[OK] Тест 10: частичный успех дописывается в ошибку без дублей")

    # ── Тест 11: _load_prep_variant — чтение варианта из папки подготовки ────
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
    print("[OK] Тест 11: _load_prep_variant - чтение варианта, нет папки/фото -> StepError")

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
        "description": "d", "price": "100", "view_price_max": "0.5",
        "locations_json": '[{"city": "Москва", "address": "ул. 1"}]',
    }
    photos = [("a.jpg", "image/jpeg", 1000)]
    assert validate_publish_form(base, photos, cp.JACKETS) == []
    # тот же размер «48 (M)» не из словаря кроссовок → ошибка по полю size
    errs = validate_publish_form(base, photos, cp.SNEAKERS)
    assert any(e["field"] == "size" for e in errs), errs
    print("[OK] validate_publish_form учитывает профиль категории")

    print("\n=== Все самотесты publisher.py пройдены ===")
