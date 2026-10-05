"""
Разбор объявлений ИИ через Claude Code CLI (docs/specs/resale-finder.md).

Определяет для каждого объявления, ноутбук ли это целиком, его бренд/серию/
видеокарту и состояние (рабочий / на запчасти) — основа для расчёта рынка
в resale_market.py. Разбор платный (лимиты подписки пользователя), поэтому
результат кэшируется в SQLite: повторный поиск по тому же объявлению с тем же
текстом не вызывает CLI повторно.

Публичный API:
    classify_items(items, *, runner=None, db_path="resale_cache.db", batch_size=40)
        -> dict[str, dict]   # item_id -> Classification (см. спеку)

Реальный runner запускает `claude -p` из нейтральной рабочей папки
tmp/resale/claude_cwd/ (чтобы CLI не подтягивал CLAUDE.md и память этого
проекта), с отключёнными инструментами и MCP — сценарий только читает текст
объявлений, никаких файловых операций или сети ИИ не нужно.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

Runner = Callable[[str], str]  # промпт -> stdout claude CLI (текст ответа модели)

BATCH_SIZE_DEFAULT = 40
CLAUDE_TIMEOUT_SECONDS = 180
CLAUDE_MODEL_ENV = "RESALE_CLAUDE_MODEL"
CLAUDE_MODEL_DEFAULT = "haiku"

# Нейтральная рабочая папка для запуска claude CLI — не в git (см. .gitignore
# tmp/). Собственного CLAUDE.md/памяти здесь нет, поэтому CLI не подтягивает
# инструкции этого проекта в системный промпт классификатора.
CLAUDE_CWD = Path("tmp") / "resale" / "claude_cwd"

_VALID_CONDITIONS = frozenset({"working", "parts", "unknown"})

_PROMPT_TEMPLATE = """\
Ты классифицируешь объявления с Авито по теме «игровые ноутбуки» для сервиса
поиска выгодных лотов под перепродажу.

На вход — JSON-массив объявлений с полями: item_id, title, description, price,
category_slug.

Для КАЖДОГО объявления верни объект со строго такими полями:
- item_id (str) — тот же, что во входе
- is_laptop (bool) — это ноутбук целиком (не зарядка, не сумка, не запчасть,
  не системный блок/ПК, не планшет)
- brand (str|null) — нормализованный бренд: "Acer", "ASUS", "Lenovo", "MSI",
  "HP" и т.п. (null, если не ноутбук или бренд не понятен)
- series (str|null) — линейка и номер БЕЗ кода модификации, нормализованное
  написание, например "Nitro 5", "ROG Strix G15", "Legion 5" (null, если не
  понятна). Код модификации (буквенно-цифровой суффикс вида "MF", "KF",
  "AN515-57") в series не входит — видеокарта и так разводит разные
  конфигурации по разным рынкам, а код модификации только дробит группы до
  «мало данных»
- gpu (str|null) — дискретная видеокарта нормализованно, например "RTX 3060",
  "GTX 1650", "RX 6600M"; null, если видеокарта не указана в тексте
- condition ("working"|"parts"|"unknown") — "parts", если на запчасти, не
  включается, разбит экран/матрица, залит, без ключевых деталей; "working",
  если явно рабочий; "unknown", если не понятно
- reason (str) — очень коротко, почему такой condition

Трудные случаи (разбирай по смыслу, а не по ключевым словам):
- «на запчасти, торг» -> is_laptop true (это ноутбук), condition "parts"
- «не включается, экран разбит» -> condition "parts"
- «зарядка для Acer Nitro 5, оригинал» -> is_laptop false (это зарядка, не ноутбук)
- «игровой ПК, RTX 3060, 32Гб» -> is_laptop false (это системный блок, не ноутбук)
- «видеокарта 3060 отдельно» -> is_laptop false (комплектующая)
- GPU без бренда: «3060» -> "RTX 3060", «1650» -> "GTX 1650" (в контексте
  игровых ноутбуков это карты Nvidia, если явно не указано AMD/Radeon)
