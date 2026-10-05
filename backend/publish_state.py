"""Атомарное дисковое состояние задач полной публикации Авито."""

from __future__ import annotations

import json
import logging
import re
import shutil
# `os` здесь — точка наблюдения тестов: они патчат publish_state.os.fsync/replace,
# а это тот же модуль os, через который пишет atomic_write.
import os  # noqa: F401
from io import BufferedRandom
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from atomic_write import write_text_atomic


logger = logging.getLogger(__name__)

STATE_FILENAME = "publish-state.json"
STATE_VERSION = 1

# id объявления в стабильной ссылке редактирования, которую пишет publisher.
_EDIT_URL_ID = re.compile(r"/items/edit/(\d+)")

# До `continue_listing` Авито ещё не получил финансового подтверждения.
# Эти шаги можно безопасно начать заново с формы текущего объявления.
SAFE_PREFINANCIAL_STEPS = frozenset({
    "",
    "prepare_variants",
    "connect_chrome",
    "open_form",
    "select_category",
    "check_category",
    "fill_title",
    "upload_photos",
    "fill_fields",
    "fill_description",
    "fill_item_price",
    "fill_address",
    "open_next_form",
})

# Шаги, начиная с денежного клика «Продолжить» на заполненной форме.
FINANCIAL_STEPS = frozenset({
    "continue_listing",
    "fill_view_price",
    "continue_view_price",
    "skip_services",
    "done",
})

# Все шаги, которые код публикации реально пишет в job["step"]
# (publisher.STEPS + "prepare_variants" из app.py + "" у новой задачи).
# Шаг вне этого списка — checkpoint не распознан, плана нет (fail-closed).
KNOWN_STEPS = SAFE_PREFINANCIAL_STEPS | FINANCIAL_STEPS


class PublishLockBusyError(RuntimeError):
    """Другой живой процесс уже публикует пакет."""

    def __init__(self, pid: int | None, job_id: str | None) -> None:
        self.pid = pid
        self.job_id = job_id
        super().__init__(f"Публикация уже выполняется: PID {pid}, задача {job_id}")


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Нет процесса: 87. Недостаточно прав: 5, значит процесс жив.
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def acquire_publish_lock(root: Path, job_id: str) -> BufferedRandom:
    """Атомарно занимает Chrome для одного процесса до завершения фоновой задачи."""
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "publish.lock").open("a+b")
    try:
        if os.name == "nt" and handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"?")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            # Windows не даёт читать захваченный другим процессом первый байт.
            handle.seek(1 if os.name == "nt" else 0)
            try:
                raw = handle.read()
                owner = json.loads((b"{" + raw if os.name == "nt" else raw).decode("utf-8"))
            except (OSError, ValueError, UnicodeError):
                owner = {}
            raise PublishLockBusyError(owner.get("pid"), owner.get("job_id"))

        handle.seek(0)
        try:
            owner = json.loads(handle.read().decode("utf-8"))
        except (ValueError, UnicodeError):
            owner = {}
        pid = owner.get("pid")
        if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
            raise PublishLockBusyError(pid, owner.get("job_id"))
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "job_id": job_id}).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        return handle
    except BaseException:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()
        raise


def release_publish_lock(handle: BufferedRandom) -> None:
    """Освобождает lock, оставляя файл на месте во избежание unlink-race."""
    try:
        handle.seek(0)
        handle.truncate()
        if os.name == "nt":
            handle.write(b"?")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

# job["money_click"] — отметка денежного клика ТЕКУЩЕГО объявления
# (job["item_index"]). Сбрасывается в None в начале каждого объявления.
#   None          — неизвестно (клик мог уйти) либо до клика ещё не дошли;
#                   на continue_listing и дальше читается консервативно: skip.
#   "not_clicked" — доказано, что button.click() финальной кнопки НЕ вызывался
#                   (упали проверки до клика или предкликовая запись checkpoint).
#   "clicked"     — клик выполнен (финальная кнопка, либо промежуточная кнопка
#                   категории увела с /additem): объявление могло быть создано,
#                   повтор запрещён на любом шаге.
# Старый checkpoint без ключа читается как None.
MONEY_CLICK_VALUES = frozenset({None, "not_clicked", "clicked"})

