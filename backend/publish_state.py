"""Атомарное дисковое состояние задач полной публикации Авито."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

STATE_FILENAME = "publish-state.json"
STATE_VERSION = 1

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
      финансовое подтверждение по текущему объявлению (то есть
      `is_publish_resume_safe` разрешает повтор). Тогда безопасно начать
      текущее объявление заново с чистой формы: ничего платного ещё не
      случилось, повтор ничего не задваивает.
    - `skip_item` — остановка произошла ПОСЛЕ `continue_listing`: объявление
      уже создано на Авито, но, возможно, не оплачено и не дожато до конца.
      Автоматике в этом случае запрещено прикасаться к нему повторно — клик
      по уже пройденным финансовым шагам может создать дубль или списать
      деньги второй раз. Поэтому это объявление молча пропускается: пакет
      продолжается со СЛЕДУЮЩЕГО индекса, а номер пропущенного возвращается
      наружу, чтобы пользователь вручную проверил и завершил его сам.

    Возвращает None, если продолжать нечего или некуда:
    - все объявления пакета уже отправлены;
    - либо пропустить пришлось бы последнее объявление пакета — тогда после
      пропуска продолжать нечем, а само пропущенное всё равно остаётся на
      совести пользователя.

    Позицию пакета считаем НЕ по job["item_index"]: после пропуска он остаётся
    от прошлого прогона и врёт. Пакет идёт строго по порядку, поэтому «закрыты»
    ровно те объявления, что отправлены или сознательно пропущены, а работа
    всегда продолжается с первого незакрытого индекса.
    """
    try:
        items_total = int(job.get("items_total") or job.get("drafts_total") or 1)
        items_published = int(job.get("items_published") or 0)
        item_index = int(job.get("item_index") or 0)
        skipped = {int(value) for value in (job.get("skipped_items") or [])}
    except (TypeError, ValueError):
        return None

    if not (0 <= items_published < items_total):
        return None

    pending = items_published + len(skipped) + 1
    if pending > items_total:
        return None

    # Индекс отстаёт от расчётного — предыдущее объявление завершено, а
    # следующее ещё не начиналось. Начинать его с формы безопасно.
    if item_index < pending:
        return {
            "mode": "retry_item",
            "start_index": pending,
            "skipped_item": None,
            "items_total": items_total,
        }

    # Объявление в работе. До continue_listing Авито его ещё не создал —
    # повтор с чистой формы ничего не задваивает.
    if str(job.get("step") or "") in SAFE_PREFINANCIAL_STEPS:
        return {
            "mode": "retry_item",
            "start_index": pending,
            "skipped_item": None,
            "items_total": items_total,
        }

    # Объявление уже создано на Авито: повторный проход по пройденным
    # финансовым шагам может создать дубль или списать деньги второй раз.
    if pending + 1 > items_total:
        return None

    return {
        "mode": "skip_item",
        "start_index": pending + 1,
        "skipped_item": pending,
        "items_total": items_total,
    }


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
    """Пишет checkpoint через временный файл и атомарный replace."""
    job_dir.mkdir(parents=True, exist_ok=True)
    state_path = job_dir / STATE_FILENAME
    tmp_path = job_dir / f"{STATE_FILENAME}.tmp"
    payload = {
        "version": STATE_VERSION,
        "job_id": job_id,
        "job": _json_safe(job),
        "draft": _serialize_draft(draft),
    }
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(state_path)
    return state_path


def load_publish_state(job_dir: Path) -> tuple[dict[str, Any], Any]:
    payload = json.loads((job_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    if payload.get("version") != STATE_VERSION:
        raise ValueError("Неподдерживаемая версия checkpoint публикации")
    job = payload.get("job")
    draft = payload.get("draft")
    if not isinstance(job, dict) or not isinstance(draft, dict):
        raise ValueError("Checkpoint публикации повреждён")
    return dict(job), _deserialize_draft(draft)


def recover_publish_states(root: Path) -> dict[str, tuple[dict[str, Any], Any]]:
    """Находит сохранённые задачи, не запуская их автоматически."""
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
        except Exception as exc:
            logger.warning("Checkpoint %s не восстановлен: %s", state_path, exc)
            continue
        if job.get("status") == "done":
            continue
        items_published = int(job.get("items_published") or 0)
        items_total = int(job.get("items_total") or job.get("drafts_total") or 1)
        # Сообщение обязано соответствовать плану возобновления: иначе
        # пользователь видит «продолжение запрещено» рядом с работающей
        # кнопкой «Продолжить со следующего».
        plan = resume_plan(job)
        if plan is None:
            message = (
                "Сервер был перезапущен. Автоматически продолжать нечего — "
                "завершите оставшееся объявление вручную."
            )
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
    return recovered
