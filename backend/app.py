"""
FastAPI-сервер для анализа спроса на Авито.

Запуск из корня проекта: python backend/app.py → http://127.0.0.1:7777
"""

import asyncio
import csv
import dataclasses
import io
import logging
import mimetypes
import os
import shutil
import sqlite3
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO, Optional

import uvicorn
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

import agents_registry
import analytics
import address_generator
import cache as cache_mod
import category_profiles
import env_file
import it_outreach
import it_outreach_store
import journal
import outreach
import outreach_state
import outreach_store
import parser as avito_parser
import photo_variation
import preparation
import publish_state
import publisher
import resale_classifier
import resale_scan
import seller_scan
import stats_collector
import strategist
from cities import CITIES, City, get_cities_by_slugs, get_city_by_slug
from filters import SearchFilters
from parser import AvitoBlockedError

# ---------------------------------------------------------------------------
# Корень проекта и рабочая директория.
# Код бэкенда лежит в backend/, но все артефакты (cache.db, logs/, debug/,
# .pw-profile/) и собранный фронтенд (frontend/dist/) находятся в корне проекта.
# Переходим в корень, чтобы относительные пути резолвились одинаково независимо
# от того, откуда запущен сервер.
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)

# ---------------------------------------------------------------------------
# CDP: подключение к Chrome, запущенному пользователем (start-chrome.bat).
# Полноценный парсинг Авито работает ТОЛЬКО через этот реальный браузер —
# свой Playwright-браузер Авито детектит и банит. Переопределяется переменной
# окружения AVITO_CDP_URL; пустая строка → парсер поднимет собственный браузер.
# ---------------------------------------------------------------------------

CDP_URL: Optional[str] = os.environ.get("AVITO_CDP_URL", "http://localhost:9222") or None

# Сколько объявлений парсить на город по умолчанию (150 по ТЗ).
# Для быстрой проверки сайта можно временно уменьшить:
#   PowerShell:  $env:AVITO_MAX_ITEMS = "10"
# Пользователь может задать произвольное количество через форму (диапазон [1, 500]).
try:
    MAX_ITEMS_PER_CITY: int = int(os.environ.get("AVITO_MAX_ITEMS", "150"))
except ValueError:
    MAX_ITEMS_PER_CITY = 150

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

import pathlib as _pathlib

# Создаём папку logs/ если её нет
_logs_dir = _pathlib.Path("logs")
_logs_dir.mkdir(exist_ok=True)

_log_fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_log_datefmt = "%Y-%m-%d %H:%M:%S"
_formatter = logging.Formatter(fmt=_log_fmt, datefmt=_log_datefmt)

# Консольный хэндлер
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_formatter)

# Файловый хэндлер — logs/app.log, utf-8, режим append
_file_handler = logging.FileHandler(
    _logs_dir / "app.log", mode="a", encoding="utf-8"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console_handler, _file_handler],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Приложение FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="Авито — анализ спроса")

# Собранный React-фронтенд (Vite build). Раздаётся через маршруты ниже.
FRONTEND_DIST_DIR = Path("frontend") / "dist"
FRONTEND_ASSETS_DIR = FRONTEND_DIST_DIR / "assets"

# ---------------------------------------------------------------------------
# Хранилище задач в памяти
# Структура: {job_id: {status, done, total, current, results, error, ...}}
# ---------------------------------------------------------------------------

JOBS: dict[str, dict[str, Any]] = {}

# Задачи публикации черновиков — ОТДЕЛЬНЫЙ dict (не смешивать с JOBS аналитики).
# Кэша для publish-задач нет: каждый запуск — новый черновик.
PUBLISH_JOBS: dict[str, dict[str, Any]] = {}

# Сильная ссылка на фоновые publish-задачи нужна и для защиты от двойного resume.
ACTIVE_PUBLISH_TASKS: dict[str, asyncio.Task[Any]] = {}

# Задачи фазы подготовки вариантов (ТЗ §17): prep_id → запись задачи.
PREP_JOBS: dict[str, dict[str, Any]] = {}

# Задачи рассылки продавцам — ОТДЕЛЬНЫЙ dict (не смешивать с JOBS/PUBLISH_JOBS).
# Один job_id — либо задача сбора кандидатов, либо задача отправки; вид
# записан в job["kind"]. Оба вида держат живую вкладку Chrome по CDP, поэтому
# одновременно может выполняться только один такой job (см. _outreach_busy).
OUTREACH_JOBS: dict[str, dict[str, Any]] = {}

# Сильная ссылка на фоновые задачи рассылки. Без неё asyncio может собрать
# задачу сборщиком мусора прямо посреди отправки, оставив продавца в статусе
# "sending" навсегда. Тот же приём, что ACTIVE_PUBLISH_TASKS.
ACTIVE_OUTREACH_TASKS: dict[str, asyncio.Task[Any]] = {}

# Предел длины шаблона письма — maxlength поля ввода в мессенджере Авито
# (<textarea data-marker='reply/input'>), подтверждено живой разведкой
# debug/send_button_map_20260921T203306Z.txt. Превышение браузер обрежет
# молча, сверка обратным чтением в outreach.py не сойдётся, и пакет встанет
# с виду как необъяснимый сбой Авито — поэтому останавливаем на входе.
OUTREACH_MESSAGE_MAX_LENGTH = 1000

# Запуски Сборщика статистики (docs/specs/agents-registry.md, Этап 2):
# run_id → {status, period_days, error, snapshot_id}. Тот же паттерн, что
# JOBS/PREP_JOBS — in-memory dict, задача уходит в фон через asyncio.create_task.
AGENT_RUNS: dict[str, dict[str, Any]] = {}
# Идёт ли сейчас вызов Claude CLI у Стратега. Каждый вызов платный: второй запрос
# (вторая вкладка, повторная отправка) получает 409, а не второй вызов LLM.
_STRATEGIST_RUNNING = False

# Временное хранилище фото для publish-задач: tmp/publish/{job_id}/
TMP_PUBLISH_DIR = Path("tmp") / "publish"

# Дисковый checkpoint задач рассылки (outreach_state.py): tmp/outreach/{job_id}/
TMP_OUTREACH_DIR = Path("tmp") / "outreach"

# Задачи «Разбора продавца» (seller_scan.py) — ОТДЕЛЬНЫЙ dict (не смешивать с
# JOBS/PUBLISH_JOBS/OUTREACH_JOBS). Тоже держит одну вкладку Chrome по CDP —
# см. _seller_scan_busy.
SELLER_JOBS: dict[str, dict[str, Any]] = {}

# Сильная ссылка на фоновые задачи разбора продавца — тот же приём, что
# ACTIVE_OUTREACH_TASKS/ACTIVE_PUBLISH_TASKS (без неё GC может собрать задачу
# посреди сканирования).
ACTIVE_SELLER_SCAN_TASKS: dict[str, asyncio.Task[Any]] = {}

# Предел N объявлений продавца за один разбор.
SELLER_SCAN_MAX_LIMIT = 300

# IT-поиск не использует Chrome, поэтому его задачи не смешиваются с Avito jobs.
IT_OUTREACH_JOBS: dict[str, dict[str, Any]] = {}
ACTIVE_IT_OUTREACH_TASKS: dict[str, asyncio.Task[Any]] = {}
IT_OUTREACH_DB_PATH = str(PROJECT_ROOT / "it_outreach.db")

# Задачи «Поиска под перепродажу» (resale_scan.py, docs/specs/resale-finder.md) —
# ОТДЕЛЬНЫЙ dict (не смешивать с JOBS/PUBLISH_JOBS/OUTREACH_JOBS/SELLER_JOBS).
# Тоже держит одну вкладку Chrome по CDP — см. _seller_scan_busy (учитывает
# и эти задачи, и наоборот: новый разбор продавца/рассылка не стартуют, пока
# идёт resale).
RESALE_JOBS: dict[str, dict[str, Any]] = {}
ACTIVE_RESALE_TASKS: dict[str, asyncio.Task[Any]] = {}

# Границы max_items/threshold_pct из спеки.
RESALE_MAX_ITEMS_MIN, RESALE_MAX_ITEMS_MAX = 20, 300
RESALE_THRESHOLD_PCT_MIN, RESALE_THRESHOLD_PCT_MAX = 5, 80


def _serialize_city(city: City) -> dict[str, Any]:
    """JSON-представление города для frontend bootstrap."""
    return {
        "name": city.name,
        "slug": city.slug,
        "has_metro": city.has_metro,
        "population": city.population,
    }


def _frontend_index_response() -> Response:
    """Отдаёт собранный frontend или понятное сообщение, если build отсутствует."""
    index_path = FRONTEND_DIST_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)

    return HTMLResponse(
        """
        <h2>Frontend build not found</h2>
        <p>Open <code>frontend/</code>, install dependencies and run <code>npm run build</code>.</p>
        """,
        status_code=503,
    )


def _job_export_url(job: dict[str, Any]) -> str:
    """Формирует URL экспорта CSV из данных задачи."""
    params: list[tuple[str, str]] = [
        ("query", job.get("query", "")),
        ("count", str(job.get("count", 150))),
        ("price_min", str(job.get("price_min", ""))),
        ("price_max", str(job.get("price_max", ""))),
    ]

    for slug in job.get("selected_slugs", []):
        params.append(("cities", slug))
    for gender_value in job.get("selected_gender", []):
        params.append(("gender", gender_value))

    return f"/export.csv?{urllib.parse.urlencode(params)}"


