"""
Общий журнал событий агентов — SQLite, отдельная база agents.db.

Отдельный файл от cache.db: тот чистят при смене логики метрик аналитики,
журнал должен это пережить. Стандартный sqlite3 из stdlib, db_path параметром —
как в cache.py.

Путь к базе не зашит в дефолт параметра: модульная DB_PATH (из переменной
окружения AVITO_AGENTS_DB) вычисляется при каждом вызове через db_path or
DB_PATH. Боевой app.py выставляет DB_PATH на agents.db в корне проекта перед
uvicorn.run; при импорте app.py как модуля (тесты/смоуки) DB_PATH остаётся
None — журнал выключен: пишущие функции тихо возвращаются, читающие отдают
пустой результат. Так тесты не портят боевой журнал, даже если дергают
реальные хуки в app.py/publisher.py.

Правило безопасности (деньги): log_event и record_listing НИКОГДА не бросают
исключения наружу, кроме единственного случая — ValueError на неизвестный actor
(это ошибка программиста, а не сбой окружения). Журнал вызывают из публикации,
и любое другое исключение там может оборвать пакет посреди оплаченных объявлений.
Любая ошибка SQLite/сериализации — logger.error и тихий возврат.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator

import agents_registry

logger = logging.getLogger(__name__)

# Путь к базе журнала по умолчанию. None (не задана переменная окружения) —
# журнал выключен: пишущие функции ничего не делают, читающие отдают пустоту.
# Боевой app.py присваивает сюда agents.db в корне проекта перед uvicorn.run.
DB_PATH: str | None = os.environ.get("AVITO_AGENTS_DB")


@contextlib.contextmanager
def _connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """
    sqlite3.connect(...) как контекстный менеджер коммитит/откатывает
    транзакцию, но НЕ закрывает соединение — на Windows это держит файл БД
    заблокированным (мешает удалению временных файлов в тестах). Закрываем
    явно, как self-test cache.py делает через gc.collect() перед os.remove.

    timeout=0.5 (вместо дефолтных 5 с): журналу дожидаться снятой блокировки
    не нужно — лучше потерять одно событие, чем задержать event loop дольше
    5-секундных дедлайнов поиска кнопок в publisher.
    """
    conn = sqlite3.connect(db_path, timeout=0.5)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ── Инициализация БД ──────────────────────────────────────────────────────────

def init_db(db_path: str | None = None) -> None:
    """
    Создаёт таблицы events, listings, stats_snapshots, hypotheses, если их ещё нет.

    Схема — из docs/specs/agents-registry.md, раздел «Архитектура», без изменений.

    Args:
        db_path: путь к файлу базы данных; None — берётся модульная DB_PATH.
                 Если и она None, журнал выключен — функция ничего не делает.
    """
    effective_path = db_path or DB_PATH
    if effective_path is None:
        logger.debug("Журнал агентов выключен (DB_PATH не задан) — init_db пропущен")
        return
    try:
        with _connection(effective_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           TEXT NOT NULL,
                    run_id       TEXT,
                    actor        TEXT NOT NULL,
                    type         TEXT NOT NULL,
                    tool         TEXT,
                    summary      TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS listings (
                    item_id        TEXT PRIMARY KEY,
                    publish_job_id TEXT,
                    prep_id        TEXT,
                    variant_index  INTEGER,
                    city_slug      TEXT,
                    city_name      TEXT,
                    category       TEXT,
                    title          TEXT,
                    published_at   TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stats_snapshots (
                    id          TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL,
                    period_days INTEGER NOT NULL,
                    date_from   TEXT NOT NULL,
                    date_to     TEXT NOT NULL,
                    data_json   TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hypotheses (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id           TEXT NOT NULL,
                    created_at       TEXT NOT NULL,
                    snapshot_id      TEXT NOT NULL,
                    scout_report_key TEXT NOT NULL,
                    change           TEXT NOT NULL,
                    basis            TEXT NOT NULL,
                    metric           TEXT NOT NULL,
                    expected_effect  TEXT NOT NULL,
                    check_days       INTEGER NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'pending',
                    decided_at       TEXT
                )
            """)
            conn.commit()
        logger.debug("База журнала агентов инициализирована: %s", effective_path)
    except sqlite3.Error as exc:
        logger.error("Ошибка при инициализации БД %s: %s", effective_path, exc)
        raise


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _now_utc_iso() -> str:
    """Текущее время UTC в ISO-8601."""
    return datetime.now(tz=timezone.utc).isoformat()