# job["current_item_id"] / job["current_item_url"] — id и адрес экрана
# /cpxpromo/<id> объявления job["item_index"], которое Авито уже создал
# (записываются отдельным checkpoint сразу после возврата continue_listing).
# Сбрасываются в None в начале каждого объявления, как money_click.
#
# job["published_items"] — [{"item_index": 1, "item_id": "…"}] отправленных
# объявлений пакета (для сверки повторного id, F37). У старых checkpoint
# ключа нет: id отправленных тогда берутся из published_urls без номера.
#
# job["skipped_items"] — объявления, созданные (или, возможно, созданные) на
# Авито, но не доведённые автоматикой до конца. Их проверяет пользователь.
# Новый формат — записи (SKIP_RECORD_KEYS), неизвестное значение — None:
#   {"item_index": 2, "item_id": "7730002",
#    "item_url": "https://www.avito.ru/cpxpromo/7730002",
#    "step": "fill_view_price", "city": "Москва", "address": "улица …, 2"}
# Старый формат — список номеров [2, 5]; читается везде, смешанный тоже.
SKIP_RECORD_KEYS = ("item_index", "item_id", "item_url", "step", "city", "address")

# Причины, по которым плана возобновления нет (общий текст для /status и /resume).
_REASON_ALL_SENT = "Все объявления этой задачи уже отправлены"
_REASON_UNRECOGNIZED = (
    "Состояние checkpoint не распознано — автоматическое продолжение "
    "запрещено, чтобы не создать дубль. Проверьте кабинет Авито вручную."
)


def _format_numbers(numbers: Any) -> str:
    return ", ".join(f"№{number}" for number in sorted(numbers))


def _reason_all_closed(skipped: set[int]) -> str:
    return (
        "Продолжать нечего: все объявления пакета отправлены или пропущены. "
        f"Пропущенные ({_format_numbers(skipped)}) проверьте в кабинете Авито вручную."
    )


def _reason_last_created(number: int) -> str:
    return (
        f"Продолжать нечего: пропустить пришлось бы последнее объявление "
        f"пакета №{number}, а оно уже создано на Авито (или могло быть создано) — "
        "проверьте и завершите его в кабинете Авито вручную."
    )


# ---------------------------------------------------------------------------
# skipped_items: чтение старого и нового формата
# ---------------------------------------------------------------------------

