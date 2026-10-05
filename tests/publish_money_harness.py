"""
Стенд «поддельный Авито под настоящими run_publish_job / publish_state /
app» — облегчённый перенос tmp/audit/spec/harness.py.

Перенесены денежные требования R1 и R3 (docs аудита,
tmp/audit/spec/EXPECTATIONS.md) и быстрая проверка достижимых состояний
диска (tmp/audit/verify2/A, класс A1_ReachableDiskStates). R17 из брифа
не перенесён — на сегодняшнем коде он не выполняется (подтверждённая
находка S6, см. docstring tests/test_publish_money_invariants.py), а
переносить тест «наоборот» бриф запрещает. Полный стенд R1-R17 (49 тестов,
~7.5 мин) и полный перебор A1 (A_FULL=1, ~1.7 часа) НЕ переносятся — см.
tests/test_publish_money_invariants.py и
tests/test_publish_disk_state_invariants.py.

Сознательно не перенесено (не нужно R1/R3 и быстрому A1):
  - окна W1/W2, item_id_mismatch, unpersisted_sent, refill_after_created —
    обслуживают требования R2/R4-R9, которые здесь не проверяются;
  - gates (конкурентный resume во время работы) — только для R11;
  - real_continue_listing (настоящий publisher._step_continue_listing) —
    только для одного сценария R12, здесь всегда фейковый шаг.

Подменяется только внешний мир: шаги Playwright (publisher._step_*), CDP,
Playwright-драйвер и диск в момент «убийства» сервера (снимок tmp/publish
и откат к нему). Управление пакетом настоящее: app.api_publish_start/
resume/status/result, app._schedule_publish/_run_publish_and_cleanup,
publisher.run_publish_job/_run_single_item, publish_state целиком (запись
на диск — через настоящую save_publish_state).
"""

from __future__ import annotations

import publish_test_isolation  # noqa: F401 — первым: мёртвый CDP, temp dirs, файловый лог publisher.py выключен

import asyncio
import contextlib
import io
import json
import logging
import shutil
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import app as backend_app  # noqa: E402
import category_profiles  # noqa: E402
import journal  # noqa: E402
import publish_state  # noqa: E402
import publisher  # noqa: E402
from playwright.async_api import Error as PlaywrightError  # noqa: E402
from starlette.datastructures import Headers, UploadFile  # noqa: E402

# Ссылка на настоящую save_publish_state — Harness подменяет
# publish_state.save_publish_state на av.save (для точек/сбоев), а тот
# вызывает REAL_SAVE. Тесты диска (A1-лайт) вместо этого подменяют сам
# REAL_SAVE на перехватчик — тогда точки/сбои Harness работают как обычно,
# а запись реально попадает под наблюдение.
REAL_SAVE = publish_state.save_publish_state
FAULT_KINDS = ("exc", "kill", "cancel")
LISTING_ID_BASE = 7730001


class ServerKilled(BaseException):
    """Процесс сервера убит. BaseException: except Exception в коде его не ловит."""


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


class _PlaywrightManager:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Поддельный браузер
# ---------------------------------------------------------------------------

class FakeForm:
    def __init__(self) -> None:
        self.title: Optional[str] = None
        self.description: Optional[str] = None
        self.photos: tuple[str, ...] = ()
        self.address_index: Optional[int] = None
        self.address_filled = False
        self.clicked = False
        self.listing: Optional[dict[str, Any]] = None


class FakePage:
    def __init__(self, avito: "FakeAvito") -> None:
        self.avito = avito
        self.url = "about:blank"
        self.form: Optional[FakeForm] = None
        self.closed = False
        self.context = avito.context

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        await self.avito.apoint("page_close")
        self.closed = True

    async def screenshot(self, **_kwargs: Any) -> None:
        await self.avito.apoint("dump_screenshot")

    async def content(self) -> str:
        await self.avito.apoint("dump_content")
        return "<html></html>"


class FakeContext:
    def __init__(self, avito: "FakeAvito") -> None:
        self.avito = avito
        self.pages: list[FakePage] = []

    async def new_page(self) -> FakePage:
        await self.avito.apoint("new_page")
        page = FakePage(self.avito)
        self.pages.append(page)
        return page