# ── Журнал событий ─────────────────────────────────────────────────────────────

def log_event(
    actor: str,
    type: str,
    summary: str,
    *,
    run_id: str | None = None,
    tool: str | None = None,
    payload: dict | None = None,
    db_path: str | None = None,
) -> None:
    """
    Пишет событие в журнал.

    Проверка прав — единственная точка контракта:
      - actor не из реестра и не "human" → ValueError (ошибка программиста,
        наружу бросается всегда, тесты её ловят);
      - tool задан и не входит в allowed_tools агента → вместо исходного события
        пишется violation с payload {attempted_type, tool, summary}, в лог —
        warning, исключения нет.

    Любая ошибка SQLite/сериализации ловится и уходит в logger.error — функция
    никогда не бросает исключение наружу, кроме ValueError выше.

    Args:
        actor:   id агента из реестра или "human"
        type:    тип события (run_started/run_finished/run_failed/artifact/
                 handoff/violation/decision)
        summary: строка по-русски для UI
        run_id:  job_id / prep_id / id запуска агента
        tool:    инструмент, если применимо
        payload: произвольные детали события
        db_path: путь к файлу БД; None — берётся модульная DB_PATH, а если
                 и она None, журнал выключен и функция ничего не пишет
    """
    if actor != "human" and agents_registry.get_agent(actor) is None:
        raise ValueError(f"Неизвестный actor журнала: {actor!r}")

    effective_path = db_path or DB_PATH
    if effective_path is None:
        logger.debug("Журнал агентов выключен (DB_PATH не задан) — событие %r пропущено", type)
        return

    effective_type = type
    effective_summary = summary
    effective_payload: dict[str, Any] = dict(payload or {})

    if tool is not None and actor != "human":
        agent = agents_registry.get_agent(actor)
        if agent is not None and tool not in agent.allowed_tools:
            logger.warning(
                "Агент %s заявил инструмент %r вне allowed_tools (событие %r)",
                actor, tool, type,
            )
            effective_type = "violation"
            effective_summary = f"Агент «{actor}» заявил инструмент вне разрешённых: {tool}"
            effective_payload = {
                "attempted_type": type,
                "tool": tool,
                "summary": summary,
            }

    try:
        payload_json = json.dumps(effective_payload, ensure_ascii=False, default=str)
        with _connection(effective_path) as conn:
            conn.execute(
                """
                INSERT INTO events (ts, run_id, actor, type, tool, summary, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_utc_iso(), run_id, actor, effective_type, tool,
                    effective_summary, payload_json,
                ),
            )
            conn.commit()
        logger.debug("Событие журнала записано: actor=%s type=%s", actor, effective_type)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.error(
            "Ошибка записи события журнала (actor=%s, type=%s): %s",
            actor, effective_type, exc,
        )


def list_events(
    actor: str | None = None,
    limit: int = 100,
    db_path: str | None = None,
) -> list[dict]:
    """
    Возвращает события журнала, новые сверху. payload отдаётся уже разобранным dict.

    Args:
        actor:   фильтр по агенту/human; None — все
        limit:   максимум записей
        db_path: путь к файлу БД; None — берётся модульная DB_PATH, а если
                 и она None, журнал выключен и функция отдаёт пустой список

    Returns:
        Список событий (пустой список при сбое БД или выключенном журнале —
        исключение наружу не летит).
    """
    effective_path = db_path or DB_PATH
    if effective_path is None:
        logger.debug("Журнал агентов выключен (DB_PATH не задан) — list_events отдаёт []")
        return []
    try:
        with _connection(effective_path) as conn:
            conn.row_factory = sqlite3.Row
            if actor is not None:
                cursor = conn.execute(
                    "SELECT * FROM events WHERE actor = ? ORDER BY id DESC LIMIT ?",
                    (actor, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
    except sqlite3.Error as exc:
        logger.error("Ошибка чтения журнала (actor=%s): %s", actor, exc)
        return []

    events: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.error("Битый payload_json в событии id=%s: %s", row["id"], exc)
            payload = {}
        events.append({
            "id": row["id"],
            "ts": row["ts"],
            "run_id": row["run_id"],
            "actor": row["actor"],
            "type": row["type"],
            "tool": row["tool"],
            "summary": row["summary"],
            "payload": payload,
        })
    return events


# ── Объявления ────────────────────────────────────────────────────────────────

def record_listing(
    item_id: str,
    *,
    publish_job_id: str | None = None,
    prep_id: str | None = None,
    variant_index: int | None = None,
    city_slug: str | None = None,
    city_name: str | None = None,
    category: str | None = None,
    title: str | None = None,
    db_path: str | None = None,
) -> None:
    """
    Записывает опубликованное объявление в listings. Идемпотентна по item_id
    (INSERT OR IGNORE): возобновление публикации (checkpoint) может прислать
    тот же item_id повторно, дублей быть не должно.

    Подчиняется тому же правилу безопасности, что и log_event: никогда не
    бросает исключение наружу — любая ошибка SQLite уходит в logger.error.

    Args:
        item_id:        id объявления на Авито (PRIMARY KEY)
        publish_job_id:  id задачи публикации
        prep_id:         id пакета подготовки, None если публиковали без него
        variant_index:   номер варианта в пакете, с 1
        city_slug:       slug города
        city_name:       название города
        category:        категория черновика
        title:           заголовок объявления
        db_path:         путь к файлу БД; None — берётся модульная DB_PATH,
                          а если и она None, журнал выключен и функция
                          ничего не пишет
    """
    effective_path = db_path or DB_PATH
    if effective_path is None:
        logger.debug(
            "Журнал агентов выключен (DB_PATH не задан) — объявление %s не записано",
            item_id,
        )
        return
    try:
        with _connection(effective_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO listings
                    (item_id, publish_job_id, prep_id, variant_index,
                     city_slug, city_name, category, title, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id, publish_job_id, prep_id, variant_index,
                    city_slug, city_name, category, title, _now_utc_iso(),
                ),
            )
            conn.commit()
        logger.debug("Объявление записано в listings: item_id=%s", item_id)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.error("Ошибка записи объявления в listings (item_id=%s): %s", item_id, exc)


def get_listings(
    item_ids: list[str] | None = None,
    db_path: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Возвращает строки listings по item_id (ключ словаря — item_id).

    Используется Сборщиком статистики (backend/stats_collector.py) для
    обогащения объявлений из API Авито данными о prep_id/варианте/городе.

    Args:
        item_ids: список item_id для выборки; None — все строки, [] — пустой
                   результат без обращения к БД
        db_path:  путь к файлу БД; None — берётся модульная DB_PATH, а если
                   и она None, журнал выключен и функция отдаёт {}

    Returns:
        Словарь item_id -> строка listings (пустой при сбое БД или
        выключенном журнале — исключение наружу не летит).
    """
    if item_ids is not None and not item_ids:
        return {}

    effective_path = db_path or DB_PATH
    if effective_path is None:
        logger.debug("Журнал агентов выключен (DB_PATH не задан) — get_listings отдаёт {}")
        return {}

    try:
        with _connection(effective_path) as conn:
            conn.row_factory = sqlite3.Row
            if item_ids is not None:
                placeholders = ",".join("?" for _ in item_ids)
                cursor = conn.execute(
                    f"SELECT * FROM listings WHERE item_id IN ({placeholders})",
                    tuple(item_ids),
                )
            else:
                cursor = conn.execute("SELECT * FROM listings")
            rows = cursor.fetchall()
    except sqlite3.Error as exc:
        logger.error("Ошибка чтения listings: %s", exc)
        return {}

    return {row["item_id"]: dict(row) for row in rows}


# ── Снимки статистики ────────────────────────────────────────────────────────

def save_stats_snapshot(
    snapshot_id: str,
    *,
    period_days: int,
    date_from: str,
    date_to: str,
    data: dict[str, Any],
    db_path: str | None = None,
) -> None:
    """
    Записывает снимок статистики Сборщика в stats_snapshots.

    В отличие от log_event/record_listing НЕ глотает исключения: снимок —
    единственный результат запуска Сборщика, и потерять его молча означало бы
    показать пользователю ложный успех. Сбой SQLite здесь должен превратить
    запуск Сборщика в run_failed — это делает вызывающий код (app.py).

    Args:
        snapshot_id: id снимка (= run_id запуска Сборщика)
        period_days: 7 или 30
        date_from:   ISO-дата начала периода
        date_to:     ISO-дата конца периода
        data:        data_json снимка (см. docs/specs/agents-registry.md, Этап 2)
        db_path:     путь к файлу БД; None — берётся модульная DB_PATH, а если
                     и она None, журнал выключен и функция ничего не пишет
    """
    effective_path = db_path or DB_PATH
    if effective_path is None:
        logger.debug(
            "Журнал агентов выключен (DB_PATH не задан) — снимок статистики %s не записан",
            snapshot_id,
        )
        return
    with _connection(effective_path) as conn:
        conn.execute(
            """
            INSERT INTO stats_snapshots (id, created_at, period_days, date_from, date_to, data_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id, _now_utc_iso(), period_days, date_from, date_to,
                json.dumps(data, ensure_ascii=False),
            ),
        )
        conn.commit()


def _row_to_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    try:
        data = json.loads(row["data_json"]) if row["data_json"] else {}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Битый data_json в снимке %s: %s", row["id"], exc)
        data = {}
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "period_days": row["period_days"],
        "date_from": row["date_from"],
        "date_to": row["date_to"],
        "data": data,
    }


def get_stats_snapshot(snapshot_id: str, db_path: str | None = None) -> dict[str, Any] | None:
    """Снимок статистики по id или None (не найден, БД недоступна, журнал выключен)."""
    effective_path = db_path or DB_PATH
    if effective_path is None:
        return None
    try:
        with _connection(effective_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM stats_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
    except sqlite3.Error as exc:
        logger.error("Ошибка чтения снимка статистики %s: %s", snapshot_id, exc)
        return None
    return _row_to_snapshot(row) if row is not None else None


def get_latest_stats_snapshot(db_path: str | None = None) -> dict[str, Any] | None:
    """Последний по created_at снимок статистики или None (снимков ещё нет,
    БД недоступна, журнал выключен — исключение наружу не летит)."""
    effective_path = db_path or DB_PATH
    if effective_path is None:
        return None
    try:
        with _connection(effective_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM stats_snapshots ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        logger.error("Ошибка чтения последнего снимка статистики: %s", exc)
        return None
    return _row_to_snapshot(row) if row is not None else None


# ── Гипотезы Стратега ────────────────────────────────────────────────────────

def save_hypotheses(
    run_id: str,
    *,
    snapshot_id: str,
    scout_report_key: str,
    hypotheses: list[dict[str, Any]],
    db_path: str | None = None,
) -> None:
    """
    Записывает гипотезы одного запуска Стратега в hypotheses, статус всех —
    "pending". Как и save_stats_snapshot, НЕ глотает исключения: гипотезы —
    единственный результат запуска Стратега, и потерять их молча означало бы
    показать пользователю ложный успех. Сбой SQLite здесь должен превратить
    запуск в run_failed — это делает вызывающий код (app.py).

    Args:
        run_id:           id запуска Стратега
        snapshot_id:      id снимка Сборщика, на котором построены факты
        scout_report_key: ключ кэша отчёта Разведчика (cache.build_cache_key)
        hypotheses:       список гипотез (change/basis/metric/expected_effect/
                          check_days) — уже провалидированных strategist.parse_hypotheses
        db_path:          путь к файлу БД; None — берётся модульная DB_PATH,
                          а если и она None, журнал выключен и функция ничего
                          не пишет
    """
    effective_path = db_path or DB_PATH
    if effective_path is None:
        logger.debug(
            "Журнал агентов выключен (DB_PATH не задан) — гипотезы запуска %s не записаны",
            run_id,
        )
        return
    created_at = _now_utc_iso()
    with _connection(effective_path) as conn:
        for hypothesis in hypotheses:
            conn.execute(
                """
                INSERT INTO hypotheses
                    (run_id, created_at, snapshot_id, scout_report_key,
                     change, basis, metric, expected_effect, check_days, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    run_id, created_at, snapshot_id, scout_report_key,
                    hypothesis["change"], hypothesis["basis"], hypothesis["metric"],
                    hypothesis["expected_effect"], hypothesis["check_days"],
                ),
            )
        conn.commit()


def _row_to_hypothesis(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "snapshot_id": row["snapshot_id"],
        "scout_report_key": row["scout_report_key"],
        "change": row["change"],
        "basis": row["basis"],
        "metric": row["metric"],
        "expected_effect": row["expected_effect"],
        "check_days": row["check_days"],
        "status": row["status"],
        "decided_at": row["decided_at"],
    }


def list_hypotheses(run_id: str | None = None, db_path: str | None = None) -> list[dict[str, Any]]:
    """
    Гипотезы одного запуска Стратега. run_id=None — гипотезы ПОСЛЕДНЕГО
    запуска (по MAX(created_at) среди уже сохранённых) — GET /api/hypotheses
    по спеке отдаёт только последний запуск, а не все гипотезы истории.

    Args:
        run_id:  id конкретного запуска; None — последний по времени
        db_path: путь к файлу БД; None — берётся модульная DB_PATH, а если
                 и она None, журнал выключен и функция отдаёт []

    Returns:
        Список гипотез (id по возрастанию — порядок, в котором их вернула
        модель). Пустой список при сбое БД, отсутствии запусков или
        выключенном журнале — исключение наружу не летит.
    """
    effective_path = db_path or DB_PATH
    if effective_path is None:
        return []
    try:
        with _connection(effective_path) as conn:
            conn.row_factory = sqlite3.Row
            target_run_id = run_id
            if target_run_id is None:
                latest = conn.execute(
                    "SELECT run_id FROM hypotheses ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if latest is None:
                    return []
                target_run_id = latest["run_id"]
            rows = conn.execute(
                "SELECT * FROM hypotheses WHERE run_id = ? ORDER BY id ASC",
                (target_run_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.error("Ошибка чтения гипотез (run_id=%s): %s", run_id, exc)
        return []
    return [_row_to_hypothesis(row) for row in rows]


def decide_hypothesis(
    hypothesis_id: int,
    decision: str,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    """
    Принимает или отклоняет гипотезу человеком (POST /api/hypotheses/{id}/decision).

    Как и save_stats_snapshot, НЕ глотает исключения SQLite — решение человека
    не должно молча потеряться. Событие decision (actor="human") в общий
    журнал пишет вызывающий код (app.py) после успешного вызова этой функции.

    Args:
        hypothesis_id: id строки hypotheses
        decision:      "accepted" или "rejected"
        db_path:       путь к файлу БД; None — берётся модульная DB_PATH,
                       а если и она None, журнал выключен и функция
                       возвращает None без обращения к БД

    Returns:
        Обновлённая строка гипотезы или None (гипотеза не найдена, БД
        недоступна, журнал выключен).

    Raises:
        ValueError: decision не "accepted" и не "rejected" (ошибка программиста).
    """
    if decision not in ("accepted", "rejected"):
        raise ValueError(f"Неизвестное решение по гипотезе: {decision!r}")

    effective_path = db_path or DB_PATH
    if effective_path is None:
        return None

    with _connection(effective_path) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)
        ).fetchone()
        if existing is None:
            return None
        conn.execute(
            "UPDATE hypotheses SET status = ?, decided_at = ? WHERE id = ?",
            (decision, _now_utc_iso(), hypothesis_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)
        ).fetchone()

    return _row_to_hypothesis(updated) if updated is not None else None
