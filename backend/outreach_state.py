"""Атомарное дисковое состояние задач рассылки продавцам Авито.

Калька с publish_state.py: та же атомарность записи и тот же дух двух режимов
возобновления. Полное обоснование обоих режимов — там, здесь только отличия.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)

STATE_FILENAME = "outreach-state.json"
STATE_VERSION = 1

# До send_click Авито не получил ни одного клика по отправке — эти шаги можно
# безопасно начать заново с текущего кандидата: письмо ему ещё не уходило.
SAFE_PRECLICK_STEPS = frozenset({
    "",
    "connect_chrome",
    "open_listing",
    "check_seller",
    "open_chat",
    "fill_message",
})


def resume_plan(job: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Решает, МОЖНО ли и КАК продолжить рассылку после терминальной остановки.

    Два режима, как у публикации (см. publish_state.resume_plan) — подмена
    одного другим стоит повторного письма живому продавцу:

    - `retry_item` — остановка случилась ДО клика отправки (текущий шаг в
      SAFE_PRECLICK_STEPS). Кандидату ничего не ушло, безопасно начать его
      заново.
    - `skip_item` — остановка случилась на `send_click` или позже. Клик уже
      сделан, судьба сообщения неизвестна (outreach_store уже пометил его
      "sending"/"uncertain" и блокирует повтор своей историей — resume_plan
      здесь второй, независимый заслон). Автоматике запрещено трогать этого
      кандидата повторно: пакет продолжается со СЛЕДУЮЩЕГО, номер
      пропущенного возвращается наружу для ручной проверки.

    Возвращает None, если продолжать нечего или некуда:
    - рассылка ещё не начиналась (items_total <= 0);
    - все кандидаты списка уже обработаны;
    - либо пропустить пришлось бы последнего кандидата списка — тогда после
      пропуска продолжать нечем.

    В отличие от публикации, здесь не нужна отдельная поправка на «отставший»
    item_index: он обновляется в момент входа в каждый шаг (см. step_cb в
    outreach.send_messages), поэтому на остановке всегда указывает точно на
    кандидата, чья обработка прервалась.
    """
    try:
        items_total = int(job.get("items_total") or 0)
        item_index = int(job.get("item_index") or 0)
    except (TypeError, ValueError):
        return None

    if items_total <= 0 or item_index > items_total:
        return None

    step = str(job.get("step") or "")

    if item_index < 1:
        # Рассылка ещё не начала ни одного кандидата — начинаем с первого.
        return {
            "mode": "retry_item",
            "start_index": 1,
            "skipped_item": None,
            "items_total": items_total,
        }

    if step in SAFE_PRECLICK_STEPS:
        return {
            "mode": "retry_item",
            "start_index": item_index,
            "skipped_item": None,
            "items_total": items_total,
        }

    # Клик отправки уже сделан (или дальше): этого кандидата повторять нельзя.
    if item_index >= items_total:
        return None

    return {
        "mode": "skip_item",
        "start_index": item_index + 1,
        "skipped_item": item_index,
        "items_total": items_total,
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def save_outreach_state(job_dir: Path, job: dict[str, Any]) -> Path:
    """Пишет checkpoint через временный файл и атомарный replace."""
    job_dir.mkdir(parents=True, exist_ok=True)
    state_path = job_dir / STATE_FILENAME
    tmp_path = job_dir / f"{STATE_FILENAME}.tmp"
    payload = {
        "version": STATE_VERSION,
        "job_id": job.get("job_id"),
        "job": _json_safe(job),
    }
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(state_path)
    return state_path


def load_outreach_state(job_dir: Path) -> dict[str, Any]:
    """Читает checkpoint. Бросает исключение, если его нет или он повреждён."""
    payload = json.loads((job_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    if payload.get("version") != STATE_VERSION:
        raise ValueError("Неподдерживаемая версия checkpoint рассылки")
    job = payload.get("job")
    if not isinstance(job, dict):
        raise ValueError("Checkpoint рассылки повреждён")
    return dict(job)


def clear_outreach_state(job_dir: Path) -> None:
    """Удаляет checkpoint — например, после успешного завершения задачи."""
    try:
        (job_dir / STATE_FILENAME).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Не удалось удалить checkpoint %s: %s", job_dir, exc)