def _skip_number(value: Any) -> int:
    """Номер объявления из записи skipped_items (число, строка-число, запись)."""
    if isinstance(value, dict):
        value = value.get("item_index")
    if isinstance(value, bool):
        raise ValueError("Номер пропущенного объявления не может быть bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    raise ValueError(f"Нераспознанная запись skipped_items: {value!r}")


def _raw_skipped(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("skipped_items должен быть списком")
    return list(raw)


def skipped_numbers(raw: Any) -> set[int]:
    """Номера пропущенных объявлений. Нераспознанная запись — ValueError."""
    return {_skip_number(value) for value in _raw_skipped(raw)}


def normalize_skipped_items(raw: Any) -> list[dict[str, Any]]:
    """Приводит skipped_items (старый, новый, смешанный) к записям по номеру.

    Одна запись на номер, по возрастанию. Если номер встречается дважды,
    остаётся запись с item_id (иначе — первая запись-словарь). Нераспознанная
    запись — ValueError: угадывать номер нельзя.
    """
    records: dict[int, dict[str, Any]] = {}
    for value in _raw_skipped(raw):
        number = _skip_number(value)
        record: dict[str, Any] = {key: None for key in SKIP_RECORD_KEYS}
        if isinstance(value, dict):
            for key in SKIP_RECORD_KEYS:
                if key in value:
                    record[key] = value[key]
        record["item_index"] = number
        existing = records.get(number)
        if (
            existing is None
            or (existing.get("item_id") is None and record.get("item_id") is not None)
            or (
                isinstance(value, dict)
                and all(existing.get(key) is None for key in SKIP_RECORD_KEYS[1:])
            )
        ):
            records[number] = record
    return [records[number] for number in sorted(records)]


def make_skip_record(
    job: dict[str, Any], item_index: int, draft: Any = None,
) -> dict[str, Any]:
    """Запись о пропуске объявления item_index по текущему состоянию job.

    item_id/URL/шаг берутся, только если job описывает именно это объявление
    (job["item_index"] == item_index): иначе они относятся к другому.
    Город и адрес — из геолокации варианта в draft (если draft есть).
    """
    record: dict[str, Any] = {key: None for key in SKIP_RECORD_KEYS}
    record["item_index"] = item_index
    try:
        same_item = int(job.get("item_index") or 0) == item_index
    except (TypeError, ValueError):
        same_item = False
    if same_item:
        record["item_id"] = job.get("current_item_id")
        record["item_url"] = job.get("current_item_url")
        record["step"] = job.get("step")
    if draft is not None:
        try:
            location = draft.location_for(item_index)
            record["city"] = location.city
            record["address"] = location.address
        except Exception:  # noqa: BLE001 — нет геолокации: адрес неизвестен
            pass
    return record


def add_skipped_item(job: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    """Добавляет (или уточняет) запись о пропуске; skipped_items — записями."""
    records = normalize_skipped_items(job.get("skipped_items"))
    number = record["item_index"]
    kept = [r for r in records if r["item_index"] != number]
    previous = next((r for r in records if r["item_index"] == number), None)
    merged = dict(record)
    if previous is not None:
        # Уже известное (например, item_id прошлой записи) не теряем.
        for key in SKIP_RECORD_KEYS:
            if merged.get(key) is None and previous.get(key) is not None:
                merged[key] = previous[key]
    kept.append(merged)
    job["skipped_items"] = sorted(kept, key=lambda r: r["item_index"])
    return job["skipped_items"]


def record_unresumable_last_item(job: dict[str, Any], draft: Any = None) -> Optional[int]:
    """S2: последнее объявление пакета создано (или могло быть), продолжать нечем.

    resume для него плана не даёт (пропускать его некуда), поэтому номер и
    следы (item_id, шаг, адрес) сразу записываются в job["skipped_items"],
    иначе пропуск последнего был бы невидим. Возвращает номер или None.
    Вызывать только для остановленной задачи (не running/done).
    """
    try:
        number = _resume_decision(job)[2]
    except Exception:  # noqa: BLE001 — нераспознанный checkpoint: не трогаем
        return None
    if number is None:
        return None
    add_skipped_item(job, make_skip_record(job, number, draft))
    return number


def known_item_ids(job: dict[str, Any]) -> dict[str, Optional[int]]:
    """id объявлений пакета, уже созданных на Авито: id -> номер (или None).

    Отправленные — из published_items (старый checkpoint: из published_urls
    /items/edit/<id>, номер неизвестен); пропущенные — из записей skipped_items.
    """
    known: dict[str, Optional[int]] = {}
    for url in job.get("published_urls") or []:
        match = _EDIT_URL_ID.search(str(url or ""))
        if match:
            known.setdefault(match.group(1), None)
    for entry in job.get("published_items") or []:
        if isinstance(entry, dict) and entry.get("item_id"):
            try:
                known[str(entry["item_id"])] = _skip_number(entry)
            except ValueError:
                known.setdefault(str(entry["item_id"]), None)
    try:
        skipped = normalize_skipped_items(job.get("skipped_items"))
    except ValueError:
        skipped = []
    for record in skipped:
        if record.get("item_id"):
            known.setdefault(str(record["item_id"]), record["item_index"])
    return known


def is_publish_resume_safe(job: dict[str, Any]) -> bool:
    """Можно ли повторить текущее объявление, не рискуя дублем и второй оплатой.

    Единственный источник правды — resume_plan: повтор разрешён ровно тогда,
    когда он выбирает режим retry_item.
    """
    plan = resume_plan(job)
    return plan is not None and plan["mode"] == "retry_item"


def resume_plan(job: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Решает, МОЖНО ли и КАК продолжить пакет после терминальной остановки.

    Есть два непохожих режима, и подмена одного другим стоит денег:

    - `retry_item` — остановка произошла ДО того, как Авито получил
      финансовое подтверждение по текущему объявлению. Тогда безопасно начать
      текущее объявление заново с чистой формы: ничего платного ещё не
      случилось, повтор ничего не задваивает.
    - `skip_item` — денежный клик по текущему объявлению был или мог быть:
      объявление, возможно, уже создано на Авито. Автоматике запрещено
      прикасаться к нему повторно — повторный клик может создать дубль или
      списать деньги второй раз. Поэтому объявление пропускается: пакет
      продолжается со СЛЕДУЮЩЕГО индекса, а номер пропущенного возвращается
      наружу, чтобы пользователь вручную проверил и завершил его сам.

    Таблица для объявления в работе (item_index == pending):

        money_click == "clicked"                     -> skip_item (любой шаг)
        безопасный шаг (SAFE_PREFINANCIAL_STEPS)     -> retry_item
        continue_listing + "not_clicked"             -> retry_item
        continue_listing и дальше + None/нет ключа   -> skip_item

    Возвращает None, если продолжать нечего или нельзя:
    - все объявления пакета уже отправлены или пропущены;
    - пропустить пришлось бы последнее объявление пакета — тогда после
      пропуска продолжать нечем, а само пропущенное остаётся на совести
      пользователя;
    - checkpoint несогласован (fail-closed): неизвестный шаг, неизвестное
      значение money_click, item_index впереди расчётной позиции, пустой шаг
      у начатого объявления. Код такие состояния не производит; если они всё
      же на диске — угадывать план нельзя.

    Позицию пакета считаем НЕ по job["item_index"]: после пропуска он остаётся
    от прошлого прогона и врёт. Пакет идёт строго по порядку, поэтому «закрыты»
    ровно те объявления, что отправлены или сознательно пропущены, а работа
    всегда продолжается с первого незакрытого индекса.
    """
    return _resume_decision(job)[0]


def resume_unavailable_reason(job: dict[str, Any]) -> Optional[str]:
    """Человекочитаемая причина, почему resume_plan(job) — None; None, если план есть."""
    return _resume_decision(job)[1]


def _resume_decision(
    job: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[str], Optional[int]]:
    """(план, None, None) либо (None, причина, N).

    N — номер последнего объявления пакета, если плана нет именно потому, что
    оно создано (или могло быть создано) и пропускать его некуда (S2); иначе
    None. Смысл остального — в докстринге resume_plan.
    """
    try:
        items_total = int(job.get("items_total") or job.get("drafts_total") or 1)
        items_published = int(job.get("items_published") or 0)
        item_index = int(job.get("item_index") or 0)
        skipped = skipped_numbers(job.get("skipped_items"))
    except (TypeError, ValueError):
        return None, _REASON_UNRECOGNIZED, None

    money_click = job.get("money_click")
    # isinstance до `in`: из JSON может прийти нехешируемый мусор (список).
    if not (money_click is None or (
        isinstance(money_click, str) and money_click in MONEY_CLICK_VALUES
    )):
        return None, _REASON_UNRECOGNIZED, None
    step = job.get("step")
    if step is None:
        step = ""
    if not isinstance(step, str) or step not in KNOWN_STEPS:
        return None, _REASON_UNRECOGNIZED, None

    if items_published >= items_total:
        return None, _REASON_ALL_SENT, None
    if items_published < 0:
        return None, _REASON_UNRECOGNIZED, None

    pending = items_published + len(skipped) + 1
    if pending > items_total:
        return None, _reason_all_closed(skipped), None

    # F09: объявление «в работе» не может обогнать первую незакрытую позицию.
    if item_index > pending:
        return None, _REASON_UNRECOGNIZED, None
    # F11: пустой шаг бывает только у ещё не начатого пакета.
    if step == "" and item_index > 0:
        return None, _REASON_UNRECOGNIZED, None

    retry = {
        "mode": "retry_item",
        "start_index": pending,
        "skipped_item": None,
        "items_total": items_total,
    }

    # Индекс отстаёт от расчётного — предыдущее объявление завершено, а
    # следующее ещё не начиналось. Начинать его с формы безопасно. Отметка
    # money_click здесь относится к предыдущему объявлению и не учитывается.
    if item_index < pending:
        return retry, None, None

    # Объявление в работе.
    if money_click != "clicked":
        # До continue_listing Авито его ещё не создал — повтор с чистой формы
        # ничего не задваивает.
        if step in SAFE_PREFINANCIAL_STEPS:
            return retry, None, None
        # Доказано, что финальный клик не вызывался.
        if step == "continue_listing" and money_click == "not_clicked":
            return retry, None, None

    # Клик был или мог быть: повторный проход может создать дубль или
    # списать деньги второй раз.
    if pending + 1 > items_total:
        return None, _reason_last_created(pending), pending

    return {
        "mode": "skip_item",
        "start_index": pending + 1,
        "skipped_item": pending,
        "items_total": items_total,
    }, None, None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _serialize_draft(draft: Any) -> dict[str, Any]:
    return {
        "title": draft.title,
        "trade_type": draft.trade_type,
        "condition": draft.condition,
        "size": draft.size,
        "brand": draft.brand,
        "color": draft.color,
        "description": draft.description,
        "price": draft.price,
        "locations": [
            {"city": location.city, "address": location.address}
            for location in draft.locations
        ],
        "view_price_max": format(draft.view_price_max, "f"),
        "photo_paths": list(draft.photo_paths),
        "category": draft.category,
        "item_type": draft.item_type,
        "material": draft.material,
        "style": draft.style,
    }


def _deserialize_draft(payload: dict[str, Any]) -> Any:
    # Ленивый импорт исключает цикл publisher -> publish_state -> publisher.
    import publisher

    locations = tuple(
        publisher.LocationData(
            city=str(item["city"]),
            address=str(item["address"]),
        )
        for item in payload["locations"]
    )
    return publisher.DraftData(
        title=str(payload["title"]),
        trade_type=str(payload["trade_type"]),
        condition=str(payload["condition"]),
        size=str(payload["size"]),
        brand=str(payload.get("brand") or ""),
        color=str(payload["color"]),
        description=str(payload["description"]),
        price=int(payload["price"]),
        locations=locations,
        photo_paths=tuple(str(path) for path in payload["photo_paths"]),
        # Фолбэк на legacy-ключ "view_price" обязателен: на диске лежат старые
        # чекпоинты (формат до перехода на единственный потолок), у них нет
        # "view_price_max" — без фолбэка сервер падал бы на восстановлении
        # каждой такой задачи при старте.
        view_price_max=Decimal(str(payload.get("view_price_max") or payload["view_price"])),
        category=str(payload.get("category") or "jackets"),
        item_type=str(payload.get("item_type") or ""),
        material=str(payload.get("material") or ""),
        style=str(payload.get("style") or ""),
    )


def save_publish_state(
    job_dir: Path,
    job_id: str,
    job: dict[str, Any],
    draft: Any,
) -> Path:
    """Пишет checkpoint атомарно и с fsync (atomic_write.write_text_atomic).

    Ошибка записи пробрасывается: вызывающий код обязан считать checkpoint
    несохранённым (в публикации это стоп до денежного клика).
    """
    job_dir.mkdir(parents=True, exist_ok=True)
    state_path = job_dir / STATE_FILENAME
    payload = {
        "version": STATE_VERSION,
        "job_id": job_id,
        "job": _json_safe(job),
        "draft": _serialize_draft(draft),
    }
    write_text_atomic(
        state_path,
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return state_path


def _has_skipped(job: dict[str, Any]) -> bool:
    """Есть ли пропущенные объявления; нераспознанный список — считаем, что есть."""
    try:
        return bool(normalize_skipped_items(job.get("skipped_items")))
    except ValueError:
        return True


def cleanup_finished_job_dir(job_dir: Path, job: dict[str, Any]) -> None:
    """Уборка каталога задачи после done. done к этому моменту уже на диске.

    Без пропусков — каталог целиком. С пропусками (F24/S3) остаётся
    publish-state.json со следами пропущенных объявлений, фото удаляются.
    Ошибки уборки только логируются: итог задачи от них не меняется.
    """
    if job.get("status") != "done":
        return
    if not _has_skipped(job):
        shutil.rmtree(job_dir, ignore_errors=True)
        return
    if not job_dir.is_dir():
        return
    for child in job_dir.iterdir():
        if child.name == STATE_FILENAME:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError as exc:
            logger.warning("Не удалось удалить %s: %s", child, exc)


def write_final_state(
    job_dir: Path, job_id: str, job: dict[str, Any], draft: Any,
) -> bool:
    """Финальная запись checkpoint задачи и (при done) уборка каталога.

    Порядок фиксирован (F16): сначала итог на диск, потом уборка. Сбой записи
    не бросается наружу (вызывается из finally), а логируется; True — записано.
    """
    try:
        save_publish_state(job_dir, job_id, job, draft)
        saved = True
    except Exception:  # noqa: BLE001 — финальная запись не должна ронять finally
        logger.exception("Финальный checkpoint задачи %s не сохранён", job_id)
        saved = False
    # Итог не записан, а пропуски есть — не трогаем каталог: на диске
    # устаревший checkpoint, и файлы рядом с ним — последние следы.
    if job.get("status") == "done" and (saved or not _has_skipped(job)):
        cleanup_finished_job_dir(job_dir, job)
    return saved


def load_publish_state(job_dir: Path) -> tuple[dict[str, Any], Any]:
    payload = json.loads((job_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    if payload.get("version") != STATE_VERSION:
        raise ValueError("Неподдерживаемая версия checkpoint публикации")
    job = payload.get("job")
    draft = payload.get("draft")
    if not isinstance(job, dict) or not isinstance(draft, dict):
        raise ValueError("Checkpoint публикации повреждён")
    return dict(job), _deserialize_draft(draft)


def load_publish_jobs(root: Path) -> dict[str, dict[str, Any]]:
    """Читает свежие job-поля всех checkpoint без разбора draft."""
    jobs: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return jobs
    for job_dir in root.iterdir():
        if not job_dir.is_dir() or job_dir.name.startswith("prep_"):
            continue
        state_path = job_dir / STATE_FILENAME
        if not state_path.is_file():
            continue
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            job = payload["job"]
            if (
                payload.get("version") != STATE_VERSION
                or payload.get("job_id") != job_dir.name
                or not isinstance(job, dict)
            ):
                raise ValueError("Checkpoint публикации повреждён")
            jobs[job_dir.name] = job
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Checkpoint %s повреждён: %s", state_path, exc)
            # Неизвестный прогресс нельзя трактовать как безопасный новый старт.
            jobs[job_dir.name] = {"status": "corrupted", "money_click": "clicked"}
    return jobs


def recover_publish_states(root: Path) -> dict[str, tuple[dict[str, Any], Any]]:
    """Находит сохранённые задачи, не запуская их автоматически.

    Разбор КАЖДОЙ задачи изолирован: битый checkpoint одной задачи (мусор в
    JSON, нечисловые поля и т.п.) не должен ронять восстановление остальных —
    иначе один повреждённый файл на диске валит старт всего сервера. Такая
    задача не восстанавливается из .bak-копии (это могло бы означать повторную
    публикацию уже отправленного объявления) — просто помечается как
    повреждённая, наружу выдаётся минимальная запись со статусом "corrupted".
    """
    recovered: dict[str, tuple[dict[str, Any], Any]] = {}
    if not root.is_dir():
        return recovered
    for job_dir in root.iterdir():
        if not job_dir.is_dir() or job_dir.name.startswith("prep_"):
            continue
        state_path = job_dir / STATE_FILENAME
        if not state_path.is_file():
            continue
        try:
            job, draft = load_publish_state(job_dir)
            if job.get("status") == "done":
                # «Завершена с пропусками» (F24/S3): остаётся видимой после
                # рестарта, иначе следы пропущенных объявлений теряются.
                # Полный done без пропусков на диске бывает, только если
                # уборка не удалась, — такой пакет восстанавливать незачем.
                if _has_skipped(job):
                    recovered[job_dir.name] = (job, draft)
                continue
            if job.get("status") == "closed":
                recovered[job_dir.name] = (job, draft)
                continue
            items_published = int(job.get("items_published") or 0)
            items_total = int(job.get("items_total") or job.get("drafts_total") or 1)
            # S2: последнее объявление создано и продолжать нечем — в список
            # пропущенных (в памяти; на диск его пишет обработчик остановки,
            # здесь — для checkpoint, записанных до этого правила).
            record_unresumable_last_item(job, draft)
            # Сообщение обязано соответствовать плану возобновления: иначе
            # пользователь видит «продолжение запрещено» рядом с работающей
            # кнопкой «Продолжить со следующего».
            plan, reason, _last = _resume_decision(job)
            if plan is None:
                message = f"Сервер был перезапущен. {reason or _REASON_UNRECOGNIZED}"
                if not message.endswith("."):
                    message += "."
            elif plan["mode"] == "skip_item":
                message = (
                    f"Сервер был перезапущен. Объявление №{plan['skipped_item']} уже создано "
                    "на Авито и повторно отправлено не будет, чтобы не создать дубль "
                    f"или повторную оплату — проверьте его вручную. Продолжить можно "
                    f"с объявления №{plan['start_index']}."
                )
            else:
                message = (
                    "Сервер был перезапущен. Можно продолжить с первого "
                    "неотправленного объявления."
                )
            # Исходную причину остановки сохраняем отдельно ДО перезаписи
            # job["error"] сообщением про рестарт — иначе реальная причина
            # сбоя (например, конкретная ошибка Playwright) теряется.
            job["interrupted_reason"] = job.get("error")
            job["status"] = "interrupted"
            job["error"] = message
            job["user_action"] = {
                "type": "resume_available",
                "resumable": plan is not None,
                "items_published": items_published,
                "items_total": items_total,
                "next_item_index": plan["start_index"] if plan else items_published + 1,
                "skipped_item": plan["skipped_item"] if plan else None,
                "message": message,
            }
            recovered[job_dir.name] = (job, draft)
        except Exception as exc:
            logger.warning(
                "Checkpoint %s повреждён, помечаем как corrupted: %s", state_path, exc,
            )
            recovered[job_dir.name] = (
                {
                    "job_id": job_dir.name,
                    "status": "corrupted",
                    "error": "Checkpoint повреждён, проверьте кабинет Авито вручную",
                },
                None,
            )
    return recovered