# ---------------------------------------------------------------------------
# Поддельный Авито: журнал денежных действий + вброс сбоев по номеру события
# ---------------------------------------------------------------------------

class FakeAvito:
    def __init__(self, harness: "Harness") -> None:
        self.h = harness
        self.context = FakeContext(self)
        self.counter = 0
        self.trace: list[tuple[int, str, Optional[int]]] = []
        self.faults: dict[int, str] = {}
        self.fault_log: list[dict[str, Any]] = []
        self.dead = False
        self.listings: list[dict[str, Any]] = []
        self.clicks: Counter = Counter()
        self.next_id = LISTING_ID_BASE

    # --- журнал ---------------------------------------------------------
    def created_indices(self) -> Counter:
        return Counter(listing["index"] for listing in self.listings)

    # --- точки и сбои -----------------------------------------------------
    def point(self, name: str, index: Optional[int] = None) -> None:
        if self.dead:
            raise ServerKilled(f"код исполняется после смерти процесса: {name}")
        self.counter += 1
        self.trace.append((self.counter, name, index))
        kind = self.faults.get(self.counter)
        if kind is None:
            return
        self.fault_log.append(
            {"n": self.counter, "point": name, "kind": kind, "index": index}
        )
        if kind == "kill":
            self.h.snapshot_disk()
            self.dead = True
            raise ServerKilled(name)
        if kind == "cancel":
            raise asyncio.CancelledError(f"стенд: отмена в точке {name}")
        if name.startswith("save"):
            raise OSError(28, f"Стенд: нет места на диске ({name})")
        raise PlaywrightError(f"Стенд: сбой Playwright в точке {name}")

    async def apoint(self, name: str, index: Optional[int] = None) -> None:
        self.point(name, index)

    # --- диск -----------------------------------------------------------
    def save(self, job_dir: Path, job_id: str, job: dict[str, Any], draft: Any) -> Path:
        label = f"save[{job.get('status')}:{job.get('step') or '-'}]"
        try:
            code_index = int(job.get("item_index") or 0) or None
        except (TypeError, ValueError):
            code_index = None
        self.point(label, code_index)
        return REAL_SAVE(job_dir, job_id, job, draft)

    # --- CDP --------------------------------------------------------------
    async def connect_over_cdp(self, _pw: Any, _cdp_url: str, **_kwargs: Any) -> FakeContext:
        await self.apoint("connect_chrome")
        return self.context

    # --- безопасные шаги формы -----------------------------------------
    async def open_form(self, page: FakePage, _profile: Any) -> str:
        await self.apoint("open_form")
        page.form = FakeForm()
        page.url = "https://www.avito.ru/additem"
        return "form"

    async def select_category(self, page: FakePage, _form_state: Any, _profile: Any) -> None:
        await self.apoint("select_category")

    async def check_category(self, page: FakePage, _profile: Any) -> None:
        await self.apoint("check_category")

    async def guard_reopened(
        self, page: FakePage, _title: str, _i: int, _n: int, **_kwargs: Any,
    ) -> None:
        # **_kwargs: настоящий guard принимает keyword-only own_draft_allowed (F27).
        await self.apoint("guard_reopened")

    async def clear_and_type(self, page: FakePage, _selector: str, value: str) -> None:
        await self.apoint("fill_title")
        page.form.title = value

    async def upload_photos(self, page: FakePage, photo_paths: tuple[str, ...]) -> None:
        await self.apoint("upload_photos")
        page.form.photos = tuple(photo_paths)

    async def fill_fields(self, page: FakePage, _data: Any, _profile: Any) -> str:
        await self.apoint("fill_fields")
        return "Без бренда"

    async def fill_description(self, page: FakePage, description: str) -> None:
        await self.apoint("fill_description")
        page.form.description = description

    async def fill_price(self, page: FakePage, _price: int) -> None:
        await self.apoint("fill_price")

    async def fill_address(self, page: FakePage, full_address: str) -> None:
        index = self.h.address_index.get(full_address)
        await self.apoint("fill_address", index)
        page.form.address_index = index
        page.form.address_filled = True
        return None

    # --- денежный путь ----------------------------------------------------
    def register_continue_listing(self, page: FakePage) -> dict[str, Any]:
        form = page.form
        index = form.address_index if form else None
        self.clicks[("continue_listing", index)] += 1
        listing = {
            "id": str(self.next_id),
            "index": index,
            "title": form.title if form else None,
            "photos": form.photos if form else (),
            "view_paid": 0,
            "services_skipped": 0,
        }
        self.next_id += 1
        self.listings.append(listing)
        if form is not None:
            form.clicked = True
            form.listing = listing
        page.url = f"https://www.avito.ru/cpxpromo/{listing['id']}"
        return listing

    async def continue_listing(self, page: FakePage) -> tuple[str, FakePage]:
        index = page.form.address_index if page.form else None
        await self.apoint("continue_listing:click", index)
        listing = self.register_continue_listing(page)
        await self.apoint("continue_listing:after", index)
        return listing["id"], page

    def _listing_of(self, page: FakePage) -> Optional[dict[str, Any]]:
        return page.form.listing if page.form is not None else None

    async def fill_view_price(self, page: FakePage, *_args: Any, **_kwargs: Any) -> Decimal:
        listing = self._listing_of(page)
        await self.apoint("fill_view_price", listing["index"] if listing else None)
        return Decimal("0.4")

    async def continue_view_price(self, page: FakePage, item_id: str) -> None:
        listing = self._listing_of(page)
        index = listing["index"] if listing else None
        await self.apoint("continue_view_price:click", index)
        self.clicks[("continue_view_price", index)] += 1
        if listing is not None:
            listing["view_paid"] += 1
        page.url = "https://www.avito.ru/pro/performance"
        await self.apoint("continue_view_price:after", index)

    async def skip_services(self, page: FakePage) -> None:
        listing = self._listing_of(page)
        index = listing["index"] if listing else None
        await self.apoint("skip_services:click", index)
        self.clicks[("skip_services", index)] += 1
        if listing is not None:
            listing["services_skipped"] += 1
        page.url = "https://www.avito.ru/profile"
        await self.apoint("skip_services:after", index)