def _serialize_job_status(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """JSON-представление статуса задачи."""
    return {
        "job_id": job_id,
        "status": job["status"],
        "done": job["done"],
        "total": job["total"],
        "current": job["current"],
        "error": job["error"],
    }


def _serialize_job_results(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """JSON-представление результатов задачи."""
    return {
        **_serialize_job_status(job_id, job),
        "query": job.get("query", ""),
        "count": job.get("count", 150),
        "price_desc": job.get("price_desc", ""),
        "price_min": job.get("price_min", ""),
        "price_max": job.get("price_max", ""),
        "cities_count": job.get("cities_count", 0),
        "selected_slugs": job.get("selected_slugs", []),
        "selected_gender": job.get("selected_gender", []),
        "results": job.get("results") or [],
        "export_url": _job_export_url(job),
    }


def _parse_count(raw_count: Any) -> int:
    """Безопасно приводит count к int и зажимает в диапазон [1, 500]."""
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        count = 150

    return max(1, min(500, count))


def _build_cities_key(slugs: list[str]) -> str:
    """Канонический ключ кэша по городам: валидация slug'ов, дедупликация и
    сортировка через справочник — единый для поиска и export.csv."""
    valid = [s for s in slugs if get_city_by_slug(s) is not None]
    cities = get_cities_by_slugs(valid)
    return ",".join(sorted(city.slug for city in cities))


def _normalize_search_payload(
    query: str,
    count: Any,
    price_min: Any,
    price_max: Any,
    cities: list[str],
    gender: list[str],
) -> Optional[dict[str, Any]]:
    """Нормализует данные поиска для form- и JSON-маршрутов."""
    clean_query = (query or "").strip()
    if not clean_query:
        return None

    safe_count = _parse_count(count)
    valid_gender = [value for value in gender if value in ("male", "female")]
    gender_value = valid_gender[0] if len(valid_gender) == 1 else None

    filters = SearchFilters.from_form(
        "" if price_min is None else str(price_min),
        "" if price_max is None else str(price_max),
        gender_value,
    )

    valid_slugs = [slug for slug in cities if get_city_by_slug(slug) is not None]
    if not valid_slugs:
        return None

    selected_cities = get_cities_by_slugs(valid_slugs)
    cities_key = _build_cities_key(cities)

    return {
        "query": clean_query,
        "count": safe_count,
        "filters": filters,
        "price_desc": filters.describe(),
        "price_min": filters.price_min if filters.price_min is not None else "",
        "price_max": filters.price_max if filters.price_max is not None else "",
        "selected_cities": selected_cities,
        "cities_key": cities_key,
        "selected_gender": [gender_value] if gender_value else [],
    }


def _create_done_job(search_data: dict[str, Any], cached_results: list[dict]) -> str:
    """Создаёт задачу из кэша и помечает её завершённой."""
    job_id = str(uuid.uuid4())
    selected_cities = search_data["selected_cities"]

    JOBS[job_id] = {
        "status": "done",
        "done": len(selected_cities),
        "total": len(selected_cities),
        "current": "",
        "results": cached_results,
        "error": None,
        "query": search_data["query"],
        "count": search_data["count"],
        "price_desc": search_data["price_desc"],
        "price_min": search_data["price_min"],
        "price_max": search_data["price_max"],
        "cities_count": len(selected_cities),
        "selected_slugs": [city.slug for city in selected_cities],
        "selected_gender": search_data["selected_gender"],
    }
    return job_id


def _create_running_job(search_data: dict[str, Any]) -> str:
    """Создаёт фоновой job и запускает парсинг."""
    job_id = str(uuid.uuid4())
    selected_cities = search_data["selected_cities"]

    JOBS[job_id] = {
        "status": "running",
        "done": 0,
        "total": len(selected_cities),
        "current": selected_cities[0].name if selected_cities else "",
        "results": None,
        "error": None,
        "query": search_data["query"],
        "count": search_data["count"],
        "price_desc": search_data["price_desc"],
        "price_min": search_data["price_min"],
        "price_max": search_data["price_max"],
        "cities_count": len(selected_cities),
        "selected_slugs": [city.slug for city in selected_cities],
        "selected_gender": search_data["selected_gender"],
    }

    asyncio.create_task(
        run_job(
            job_id,
            search_data["query"],
            search_data["count"],
            search_data["filters"],
            selected_cities,
        )
    )
    return job_id


def _log_scout_run_started(job_id: str, search_data: dict[str, Any]) -> None:
    """Событие журнала: Разведчик приступил к задаче job_id (кэш или живой прогон)."""
    journal.log_event(
        "scout",
        "run_started",
        "Разведчик запущен",
        run_id=job_id,
        payload={
            "query": search_data["query"],
            "cities": len(search_data["selected_cities"]),
        },
    )


def _prepare_search_job(search_data: dict[str, Any]) -> tuple[str, bool]:
    """Создаёт задачу из кэша или запускает новую."""
    filters_key = search_data["filters"].cache_key_part()
    cached = cache_mod.get_result(
        search_data["query"],
        search_data["count"],
        filters_key=filters_key,
        cities_key=search_data["cities_key"],
    )
    if cached is not None:
        job_id = _create_done_job(search_data, cached)
        _log_scout_run_started(job_id, search_data)
        cache_key = cache_mod.build_cache_key(
            search_data["query"],
            search_data["count"],
            filters_key=filters_key,
            cities_key=search_data["cities_key"],
        )
        journal.log_event(
            "scout",
            "artifact",
            "Отчёт спроса взят из кэша",
            run_id=job_id,
            tool="sqlite_cache",
            payload={"cache_key": cache_key},
        )
        journal.log_event(
            "scout",
            "run_finished",
            "Разведчик отдал отчёт из кэша",
            run_id=job_id,
            payload={
                "query": search_data["query"],
                "cities": len(search_data["selected_cities"]),
                "cached": True,
            },
        )
        return job_id, True

    job_id = _create_running_job(search_data)
    _log_scout_run_started(job_id, search_data)
    return job_id, False


# ---------------------------------------------------------------------------
# Инициализация при старте
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup() -> None:
    """Инициализируем кэш, журнал агентов и возвращаем незавершённые publish-задачи с диска."""
    cache_mod.init_db()
    logger.info("SQLite-кэш инициализирован")
    outreach_store.init_db()
    logger.info("История рассылки инициализирована")
    try:
        journal.init_db()
        logger.info("Журнал агентов инициализирован")
    except sqlite3.Error as exc:
        logger.error(
            "Журнал агентов не инициализирован (%s) — сервер продолжает без него", exc,
        )
    recovered = publish_state.recover_publish_states(TMP_PUBLISH_DIR)
    restored_preps = 0
    for job_id, (job, _draft) in recovered.items():
        PUBLISH_JOBS[job_id] = job
        # Publish-задача может ссылаться на prep_id — восстанавливаем и запись
        # PREP_JOBS (иначе фронт при resume не найдёт превью вариантов).
        # recover_prep_job сам решает: если подготовка успела полностью
        # завершиться — просто собирает запись; если её прервало посреди
        # генерации — дописывает недостающее тем же сидом (детерминировано,
        # см. preparation._write_seed_info). None — только если на диске нет
        # даже сида/исходников для этого (совсем ранний крах или legacy-
        # каталог без seed.json).
        prep_id = job.get("prep_id")
        # Завершённому пакету (done с пропусками) варианты больше не нужны:
        # их можно было удалить после done — не ищем и не пишем предупреждение.
        if prep_id and prep_id not in PREP_JOBS and job.get("status") != "done":
            prep_dir = TMP_PUBLISH_DIR / f"prep_{prep_id}"
            restored_prep = preparation.recover_prep_job(prep_dir)
            if restored_prep is not None:
                PREP_JOBS[prep_id] = restored_prep
                restored_preps += 1
            else:
                logger.warning(
                    "Prep %s задачи %s не восстановлен: на диске нет ни "
                    "полного набора черновиков, ни сохранённого сида для "
                    "детерминированной дозаписи",
                    prep_id, job_id,
                )
    if recovered:
        logger.warning(
            "Восстановлено незавершённых publish-задач: %d",
            len(recovered),
        )
    if restored_preps:
        logger.info("Восстановлено записей PREP_JOBS: %d", restored_preps)


# ---------------------------------------------------------------------------
# Фоновая задача: запускает парсер и сохраняет результаты в JOBS
# ---------------------------------------------------------------------------

async def run_job(
    job_id: str,
    query: str,
    count: int,
    filters: SearchFilters,
    cities: list[City],
) -> None:
    """
    Запускает парсинг выбранных городов, считает метрики и сохраняет результат
    в JOBS[job_id] и в SQLite-кэш.

    Args:
        job_id:   идентификатор задачи в JOBS
        query:    поисковый запрос пользователя
        count:    количество объявлений на город (1–500)
        filters:  фильтры поиска (цена и др.)
        cities:   список городов для парсинга (подмножество CITIES)
    """
    job = JOBS[job_id]

    def progress_cb(done: int, total: int, city_name: str) -> None:
        """Callback из парсера — обновляем состояние задачи."""
        job["done"] = done
        job["total"] = total
        job["current"] = city_name
        logger.info("Прогресс: %d/%d — %s", done, total, city_name)

    try:
        # Запускаем парсинг с выбранным количеством объявлений, фильтрами и городами
        raw_results: dict[str, list[dict]] = await avito_parser.parse_all(
            query,
            progress_cb=progress_cb,
            headless=False,
            cdp_url=CDP_URL,
            max_items=count,
            filters=filters,
            cities=cities,
        )

        # Считаем метрики только по выбранным городам; знаменатель = число местных объявлений
        city_results: list[dict] = []
        for city in cities:
            listings = raw_results.get(city.slug, [])
            try:
                result = analytics.compute_city_result(city, listings)
                city_results.append(result)
            except Exception as exc:
                logger.error(
                    "Ошибка при расчёте метрик для %s: %s", city.name, exc
                )

        # Сортируем по убыванию среднего просмотров/день
        city_results.sort(key=lambda x: x["avg_views_today"], reverse=True)

        # Формируем cities_key для изоляции кэша по набору городов
        cities_key = ",".join(sorted(c.slug for c in cities))

        # Сохраняем в кэш (ключ учитывает count, фильтры и набор городов) и в состояние задачи
        cache_mod.save_result(
            query, city_results, count,
            filters_key=filters.cache_key_part(),
            cities_key=cities_key,
        )
        job["results"] = city_results
        job["status"] = "done"
        job["done"] = job["total"]
        logger.info("Задача %s завершена. Городов: %d", job_id, len(city_results))

        cache_key = cache_mod.build_cache_key(
            query, count,
            filters_key=filters.cache_key_part(),
            cities_key=cities_key,
        )
        journal.log_event(
            "scout", "artifact", "Отчёт спроса сохранён в кэш",
            run_id=job_id, tool="sqlite_cache",
            payload={"cache_key": cache_key},
        )
        journal.log_event(
            "scout", "run_finished", "Разведчик завершил работу",
            run_id=job_id,
            payload={"query": query, "cities": len(cities), "cached": False},
        )

    except AvitoBlockedError as exc:
        logger.error("Блокировка Авито при выполнении задачи %s: %s", job_id, exc)
        job["status"] = "blocked"
        job["error"] = (
            "Авито заблокировал запрос. Попробуйте включить VPN и повторить поиск."
        )
        journal.log_event(
            "scout", "run_failed", "Авито заблокировал запрос разведки",
            run_id=job_id,
            payload={"query": query, "error": str(exc)},
        )

    except Exception as exc:
        logger.error("Непредвиденная ошибка задачи %s: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = f"Ошибка парсинга: {exc}"
        journal.log_event(
            "scout", "run_failed", "Ошибка разведки",
            run_id=job_id,
            payload={"query": query, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index() -> Response:
    """Главная страница с формой поиска. Передаёт список городов для выбора."""
    return _frontend_index_response()


@app.get("/it-outreach", response_class=HTMLResponse)
async def it_outreach_page() -> Response:
    """Переводит прямой URL в hash-маршрут React-приложения."""
    return RedirectResponse(url="/#/it-outreach", status_code=307)


@app.get("/assets/{asset_path:path}")
async def frontend_assets(asset_path: str) -> Response:
    """Раздаёт собранные Vite-ассеты."""
    file_path = FRONTEND_ASSETS_DIR / asset_path
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)

    return HTMLResponse("<h2>Asset not found</h2>", status_code=404)


@app.get("/api/bootstrap")
async def api_bootstrap() -> JSONResponse:
    """Возвращает начальные данные frontend."""
    return JSONResponse(
        {
            "app_name": "Avito Research",
            "cities": [_serialize_city(city) for city in CITIES],
            "defaults": {
                "count": 150,
                "selected_city_slugs": [city.slug for city in CITIES[:10]],
            },
            "limits": {
                "count_min": 1,
                "count_max": 500,
            },
        }
    )


@app.get("/api/agents")
async def api_agents() -> JSONResponse:
    """Реестр агентов для страницы «Агенты» — фронт ничего не хардкодит."""
    return JSONResponse(
        [dataclasses.asdict(agent) for agent in agents_registry.list_agents()]
    )


@app.get("/api/journal")
async def api_journal(
    actor: Optional[str] = None,
    limit: int = 100,
) -> JSONResponse:
    """События общего журнала, новые сверху; опционально фильтр по агенту."""
    return JSONResponse(journal.list_events(actor=actor, limit=limit))


# ---------------------------------------------------------------------------
# Сборщик статистики (docs/specs/agents-registry.md, Этап 2)
# ---------------------------------------------------------------------------

async def _run_stats_collector_job(run_id: str, period_days: int) -> None:
    """
    Фоновая задача Сборщика статистики: run_id → AGENT_RUNS[run_id].

    stats_collector.run() блокирующий (urllib + sqlite через journal.py) —
    запускается через asyncio.to_thread, иначе event loop замрёт на время
    HTTP-запросов к Авито.
    """
    run = AGENT_RUNS[run_id]
    run["status"] = "running"
    journal.log_event(
        "stats_collector", "run_started", "Сборщик статистики запущен",
        run_id=run_id, payload={"period_days": period_days},
    )
    try:
        snapshot_id = await asyncio.to_thread(stats_collector.run, period_days)
        snapshot = journal.get_stats_snapshot(snapshot_id)
        items_count = len(snapshot["data"].get("items", [])) if snapshot else 0

        run["status"] = "done"
        run["snapshot_id"] = snapshot_id

        journal.log_event(
            "stats_collector", "artifact", "Сборщик статистики собрал снимок",
            run_id=run_id, tool="avito_api",
            payload={"snapshot_id": snapshot_id, "items": items_count},
        )
        journal.log_event(
            "stats_collector", "run_finished", "Сборщик статистики завершил работу",
            run_id=run_id,
            payload={"snapshot_id": snapshot_id, "items": items_count, "period_days": period_days},
        )
    except Exception as exc:
        logger.error("Сборщик статистики: запуск %s не удался: %s", run_id, exc)
        run["status"] = "error"
        run["error"] = str(exc)
        journal.log_event(
            "stats_collector", "run_failed", "Сборщик статистики не смог собрать снимок",
            run_id=run_id, payload={"period_days": period_days, "error": str(exc)},
        )


@app.post("/api/agents/stats_collector/run")
async def api_stats_collector_run(request: Request) -> JSONResponse:
    """Запускает Сборщика статистики за период 7 или 30 дней. Тело: {period_days}."""
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        payload = {}

    period_days = payload.get("period_days") if isinstance(payload, dict) else None
    if period_days not in (7, 30):
        return JSONResponse(
            {"errors": [{"field": "period_days", "error": "period_days должен быть 7 или 30"}]},
            status_code=422,
        )

    run_id = str(uuid.uuid4())
    AGENT_RUNS[run_id] = {
        "status": "queued",
        "period_days": period_days,
        "error": None,
        "snapshot_id": None,
    }
    asyncio.create_task(_run_stats_collector_job(run_id, period_days))

    logger.info("Сборщик статистики: запуск %s создан (period_days=%d)", run_id, period_days)
    return JSONResponse({"run_id": run_id})


@app.get("/api/agents/runs/{run_id}")
async def api_agent_run_status(run_id: str) -> JSONResponse:
    """Статус запуска агента (пока только Сборщик статистики)."""
    if run_id not in AGENT_RUNS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    run = AGENT_RUNS[run_id]
    return JSONResponse({
        "run_id": run_id,
        "status": run["status"],
        "period_days": run["period_days"],
        "error": run["error"],
        "snapshot_id": run["snapshot_id"],
    })


@app.get("/api/stats/latest")
async def api_stats_latest() -> JSONResponse:
    """Последний снимок статистики Сборщика, если он уже был."""
    snapshot = journal.get_latest_stats_snapshot()
    if snapshot is None:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(snapshot)


# ---------------------------------------------------------------------------
# Стратег (docs/specs/agents-registry.md, Этап 3)
# ---------------------------------------------------------------------------

def _dump_strategist_raw_response(run_id: str, raw: str) -> Optional[str]:
    """
    Сохраняет сырой ответ CLI при невалидном JSON — «Решения, которых нет в
    спеке» брифа 06: невалидный ответ не должен пропасть, его нужно видеть
    при разборе, что вернула модель. Путь — относительно PROJECT_ROOT.
    """
    path = _logs_dir / f"strategist-{run_id}.txt"
    try:
        path.write_text(raw, encoding="utf-8")
        return str(path)
    except OSError as exc:
        logger.error("Не удалось сохранить сырой ответ Стратега %s: %s", run_id, exc)
        return None


@app.get("/api/scout/reports")
async def api_scout_reports() -> JSONResponse:
    """Отчёты Разведчика из кэша (включая устаревшие по TTL) для выпадающего
    списка на карточке Стратега."""
    return JSONResponse(cache_mod.list_entries())


@app.post("/api/agents/strategist/run")
async def api_strategist_run(request: Request) -> JSONResponse:
    """
    Запускает Стратега: факты (снимок + отчёт Разведчика) → Claude CLI →
    гипотезы. Синхронный ответ (до CLI_TIMEOUT_S=180 с) — единственный вызов
    LLM за запуск, без ретраев, поэтому промежуточный статус опроса не нужен.
    Тело: {scout_report_key}. Берёт последний снимок статистики Сборщика.
    """
    try:
        payload = await request.json()
    except ValueError:
        payload = {}

    scout_report_key = (payload.get("scout_report_key") if isinstance(payload, dict) else None) or ""
    if not scout_report_key:
        return JSONResponse(
            {"errors": [{"field": "scout_report_key", "error": "Не выбран отчёт Разведчика"}]},
            status_code=422,
        )

    scout_report = cache_mod.get_entry_by_key(scout_report_key)
    if scout_report is None:
        return JSONResponse(
            {"errors": [{"field": "scout_report_key", "error": "Отчёт Разведчика не найден"}]},
            status_code=404,
        )

    snapshot = journal.get_latest_stats_snapshot()
    if snapshot is None:
        return JSONResponse(
            {"error": "Сначала нужен снимок статистики — запустите Сборщика статистики"},
            status_code=409,
        )

    global _STRATEGIST_RUNNING
    # Проверка и установка флага идут без await между ними — в одном event loop
    # это атомарно, два параллельных запроса не проскочат оба.
    if _STRATEGIST_RUNNING:
        return JSONResponse(
            {"error": "Стратег уже запущен — дождитесь ответа текущего запуска"},
            status_code=409,
        )
    _STRATEGIST_RUNNING = True
    try:
        return await _run_strategist(scout_report_key, scout_report, snapshot)
    finally:
        _STRATEGIST_RUNNING = False


async def _run_strategist(
    scout_report_key: str,
    scout_report: dict[str, Any],
    snapshot: dict[str, Any],
) -> JSONResponse:
    """Один запуск Стратега: события журнала, единственный вызов CLI, сохранение гипотез."""
    run_id = str(uuid.uuid4())
    journal.log_event(
        "strategist", "run_started", "Стратег запущен",
        run_id=run_id,
        payload={"scout_report_key": scout_report_key, "snapshot_id": snapshot["id"]},
    )
    journal.log_event(
        "strategist", "handoff", "Стратег получил отчёт Разведчика",
        run_id=run_id, payload={"from": "scout", "scout_report_key": scout_report_key},
    )
    journal.log_event(
        "strategist", "handoff", "Стратег получил снимок статистики",
        run_id=run_id, payload={"from": "stats_collector", "snapshot_id": snapshot["id"]},
    )

    facts = strategist.build_facts(snapshot["data"], scout_report)
    prompt = strategist.build_prompt(facts)

    try:
        raw = await asyncio.to_thread(strategist.call_claude_cli, prompt)
    except strategist.StrategistError as exc:
        logger.error("Стратег: запуск %s не удался (CLI): %s", run_id, exc)
        journal.log_event(
            "strategist", "run_failed", "Стратег не смог получить ответ Claude CLI",
            run_id=run_id, payload={"error": str(exc)},
        )
        return JSONResponse({"run_id": run_id, "status": "error", "error": str(exc)}, status_code=502)

    try:
        hypotheses = strategist.parse_hypotheses(raw)
    except strategist.StrategistError as exc:
        dump_path = _dump_strategist_raw_response(run_id, raw)
        logger.error(
            "Стратег: запуск %s не удался (разбор ответа): %s (сырой ответ: %s)",
            run_id, exc, dump_path,
        )
        journal.log_event(
            "strategist", "run_failed", "Стратег получил невалидный ответ от Claude CLI",
            run_id=run_id, payload={"error": str(exc), "raw_response_file": dump_path},
        )
        return JSONResponse({"run_id": run_id, "status": "error", "error": str(exc)}, status_code=502)

    try:
        journal.save_hypotheses(
            run_id,
            snapshot_id=snapshot["id"],
            scout_report_key=scout_report_key,
            hypotheses=hypotheses,
        )
    except sqlite3.Error as exc:
        logger.error("Стратег: запуск %s — гипотезы не сохранены: %s", run_id, exc)
        journal.log_event(
            "strategist", "run_failed", "Стратег не смог сохранить гипотезы",
            run_id=run_id, payload={"error": str(exc)},
        )
        return JSONResponse({"run_id": run_id, "status": "error", "error": str(exc)}, status_code=500)

    journal.log_event(
        "strategist", "artifact", "Стратег сформулировал гипотезы",
        run_id=run_id, tool="llm_claude_cli", payload={"count": len(hypotheses)},
    )
    journal.log_event(
        "strategist", "run_finished", "Стратег завершил работу",
        run_id=run_id, payload={"count": len(hypotheses)},
    )

    return JSONResponse({
        "run_id": run_id,
        "status": "done",
        "hypotheses": journal.list_hypotheses(run_id=run_id),
    })


@app.get("/api/hypotheses")
async def api_hypotheses() -> JSONResponse:
    """Гипотезы последнего запуска Стратега."""
    return JSONResponse(journal.list_hypotheses())


@app.post("/api/hypotheses/{hypothesis_id}/decision")
async def api_hypothesis_decision(hypothesis_id: int, request: Request) -> JSONResponse:
    """Человек принимает или отклоняет гипотезу. Тело: {decision}. Никаких
    действий на Авито — меняется только hypotheses и журнал (actor="human")."""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}

    decision = payload.get("decision") if isinstance(payload, dict) else None
    if decision not in ("accepted", "rejected"):
        return JSONResponse(
            {"errors": [{"field": "decision", "error": "decision должен быть accepted или rejected"}]},
            status_code=422,
        )

    updated = journal.decide_hypothesis(hypothesis_id, decision)
    if updated is None:
        return JSONResponse({"status": "not_found"}, status_code=404)

    journal.log_event(
        "human", "decision", f"Гипотеза №{hypothesis_id} ({decision}): {updated['change']}",
        run_id=updated["run_id"],
        payload={"hypothesis_id": hypothesis_id, "decision": decision},
    )
    return JSONResponse(updated)


@app.post("/api/search")
async def api_search(request: Request) -> JSONResponse:
    """JSON-версия запуска поиска для React frontend."""
    payload = await request.json()
    raw_cities = payload.get("cities") or []
    raw_gender = payload.get("gender") or []

    search_data = _normalize_search_payload(
        str(payload.get("query", "")),
        payload.get("count", 150),
        payload.get("price_min", ""),
        payload.get("price_max", ""),
        [str(slug) for slug in raw_cities],
        [str(value) for value in raw_gender],
    )
    if search_data is None:
        return JSONResponse(
            {"error": "Некорректный запрос поиска или не выбраны города."},
            status_code=400,
        )

    job_id, cached = _prepare_search_job(search_data)
    job = JOBS[job_id]

    logger.info(
        "API-задача %s для '%s' (cached=%s, cities=%s)",
        job_id,
        search_data["query"],
        cached,
        search_data["cities_key"],
    )
    return JSONResponse(
        {
            **_serialize_job_status(job_id, job),
            "cached": cached,
            "results_url": f"/api/results/{job_id}",
        }
    )


@app.get("/api/status/{job_id}")
async def api_job_status(job_id: str) -> JSONResponse:
    """JSON-статус задачи для опроса из frontend."""
    if job_id not in JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = JOBS[job_id]
    return JSONResponse(_serialize_job_status(job_id, job))


@app.get("/api/results/{job_id}")
async def api_results(job_id: str) -> JSONResponse:
    """JSON-результаты задачи для frontend."""
    if job_id not in JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = JOBS[job_id]
    return JSONResponse(_serialize_job_results(job_id, job))


@app.get("/export.csv")
async def export_csv(
    query: str = "",
    count: int = 150,
    price_min: str = "",
    price_max: str = "",
    cities: list[str] = Query([]),
    gender: list[str] = Query([]),
) -> StreamingResponse:
    """
    Экспорт результатов в CSV (UTF-8 с BOM для корректного открытия в Excel).

    Колонки: Город, Адрес/метро, Среднее просм/день, Топ-1, Топ-2, Топ-3.
    Для МСК/СПб добавляются строки по каждой станции метро.

    Args:
        query:     поисковый запрос
        count:     количество объявлений на город (используется как ключ кэша)
        price_min: минимальная цена (сырая строка; нормализуется для ключа кэша)
        price_max: максимальная цена (сырая строка; нормализуется для ключа кэша)
        cities:    список slug'ов выбранных городов (повторяющийся query-параметр)
        gender:    список значений чекбоксов пола ("male"/"female"); 0 или 2 → без фильтра
    """
    query = query.strip()

    # Разбираем параметр пола: ровно 1 значение → фильтр; 0 или 2 → без фильтра
    valid_g = [g for g in gender if g in ("male", "female")]
    gender_val = valid_g[0] if len(valid_g) == 1 else None

    # Нормализуем фильтры (цена + пол) — они влияют на ключ кэша
    filters = SearchFilters.from_form(price_min, price_max, gender_val)

    # Валидируем и разрешаем города (как в /results)
    cities_key = _build_cities_key(cities)

    results: Optional[list] = (
        cache_mod.get_result(
            query, count,
            filters_key=filters.cache_key_part(),
            cities_key=cities_key,
        )
        if query else None
    )

    output = io.StringIO()
    # BOM добавляется при encode("utf-8-sig") ниже — здесь не нужен
    writer = csv.writer(output, dialect="excel")

    # Заголовок
    writer.writerow([
        "Город", "Адрес/метро", "Среднее просм/день",
        "Топ-1 (просм — ссылка)", "Топ-2 (просм — ссылка)", "Топ-3 (просм — ссылка)",
    ])

    if results:
        for city_result in results:
            city_name: str = city_result.get("city_name", "")
            avg: float = city_result.get("avg_views_today", 0.0)
            top3: list = city_result.get("top3") or []

            # Формируем топ-ячейки: «N просм — https://...»
            top_cells = _format_top3_cells(top3)

            # Основная строка города
            local_count = city_result.get("local_count", 0)
            writer.writerow([city_name, f"Весь город ({local_count} местных)", avg] + top_cells)

            # Строки по станциям метро (МСК/СПб)
            metro_bd: Optional[list] = city_result.get("metro_breakdown")
            if metro_bd:
                for station in metro_bd:
                    metro_name: str = station.get("metro", "")
                    metro_avg: float = station.get("avg_views_today", 0.0)
                    metro_top3: list = station.get("top3") or []
                    metro_cells = _format_top3_cells(metro_top3)
                    writer.writerow(
                        [city_name, f"м. {metro_name}", metro_avg] + metro_cells
                    )
    else:
        writer.writerow(["Нет данных — сначала выполните поиск", "", "", "", "", ""])

    content = output.getvalue()

    # Формируем имя файла.
    # Content-Disposition должен быть latin-1-кодируемым, поэтому:
    # - ASCII-запасное имя (только ASCII-символы)
    # - RFC 5987 filename* для юникодного имени (поддерживается всеми современными браузерами)
    safe_query = query.replace(" ", "_")[:40] if query else "result"
    # ASCII-fallback: убираем всё, что не ASCII
    ascii_name = "".join(c if ord(c) < 128 else "_" for c in safe_query)
    filename_ascii = f"avito_{ascii_name}.csv"
    # RFC 5987: percent-encode UTF-8 байты
    filename_utf8_encoded = urllib.parse.quote(f"avito_{safe_query}.csv", safe="")
    content_disposition = (
        f'attachment; filename="{filename_ascii}"; '
        f"filename*=UTF-8''{filename_utf8_encoded}"
    )

    return StreamingResponse(
        iter([content.encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition,
        },
    )


def _format_top3_cells(top3: list) -> list[str]:
    """
    Преобразует список топ-3 в три строки вида «N просм — https://...».
    Возвращает ровно 3 элемента (пустые строки для отсутствующих позиций).
    """
    cells: list[str] = []
    for item in top3[:3]:
        views: Optional[int] = item.get("views_today")
        url: str = item.get("url") or ""
        views_str = f"{views} просм" if views is not None else "? просм"
        cells.append(f"{views_str} — {url}" if url else views_str)

    # Добиваем до 3 пустых ячеек если топов меньше
    while len(cells) < 3:
        cells.append("")

    return cells


# ---------------------------------------------------------------------------
# Полная публикация объявлений
# ---------------------------------------------------------------------------

_PUBLISH_RESUMABLE_STATUSES = frozenset({"failed", "needs_user_action", "interrupted"})


def _active_publish_job(except_job_id: Optional[str] = None) -> Optional[str]:
    """Единый gate для Chrome: reservation и ещё завершающаяся task считаются живыми."""
    for job_id, job in PUBLISH_JOBS.items():
        if job_id != except_job_id and job.get("status") in {"queued", "running"}:
            return job_id
    for job_id, task in ACTIVE_PUBLISH_TASKS.items():
        if job_id != except_job_id and not task.done():
            return job_id
    return None


def _publish_job_records(*, include_disk: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """Диск — источник финального статуса; неизвестный прогресс блокируем консервативно."""
    if not include_disk:
        return list(PUBLISH_JOBS.items())
    disk_jobs = publish_state.load_publish_jobs(TMP_PUBLISH_DIR)
    records = list(disk_jobs.items())
    for job_id, job in PUBLISH_JOBS.items():
        saved = disk_jobs.get(job_id)
        if saved is None or (
            saved.get("status") not in {"done", "closed"}
            and job.get("status") not in {"done", "closed"}
        ):
            records.append((job_id, job))
    return records


def _start_publish_conflict(
    prep_id: str, *, except_job_id: Optional[str] = None, include_disk: bool = False,
) -> Optional[str]:
    active = _active_publish_job(except_job_id)
    if active:
        return active
    for job_id, job in _publish_job_records(include_disk=include_disk):
        if job_id == except_job_id:
            continue
        if job.get("status") in {"queued", "running"}:
            return job_id
        if job.get("status") in {"done", "closed"}:
            continue
        if prep_id and job.get("prep_id") == prep_id:
            return job_id
        try:
            published = int(job.get("items_published") or 0)
        except (TypeError, ValueError):
            return job_id
        step = job.get("step")
        if not isinstance(step, (str, type(None))):
            return job_id
        if (
            published > 0
            or step in publish_state.FINANCIAL_STEPS
            or job.get("money_click") == "clicked"
        ):
            return job_id
    return None


def _publish_busy_response(job_id: str, *, pid: Optional[int] = None) -> JSONResponse:
    reason = f"Задача публикации {job_id} уже занимает Chrome"
    if pid is not None:
        reason += f" (PID {pid})"
    return JSONResponse({"error": reason, "job_id": job_id}, status_code=409)


def _publish_resume_view(
    job_id: str, job: dict[str, Any],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(план, причина его отсутствия) для /status — строго по checkpoint на диске.

    Один источник правды с /resume (F06): память может разойтись с диском,
    если запись checkpoint не удалась, а продолжать будет именно диск.
    Диск читается только для терминальных статусов — активная задача
    возобновления не предлагает, опрос статуса не трогает файл.
    """
    if job.get("status") not in _PUBLISH_RESUMABLE_STATUSES:
        return None, None
    # F30: статус в job уже терминальный, но фоновая задача ещё не доделала
    # уборку (закрытие вкладки, финальная запись) — предлагать «Продолжить»
    # рано: клик в это окно получил бы 409 «уже занимает Chrome».
    task = ACTIVE_PUBLISH_TASKS.get(job_id)
    if task is not None and not task.done():
        return None, (
            "Задача ещё завершается — подождите несколько секунд и обновите страницу."
        )
    # F08: шаг явно помечен как неповторяемый (например, минимум цены Авито
    # выше потолка уже создал объявление без оплаты). Предлагать «Продолжить
    # со следующего» отсюда нельзя: тот же потолок с высокой вероятностью
    # застрянет так же на следующем городе, и брошенные объявления пойдут
    # по кругу. Пользователь решает сам — поднимает потолок и закрывает
    # объявление вручную, либо закрывает задачу.
    user_action = job.get("user_action")
    if isinstance(user_action, dict) and user_action.get("resumable") is False:
        item_index = user_action.get("item_index")
        stuck = f"объявление №{item_index}" if item_index is not None else "созданное объявление"
        return None, (
            f"Автоматика не предложит «Продолжить»: {stuck} уже создано на Авито, "
            "но не оплачено, а повтор с тем же потолком цены может застрять так же "
            "на следующем городе. Поднимите потолок и завершите объявление вручную "
            "в кабинете Авито, либо закройте задачу."
        )
    try:
        saved_job, _draft = publish_state.load_publish_state(TMP_PUBLISH_DIR / job_id)
    except Exception as exc:  # noqa: BLE001 — любой сбой чтения = плана нет
        logger.warning("Статус %s: checkpoint не читается: %s", job_id, exc)
        return None, (
            "Сохранённое состояние задачи не читается с диска — автоматическое "
            "продолжение недоступно. Проверьте кабинет Авито вручную."
        )
    plan = publish_state.resume_plan(saved_job)
    if plan is None:
        return None, publish_state.resume_unavailable_reason(saved_job)
    return plan, None


def _serialize_skipped_items(raw: Any) -> list[Any]:
    """skipped_items для /status — всегда записями (старые номера приводятся).

    Запись: item_index, item_id, item_url, step, city, address; неизвестное —
    null. Нераспознанный список отдаётся как есть: угадывать номер нельзя.
    """
    try:
        return publish_state.normalize_skipped_items(raw)
    except ValueError:
        return list(raw) if isinstance(raw, (list, tuple)) else []


def _serialize_publish_status(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """
    JSON-статус publish-задачи с прогрессом переданных Авито объявлений.

    Новые item-поля — источник правды. Старые draft-поля возвращаются как
    вычисленные алиасы, чтобы не ломать старый frontend во время обновления.
    step/step_label/done/total описывают ТЕКУЩЕЕ объявление.
    debug_dir — относительный путь дампа сбоя «debug/publish/<job_id>» или None.
    Дефолты всех ключей задаются ОДИН раз при создании задачи в api_publish_start.
    """
    items_total = (
        job.get("items_total")
        if "items_total" in job
        else job.get("drafts_total")
    )
    item_index = (
        job.get("item_index")
        if "item_index" in job
        else job.get("draft_index")
    )
    items_published = (
        job.get("items_published")
        if "items_published" in job
        else job.get("drafts_saved")
    )
    raw_urls = (
        job.get("published_urls")
        if "published_urls" in job
        else job.get("saved_urls")
    )
    published_urls = list(raw_urls or [])

    resume_plan, resume_unavailable_reason = _publish_resume_view(job_id, job)
    resume_available = resume_plan is not None

    created_at = job.get("created_at")
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            raise ValueError("Время без часового пояса")
        is_older_than_24h = datetime.now(timezone.utc) - created >= timedelta(hours=24)
    except (TypeError, ValueError):
        created_at = None
        is_older_than_24h = True

    return {
        "job_id": job_id,
        "created_at": created_at,
        "is_older_than_24h": is_older_than_24h,
        "status": job.get("status"),
        "step": job.get("step"),
        "step_label": job.get("step_label"),
        "done": job.get("done"),
        "total": job.get("total"),
        "error": job.get("error"),
        "debug_dir": job.get("debug_dir"),
        "item_index": item_index,
        "items_total": items_total,
        "items_published": items_published,
        "published_urls": published_urls,
        "brand_selected": job.get("brand_selected"),
        "applied_view_prices": list(job.get("applied_view_prices") or []),
        "address_warnings": list(job.get("address_warnings") or []),
        "user_action": job.get("user_action"),
        "resume_available": resume_available,
        "resume_plan": resume_plan,
        "resume_unavailable_reason": resume_unavailable_reason,
        "skipped_items": _serialize_skipped_items(job.get("skipped_items")),
        # Временные legacy-алиасы, всегда вычисленные из новых полей.
        "draft_index": item_index,
        "drafts_total": items_total,
        "drafts_saved": items_published,
        "saved_urls": list(published_urls),
    }


async def _run_prep_job_with_journal(
    prep_id: str,
    prep_job: dict[str, Any],
    *,
    title: str,
    description: str,
    source_photos: list[Path],
    drafts_count: int,
    facts: dict[str, str],
    base_dir: Path,
) -> None:
    """Обёртка над preparation.run_prep_job с событиями журнала агентов.

    Единая точка запуска Подготовщика: используется и при ручном запуске
    (api_prepare_start), и при авто-подготовке внутри публикации
    (_run_publish_and_cleanup) — оба места логируются одинаково.
    """
    journal.log_event(
        "preparer", "run_started", "Подготовщик запущен",
        run_id=prep_id,
        payload={"drafts_count": drafts_count},
    )
    await preparation.run_prep_job(
        prep_id,
        prep_job,
        title=title,
        description=description,
        source_photos=source_photos,
        drafts_count=drafts_count,
        facts=facts,
        base_dir=base_dir,
    )
    if prep_job.get("status") == "done":
        journal.log_event(
            "preparer", "run_finished", "Подготовщик собрал варианты",
            run_id=prep_id,
            payload={"prep_id": prep_id, "drafts_count": drafts_count},
        )
    else:
        journal.log_event(
            "preparer", "run_failed", "Подготовщик не смог собрать варианты",
            run_id=prep_id,
            payload={"prep_id": prep_id, "error": prep_job.get("error")},
        )


async def _run_publish_and_cleanup(
    job_id: str,
    draft: "publisher.DraftData",
    tmp_dir: Path,
    prep_id: Optional[str],
    start_index: Optional[int] = None,
) -> None:
    """Фоновая publish-задача со страховкой (F19, S5).

    Исключение задачи, созданной через create_task, никто не читает: без
    страховки задача осталась бы «running» навсегда. Необработанное
    исключение → failed с текстом, checkpoint записан, traceback в лог.
    Отмена → interrupted в памяти и на диске, затем проброс дальше.
    Итог, уже выставленный publisher (done/failed/...), не перезаписывается.
    """
    job = PUBLISH_JOBS[job_id]
    try:
        await _run_publish_and_cleanup_body(
            job_id, draft, tmp_dir, prep_id, start_index=start_index,
        )
    except asyncio.CancelledError:
        if job.get("status") in {"queued", "running"}:
            job["status"] = "interrupted"
            job["user_action"] = None
            job["error"] = (
                "Публикация прервана: сервер остановлен или задача отменена. "
                "Состояние сохранено — после перезапуска можно продолжить."
            )
            _save_publish_outcome(job_id, job, draft, tmp_dir)
        raise
    except Exception as exc:
        logger.error(
            "Задача публикации %s: необработанное исключение", job_id, exc_info=exc,
        )
        if job.get("status") in {"queued", "running"}:
            job["status"] = "failed"
            job["user_action"] = None
            job["error"] = (
                f"Непредвиденная ошибка публикации на шаге {job.get('step') or 'unknown'}: "
                f"{exc}. Автоматическое продолжение решит по сохранённому состоянию; "
                "уже созданные объявления проверьте в кабинете Авито."
            )
            _save_publish_outcome(job_id, job, draft, tmp_dir)


def _save_publish_outcome(
    job_id: str, job: dict[str, Any], draft: "publisher.DraftData", tmp_dir: Path,
) -> None:
    """Итог остановки на диск (S2 — последний созданный в пропущенные); не бросает."""
    try:
        publish_state.record_unresumable_last_item(job, draft)
        publish_state.save_publish_state(tmp_dir, job_id, job, draft)
    except Exception:  # noqa: BLE001 — вызывается из обработчика ошибок
        logger.exception("Не удалось сохранить итог задачи %s", job_id)


async def _run_publish_and_cleanup_body(
    job_id: str,
    draft: "publisher.DraftData",
    tmp_dir: Path,
    prep_id: Optional[str],
    start_index: Optional[int] = None,
) -> None:
    """Обёртка фоновой publish-задачи.

    Авто-подготовка (ТЗ §17): N≥2 БЕЗ prep_id раньше молча давало N одинаковых
    клонов (пакетный режим §16) — теперь варианты готовятся автоматически,
    prep_id = job_id. После ПОЛНОГО успеха удаляются prep_{prep_id} и запись
    PREP_JOBS (ТЗ §17.4); при падении/частичном успехе — остаются для разбора.
    ВАЖНО: run_publish_job вызывается через атрибут модуля
    (publisher.run_publish_job), чтобы подмена в smoke-тестах работала."""
    job = PUBLISH_JOBS[job_id]
    items_total = int(job.get("items_total") or job.get("drafts_total") or 1)

    journal.log_event(
        "publisher", "run_started", "Публикатор запущен",
        run_id=job_id,
        payload={"items_total": items_total, "start_index": start_index},
    )

    if (
        not prep_id
        and items_total >= 2
        and (start_index is None or int(job.get("items_published") or 0) == 0)
    ):
        prep_id = job_id  # отдельный uuid не нужен — job_id уникален
        prep_dir = TMP_PUBLISH_DIR / f"prep_{prep_id}"
        job["status"] = "running"
        job["step"] = "prepare_variants"
        job["step_label"] = "Подготовка вариантов"
        facts = {
            key: value
            for key, value in (
                ("size", draft.size),
                ("condition", draft.condition),
                ("brand", draft.brand),
            )
            if value.strip()
        }
        prep_job: dict[str, Any] = {}
        logger.info(
            "Авто-подготовка вариантов для задачи %s: %d черновиков",
            job_id, items_total,
        )
        await _run_prep_job_with_journal(
            prep_id,
            prep_job,
            title=draft.title,
            description=draft.description,
            source_photos=[Path(p) for p in draft.photo_paths],
            drafts_count=items_total,
            facts=facts,
            base_dir=prep_dir,
        )
        if prep_job.get("status") != "done":
            job["status"] = "failed"
            job["error"] = (
                "Не удалось подготовить варианты: "
                f"{prep_job.get('error') or 'неизвестная ошибка'}"
            )
            logger.error(
                "Авто-подготовка задачи %s провалилась: %s", job_id, job["error"]
            )
            try:
                publish_state.save_publish_state(tmp_dir, job_id, job, draft)
            except Exception as exc:
                logger.error("Не удалось сохранить сбой подготовки %s: %s", job_id, exc)
            journal.log_event(
                "publisher", "run_failed", "Публикатор остановлен: авто-подготовка не удалась",
                run_id=job_id,
                payload={"status": job["status"], "error": job["error"]},
            )
            return
        job["prep_id"] = prep_id

    if prep_id:
        journal.log_event(
            "publisher", "handoff", "Публикатор получил варианты от Подготовщика",
            run_id=job_id,
            payload={"from": "preparer", "prep_id": prep_id},
        )

    publish_kwargs: dict[str, Any] = {
        "cdp_url": CDP_URL,
        "tmp_dir": str(tmp_dir),
    }
    if start_index is not None:
        publish_kwargs["start_index"] = start_index
    await publisher.run_publish_job(job_id, job, draft, **publish_kwargs)
    if job.get("status") == "done":
        # В production итог и уборку уже сделал publisher (каталога нет или в
        # нём остался только checkpoint с пропусками); повтор безопасен и
        # нужен тестовым драйверам, подменяющим publisher.run_publish_job.
        # Порядок тот же: done на диск, потом уборка (F16, F24/S3).
        if tmp_dir.is_dir():
            publish_state.write_final_state(tmp_dir, job_id, job, draft)
        journal.log_event(
            "publisher", "run_finished", "Публикатор завершил пакет",
            run_id=job_id,
            payload={
                "items_total": items_total,
                "items_published": job.get("items_published"),
            },
        )
    else:
        try:
            publish_state.save_publish_state(tmp_dir, job_id, job, draft)
        except Exception as exc:
            logger.error("Не удалось сохранить итог задачи %s: %s", job_id, exc)
        # needs_user_action, частичный успех и прочие ошибки — всё это run_failed
        # (только "done" считается успехом Публикатора, см. брифинг).
        journal.log_event(
            "publisher", "run_failed", "Публикатор не завершил пакет",
            run_id=job_id,
            payload={
                "status": job.get("status"),
                "items_total": items_total,
                "items_published": job.get("items_published"),
                "error": job.get("error"),
            },
        )
    if not prep_id:
        return
    if PUBLISH_JOBS.get(job_id, {}).get("status") != "done":
        logger.info("Prep %s оставлен для разбора: задача %s не успешна", prep_id, job_id)
        return
    if any(
        other_id != job_id
        and other.get("prep_id") == prep_id
        and other.get("status") not in {"done", "closed"}
        for other_id, other in _publish_job_records(include_disk=True)
    ):
        logger.info("Prep %s оставлен: на него ссылается другая задача", prep_id)
        return
    shutil.rmtree(TMP_PUBLISH_DIR / f"prep_{prep_id}", ignore_errors=True)
    PREP_JOBS.pop(prep_id, None)
    logger.info("Prep %s удалён после успешной заливки (задача %s)", prep_id, job_id)


def _schedule_publish(
    job_id: str,
    draft: "publisher.DraftData",
    tmp_dir: Path,
    prep_id: Optional[str],
    *,
    start_index: Optional[int] = None,
    lock_handle: Optional[BinaryIO] = None,
) -> asyncio.Task[Any]:
    """После успешного возврата владеет lock до done-callback; иначе владеет вызывающий."""
    own_lock = lock_handle is None
    lock = lock_handle
    try:
        blocker = _active_publish_job(except_job_id=job_id)
        if blocker:
            raise publish_state.PublishLockBusyError(os.getpid(), blocker)
        current = ACTIVE_PUBLISH_TASKS.get(job_id)
        if current is not None and not current.done():
            raise publish_state.PublishLockBusyError(os.getpid(), job_id)
        if lock is None:
            lock = publish_state.acquire_publish_lock(TMP_PUBLISH_DIR, job_id)
        coroutine = _run_publish_and_cleanup(
            job_id, draft, tmp_dir, prep_id, start_index=start_index,
        )
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
    except BaseException:
        if own_lock and lock is not None:
            publish_state.release_publish_lock(lock)
        raise

    def _forget(completed: asyncio.Task[Any]) -> None:
        if ACTIVE_PUBLISH_TASKS.get(job_id) is completed:
            ACTIVE_PUBLISH_TASKS.pop(job_id, None)
        publish_state.release_publish_lock(lock)

    try:
        ACTIVE_PUBLISH_TASKS[job_id] = task
        task.add_done_callback(_forget)
    except BaseException:
        if ACTIVE_PUBLISH_TASKS.get(job_id) is task:
            ACTIVE_PUBLISH_TASKS.pop(job_id, None)
        task.cancel()
        if own_lock:
            publish_state.release_publish_lock(lock)
        raise
    return task


@app.get("/api/publish/categories")
async def api_publish_categories() -> JSONResponse:
    """Списки полей формы по категориям — единый источник правды для фронта."""
    cats = []
    for p in category_profiles.PROFILES.values():
        cats.append({
            "key": p.key,
            "label": p.label,
            "path": list(p.full_path),
            "sizes": list(p.size_options.keys()),
            "colors": list(p.color_options.keys()),
            "conditions": list(p.condition_options.keys()),
            "trade_types": list(p.trade_type_options.keys()),
            # Есть не у всех категорий: пустой список = поле не рисовать
            "item_types": list(p.item_type_options.keys()),
            "materials": list(p.material_options.keys()),
            "styles": list(p.style_options.keys()),
        })
    return JSONResponse({"categories": cats})


@app.post("/api/addresses/generate")
async def api_generate_addresses(request: Request) -> JSONResponse:
    """Выдаёт существующие адреса OSM для уже введённых пользователем городов."""
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "Передайте JSON со списком городов."}, status_code=400)
    cities = payload.get("cities") if isinstance(payload, dict) else None
    used_locations = payload.get("used_locations", []) if isinstance(payload, dict) else []
    if not isinstance(cities, list) or not 1 <= len(cities) <= 20:
        return JSONResponse({"error": "Можно запросить от 1 до 20 городов."}, status_code=422)
    if not isinstance(used_locations, list) or len(used_locations) > 200:
        return JSONResponse({"error": "Некорректный список уже использованных адресов."}, status_code=422)
    clean_cities = []
    for index, city in enumerate(cities, start=1):
        if not isinstance(city, str):
            return JSONResponse({"error": f"Город №{index} должен быть строкой."}, status_code=422)
        value = " ".join(city.split())
        if not value or len(value) > 120:
            return JSONResponse({"error": f"Город №{index} должен быть строкой до 120 символов."}, status_code=422)
        clean_cities.append(value)
    clean_used = []
    for index, location in enumerate(used_locations, start=1):
        if not isinstance(location, dict) or not isinstance(location.get("city"), str) or not isinstance(location.get("address"), str):
            return JSONResponse({"error": f"Использованный адрес №{index} должен содержать строки city и address."}, status_code=422)
        city = " ".join(location["city"].split())
        address = " ".join(location["address"].split())
        if len(city) > 120 or len(address) > 200:
            return JSONResponse({"error": "Город или адрес слишком длинный."}, status_code=422)
        clean_used.append({"city": city, "address": address})
    try:
        results = await asyncio.to_thread(address_generator.generate_addresses, clean_cities, clean_used)
    except Exception:
        logger.exception("Неожиданная ошибка генератора адресов")
        return JSONResponse({"error": "Сервис адресов временно недоступен. Введите адрес вручную."}, status_code=503)
    return JSONResponse({
        "results": results,
        "source": "openstreetmap",
        "attribution": "© OpenStreetMap contributors",
    })


@app.post("/api/publish/start")
async def api_publish_start(
    title: str = Form(""),
    trade_type: str = Form(""),
    condition: str = Form(""),
    size: str = Form(""),
    brand: str = Form(""),
    color: str = Form(""),
    description: str = Form(""),
    price: str = Form(""),
    locations_json: str = Form(""),
    view_price_max: str = Form(""),
    drafts_count: str = Form(""),
    prep_id: str = Form(""),
    category: str = Form("jackets"),
    item_type: str = Form(""),
    material: str = Form(""),
    style: str = Form(""),
    photos: list[UploadFile] = File([]),
) -> JSONResponse:
    """
    Запуск задачи полной публикации объявлений (multipart/form-data).

    Серверная валидация по ТЗ §8 — backend не доверяет фронту.
    Невалидная форма → HTTP 422 со списком полей, задача НЕ создаётся.
    Валидная → фото сохраняются в tmp/publish/{job_id}/, задача уходит в фон.

    Пакетный режим: locations_json содержит отдельную геолокацию для каждого
    объявления, view_price_max — единый потолок стоимости просмотра для всех
    (Decimal в publisher; движок всегда берёт минимальную цену Авито),
    drafts_count задаёт количество объявлений (1–20, дефолт 1).
    Вариативность (ТЗ §17):
    При drafts_count ≥ 2 без prep_id варианты готовятся автоматически перед
    заливкой (N одинаковых клонов больше не создаются). prep_id из превью
    используется как раньше.
    """
    # Диск — источник финального статуса: задачу мог закрыть другой процесс.
    blocker = _start_publish_conflict(prep_id, include_disk=True)
    if blocker:
        return _publish_busy_response(blocker)

    # Шаг 5.7: если передан prep_id, проверяем что подготовка завершена
    if prep_id:
        prep_status = PREP_JOBS.get(prep_id, {}).get("status")
        if prep_status != "done":
            return JSONResponse(
                {"errors": [{"field": "prep_id", "error": "Подготовка вариантов ещё не завершена или не найдена"}]},
                status_code=422,
            )

    fields: dict[str, Any] = {
        "title": title,
        "trade_type": trade_type,
        "condition": condition,
        "size": size,
        "brand": brand,
        "color": color,
        "description": description,
        "price": price,
        "locations_json": locations_json,
        "view_price_max": view_price_max,
        "drafts_count": drafts_count,
        "item_type": item_type,
        "material": material,
        "style": style,
    }

    # Метаданные фото для валидации БЕЗ чтения содержимого в память:
    # размер берём по спулу Starlette через seek/tell (лимит 25 МБ/файл
    # проверяется по факту, пиковая память не растёт со списком blob'ов)
    photo_meta: list[tuple[str, Optional[str], int]] = []
    for upload in photos:
        upload.file.seek(0, os.SEEK_END)
        size = upload.file.tell()
        upload.file.seek(0)
        photo_meta.append((upload.filename or "", upload.content_type, size))

    # Профиль категории: из формы (по умолчанию «Пиджаки и костюмы»);
    # неизвестный ключ → дефолтная категория (category_profiles.get_profile).
    profile = category_profiles.get_profile(category)

    errors = publisher.validate_publish_form(fields, photo_meta, profile)
    if errors:
        logger.info("Publish: форма не прошла валидацию (%d ошибок)", len(errors))
        return JSONResponse({"errors": errors}, status_code=422)

    # Сколько черновиков (ТЗ §16): валидация уже прошла, None невозможен
    drafts_total = publisher.parse_drafts_count(drafts_count) or publisher.DRAFTS_DEFAULT

    # Авто-подготовка при N≥2 обрабатывает фото через Pillow: HEIC без
    # pillow-heif молча дал бы всем вариантам одинаковые оригиналы — отклоняем.
    if drafts_total >= 2 and not prep_id:
        has_heic = any(
            Path(name or "").suffix.lower() in (".heic", ".heif")
            for name, _, _ in photo_meta
        )
        if has_heic and not photo_variation.heif_available():
            return JSONResponse(
                {"errors": [{
                    "field": "photos",
                    "error": (
                        "HEIC-фото при нескольких черновиках требуют пакет "
                        "pillow-heif (pip install pillow-heif) — установите его "
                        "или конвертируйте фото в JPEG"
                    ),
                }]},
                status_code=422,
            )

    # Создаём задачу и сохраняем фото в tmp/publish/{job_id}/ —
    # читаем и пишем ПО ОДНОМУ файлу, не накапливая их содержимое в памяти
    # Диск — источник финального статуса: задачу мог закрыть другой процесс.
    blocker = _start_publish_conflict(prep_id, include_disk=True)
    if blocker:
        return _publish_busy_response(blocker)
    job_id = str(uuid.uuid4())
    PUBLISH_JOBS[job_id] = {
        "status": "queued",
        "prep_id": prep_id or None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp_dir = TMP_PUBLISH_DIR / job_id
    photo_paths: list[str] = []
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for idx, upload in enumerate(photos, start=1):
            # Безопасное имя: порядковый номер + расширение исходника
            ext = Path(upload.filename or "").suffix.lower() or ".jpg"
            file_path = tmp_dir / f"photo_{idx:02d}{ext}"
            file_path.write_bytes(await upload.read())
            photo_paths.append(str(file_path))
    except asyncio.CancelledError:
        PUBLISH_JOBS.pop(job_id, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        PUBLISH_JOBS.pop(job_id, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.error("Publish: не удалось сохранить фото в %s: %s", tmp_dir, exc)
        return JSONResponse(
            {"errors": [{"field": "photos", "error": f"Не удалось сохранить фото: {exc}"}]},
            status_code=500,
        )

    try:
        draft = publisher.build_draft_data(fields, photo_paths, category=profile.key)
        summary = draft.summary()
    except Exception as exc:
        PUBLISH_JOBS.pop(job_id, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.exception("Publish: не удалось собрать задачу %s", job_id)
        return JSONResponse({"error": f"Не удалось создать задачу публикации: {exc}"}, status_code=500)

    PUBLISH_JOBS[job_id].update({
        "step": "",
        "step_label": "",
        "done": 0,
        "total": publisher.TOTAL_STEPS,
        "error": None,
        "debug_dir": None,  # путь дампа сбоя; заполняет publisher._dump_failure
        "result_url": None,
        "summary": summary,
        # Новые поля — источник правды для publisher и API.
        "item_index": 0,
        "items_total": drafts_total,
        "items_published": 0,
        "published_urls": [],
        "brand_selected": None,
        "applied_view_prices": [],
        "address_warnings": [],
        "user_action": None,
        # Временные алиасы для обратной совместимости.
        "draft_index": 0,
        "drafts_total": drafts_total,
        "drafts_saved": 0,
        "saved_urls": [],
        # Отметка денежного клика текущего объявления (publish_state.resume_plan):
        # None / "not_clicked" / "clicked"; publisher сбрасывает на каждом объявлении.
        "money_click": None,
        # Номер объявления, которое resume повторяет по retry_item (F27);
        # проставляет только /api/publish/resume.
        "resume_retry_item": None,
    })

    try:
        publish_state.save_publish_state(tmp_dir, job_id, PUBLISH_JOBS[job_id], draft)
    except Exception as exc:
        PUBLISH_JOBS.pop(job_id, None)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.error("Publish: не удалось сохранить начальный checkpoint %s: %s", job_id, exc)
        return JSONResponse(
            {"errors": [{
                "field": "photos",
                "error": f"Не удалось безопасно сохранить задачу публикации: {exc}",
            }]},
            status_code=500,
        )

    lock_handle: Optional[BinaryIO] = None
    scheduled = False
    try:
        lock_handle = publish_state.acquire_publish_lock(TMP_PUBLISH_DIR, job_id)
        blocker = _start_publish_conflict(
            prep_id, except_job_id=job_id, include_disk=True,
        )
        if blocker:
            return _publish_busy_response(blocker)
        task = _schedule_publish(
            job_id, draft, tmp_dir, prep_id or None, lock_handle=lock_handle,
        )
        if isinstance(task, asyncio.Task):
            lock_handle = None  # владение передано done-callback
        scheduled = True
    except publish_state.PublishLockBusyError as exc:
        return _publish_busy_response(exc.job_id or "другая задача", pid=exc.pid)
    except Exception:
        logger.exception("Publish: не удалось запустить задачу %s", job_id)
        return JSONResponse({"error": "Не удалось запустить задачу публикации"}, status_code=500)
    finally:
        if lock_handle is not None:
            publish_state.release_publish_lock(lock_handle)
        if not scheduled:
            PUBLISH_JOBS.pop(job_id, None)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    logger.info(
        "Publish-задача %s создана: '%s', фото: %d, объявлений: %d",
        job_id, draft.title, len(photo_paths), drafts_total,
    )
    return JSONResponse({"job_id": job_id})


@app.post("/api/publish/resume/{job_id}")
async def api_publish_resume(job_id: str) -> JSONResponse:
    """Продолжает сохранённую задачу с первого неотправленного объявления."""
    blocker = _active_publish_job()
    if blocker:
        return _publish_busy_response(blocker)

    # job_id всегда генерируется сервером через uuid.uuid4() — легитимное
    # значение обязано быть валидным UUID. Проверка до использования в пути
    # закрывает path traversal ("../../etc/passwd" и т.п.).
    try:
        uuid.UUID(job_id)
    except ValueError:
        return JSONResponse({"error": "Задача не найдена"}, status_code=404)

    job_dir = TMP_PUBLISH_DIR / job_id
    # Defense in depth: даже если формат UUID сам по себе не спасает от
    # какого-то иного вектора, итоговый путь обязан остаться внутри
    # TMP_PUBLISH_DIR.
    tmp_publish_root = TMP_PUBLISH_DIR.resolve()
    resolved_job_dir = job_dir.resolve()
    if tmp_publish_root not in resolved_job_dir.parents and resolved_job_dir != tmp_publish_root:
        return JSONResponse({"error": "Задача не найдена"}, status_code=404)

    return _resume_publish_under_lock(job_id, job_dir)


def _resume_publish_under_lock(job_id: str, job_dir: Path) -> JSONResponse:
    """Держит lock от чтения checkpoint до передачи фоновой задаче."""
    lock_handle: Optional[BinaryIO] = None
    try:
        lock_handle = publish_state.acquire_publish_lock(TMP_PUBLISH_DIR, job_id)
        blocker = _active_publish_job()
        if blocker:
            return _publish_busy_response(blocker)
        response, handed_off = _resume_publish_locked(job_id, job_dir, lock_handle)
        if handed_off:
            lock_handle = None
        return response
    except publish_state.PublishLockBusyError as exc:
        return _publish_busy_response(exc.job_id or "другая задача", pid=exc.pid)
    finally:
        if lock_handle is not None:
            publish_state.release_publish_lock(lock_handle)


def _resume_publish_locked(
    job_id: str, job_dir: Path, lock_handle: BinaryIO,
) -> tuple[JSONResponse, bool]:
    """Читает свежий checkpoint и вычисляет план при занятом lock."""

    try:
        saved_job, draft = publish_state.load_publish_state(job_dir)
    except OSError as exc:
        # Каталога или файла checkpoint'а физически нет — задачи не существует.
        logger.warning("Resume %s невозможен: %s", job_id, exc)
        return JSONResponse({"error": "Задача не найдена"}, status_code=404), False
    except (ValueError, KeyError, InvalidOperation) as exc:
        # Файл существует, но содержимое не парсится (мусорный JSON, битый
        # Decimal и т.п.) — задача сохранена, но нерабочая для resume.
        logger.warning("Resume %s невозможен: %s", job_id, exc)
        return JSONResponse(
            {"error": "Checkpoint повреждён, проверьте кабинет Авито"},
            status_code=409,
        ), False

    if saved_job.get("status") == "closed":
        return JSONResponse({"error": "Задача закрыта и не может быть возобновлена"}, status_code=409), False
    if saved_job.get("status") == "done":
        # «Завершена с пропусками»: пакет пройден до конца, пропущенные
        # объявления пользователь проверяет в кабинете — запускать нечего.
        message = "Пакет уже завершён — продолжать нечего."
        try:
            skipped = publish_state.skipped_numbers(saved_job.get("skipped_items"))
        except ValueError:
            skipped = set()
        if skipped:
            numbers = ", ".join(f"№{n}" for n in sorted(skipped))
            message += f" Пропущенные ({numbers}) проверьте в кабинете Авито вручную."
        return JSONResponse({"error": message}, status_code=409), False

    try:
        plan = publish_state.resume_plan(saved_job)
        if plan is None:
            # Тот же текст, что /status отдаёт в resume_unavailable_reason.
            message = publish_state.resume_unavailable_reason(saved_job) or (
                "Состояние checkpoint не распознано — проверьте кабинет Авито вручную."
            )
            return JSONResponse({"error": message}, status_code=409), False
    except (TypeError, ValueError) as exc:
        logger.warning("Resume %s невозможен: %s", job_id, exc)
        return JSONResponse(
            {"error": "Checkpoint повреждён, проверьте кабинет Авито"},
            status_code=409,
        ), False

    start_index = plan["start_index"]
    items_total = plan["items_total"]

    # skip_item: текущее (уже созданное на Авито) объявление не трогаем вообще —
    # ни одного повторного финансового клика, только запоминаем номер для
    # ручной проверки и едем дальше со следующего индекса.
    if plan["mode"] == "skip_item":
        skipped_item = plan["skipped_item"]
        # Запись со следами (item_id, шаг, адрес варианта), старые номера в
        # списке приводятся к записям (publish_state.normalize_skipped_items).
        publish_state.add_skipped_item(
            saved_job, publish_state.make_skip_record(saved_job, skipped_item, draft),
        )
        logger.warning(
            "Задача %s: объявление №%d пропущено при возобновлении — оно уже "
            "создано на Авито и не будет отправлено повторно",
            job_id, skipped_item,
        )

    # F27: retry_item того же объявления, что было в работе (сохранённый
    # item_index == start_index) — Авито может переоткрыть наш же несозданный
    # черновик с нашим названием; это не дубль. Свежий старт следующего
    # объявления и skip_item — не «своё».
    try:
        saved_item_index = int(saved_job.get("item_index") or 0)
    except (TypeError, ValueError):
        saved_item_index = 0
    saved_job["resume_retry_item"] = (
        start_index
        if plan["mode"] == "retry_item" and saved_item_index == start_index
        else None
    )

    # F17: без этого статус ещё несколько секунд (пока идёт connect_chrome)
    # показывал бы номер и шаг ТОЛЬКО ЧТО пропущенного объявления — будто
    # автоматика снова его трогает. На корректность плана это не влияет:
    # пропущенное уже записано в skipped_items, а connect_chrome сам
    # перезапишет шаг настоящим, как только дойдёт до дела.
    saved_job["item_index"] = start_index
    saved_job["step"] = "connect_chrome"
    saved_job["step_label"] = publisher.STEP_LABELS.get("connect_chrome", "connect_chrome")

    saved_job["status"] = "queued"
    saved_job["error"] = None
    saved_job["user_action"] = None
    previous_job = PUBLISH_JOBS.get(job_id)
    PUBLISH_JOBS[job_id] = saved_job
    try:
        task = _schedule_publish(
            job_id,
            draft,
            job_dir,
            saved_job.get("prep_id"),
            start_index=start_index,
            lock_handle=lock_handle,
        )
    except publish_state.PublishLockBusyError as exc:
        if previous_job is None:
            PUBLISH_JOBS.pop(job_id, None)
        else:
            PUBLISH_JOBS[job_id] = previous_job
        return _publish_busy_response(exc.job_id or "другая задача", pid=exc.pid), False
    except Exception:
        if previous_job is None:
            PUBLISH_JOBS.pop(job_id, None)
        else:
            PUBLISH_JOBS[job_id] = previous_job
        logger.exception("Resume %s: не удалось запустить задачу", job_id)
        return JSONResponse({"error": "Не удалось возобновить задачу"}, status_code=500), False
    logger.info(
        "Publish-задача %s возобновлена с объявления %d/%d (режим %s)",
        job_id,
        start_index,
        items_total,
        plan["mode"],
    )
    return JSONResponse({
        "job_id": job_id,
        "resumed_from": start_index,
        "mode": plan["mode"],
        "skipped_item": plan["skipped_item"],
    }), isinstance(task, asyncio.Task)


def _publish_pending_summary(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Сокращённая карточка незавершённой задачи для баннера на форме (F01)."""
    status = _serialize_publish_status(job_id, job)
    return {
        "job_id": job_id,
        "status": status["status"],
        "step": status["step"],
        "created_at": status["created_at"],
        "is_older_than_24h": status["is_older_than_24h"],
        "items_total": status["items_total"],
        "items_published": status["items_published"],
        "resume_available": status["resume_available"],
        "resume_plan": status["resume_plan"],
        "resume_unavailable_reason": status["resume_unavailable_reason"],
        "skipped_items": status["skipped_items"],
    }


@app.get("/api/publish/pending")
async def api_publish_pending() -> JSONResponse:
    """Незавершённые publish-задачи — баннер «Есть незавершённая публикация» (F01).

    Незавершённая — любой статус, кроме closed, и кроме done БЕЗ пропусков
    (done с пропусками остаётся видимым, см. F24/S3 из брифа 05). Задачи
    собираются и из памяти, и с диска — второй процесс или задача, ещё не
    попавшая в память после рестарта, тоже должны быть видны. При дубле
    job_id между диском и памятью побеждает память (свежее состояние).
    Отсортировано по created_at по убыванию — новые задачи первыми; у задач
    без даты (старый checkpoint) created_at начала строки не определён,
    такие уходят в конец.
    """
    merged: dict[str, dict[str, Any]] = {}
    for job_id, job in _publish_job_records(include_disk=True):
        merged[job_id] = job
    jobs = []
    for job_id, job in merged.items():
        status = job.get("status")
        if status == "closed":
            continue
        if status == "done" and not _serialize_skipped_items(job.get("skipped_items")):
            continue
        jobs.append(_publish_pending_summary(job_id, job))
    jobs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return JSONResponse({"jobs": jobs})


@app.get("/api/publish/status/{job_id}")
async def api_publish_status(job_id: str) -> JSONResponse:
    """JSON-статус publish-задачи для опроса фронтом."""
    if job_id not in PUBLISH_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    return JSONResponse(_serialize_publish_status(job_id, PUBLISH_JOBS[job_id]))


@app.post("/api/publish/close/{job_id}")
async def api_publish_close(job_id: str) -> JSONResponse:
    """Закрывает остановленный пакет после ручной проверки объявлений."""
    try:
        uuid.UUID(job_id)
    except ValueError:
        return JSONResponse({"error": "Задача не найдена"}, status_code=404)

    task = ACTIVE_PUBLISH_TASKS.get(job_id)
    if task is not None and not task.done():
        return _publish_busy_response(job_id)

    job_dir = TMP_PUBLISH_DIR / job_id
    if TMP_PUBLISH_DIR.resolve() not in job_dir.resolve().parents:
        return JSONResponse({"error": "Задача не найдена"}, status_code=404)
    if not (job_dir / publish_state.STATE_FILENAME).is_file():
        return JSONResponse({"error": "Задача не найдена"}, status_code=404)
    try:
        lock = publish_state.acquire_publish_lock(TMP_PUBLISH_DIR, job_id)
    except publish_state.PublishLockBusyError as exc:
        return _publish_busy_response(exc.job_id or "другая задача", pid=exc.pid)
    try:
        try:
            saved_job, draft = publish_state.load_publish_state(job_dir)
        except OSError:
            return JSONResponse({"error": "Задача не найдена"}, status_code=404)
        except (ValueError, KeyError, InvalidOperation) as exc:
            logger.warning("Close %s невозможен: %s", job_id, exc)
            return JSONResponse({"error": "Checkpoint повреждён"}, status_code=409)

        if saved_job.get("status") not in {
            "queued", "running", "failed", "interrupted", "needs_user_action", "closed",
        }:
            return JSONResponse({"error": "Закрыть можно только остановленную задачу"}, status_code=409)
        if saved_job.get("status") != "closed":
            saved_job["status"] = "closed"
            try:
                publish_state.save_publish_state(job_dir, job_id, saved_job, draft)
            except OSError as exc:
                logger.error("Close %s: checkpoint не сохранён: %s", job_id, exc)
                return JSONResponse({"error": "Не удалось сохранить закрытие задачи"}, status_code=500)
        PUBLISH_JOBS[job_id] = saved_job
        return JSONResponse({"job_id": job_id, "status": "closed"})
    finally:
        publish_state.release_publish_lock(lock)


@app.get("/api/publish/result/{job_id}")
async def api_publish_result(job_id: str) -> JSONResponse:
    """Итог publish-задачи: статус, конечный URL, ошибка/инструкция, сводка данных."""
    if job_id not in PUBLISH_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = PUBLISH_JOBS[job_id]
    return JSONResponse(
        {
            **_serialize_publish_status(job_id, job),
            "result_url": job.get("result_url"),
            "summary": job.get("summary") or {},
        }
    )


# ---------------------------------------------------------------------------
# Фаза подготовки вариантов черновиков (ТЗ §17, Задача 5)
# ---------------------------------------------------------------------------


@app.post("/api/publish/prepare")
async def api_prepare_start(
    title: str = Form(""),
    description: str = Form(""),
    drafts_count: str = Form(""),
    size: str = Form(""),
    condition: str = Form(""),
    brand: str = Form(""),
    photos: list[UploadFile] = File([]),
) -> JSONResponse:
    """
    Запуск фазы подготовки N вариантов текста и фото (ТЗ §17).

    Принимает multipart/form-data: title, description, drafts_count (обязательные),
    size/condition/brand (опциональные, для facts), photos (1–10 файлов).
    Невалидная форма → HTTP 422 со списком ошибок.
    Валидная → фото сохраняются в tmp/publish/prep_{prep_id}/, задача уходит в фон.
    Ответ: {"prep_id": "<uuid>"}.
    """
    # Метаданные фото для валидации (seek/tell, без чтения blob-ов в память)
    photo_meta: list[tuple[str, Optional[str], int]] = []
    for upload in photos:
        upload.file.seek(0, os.SEEK_END)
        file_size = upload.file.tell()
        upload.file.seek(0)
        photo_meta.append((upload.filename or "", upload.content_type, file_size))

    fields: dict[str, Any] = {
        "title": title,
        "description": description,
        "drafts_count": drafts_count,
    }
    errors = preparation.validate_prepare_form(fields, photo_meta)
    if errors:
        logger.info("Prepare: форма не прошла валидацию (%d ошибок)", len(errors))
        return JSONResponse({"errors": errors}, status_code=422)

    # Создаём prep_id и рабочую директорию
    prep_id = str(uuid.uuid4())
    prep_dir = TMP_PUBLISH_DIR / f"prep_{prep_id}"

    # Сохраняем исходные фото во временные файлы
    source_photos: list[Path] = []
    try:
        prep_dir.mkdir(parents=True, exist_ok=True)
        tmp_photos_dir = prep_dir / "_input"
        tmp_photos_dir.mkdir(parents=True, exist_ok=True)
        for idx, upload in enumerate(photos, start=1):
            ext = Path(upload.filename or "").suffix.lower() or ".jpg"
            file_path = tmp_photos_dir / f"photo_{idx:02d}{ext}"
            file_path.write_bytes(await upload.read())
            source_photos.append(file_path)
    except OSError as exc:
        logger.error("Prepare: не удалось сохранить фото в %s: %s", prep_dir, exc)
        return JSONResponse(
            {"errors": [{"field": "photos", "error": f"Не удалось сохранить фото: {exc}"}]},
            status_code=500,
        )

    # Количество черновиков (валидация уже прошла, None невозможен)
    n_drafts = publisher.parse_drafts_count(drafts_count) or publisher.DRAFTS_DEFAULT

    # facts — только из непустых опциональных полей
    facts: dict[str, str] = {}
    if size.strip():
        facts["size"] = size.strip()
    if condition.strip():
        facts["condition"] = condition.strip()
    if brand.strip():
        facts["brand"] = brand.strip()

    # Заводим запись задачи подготовки
    PREP_JOBS[prep_id] = {
        "status": "queued",
        "step": "",
        "step_label": "",
        "done": 0,
        "total": len(preparation.PREP_STEPS),
        "error": None,
        "drafts_count": n_drafts,
    }

    # Запускаем фоновую задачу
    asyncio.create_task(
        _run_prep_job_with_journal(
            prep_id,
            PREP_JOBS[prep_id],
            title=title.strip(),
            description=description.strip(),
            source_photos=source_photos,
            drafts_count=n_drafts,
            facts=facts,
            base_dir=prep_dir,
        )
    )

    logger.info(
        "Prepare-задача %s создана: '%s', фото: %d, черновиков: %d",
        prep_id, title.strip(), len(source_photos), n_drafts,
    )
    return JSONResponse({"prep_id": prep_id})


@app.get("/api/publish/prepare/status/{prep_id}")
async def api_prepare_status(prep_id: str) -> JSONResponse:
    """JSON-статус задачи подготовки вариантов для опроса фронтом."""
    if prep_id not in PREP_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = PREP_JOBS[prep_id]
    return JSONResponse({
        "prep_id": prep_id,
        "status": job.get("status"),
        "step": job.get("step"),
        "step_label": job.get("step_label"),
        "done": job.get("done"),
        "total": job.get("total"),
        "error": job.get("error"),
        "drafts_count": job.get("drafts_count"),
    })


@app.get("/api/publish/prepare/result/{prep_id}")
async def api_prepare_result(prep_id: str) -> JSONResponse:
    """
    Результат задачи подготовки: список карточек вариантов черновиков.

    409, если задача ещё не завершена (status != done).
    Иначе: {"prep_id", "drafts": [карточки...]}.
    """
    if prep_id not in PREP_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = PREP_JOBS[prep_id]
    if job.get("status") != "done":
        return JSONResponse(
            {"status": job.get("status"), "error": "Подготовка ещё не завершена"},
            status_code=409,
        )

    prep_dir = TMP_PUBLISH_DIR / f"prep_{prep_id}"
    drafts_count: int = job.get("drafts_count") or 1
    drafts = preparation.build_result(prep_dir, drafts_count)

    return JSONResponse({"prep_id": prep_id, "drafts": drafts})


@app.get("/api/publish/prepare/photo/{prep_id}/{draft_index}/{photo_index}")
async def api_prepare_photo(
    prep_id: str,
    draft_index: int,
    photo_index: int,
) -> Response:
    """
    Отдаёт фото варианта черновика.

    Путь строится только из проверенного prep_id (по PREP_JOBS) и целых индексов
    — защита от path traversal. prep_id из запроса не используется напрямую как
    путь файловой системы без проверки.
    Нет файла → 404.
    """
    if prep_id not in PREP_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    prep_dir = TMP_PUBLISH_DIR / f"prep_{prep_id}"
    photos_dir = prep_dir / f"draft_{draft_index:02d}" / "photos"

    if not photos_dir.is_dir():
        return JSONResponse({"status": "not_found"}, status_code=404)

    # Ищем файл с именем photo_{NN}.* (расширение исходника)
    photo_files = sorted(p for p in photos_dir.iterdir() if p.is_file())
    if photo_index < 1 or photo_index > len(photo_files):
        return JSONResponse({"status": "not_found"}, status_code=404)

    photo_path = photo_files[photo_index - 1]  # photo_index — 1-based

    if not photo_path.exists():
        return JSONResponse({"status": "not_found"}, status_code=404)

    # Определяем media_type по расширению
    media_type, _ = mimetypes.guess_type(photo_path.name)
    if not media_type:
        media_type = "application/octet-stream"

    return FileResponse(photo_path, media_type=media_type)


@app.post("/api/publish/prepare/regenerate")
async def api_prepare_regenerate(request: Request) -> JSONResponse:
    """
    Перегенерирует один черновик с новым seed.

    Тело JSON: {"prep_id": str, "draft_index": int}.
    404, если prep_id неизвестен.
    422, если draft_index вне допустимого диапазона (1..N).
    Иначе: обновлённая карточка черновика.
    """
    payload = await request.json()
    prep_id: str = str(payload.get("prep_id") or "")
    draft_index_raw = payload.get("draft_index")

    if prep_id not in PREP_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = PREP_JOBS[prep_id]
    n_drafts: int = job.get("drafts_count") or 1

    # Валидация draft_index: допустим любой существующий вариант.
    try:
        draft_index = int(draft_index_raw)
    except (TypeError, ValueError):
        return JSONResponse(
            {"errors": [{"field": "draft_index", "error": "draft_index должен быть целым числом"}]},
            status_code=422,
        )

    if draft_index < 1 or draft_index > n_drafts:
        return JSONResponse(
            {
                "errors": [{
                    "field": "draft_index",
                    "error": f"draft_index должен быть в диапазоне 1..{n_drafts}",
                }]
            },
            status_code=422,
        )

    prep_dir = TMP_PUBLISH_DIR / f"prep_{prep_id}"
    try:
        card = preparation.regenerate_draft(prep_dir, draft_index)
    except ValueError as exc:
        return JSONResponse(
            {"errors": [{"field": "draft_index", "error": str(exc)}]},
            status_code=422,
        )

    logger.info(
        "Prepare %s: перегенерирован черновик %d", prep_id, draft_index
    )
    return JSONResponse(card)


@app.post("/api/publish/prepare/update-text")
async def api_prepare_update_text(request: Request) -> JSONResponse:
    """Сохраняет ручную правку названия и описания подготовленного варианта."""
    payload = await request.json()
    prep_id = str(payload.get("prep_id") or "").strip()
    draft_index_raw = payload.get("draft_index")

    if prep_id not in PREP_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = PREP_JOBS[prep_id]
    n_drafts = int(job.get("drafts_count") or 1)
    try:
        draft_index = int(draft_index_raw)
    except (TypeError, ValueError):
        return JSONResponse(
            {"errors": [{
                "field": "draft_index",
                "error": "draft_index должен быть целым числом",
            }]},
            status_code=422,
        )

    if draft_index < 1 or draft_index > n_drafts:
        return JSONResponse(
            {"errors": [{
                "field": "draft_index",
                "error": f"draft_index должен быть в диапазоне 1..{n_drafts}",
            }]},
            status_code=422,
        )

    title = payload.get("title")
    description = payload.get("description")
    errors: list[dict[str, str]] = []
    if not isinstance(title, str) or not title.strip():
        errors.append({"field": "title", "error": "Название: поле не заполнено"})
    if not isinstance(description, str) or not description.strip():
        errors.append({
            "field": "description",
            "error": "Описание: поле не заполнено",
        })
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    prep_dir = TMP_PUBLISH_DIR / f"prep_{prep_id}"
    try:
        card = preparation.update_draft_text(
            prep_dir,
            draft_index,
            title,
            description,
        )
    except ValueError as exc:
        error_message = str(exc)
        error_field = "title" if error_message.startswith("Название") else "draft_index"
        return JSONResponse(
            {
                "error": error_message,
                "errors": [{"field": error_field, "error": error_message}],
            },
            status_code=422,
        )

    logger.info(
        "Prepare %s: ручная правка текста черновика %d сохранена",
        prep_id,
        draft_index,
    )
    return JSONResponse(card)


# ---------------------------------------------------------------------------
# Рассылка продавцам (docs/_internal/designs/2026-09-21-avito-outreach-design.md)
# ---------------------------------------------------------------------------


def _schedule_outreach(job_id: str, coro: Any) -> None:
    """Запускает фоновую задачу рассылки, удерживая ссылку до её завершения."""
    task = asyncio.create_task(coro)
    ACTIVE_OUTREACH_TASKS[job_id] = task

    def _release(completed: asyncio.Task[Any]) -> None:
        if ACTIVE_OUTREACH_TASKS.get(job_id) is completed:
            ACTIVE_OUTREACH_TASKS.pop(job_id, None)

    task.add_done_callback(_release)


def _outreach_busy() -> bool:
    """Идёт ли уже какой-то job рассылки (сбор или отправка) в Chrome.

    outreach.py держит одну свою вкладку по CDP — вторая параллельная вкладка
    вешает CDP (грабли проекта), поэтому второй collect/send, пока первый
    выполняется, должен получить 409, а не открыть вторую вкладку.
    """
    return any(job.get("status") == "running" for job in OUTREACH_JOBS.values())


def _serialize_outreach_status(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """
    JSON-статус job'а рассылки (общий для сбора и отправки).

    step/step_label/item_index/items_total и resume_available/resume_plan
    заполнены смыслом только у задач отправки (kind == "send") — у сбора
    возобновления нет, задача либо done, либо needs_user_action/failed целиком.
    Формат resume_plan — тот же, что у публикации (publish_state.resume_plan):
    фронт уже на него рассчитан.
    """
    plan = outreach_state.resume_plan(job) if job.get("kind") == "send" else None
    resume_available = (
        job.get("kind") == "send"
        and job.get("status") in {"failed", "needs_user_action", "interrupted"}
        and plan is not None
    )
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "current": job.get("current", 0),
        "total": job.get("total", 0),
        "label": job.get("label", ""),
        "error": job.get("error"),
        "step": job.get("step"),
        "step_label": job.get("step_label"),
        "item_index": job.get("item_index"),
        "items_total": job.get("items_total"),
        "resume_available": resume_available,
        "resume_plan": plan if resume_available else None,
    }


def _outreach_candidate_key(candidate: dict[str, Any]) -> str:
    """Ключ кандидата так же, как его строит фронт (frontend/src/pages/OutreachPage.jsx)."""
    return candidate.get("seller_key") or candidate.get("seller_id") or candidate.get("profile_url") or ""


def _build_outreach_summary(result: dict[str, Any]) -> str:
    """Короткая русская сводка отправки для показа пользователю."""
    summary = (
        f"Отправлено: {result.get('sent', 0)}, "
        f"пропущено: {result.get('skipped', 0)}, "
        f"ошибок: {result.get('failed', 0)}."
    )
    uncertain = result.get("uncertain", 0)
    if uncertain:
        summary += (
            f" Неизвестных: {uncertain} — клик по отправке был сделан, но результат "
            "не подтверждён. Повтор заблокирован, дубля не будет; откройте чат и "
            "проверьте вручную, ушло ли сообщение."
        )
    return summary


def _merge_outreach_result(job: dict[str, Any], result: dict[str, Any]) -> None:
    """
    Прибавляет результат одного прогона send_messages к накопленному итогу задачи.

    После возобновления (resume) send_messages обрабатывает только хвост
    списка кандидатов — /api/outreach/result обязан показывать итог по всей
    задаче целиком, а не только по последнему прогону, иначе повторный запуск
    «теряет» уже отправленные сообщения из сводки. На первом (нерезюмированном)
    прогоне job["result"] по составу ключей совпадает с result один в один —
    только числовые поля суммируются с уже накопленными (изначально 0).
    """
    merged = dict(job.get("result") or {})
    counters = ("sent", "failed", "skipped", "uncertain")
    for key, value in result.items():
        if key == "results" or key in counters:
            continue
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = merged.get(key, 0) + value
        else:
            merged[key] = value
    merged_rows = {}
    for item in list(merged.get("results") or []) + list(result.get("results") or []):
        merged_rows[_outreach_candidate_key(item)] = item
    merged["results"] = list(merged_rows.values())
    for status in counters:
        merged[status] = sum(item.get("status") == status for item in merged["results"])
    job["result"] = merged
    # Плоские счётчики и sent_keys — часть схемы checkpoint'а (outreach_state.py).
    job["sent"] = merged.get("sent", 0)
    job["failed"] = merged.get("failed", 0)
    job["skipped"] = merged.get("skipped", 0)
    job["uncertain"] = merged.get("uncertain", 0)
    job["results"] = merged.get("results", [])
    job["sent_keys"] = sorted(set(job.get("sent_keys") or []) | {
        _outreach_candidate_key(item) for item in merged.get("results", [])
        if item.get("status") == "sent"
    })
    job["summary"] = _build_outreach_summary(merged)


async def _run_outreach_collect_job(job_id: str, city: str, query: str, limit: int) -> None:
    """Фоновая задача сбора кандидатов. Ни одно сообщение здесь не отправляется."""
    job = OUTREACH_JOBS[job_id]

    def progress_cb(done: int, total: int, label: str) -> None:
        job["current"] = done
        job["total"] = total
        job["label"] = label

    try:
        candidates = await outreach.collect_candidates(city, query, limit, progress_cb=progress_cb)
        job["candidates"] = candidates
        job["current"] = len(candidates)
        job["status"] = "done"
        logger.info("Рассылка: сбор %s завершён, кандидатов: %d", job_id, len(candidates))
    except outreach.OutreachStoppedError as exc:
        logger.warning("Рассылка: сбор %s остановлен: %s", job_id, exc)
        job["status"] = "needs_user_action"
        job["error"] = str(exc)
    except Exception as exc:
        logger.error("Рассылка: сбор %s упал: %s", job_id, exc)
        job["status"] = "failed"
        job["error"] = str(exc)


async def _run_outreach_send_job(
    job_id: str,
    candidates: list[dict[str, Any]],
    message: str,
    start_index: int = 1,
) -> None:
    """
    Фоновая задача отправки. Финансово необратимые клики делает outreach.send_messages.

    Дисковый checkpoint (outreach_state.py) пишется после каждого шага и после
    каждого завершённого кандидата — если сервер упадёт или пользователь
    остановит задачу, /api/outreach/resume/{job_id} сможет продолжить, не
    повторяя уже отправленные письма (см. outreach_state.resume_plan).
    """
    job = OUTREACH_JOBS[job_id]
    job_dir = TMP_OUTREACH_DIR / job_id

    def _checkpoint() -> None:
        try:
            outreach_state.save_outreach_state(job_dir, job)
        except Exception:
            logger.warning("Рассылка: checkpoint %s не сохранён", job_id, exc_info=True)

    def progress_cb(done: int, total: int, label: str) -> None:
        job["current"] = done
        job["total"] = total
        job["label"] = label
        _checkpoint()

    def step_cb(step: str, item_index: int) -> None:
        job["step"] = step
        job["step_label"] = outreach.STEP_LABELS.get(step, step)
        job["item_index"] = item_index
        _checkpoint()

    job["step"] = "connect_chrome"
    job["step_label"] = outreach.STEP_LABELS.get("connect_chrome", "connect_chrome")
    job["item_index"] = start_index
    _checkpoint()

    try:
        result = await outreach.send_messages(
            candidates, message, progress_cb=progress_cb, step_cb=step_cb,
            start_index=start_index,
        )
        _merge_outreach_result(job, result)
        job["current"] = len(candidates)
        job["status"] = "done"
        _checkpoint()
        outreach_state.clear_outreach_state(job_dir)
        logger.info("Рассылка: отправка %s завершена: %s", job_id, job["summary"])
    except outreach.OutreachStoppedError as exc:
        logger.warning("Рассылка: отправка %s остановлена: %s", job_id, exc)
        job["status"] = "needs_user_action"
        job["error"] = str(exc)
        if exc.result:
            _merge_outreach_result(job, exc.result)
        _checkpoint()
    except Exception as exc:
        logger.error("Рассылка: отправка %s упала: %s", job_id, exc)
        job["status"] = "failed"
        job["error"] = str(exc)
        _checkpoint()


@app.post("/api/outreach/import")
async def api_outreach_import(request: Request) -> JSONResponse:
    """Массовый импорт ссылок «уже писали» в постоянную историю контактов."""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}

    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list):
        return JSONResponse(
            {"errors": [{"field": "urls", "error": "urls должен быть списком ссылок"}]},
            status_code=422,
        )

    result = outreach_store.import_contact_urls([str(url) for url in urls])
    logger.info(
        "Рассылка: импорт истории — добавлено %d, уже было %d, некорректных %d",
        result["imported"], result["skipped"], len(result["invalid"]),
    )
    return JSONResponse(result)


@app.get("/api/outreach/history")
async def api_outreach_history() -> JSONResponse:
    """Вся история контактов, новые сверху."""
    return JSONResponse({"contacts": outreach_store.list_contacts()})


@app.post("/api/outreach/collect")
async def api_outreach_collect(request: Request) -> JSONResponse:
    """Запускает фоновый сбор кандидатов в отдельной вкладке Chrome по CDP."""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    city = str(payload.get("city") or "")
    query = str(payload.get("query") or "").strip()
    raw_limit = payload.get("limit")

    errors: list[dict[str, str]] = []
    if get_city_by_slug(city) is None:
        errors.append({"field": "city", "error": "Выберите известный город"})
    if not query:
        errors.append({"field": "query", "error": "Поисковый запрос не заполнен"})
    try:
        limit = int(raw_limit)
        if not 1 <= limit <= 150:
            raise ValueError
    except (TypeError, ValueError):
        errors.append({"field": "limit", "error": "Лимит должен быть целым числом от 1 до 150"})
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    if _outreach_busy():
        return JSONResponse(
            {"error": "В Chrome уже выполняется задача рассылки — дождитесь её завершения"},
            status_code=409,
        )

    job_id = str(uuid.uuid4())
    OUTREACH_JOBS[job_id] = {
        "kind": "collect",
        "status": "running",
        "current": 0,
        "total": limit,
        "label": "",
        "error": None,
        "candidates": None,
        "send_job_id": None,
    }
    _schedule_outreach(job_id, _run_outreach_collect_job(job_id, city, query, limit))
    logger.info("Рассылка: сбор %s создан (город=%s, запрос='%s', лимит=%d)", job_id, city, query, limit)
    return JSONResponse({"job_id": job_id, "status": "running"})


@app.get("/api/outreach/status/{job_id}")
async def api_outreach_status(job_id: str) -> JSONResponse:
    """JSON-статус job'а рассылки (сбор или отправка) для опроса фронтом."""
    if job_id not in OUTREACH_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(_serialize_outreach_status(job_id, OUTREACH_JOBS[job_id]))


@app.get("/api/outreach/result/{job_id}")
async def api_outreach_result(job_id: str) -> JSONResponse:
    """Результат job'а: кандидаты для сбора, итог отправки для рассылки."""
    if job_id not in OUTREACH_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = OUTREACH_JOBS[job_id]
    base = _serialize_outreach_status(job_id, job)
    if job.get("kind") == "collect":
        return JSONResponse({**base, "candidates": job.get("candidates") or []})

    result = job.get("result") or {}
    return JSONResponse({
        **base,
        "sent": result.get("sent", 0),
        "failed": result.get("failed", 0),
        "skipped": result.get("skipped", 0),
        "results": result.get("results", []),
        "summary": job.get("summary", ""),
    })


@app.post("/api/outreach/send/{job_id}")
async def api_outreach_send(job_id: str, request: Request) -> JSONResponse:
    """
    Подтверждённая отправка выбранным кандидатам завершённого сбора.

    job_id в пути — задача СБОРА; в ответе — job_id новой задачи ОТПРАВКИ.
    Отправка необратима и платна ошибкой — любое несовпадение входа
    останавливает создание задачи, а не запускает её «на всякий случай».
    """
    collect_job = OUTREACH_JOBS.get(job_id)
    if collect_job is None or collect_job.get("kind") != "collect":
        return JSONResponse({"status": "not_found"}, status_code=404)

    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    if payload.get("confirmed") is not True:
        return JSONResponse(
            {"error": "Отправка требует явного подтверждения (confirmed: true)"},
            status_code=400,
        )

    candidate_keys = payload.get("candidate_keys")
    if not isinstance(candidate_keys, list) or not candidate_keys:
        return JSONResponse({"error": "Нужно выбрать хотя бы одного продавца"}, status_code=400)

    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return JSONResponse({"error": "Заполните текст письма"}, status_code=400)
    stripped_length = len(message.strip())
    if stripped_length > OUTREACH_MESSAGE_MAX_LENGTH:
        return JSONResponse(
            {
                "error": (
                    f"Шаблон письма длиннее {OUTREACH_MESSAGE_MAX_LENGTH} символов "
                    f"(сейчас {stripped_length}) — поле ввода Авито обрежет текст "
                    f"молча, сократите шаблон на {stripped_length - OUTREACH_MESSAGE_MAX_LENGTH}"
                )
            },
            status_code=400,
        )

    candidates = collect_job.get("candidates")
    if collect_job.get("status") != "done" or candidates is None:
        return JSONResponse({"error": "Сбор кандидатов ещё не завершён"}, status_code=400)

    candidate_map = {_outreach_candidate_key(candidate): candidate for candidate in candidates}
    unknown_keys = [key for key in candidate_keys if key not in candidate_map]
    if unknown_keys:
        return JSONResponse(
            {"error": "Среди candidate_keys есть ключи не из этого сбора кандидатов"},
            status_code=400,
        )

    if collect_job.get("send_job_id"):
        return JSONResponse(
            {"error": "Отправка по этому сбору кандидатов уже была запущена"},
            status_code=409,
        )
    if _outreach_busy():
        return JSONResponse(
            {"error": "В Chrome уже выполняется задача рассылки — дождитесь её завершения"},
            status_code=409,
        )

    # Дедупликация с сохранением порядка — на случай повторов в candidate_keys.
    selected = [candidate_map[key] for key in dict.fromkeys(candidate_keys)]
    message = message.strip()

    send_job_id = str(uuid.uuid4())
    OUTREACH_JOBS[send_job_id] = {
        "job_id": send_job_id,
        "kind": "send",
        "status": "running",
        "current": 0,
        "total": len(selected),
        "label": "",
        "error": None,
        "result": None,
        "summary": "",
        # Поля checkpoint'а (outreach_state.py) — полный список кандидатов и
        # текст письма нужны целиком, чтобы resume мог продолжить с диска.
        "candidates": selected,
        "message": message,
        "step": "",
        "step_label": "",
        "item_index": 0,
        "items_total": len(selected),
        "sent_keys": [],
        "skipped_items": [],
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "uncertain": 0,
        "results": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    collect_job["send_job_id"] = send_job_id
    _schedule_outreach(send_job_id, _run_outreach_send_job(send_job_id, selected, message))
    logger.info(
        "Рассылка: отправка %s создана из сбора %s (получателей: %d)",
        send_job_id, job_id, len(selected),
    )
    return JSONResponse({"job_id": send_job_id, "status": "running"})


@app.post("/api/outreach/resume/{job_id}")
async def api_outreach_resume(job_id: str, request: Request) -> JSONResponse:
    """
    Продолжает остановленную отправку с первого необработанного кандидата.

    Калька с api_publish_resume (см. publish_state.resume_plan): два режима —
    retry_item (до клика отправки, кандидата можно начать заново) и skip_item
    (клик уже сделан, кандидата пропускаем и едем дальше, номер отдаём наружу
    для ручной проверки). Дополнительно, в отличие от публикации, проверяем
    _outreach_busy() — рассылка держит ровно одну вкладку по CDP, вторая
    параллельная вкладка вешает CDP (грабли проекта, см. CLAUDE.md).
    """
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in {"retry_current", "skip_current"}:
        return JSONResponse(
            {"error": "Нужно явно выбрать action: retry_current или skip_current"}, status_code=422,
        )

    current_task = ACTIVE_OUTREACH_TASKS.get(job_id)
    if current_task is not None and not current_task.done():
        return JSONResponse({"error": "Задача рассылки уже выполняется"}, status_code=409)

    job_dir = TMP_OUTREACH_DIR / job_id
    try:
        saved_job = outreach_state.load_outreach_state(job_dir)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("Resume рассылки %s невозможен: %s", job_id, exc)
        return JSONResponse(
            {"error": "Сохранённое состояние рассылки не найдено или повреждено"},
            status_code=404,
        )

    plan = outreach_state.resume_plan(saved_job)
    if action == "retry_current":
        if plan is None or plan["mode"] != "retry_item":
            return JSONResponse(
                {"error": "Повтор запрещён: клик отправки уже мог быть сделан, возможен дубль письма."},
                status_code=409,
            )
        start_index = plan["start_index"]
        skipped_item = None
    else:
        try:
            skipped_item = int(saved_job.get("item_index") or 0)
            items_total = int(saved_job.get("items_total") or 0)
            candidate = saved_job["candidates"][skipped_item - 1]
        except (IndexError, KeyError, TypeError, ValueError):
            return JSONResponse({"error": "Текущий кандидат для пропуска не найден"}, status_code=409)
        if not 1 <= skipped_item <= items_total:
            return JSONResponse({"error": "Текущий кандидат для пропуска не найден"}, status_code=409)
        start_index = skipped_item + 1
        saved_job["skipped_items"] = sorted(
            set(saved_job.get("skipped_items") or []) | {skipped_item}
        )
        if saved_job.get("step") == "back_to_search":
            # Этот шаг начинается только после подтверждённой отправки.
            skipped_status = "sent"
        else:
            skipped_status = "skipped" if plan and plan["mode"] == "retry_item" else "uncertain"
        skipped_error = (
            "Пропущен вручную"
            if skipped_status == "skipped"
            else ("" if skipped_status == "sent"
                  else "Пропущен вручную; результат отправки неизвестен, проверьте чат.")
        )
        _merge_outreach_result(saved_job, {
            "sent": int(skipped_status == "sent"), "failed": 0,
            "skipped": int(skipped_status == "skipped"),
            "uncertain": int(skipped_status == "uncertain"),
            "results": [{**candidate, "status": skipped_status, "error": skipped_error}],
        })
        logger.warning("Рассылка %s: кандидат №%d пропущен вручную", job_id, skipped_item)

        if start_index > items_total:
            saved_job.update(status="done", error=None, current=items_total)
            OUTREACH_JOBS[job_id] = saved_job
            outreach_state.clear_outreach_state(job_dir)
            logger.info("Рассылка %s завершена ручным пропуском последнего кандидата", job_id)
            return JSONResponse({
                "job_id": job_id, "action": action, "resumed_from": start_index,
                "skipped_item": skipped_item, "status": "done",
            })

    if _outreach_busy():
        return JSONResponse(
            {"error": "В Chrome уже выполняется задача рассылки — дождитесь её завершения"},
            status_code=409,
        )

    saved_job["status"] = "running"
    saved_job["error"] = None
    saved_job["current"] = start_index - 1
    saved_job["item_index"] = start_index
    saved_job["step"] = "connect_chrome"
    saved_job["step_label"] = outreach.STEP_LABELS["connect_chrome"]
    try:
        outreach_state.save_outreach_state(job_dir, saved_job)
    except OSError as exc:
        logger.error("Рассылка %s: не удалось сохранить checkpoint перед продолжением: %s", job_id, exc)
        return JSONResponse({"error": "Не удалось сохранить состояние перед продолжением"}, status_code=500)
    OUTREACH_JOBS[job_id] = saved_job
    _schedule_outreach(
        job_id,
        _run_outreach_send_job(
            job_id, saved_job["candidates"], saved_job["message"], start_index=start_index,
        ),
    )
    logger.info(
        "Рассылка %s продолжена с кандидата %d действием %s",
        job_id, start_index, action,
    )
    return JSONResponse({
        "job_id": job_id,
        "action": action,
        "resumed_from": start_index,
        "mode": plan["mode"] if plan else "skip_item",
        "skipped_item": skipped_item,
    })


# ---------------------------------------------------------------------------
# Разбор продавца (seller_scan.py): просмотры всех объявлений со страницы
# профиля продавца — читаем, не кликаем.
# ---------------------------------------------------------------------------


def _schedule_it_outreach(job_id: str, coro: Any) -> None:
    """Запускает IT-поиск и удерживает ссылку на фоновую задачу."""
    task = asyncio.create_task(coro)
    ACTIVE_IT_OUTREACH_TASKS[job_id] = task

    def _release(completed: asyncio.Task[Any]) -> None:
        if ACTIVE_IT_OUTREACH_TASKS.get(job_id) is completed:
            ACTIVE_IT_OUTREACH_TASKS.pop(job_id, None)

    task.add_done_callback(_release)


def _serialize_it_outreach_status(job: dict[str, Any]) -> dict[str, Any]:
    """Контракт статуса IT-поиска для периодического опроса frontend."""
    return {
        "status": job.get("status"),
        "scanned": job.get("scanned", 0),
        "total": job.get("total", 0),
        "found_emails": job.get("found_emails", 0),
        "current_site": job.get("current_site", ""),
        "error": job.get("error"),
    }


async def _run_it_outreach_job(job_id: str, limit: int) -> None:
    """Находит и проверяет сайты, сохраняя каждый результат сразу в SQLite."""
    job = IT_OUTREACH_JOBS[job_id]
    try:
        companies = await asyncio.to_thread(it_outreach_store.list_companies, IT_OUTREACH_DB_PATH)
        known_domains = {str(company["domain"]) for company in companies if company.get("domain")}
        sites = await asyncio.to_thread(it_outreach.discover_sites, limit, known_domains)
        job["total"] = len(sites)
        for site_url in sites:
            job["current_site"] = site_url
            try:
                record = await asyncio.to_thread(it_outreach.inspect_site, site_url)
            except it_outreach.SourceUnavailableError as exc:
                job["status"] = "blocked"
                job["error"] = f"Источник недоступен: {exc}"
                return
            except Exception as exc:
                record = {
                    "domain": it_outreach.domain_key(site_url), "site_url": site_url,
                    "status": "error", "error": str(exc),
                }
            await asyncio.to_thread(it_outreach_store.upsert_company, record, IT_OUTREACH_DB_PATH)
            job["scanned"] += 1
            job["found_emails"] += int(bool(record.get("email")))
        job["status"] = "done"
        job["current_site"] = ""
        logger.info("IT-поиск %s завершён: сайтов %d, почт %d", job_id, job["scanned"], job["found_emails"])
    except it_outreach.SourceUnavailableError as exc:
        logger.warning("IT-поиск %s остановлен: %s", job_id, exc)
        job["status"] = "blocked"
        job["error"] = f"Источник недоступен: {exc}"
    except Exception as exc:
        logger.exception("IT-поиск %s упал: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


@app.post("/api/it-outreach/start")
async def api_it_outreach_start(request: Request) -> JSONResponse:
    """Запускает независимый от Chrome фоновый поиск IT-контактов."""
    try:
        payload = await request.json()
        raw_limit = payload.get("limit") if isinstance(payload, dict) else None
        limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 0
    except ValueError:
        limit = 0
    if not 1 <= limit <= 100:
        return JSONResponse({"error": "limit должен быть целым числом от 1 до 100"}, status_code=400)
    if any(job.get("status") == "running" for job in IT_OUTREACH_JOBS.values()):
        return JSONResponse({"error": "IT-поиск уже выполняется"}, status_code=409)

    job_id = str(uuid.uuid4())
    IT_OUTREACH_JOBS[job_id] = {
        "status": "running", "scanned": 0, "total": 0, "found_emails": 0,
        "current_site": "", "error": None,
    }
    _schedule_it_outreach(job_id, _run_it_outreach_job(job_id, limit))
    return JSONResponse({"job_id": job_id})


@app.get("/api/it-outreach/status/{job_id}")
async def api_it_outreach_status(job_id: str) -> JSONResponse:
    """Возвращает состояние фонового поиска IT-контактов."""
    job = IT_OUTREACH_JOBS.get(job_id)
    if job is None:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(_serialize_it_outreach_status(job))


@app.get("/api/it-outreach/results")
async def api_it_outreach_results() -> JSONResponse:
    """Возвращает сохранённые SQLite-результаты, переживающие рестарт сервера."""
    companies = await asyncio.to_thread(it_outreach_store.list_companies, IT_OUTREACH_DB_PATH)
    return JSONResponse({"companies": companies})


def _csv_safe(value: Any) -> str:
    """Не даёт Excel интерпретировать текстовую ячейку как формулу."""
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@app.get("/api/it-outreach/export.csv")
async def api_it_outreach_export_csv() -> StreamingResponse:
    """Экспортирует SQLite-результаты в безопасный для Excel CSV."""
    companies = await asyncio.to_thread(it_outreach_store.list_companies, IT_OUTREACH_DB_PATH)
    columns = (
        ("domain", "Домен"), ("company_name", "Компания"), ("city", "Город"),
        ("site_url", "Сайт"), ("vacancy_url", "Вакансии"),
        ("internship_url", "Стажировки"), ("email", "Email"),
        ("email_kind", "Тип email"), ("source_url", "Страница-источник"),
        ("status", "Статус"), ("error", "Ошибка"), ("checked_at", "Проверено"),
    )
    output = io.StringIO()
    writer = csv.writer(output, dialect="excel")
    writer.writerow([label for _, label in columns] + ["Источник"])
    attribution = "OpenStreetMap (ODbL): https://www.openstreetmap.org/copyright"
    for company in companies:
        writer.writerow([_csv_safe(company.get(key)) for key, _ in columns] + [attribution])
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="it_outreach.csv"'},
    )


def _schedule_seller_scan(job_id: str, coro: Any) -> None:
    """Запускает фоновую задачу разбора продавца, удерживая ссылку до конца."""
    task = asyncio.create_task(coro)
    ACTIVE_SELLER_SCAN_TASKS[job_id] = task

    def _release(completed: asyncio.Task[Any]) -> None:
        if ACTIVE_SELLER_SCAN_TASKS.get(job_id) is completed:
            ACTIVE_SELLER_SCAN_TASKS.pop(job_id, None)

    task.add_done_callback(_release)


def _seller_scan_busy() -> bool:
    """Идёт ли уже задача, держащая вкладку Chrome — свой разбор, рассылка,
    публикация или поиск под перепродажу (resale). Все они работают через
    одно CDP-подключение к живому Chrome пользователя, вторая одновременная
    вкладка мешает первой (см. _outreach_busy) — новый разбор в таком случае
    получает 409. Эта же функция используется для 409 старта resale
    (см. api_resale_scan) — имя оставлено историческим, проверка общая
    для всех задач, занимающих Chrome."""
    return (
        any(job.get("status") == "running" for job in SELLER_JOBS.values())
        or _outreach_busy()
        or any(job.get("status") == "running" for job in PUBLISH_JOBS.values())
        or any(job.get("status") == "running" for job in RESALE_JOBS.values())
    )


def _serialize_seller_scan_status(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """JSON-статус job'а разбора продавца для опроса фронтом."""
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "current": job.get("current", 0),
        "total": job.get("total", 0),
        "label": job.get("label", ""),
        "error": job.get("error"),
        "profile_url": job.get("profile_url", ""),
        "limit": job.get("limit", 0),
    }


async def _run_seller_scan_job(job_id: str, profile_url: str, limit: int) -> None:
    """Фоновая задача разбора продавца. Ни одного клика по Авито — только чтение."""
    job = SELLER_JOBS[job_id]

    def progress_cb(done: int, total: int, label: str) -> None:
        job["current"] = done
        job["total"] = total
        job["label"] = label

    try:
        result = await seller_scan.scan_seller(profile_url, limit, progress_cb=progress_cb)
        job["result"] = result
        job["current"] = result["scanned"]
        job["status"] = "done"
        logger.info(
            "Разбор продавца %s завершён: пройдено %d, ошибок %d, найдено всего %s",
            job_id, result["scanned"], result["errors"], result.get("found_total"),
        )
    except AvitoBlockedError as exc:
        # Тот же статус, что у аналитики (см. run_job) — дальше долбить
        # заблокированный IP нельзя, задача целиком останавливается.
        logger.error("Разбор продавца %s остановлен блокировкой Авито: %s", job_id, exc)
        job["status"] = "blocked"
        job["error"] = "Авито заблокировал запрос. Попробуйте включить VPN и повторить."
    except Exception as exc:
        logger.error("Разбор продавца %s упал: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


@app.post("/api/seller/scan")
async def api_seller_scan(request: Request) -> JSONResponse:
    """Запускает фоновый разбор продавца в отдельной вкладке Chrome по CDP."""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    errors: list[dict[str, str]] = []
    try:
        profile_url = seller_scan.validate_profile_url(str(payload.get("url") or ""))
    except ValueError as exc:
        profile_url = ""
        errors.append({"field": "url", "error": str(exc)})

    raw_limit = payload.get("limit")
    try:
        limit = int(raw_limit)
        if not 1 <= limit <= SELLER_SCAN_MAX_LIMIT:
            raise ValueError
    except (TypeError, ValueError):
        limit = 0
        errors.append({
            "field": "limit",
            "error": f"Лимит должен быть целым числом от 1 до {SELLER_SCAN_MAX_LIMIT}",
        })
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    if _seller_scan_busy():
        return JSONResponse(
            {"error": "В Chrome уже выполняется другая задача (разбор/рассылка/публикация) — дождитесь её завершения"},
            status_code=409,
        )

    job_id = str(uuid.uuid4())
    SELLER_JOBS[job_id] = {
        "status": "running", "current": 0, "total": limit, "label": "",
        "error": None, "result": None, "profile_url": profile_url, "limit": limit,
    }
    _schedule_seller_scan(job_id, _run_seller_scan_job(job_id, profile_url, limit))
    logger.info("Разбор продавца %s создан (url=%s, лимит=%d)", job_id, profile_url, limit)
    return JSONResponse({"job_id": job_id, "status": "running"})


@app.get("/api/seller/status/{job_id}")
async def api_seller_scan_status(job_id: str) -> JSONResponse:
    """JSON-статус job'а разбора продавца для опроса фронтом."""
    if job_id not in SELLER_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(_serialize_seller_scan_status(job_id, SELLER_JOBS[job_id]))


@app.get("/api/seller/result/{job_id}")
async def api_seller_scan_result(job_id: str) -> JSONResponse:
    """Результат разбора: сводка + обе таблицы, уже отсортированные бэкендом."""
    if job_id not in SELLER_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)

    job = SELLER_JOBS[job_id]
    result = job.get("result") or {}
    items = result.get("items") or []
    return JSONResponse({
        **_serialize_seller_scan_status(job_id, job),
        "found_total": result.get("found_total"),
        "scanned": result.get("scanned", 0),
        "errors": result.get("errors", 0),
        "duplicates": result.get("duplicates", 0),
        "items_by_total": seller_scan.sort_items_by_views(items, "views_total"),
        "items_by_today": seller_scan.sort_items_by_views(items, "views_today"),
    })


@app.get("/api/seller/export.csv")
async def api_seller_scan_export_csv(job_id: str, sort: str = "total") -> StreamingResponse:
    """
    CSV одной из двух таблиц разбора продавца (UTF-8 с BOM — как /export.csv).

    sort: "total" — по просмотрам всего, "today" — по просмотрам сегодня.
    Колонки: Название, Ссылка, Просмотров всего, Просмотров сегодня, Ошибка.
    """
    job = SELLER_JOBS.get(job_id)
    result = (job or {}).get("result") or {}
    items = result.get("items") or []
    key = "views_today" if sort == "today" else "views_total"
    rows = seller_scan.sort_items_by_views(items, key)

    output = io.StringIO()
    writer = csv.writer(output, dialect="excel")
    writer.writerow(["Название", "Ссылка", "Просмотров всего", "Просмотров сегодня", "Ошибка"])
    for item in rows:
        writer.writerow([
            item.get("title") or "",
            item.get("url") or "",
            item.get("views_total") if item.get("views_total") is not None else "",
            item.get("views_today") if item.get("views_today") is not None else "",
            item.get("error") or "",
        ])
    content = output.getvalue()

    suffix = "today" if sort == "today" else "total"
    filename_ascii = f"avito_seller_{suffix}.csv"
    content_disposition = f'attachment; filename="{filename_ascii}"'

    return StreamingResponse(
        iter([content.encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": content_disposition},
    )


# ---------------------------------------------------------------------------
# Поиск под перепродажу (resale_scan.py, docs/specs/resale-finder.md)
# ---------------------------------------------------------------------------

def _validate_resale_payload(payload: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[dict[str, str]]]:
    """Валидирует тело POST /api/resale/scan. errors пуст ⇔ данные не None."""
    errors: list[dict[str, str]] = []

    query = str(payload.get("query") or "").strip()
    if not query:
        errors.append({"field": "query", "error": "Запрос не заполнен"})

    city_slug = str(payload.get("city") or "").strip()
    city = get_city_by_slug(city_slug) if city_slug else None
    if city is None:
        errors.append({"field": "city", "error": "Город не найден в справочнике"})

    raw_threshold = payload.get("threshold_pct", 20)
    try:
        threshold_pct = float(raw_threshold)
        if not (RESALE_THRESHOLD_PCT_MIN <= threshold_pct <= RESALE_THRESHOLD_PCT_MAX):
            raise ValueError
    except (TypeError, ValueError):
        threshold_pct = 0.0
        errors.append({
            "field": "threshold_pct",
            "error": f"Порог скидки должен быть числом от {RESALE_THRESHOLD_PCT_MIN} до {RESALE_THRESHOLD_PCT_MAX}",
        })

    raw_max_items = payload.get("max_items", 150)
    try:
        max_items = int(raw_max_items)
        if not (RESALE_MAX_ITEMS_MIN <= max_items <= RESALE_MAX_ITEMS_MAX):
            raise ValueError
    except (TypeError, ValueError):
        max_items = 0
        errors.append({
            "field": "max_items",
            "error": f"Максимум объявлений должен быть целым числом от {RESALE_MAX_ITEMS_MIN} до {RESALE_MAX_ITEMS_MAX}",
        })

    if errors:
        return None, errors

    filters = SearchFilters.from_form(payload.get("price_min"), payload.get("price_max"))
    return {
        "query": query, "city": city, "threshold_pct": threshold_pct,
        "max_items": max_items, "filters": filters,
    }, []


def _schedule_resale_scan(job_id: str, coro: Any) -> None:
    """Запускает фоновую задачу поиска под перепродажу, удерживая ссылку до
    конца (тот же приём, что ACTIVE_SELLER_SCAN_TASKS/ACTIVE_OUTREACH_TASKS —
    без него GC может собрать задачу посреди разбора)."""
    task = asyncio.create_task(coro)
    ACTIVE_RESALE_TASKS[job_id] = task

    def _release(completed: asyncio.Task[Any]) -> None:
        if ACTIVE_RESALE_TASKS.get(job_id) is completed:
            ACTIVE_RESALE_TASKS.pop(job_id, None)

    task.add_done_callback(_release)


def _serialize_resale_status(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """JSON-статус job'а resale для опроса фронтом."""
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "stage": job.get("stage", ""),
        "current": job.get("current", 0),
        "total": job.get("total", 0),
        "error": job.get("error"),
    }


async def _run_resale_scan_job(
    job_id: str,
    query: str,
    city: City,
    threshold_pct: float,
    max_items: int,
    filters: SearchFilters,
) -> None:
    """Фоновая задача поиска под перепродажу. Только чтение Авито — ни одного
    денежного клика (см. docs/specs/resale-finder.md)."""
    job = RESALE_JOBS[job_id]

    def progress_cb(stage: str, done: int, total: int) -> None:
        job["stage"] = stage
        job["current"] = done
        job["total"] = total

    try:
        result = await resale_scan.run_resale_scan(
            query, city,
            max_items=max_items, threshold_pct=threshold_pct,
            filters=filters, progress_cb=progress_cb,
        )
        job["result"] = result
        job["status"] = "done"
        logger.info(
            "Resale %s завершён: объявлений %d, лотов %d, доборов %d",
            job_id, result.get("total_items", 0),
            len(result.get("deals") or []), len(result.get("topup_queries") or []),
        )
    except AvitoBlockedError as exc:
        # Тот же статус, что у аналитики и разбора продавца (см. run_job,
        # _run_seller_scan_job) — дальше долбить заблокированный IP нельзя.
        logger.error("Resale %s остановлен блокировкой Авито: %s", job_id, exc)
        job["status"] = "blocked"
        job["error"] = "Авито заблокировал запрос. Попробуйте включить VPN и повторить."
    except resale_classifier.ClaudeCliError as exc:
        # claude CLI не работает систематически (не найден/авторизация/сеть) —
        # см. брифинг задачи: не гоняем десятки заведомо провальных пачек.
        logger.error("Resale %s: claude CLI недоступен: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = (
            f"Не удалось разобрать объявления через claude CLI: {exc}. "
            "Вероятная причина — VPN не пускает claude, либо CLI не авторизован."
        )
    except Exception as exc:
        logger.error("Resale %s упал: %s", job_id, exc)
        job["status"] = "error"
        job["error"] = str(exc)


@app.post("/api/resale/scan")
async def api_resale_scan(request: Request) -> JSONResponse:
    """Запускает фоновый поиск под перепродажу в отдельной вкладке Chrome по CDP."""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    data, errors = _validate_resale_payload(payload)
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)

    if _seller_scan_busy():
        return JSONResponse(
            {"error": "В Chrome уже выполняется другая задача (разбор/рассылка/публикация/перепродажа) — дождитесь её завершения"},
            status_code=409,
        )

    job_id = str(uuid.uuid4())
    RESALE_JOBS[job_id] = {
        "status": "running", "stage": "collecting", "current": 0, "total": data["max_items"],
        "error": None, "result": None,
        "query": data["query"], "city": data["city"].slug,
    }
    _schedule_resale_scan(job_id, _run_resale_scan_job(
        job_id, data["query"], data["city"], data["threshold_pct"], data["max_items"], data["filters"],
    ))
    logger.info("Resale %s создан (query=%s, city=%s)", job_id, data["query"], data["city"].slug)
    return JSONResponse({"job_id": job_id, "status": "running"})


@app.get("/api/resale/status/{job_id}")
async def api_resale_scan_status(job_id: str) -> JSONResponse:
    """JSON-статус job'а resale для опроса фронтом."""
    if job_id not in RESALE_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(_serialize_resale_status(job_id, RESALE_JOBS[job_id]))


@app.get("/api/resale/result/{job_id}")
async def api_resale_scan_result(job_id: str) -> JSONResponse:
    """Статус + отчёт resale_scan.run_resale_scan (deals/groups/excluded/...)."""
    if job_id not in RESALE_JOBS:
        return JSONResponse({"status": "not_found"}, status_code=404)
    job = RESALE_JOBS[job_id]
    result = job.get("result") or {}
    return JSONResponse({
        **_serialize_resale_status(job_id, job),
        **result,
    })


@app.get("/api/resale/export.csv")
async def api_resale_scan_export_csv(job_id: str) -> StreamingResponse:
    """
    CSV выгодных лотов resale (UTF-8 с BOM — как /export.csv).

    Колонки: Модель, Название, Цена, Рынок, Скидка %, Выборка, Мало данных,
    Состояние, Ссылка.
    """
    job = RESALE_JOBS.get(job_id)
    result = (job or {}).get("result") or {}
    deals = result.get("deals") or []

    output = io.StringIO()
    writer = csv.writer(output, dialect="excel")
    writer.writerow([
        "Модель", "Название", "Цена", "Рынок", "Скидка %", "Выборка",
        "Мало данных", "Состояние", "Ссылка",
    ])
    for deal in deals:
        writer.writerow([
            deal.get("model") or "",
            deal.get("title") or "",
            deal.get("price") if deal.get("price") is not None else "",
            deal.get("market_price") if deal.get("market_price") is not None else "",
            deal.get("discount_pct") if deal.get("discount_pct") is not None else "",
            deal.get("sample") if deal.get("sample") is not None else "",
            "да" if deal.get("low_data") else "нет",
            deal.get("condition") or "",
            deal.get("url") or "",
        ])
    content = output.getvalue()

    return StreamingResponse(
        iter([content.encode("utf-8-sig")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="resale_deals.csv"'},
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Боевой запуск сервера пишет журнал агентов в agents.db в корне проекта.
    # Импорт app.py как модуля (тесты, смоук-скрипты) этот блок не выполняет —
    # journal.DB_PATH остаётся None, журнал молчит, боевая база не портится.
    if journal.DB_PATH is None:
        journal.DB_PATH = str(PROJECT_ROOT / "agents.db")
    # Ключи API Авито (AVITO_CLIENT_ID / AVITO_CLIENT_SECRET) — из .env в корне.
    # Тоже только при боевом запуске: тесты не должны видеть реальные ключи.
    env_file.load_env_file()
    uvicorn.run(app, host="127.0.0.1", port=7777)