- Разные написания серий: «нитро 5» -> "Nitro 5", «легион 5» -> "Legion 5",
  «рог стрикс» -> "ROG Strix"
- Код модификации не входит в series: «Gigabyte G5 MF» и «Gigabyte G5 KF» ->
  series "G5" (это одна линейка, MF/KF — код модификации); «Acer Nitro 5
  AN515-57» -> series "Nitro 5" (AN515-57 — код модификации, не часть
  названия линейки)

Ответь СТРОГО JSON-массивом объектов с этими полями, без пояснений до или
после. Массив можно обернуть в ```json блок.

Объявления:
{items_json}
"""


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _cache_key(item_id: str, title: str, description: str) -> str:
    """item_id + sha1(title + \\n + description) — продавец поправил текст,
    разбираем заново (см. спеку)."""
    digest = _sha1((title or "") + "\n" + (description or ""))
    return f"{item_id}:{digest}"


def _init_db(db_path: str) -> None:
    # closing() — `with sqlite3.connect(...) as conn` коммитит/откатывает
    # транзакцию, но НЕ закрывает соединение (частая ловушка sqlite3); на
    # Windows файл кэша иначе остаётся залоченным до сборки мусора.
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS classifications (
                    cache_key           TEXT PRIMARY KEY,
                    item_id             TEXT NOT NULL,
                    classification_json TEXT NOT NULL,
                    created_at          TEXT NOT NULL
                )
                """
            )


def _get_cached(db_path: str, cache_key: str) -> Optional[dict[str, Any]]:
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "SELECT classification_json FROM classifications WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Разбор ИИ: ошибка чтения кэша %s: %s", db_path, exc)
        return None
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        logger.warning("Разбор ИИ: битая запись кэша для ключа %s — игнорирую", cache_key)
        return None


def _save_cached(db_path: str, cache_key: str, item_id: str, classification: dict[str, Any]) -> None:
    """Записи с error не кэшируются (см. спеку) — вызывающий код это фильтрует."""
    from datetime import datetime, timezone

    try:
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO classifications (cache_key, item_id, classification_json, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        classification_json = excluded.classification_json,
                        created_at          = excluded.created_at
                    """,
                    (cache_key, item_id, json.dumps(classification, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
                )
    except sqlite3.Error as exc:
        logger.warning("Разбор ИИ: не удалось сохранить кэш для %s: %s", item_id, exc)


def _error_classification(item_id: str, error: str) -> dict[str, Any]:
    """Classification-заглушка для не разобранного объявления (см. спеку:
    error не None — остальные поля None/"unknown")."""
    return {
        "item_id": item_id,
        "is_laptop": False,
        "brand": None,
        "series": None,
        "gpu": None,
        "condition": "unknown",
        "reason": "",
        "error": error,
    }


def _normalize_one(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Приводит один объект ответа модели к контракту Classification.

    None — объект без валидного item_id (нечем ключевать, отбрасываем).
    """
    item_id = raw.get("item_id")
    if item_id is None:
        return None
    condition = raw.get("condition")
    if condition not in _VALID_CONDITIONS:
        condition = "unknown"
    return {
        "item_id": str(item_id),
        "is_laptop": bool(raw.get("is_laptop")),
        "brand": raw.get("brand") or None,
        "series": raw.get("series") or None,
        "gpu": raw.get("gpu") or None,
        "condition": condition,
        "reason": str(raw.get("reason") or ""),
        "error": None,
    }


def _extract_json_array(text: str) -> Optional[list[Any]]:
    """Достаёт JSON-массив из ответа модели, включая ```-обёртку (см. спеку)."""
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    candidate = fence_match.group(1).strip() if fence_match else stripped

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return None

    return data if isinstance(data, list) else None


class ClaudeCliError(RuntimeError):
    """Ошибка вызова claude CLI — вся пачка (или вся задача) получает error, не трейсбек."""


def _real_runner(prompt: str) -> str:
    """
    Реальный вызов claude CLI: промпт через stdin (см. спеку — лимит длины
    командной строки Windows), --output-format json, модель из
    RESALE_CLAUDE_MODEL (по умолчанию haiku), инструменты и MCP отключены —
    сценарий только читает текст, файлы/сеть ИИ не нужны. Таймаут 180 с.

    Разведано вручную (27.09.2026, версия claude 2.1.280): --output-format json
    отдаёт JSON-МАССИВ событий сессии (system/assistant/rate_limit_event/result),
    не единый объект — нужный текст лежит в поле "result" события с
    type == "result".
    """
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise ClaudeCliError(
            "Не найден claude CLI в PATH. Установите Claude Code "
            "(npm install -g @anthropic-ai/claude-code) и повторите."
        )

    model = os.environ.get(CLAUDE_MODEL_ENV, CLAUDE_MODEL_DEFAULT)
    CLAUDE_CWD.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            [
                claude_bin, "-p",
                "--output-format", "json",
                "--model", model,
                "--tools", "",
                "--strict-mcp-config",
            ],
            input=prompt.encode("utf-8"),
            cwd=str(CLAUDE_CWD),
            capture_output=True,
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude CLI не ответил за {CLAUDE_TIMEOUT_SECONDS} с") from exc
    except OSError as exc:
        raise ClaudeCliError(f"Не удалось запустить claude CLI: {exc}") from exc

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise ClaudeCliError(
            f"claude CLI завершился с кодом {proc.returncode}: {(stderr or stdout)[:500]}"
        )

    try:
        events = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(f"Не удалось разобрать ответ claude CLI: {exc}") from exc

    result_event: Optional[dict[str, Any]] = None
    if isinstance(events, list):
        for event in reversed(events):
            if isinstance(event, dict) and event.get("type") == "result":
                result_event = event
                break
    elif isinstance(events, dict):
        result_event = events

    if result_event is None:
        raise ClaudeCliError("В ответе claude CLI не найдено событие result")
    if result_event.get("is_error"):
        raise ClaudeCliError(
            f"claude CLI вернул ошибку: {result_event.get('result') or result_event.get('subtype')}"
        )

    text = result_event.get("result")
    if not isinstance(text, str):
        raise ClaudeCliError("Событие result claude CLI без текстового поля result")
    return text


def _classify_batch(
    batch: list[dict[str, Any]],
    runner: Runner,
) -> tuple[dict[str, dict[str, Any]], Optional[str]]:
    """
    Разбирает одну пачку (≤ batch_size). Сбой — error на все item_id пачки,
    НИКОГДА не пробрасывается исключением (см. classify_items).

    Возвращает (результат, systemic_error). systemic_error не None только
    когда упал САМ вызов runner (claude не найден, ненулевой код выхода,
    таймаут, не найдено событие result) — это отказ инфраструктуры: claude
    CLI сломан целиком, и вызывающий код (classify_items) может решить не
    дёргать runner на следующих пачках, каждая провалится тем же образом
    и потратит впустую 180 с таймаута. Сбой РАЗБОРА ответа (модель ответила,
    но не JSON) под systemic_error не попадает — это проблема качества
    ответа на конкретной пачке, а не отказ инфраструктуры, следующие пачки
    вызываются как обычно.
    """
    payload = [
        {
            "item_id": item["item_id"],
            "title": item.get("title") or "",
            "description": item.get("description") or "",
            "price": item.get("price"),
            "category_slug": item.get("category_slug"),
        }
        for item in batch
    ]
    prompt = _PROMPT_TEMPLATE.format(items_json=json.dumps(payload, ensure_ascii=False, indent=2))

    try:
        raw_text = runner(prompt)
    except Exception as exc:
        logger.warning("Разбор ИИ: пачка из %d объявлений не разобрана: %s", len(batch), exc)
        error_text = str(exc)
        result = {item["item_id"]: _error_classification(item["item_id"], error_text) for item in batch}
        return result, error_text

    raw_array = _extract_json_array(raw_text)
    if raw_array is None:
        logger.warning("Разбор ИИ: не удалось разобрать JSON-ответ модели на пачке из %d объявлений", len(batch))
        error_text = "Ответ модели не является JSON-массивом"
        result = {item["item_id"]: _error_classification(item["item_id"], error_text) for item in batch}
        return result, None

    parsed: dict[str, dict[str, Any]] = {}
    for raw_obj in raw_array:
        if not isinstance(raw_obj, dict):
            continue
        normalized = _normalize_one(raw_obj)
        if normalized is not None:
            parsed[normalized["item_id"]] = normalized

    # item_id, которых модель не вернула, получают error (см. спеку).
    result: dict[str, dict[str, Any]] = {}
    for item in batch:
        item_id = item["item_id"]
        if item_id in parsed:
            result[item_id] = parsed[item_id]
        else:
            logger.warning("Разбор ИИ: объявление %s отсутствует в ответе модели", item_id)
            result[item_id] = _error_classification(item_id, "Модель не вернула это объявление в ответе")

    return result, None


def classify_items(
    items: list[dict[str, Any]],
    *,
    runner: Optional[Runner] = None,
    db_path: str = "resale_cache.db",
    batch_size: int = BATCH_SIZE_DEFAULT,
) -> dict[str, dict[str, Any]]:
    """
    Разбирает объявления через ИИ пачками по ≤ batch_size, с кэшем в SQLite.

    Объявление без item_id не разбирается (нечем ключевать) — не попадает
    в результат (см. спеку). Кэш-хит не вызывает runner. Записи с error
    не кэшируются — при следующем запуске такие объявления разбираются заново.

    Возвращает item_id -> Classification (без служебного item_id внутри
    значения — он и так ключ словаря, но для единообразия с ответом модели
    оставляем поле, вызывающий код на него не полагается).
    """
    active_runner: Runner = runner if runner is not None else _real_runner

    _init_db(db_path)

    keyed_items: list[dict[str, Any]] = []
    for item in items:
        item_id = item.get("item_id")
        if not item_id:
            continue
        keyed_items.append(item)

    results: dict[str, dict[str, Any]] = {}
    to_classify: list[dict[str, Any]] = []
    cache_keys: dict[str, str] = {}

    for item in keyed_items:
        item_id = str(item["item_id"])
        cache_key = _cache_key(item_id, item.get("title") or "", item.get("description") or "")
        cache_keys[item_id] = cache_key
        cached = _get_cached(db_path, cache_key)
        if cached is not None:
            results[item_id] = cached
        else:
            to_classify.append({**item, "item_id": item_id})

    logger.info(
        "Разбор ИИ: %d объявлений из кэша, %d к разбору (пачками по %d)",
        len(results), len(to_classify), batch_size,
    )

    # Сбой САМОГО вызова CLI (не найден, авторизация, сеть) на одной пачке —
    # отказ инфраструктуры: все следующие пачки провалятся тем же образом.
    # classify_items НЕ бросает исключение (это разбор одного шага — лог и
    # пропуск, не падение), но и не тратит время на заведомо провальные
    # пачки: как только вызов runner один раз падает, оставшиеся пачки
    # получают ту же ошибку без повторного вызова runner. Отличать такой
    # отказ от «задача целиком не задалась» — дело вызывающего кода
    # (resale_scan.py: видит error у всех объявлений — завершает job error).
    systemic_error: Optional[str] = None
    for start in range(0, len(to_classify), batch_size):
        batch = to_classify[start:start + batch_size]
        if systemic_error is not None:
            batch_result = {
                item["item_id"]: _error_classification(item["item_id"], systemic_error) for item in batch
            }
        else:
            batch_result, batch_systemic_error = _classify_batch(batch, active_runner)
            if batch_systemic_error is not None:
                systemic_error = batch_systemic_error
        for item_id, classification in batch_result.items():
            results[item_id] = classification
            if not classification.get("error"):
                _save_cached(db_path, cache_keys[item_id], item_id, classification)

    return results