# ---------------------------------------------------------------------------
# Стенд целиком: сервер (app) + поддельный Авито + временный диск
# ---------------------------------------------------------------------------

class Harness:
    def __init__(self, n_items: int = 3, *, use_prep: bool = True) -> None:
        self.n = n_items
        self.use_prep = use_prep
        self.tmp = Path(tempfile.mkdtemp(prefix="avito-money-"))
        self.publish_root = self.tmp / "publish"
        self.publish_root.mkdir()
        self.debug_root = self.tmp / "debug"
        self.snapshot_root = self.tmp / "snapshot"
        self.db_path = self.tmp / "agents.db"
        self.prep_id = "moneyprep01" if use_prep else None
        self.locations = [("Москва", f"улица Спековая, {k}") for k in range(1, n_items + 1)]
        self.address_index = {
            f"{city}, {address}": k
            for k, (city, address) in enumerate(self.locations, start=1)
        }
        self.variant_titles = {
            k: f"Кроссовки спек вариант {k:02d}" for k in range(1, n_items + 1)
        }
        self.avito = FakeAvito(self)
        self.killed = False
        self.job_id: Optional[str] = None
        self._stack = contextlib.ExitStack()

    def __enter__(self) -> "Harness":
        logging.disable(logging.CRITICAL)
        av = self.avito
        s = self._stack
        s.enter_context(mock.patch.object(backend_app, "TMP_PUBLISH_DIR", self.publish_root))
        s.enter_context(mock.patch.object(publisher, "TMP_PUBLISH_DIR", self.publish_root))
        s.enter_context(mock.patch.object(publisher, "DEBUG_PUBLISH_DIR", self.debug_root))
        s.enter_context(mock.patch.object(journal, "DB_PATH", str(self.db_path)))
        s.enter_context(mock.patch.object(backend_app, "CDP_URL", "http://127.0.0.1:9222"))
        s.enter_context(mock.patch.object(backend_app.cache_mod, "init_db", lambda *a, **k: None))
        s.enter_context(
            mock.patch.object(backend_app.outreach_store, "init_db", lambda *a, **k: None)
        )
        s.enter_context(mock.patch.object(publisher, "_pause", _noop))
        s.enter_context(mock.patch.object(publisher, "DRAFT_PAUSE_MIN_S", 0.0))
        s.enter_context(mock.patch.object(publisher, "DRAFT_PAUSE_MAX_S", 0.0))
        s.enter_context(mock.patch.object(publish_state, "save_publish_state", av.save))
        s.enter_context(
            mock.patch("playwright.async_api.async_playwright", lambda: _PlaywrightManager())
        )
        s.enter_context(mock.patch("browser.connect_over_cdp", av.connect_over_cdp))
        steps = {
            "_step_open_form": av.open_form,
            "_step_select_category": av.select_category,
            "_step_check_category": av.check_category,
            "_guard_against_reopened_draft": av.guard_reopened,
            "_clear_and_type": av.clear_and_type,
            "_step_upload_photos": av.upload_photos,
            "_step_fill_fields": av.fill_fields,
            "_step_fill_description": av.fill_description,
            "_step_fill_price": av.fill_price,
            "_step_fill_address": av.fill_address,
            "_step_continue_listing": av.continue_listing,
            "_step_fill_view_price": av.fill_view_price,
            "_step_continue_view_price": av.continue_view_price,
            "_step_skip_services": av.skip_services,
        }
        for name, fake in steps.items():
            s.enter_context(mock.patch.object(publisher, name, fake))
        self._reset_memory()
        journal.init_db()
        self._make_prep()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stack.close()
        self._reset_memory()
        logging.disable(logging.NOTSET)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _reset_memory() -> None:
        backend_app.PUBLISH_JOBS.clear()
        backend_app.ACTIVE_PUBLISH_TASKS.clear()
        backend_app.PREP_JOBS.clear()

    def _make_prep(self) -> None:
        if not self.prep_id:
            return
        prep_dir = self.publish_root / f"prep_{self.prep_id}"
        for k in range(1, self.n + 1):
            draft_dir = prep_dir / f"draft_{k:02d}"
            (draft_dir / "photos").mkdir(parents=True)
            (draft_dir / "title.txt").write_text(self.variant_titles[k], encoding="utf-8")
            (draft_dir / "text.txt").write_text(f"Описание варианта {k}", encoding="utf-8")
            (draft_dir / "photos" / "01.jpg").write_bytes(b"\xff\xd8money")
        backend_app.PREP_JOBS[self.prep_id] = {"status": "done"}

    # --- «убийство» и перезапуск ------------------------------------------
    def _job_dirs(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        return [p for p in root.iterdir() if p.is_dir() and not p.name.startswith("prep_")]

    def snapshot_disk(self) -> None:
        if self.snapshot_root.exists():
            shutil.rmtree(self.snapshot_root)
        self.snapshot_root.mkdir()
        for job_dir in self._job_dirs(self.publish_root):
            shutil.copytree(job_dir, self.snapshot_root / job_dir.name)
        self.killed = True

    def restart(self) -> dict[str, int]:
        """Перезапуск сервера: память теряется, после kill диск = снимок."""
        self._reset_memory()
        if self.killed:
            for job_dir in self._job_dirs(self.publish_root):
                shutil.rmtree(job_dir, ignore_errors=True)
            for saved in self._job_dirs(self.snapshot_root):
                shutil.copytree(saved, self.publish_root / saved.name)
            self.killed = False
        self.avito.dead = False
        before_events = self.avito.counter
        tasks_before = set(asyncio.all_tasks())
        backend_app.on_startup()
        new_tasks = set(asyncio.all_tasks()) - tasks_before
        return {
            "events": self.avito.counter - before_events,
            "new_tasks": len(new_tasks),
        }

    # --- запросы к серверу --------------------------------------------------
    def form_fields(self, prep_id: Optional[str] = "default") -> dict[str, str]:
        profile = category_profiles.get_profile("sneakers")

        def first(options: Any) -> str:
            return next(iter(options)) if options else ""

        if prep_id == "default":
            prep_id = self.prep_id
        return {
            "title": "Кроссовки спек",
            "trade_type": first(profile.trade_type_options),
            "condition": first(profile.condition_options),
            "size": first(profile.size_options),
            "brand": "Heckel",
            "color": first(profile.color_options),
            "description": "Описание спек",
            "price": "5000",
            "locations_json": json.dumps(
                [{"city": c, "address": a} for c, a in self.locations],
                ensure_ascii=False,
            ),
            "view_price_max": "2",
            "drafts_count": str(self.n),
            "prep_id": prep_id or "",
            "category": profile.key,
            "item_type": "",
            "material": "",
            "style": "",
        }

    async def start(self, prep_id: Optional[str] = "default") -> tuple[Optional[int], Any]:
        """Настоящий POST /api/publish/start (прямой вызов функции эндпоинта)."""
        photos = [
            UploadFile(
                io.BytesIO(b"\xff\xd8money-photo"),
                filename="photo.jpg",
                headers=Headers({"content-type": "image/jpeg"}),
            )
        ]
        known = {p.name for p in self.publish_root.iterdir()} if self.publish_root.exists() else set()
        try:
            response = await backend_app.api_publish_start(
                **self.form_fields(prep_id), photos=photos
            )
        except (ServerKilled, asyncio.CancelledError) as exc:
            new_dirs = [
                p.name for p in self.publish_root.iterdir()
                if p.name not in known and not p.name.startswith("prep_")
            ]
            self.job_id = new_dirs[0] if new_dirs else None
            return None, repr(exc)
        payload = json.loads(response.body)
        if response.status_code == 200:
            self.job_id = payload["job_id"]
        return response.status_code, payload

    async def wait_task(self) -> str:
        task = backend_app.ACTIVE_PUBLISH_TASKS.get(self.job_id) if self.job_id else None
        if task is None:
            return "no-task"
        await asyncio.wait([task])
        await asyncio.sleep(0)  # дать отработать done-callback _forget
        if task.cancelled():
            return "cancelled"
        exc = task.exception()
        if exc is None:
            return "ok"
        if isinstance(exc, ServerKilled):
            return "killed"
        return f"raised:{type(exc).__name__}:{exc}"

    async def resume(self) -> tuple[Optional[int], dict[str, Any]]:
        if not self.job_id:
            return None, {}
        response = await backend_app.api_publish_resume(self.job_id)
        return response.status_code, json.loads(response.body)

    async def status(self) -> tuple[Optional[int], dict[str, Any]]:
        if not self.job_id:
            return None, {}
        response = await backend_app.api_publish_status(self.job_id)
        return response.status_code, json.loads(response.body)


async def resume_to_end(h: Harness, limit: int = 6) -> list[tuple[Any, dict[str, Any]]]:
    """Дожимает resume до терминального done (или до отказа/лимита попыток)."""
    answers = []
    for _ in range(limit):
        code, payload = await h.resume()
        answers.append((code, payload))
        if code != 200:
            break
        outcome = await h.wait_task()
        if outcome != "ok" or h.killed:
            h.restart()
            continue
        if (backend_app.PUBLISH_JOBS.get(h.job_id) or {}).get("status") == "done":
            break
    return answers


def clean_trace_with_boundary(
    n_items: int = 3,
) -> tuple[list[tuple[int, str, Optional[int]]], int]:
    """(события, граница): события с номером <= границы происходят внутри
    самого POST /api/publish/start, до того как задача принята."""

    async def _run() -> tuple[list[tuple[int, str, Optional[int]]], int]:
        with Harness(n_items) as h:
            await h.start()
            boundary = h.avito.counter
            await h.wait_task()
            return list(h.avito.trace), boundary

    return asyncio.run(_run())


def clean_trace(n_items: int = 3) -> list[tuple[int, str, Optional[int]]]:
    """Последовательность событий чистого прогона (без сбоев)."""
    return clean_trace_with_boundary(n_items)[0]


def find_event(
    trace: list[tuple[int, str, Optional[int]]],
    name: str,
    index: Optional[int] = None,
    occurrence: int = 1,
) -> int:
    seen = 0
    for n, point, idx in trace:
        if point == name and (index is None or idx == index):
            seen += 1
            if seen == occurrence:
                return n
    raise LookupError(f"событие {name} №{index} (#{occurrence}) не найдено")
