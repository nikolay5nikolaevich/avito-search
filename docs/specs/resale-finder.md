# Поиск под перепродажу («resale»)

Статус: в разработке, начато 27.09.2026. Первая тематика — ноутбуки (игровые).

## Зачем

Пользователь вводит запрос («игровой ноутбук») и город. Сервис собирает выдачу,
определяет модель каждого объявления, считает рыночную цену по модели и
показывает объявления заметно дешевле рынка — кандидаты на перепродажу.

Только чтение Авито: ни одного клика, никаких денежных действий.

## Решения (согласованы с пользователем 27.09.2026)

- **География** (пересмотрено 28.09.2026 после первого живого прогона): рынок
  считается по **всем** объявлениям, которые Авито показывает в выдаче города,
  включая чужие города с доставкой; выгодные лоты — **только местные**
  (`analytics._is_local` / `parser.is_local_listing`). Причина: в Петербурге по
  «ноутбук Gigabyte G5 MF» Авито показал 41 объявление, местных из них 3 — по
  местным рынок не посчитать.
- **«Та же модель»** = бренд + серия + видеокарта. «Acer Nitro 5 + RTX 3060»
  и «Acer Nitro 5 + GTX 1650» — разные рынки.
- **Распознавание — ИИ** через Claude Code CLI (`claude -p`, подписка
  пользователя, без API-ключа). ИИ также отличает «на запчасти / неисправен»
  и «это не ноутбук» (зарядки, сумки, комплектующие) — они не портят рынок.
- **Режим** — разовый поиск по кнопке. Мониторинг — позже, поверх того же ядра.
- **VPN** — у пользователя раздельное туннелирование: Chrome идёт напрямую
  (РУ-IP для Авито), `claude` — через VPN. Поэтому один сквозной сценарий,
  без ручного переключения между этапами.

## Сценарий

1. **Сбор.** `parser._collect_listing_items(..., local_only=False)` по запросу
   в городе, до `max_items` объявлений выдачи (местных и с доставкой, дедуп по
   `item_id`); у каждого `is_local: bool`. Каждое объявление несёт `item_id`, `title`, `price`,
   `description` (сниппет из JSON каталога, ~250 символов), `category_slug`,
   `url`, `address`.
2. **Разбор ИИ.** Пачками по ≤ 40 объявлений через `claude -p`. Результат
   кэшируется (см. ниже) — повторный поиск не тратит лимиты подписки.
3. **Рынок.** Группировка по ключу модели, медиана цены по рабочим.
4. **Добор.** Если в группе есть кандидат в выгодные лоты, но рабочих
   объявлений меньше `min_sample` — уточняющий поиск `"{brand} {series} {gpu}"`
   в том же городе (≤ 50 объявлений), новые объявления (дедуп по `item_id`)
   разбираются ИИ, отчёт пересчитывается. Не более `MAX_TOPUP_QUERIES = 8`
   уточняющих поисков за задачу — нагрузка на Авито.
5. **Отчёт** + CSV.

Ошибка одного шага (пачка ИИ, уточняющий поиск) — лог и пропуск, не падение
задачи. Блокировка Авито (`AvitoBlockedError`) — статус `blocked`, как в
аналитике и разборе продавца.

## Модули и контракты

### `backend/parser.py` — доработка

`_collect_listing_items` получает `local_only: bool = True` (по умолчанию —
прежнее поведение для аналитики). При `False` собирает все объявления выдачи
и проставляет `is_local`. Дедуп по `item_id` и стоп «на странице нет новых»
действуют в обоих режимах.

Описание карточки на живой выдаче (28.09.2026, `debug/search_live_noutbuki.html`)
лежит не в `meta[itemprop=description]`, а в первом
`p[style*='--module-max-lines-size']` карточки; дальше в карточке этим же
стилем идут имя продавца и отзывы. Meta — резерв для старой вёрстки.

`_item_from_json` дополнительно возвращает:
`item_id: str | None` (`raw["id"]` строкой), `description: str` (`raw["description"]`
или `""`), `category_slug: str | None` (`raw["category"]["slug"]`).
HTML-резерв `_extract_items_from_html` — те же ключи (`description=""`,
`category_slug=None`, `item_id` из `data-item-id`). Аналитика спроса от этого
не меняется (лишние ключи ей не мешают).

### `backend/resale_classifier.py` — разбор ИИ

```python
Runner = Callable[[str], str]   # промпт -> stdout claude CLI

def classify_items(
    items: list[dict],             # ключи item_id, title, description, price, category_slug
    *,
    runner: Runner | None = None,  # None -> реальный claude CLI; тесты подменяют
    db_path: str = "resale_cache.db",
    batch_size: int = 40,
) -> dict[str, dict]:              # item_id -> Classification
```

`Classification`:

| поле | тип | смысл |
|---|---|---|
| `is_laptop` | bool | это ноутбук целиком (не зарядка/сумка/запчасть/ПК) |
| `brand` | str \| None | нормализованный бренд: `"Acer"`, `"ASUS"`, `"Lenovo"`, `"MSI"`, `"HP"` … |
| `series` | str \| None | серия/модель: `"Nitro 5"`, `"ROG Strix G15"`, `"Legion 5"` |
| `gpu` | str \| None | дискретная видеокарта нормализованно: `"RTX 3060"`, `"GTX 1650"`, `"RX 6600M"`; `None`, если не указана |
| `condition` | `"working"` \| `"parts"` \| `"unknown"` | `parts` — на запчасти, не включается, разбит экран/матрица, залит, без ключевых деталей |
| `reason` | str | коротко, почему такой `condition` (для колонки в отчёте) |
| `error` | str \| None | пачка не разобрана — текст ошибки; остальные поля тогда `None`/`"unknown"` |

Правила:
- **Кэш** — SQLite `resale_cache.db` в корне (не в git). Ключ —
  `item_id` + sha1(`title` + `\n` + `description`): продавец поправил текст —
  разбираем заново. Записи с `error` не кэшируются. Кэш-хит не вызывает `runner`.
- Объявление без `item_id` не разбирается (нечем ключевать) — не попадает в результат.
- Ответ модели — JSON-массив объектов с `item_id`. Допустим JSON, обёрнутый
  в ```-блок. `item_id`, которых модель не вернула, получают `error`.
  Нераспарсенный ответ / таймаут / ненулевой код выхода — `error` на всю пачку.
- **Реальный runner**: `shutil.which("claude")` (на Windows это `claude.cmd`),
  промпт через stdin (не аргументом — лимит длины командной строки Windows),
  `--output-format json`, текст модели в поле `result`. Модель — env
  `RESALE_CLAUDE_MODEL`, по умолчанию `haiku`. Таймаут 180 с на пачку.
  Запуск из нейтральной рабочей папки `tmp/resale/claude_cwd/`, чтобы CLI не
  подтягивал CLAUDE.md и память проекта; инструменты и MCP отключить флагами
  CLI, если они есть (свериться с `claude --help`, версия у пользователя 2.1.280).
- `claude` не найден в PATH — понятная ошибка на всю задачу
  («Не найден claude CLI …»), не падение с трейсбеком.
- **`classify_items` никогда не бросает исключение** — любые сбои runner'а
  (исключение, не найден CLI, таймаут, код выхода) превращаются в `error`.
  Быстрый отказ: если **первая** пачка упала целиком (runner бросил
  исключение), остальные пачки не вызываются — все объявления получают ту же
  `error`. Оркестратор (`run_resale_scan`), получив `error` у всех объявлений,
  завершает задачу статусом `error` с текстом этой ошибки (вероятная причина —
  VPN не пускает claude).
- Соединения SQLite закрываются явно (`with sqlite3.connect()` не закрывает
  соединение, на Windows это держит файл кэша).

### `backend/resale_market.py` — чистая логика отчёта (без I/O)

```python
def model_key(c: dict) -> str | None
    # "acer nitro 5 | rtx 3060": brand+series+gpu в нижнем регистре, пробелы
    # схлопнуты; None, если нет brand, series или gpu, или is_laptop=False.

def build_report(
    items: list[dict],
    classifications: dict[str, dict],
    *,
    threshold_pct: float = 20,
    min_sample: int = 5,
) -> dict
```

- **Рынок группы** — медиана (`statistics.median`) цен объявлений группы с
  `condition == "working"` и ценой > 0. `sample` — их число.
- **Рынок** — по всем объявлениям группы независимо от `is_local`.
- **Выгодный лот** — **местное** объявление (`is_local` не `False`; ключа нет —
  считается местным) группы с `condition` `working` или `unknown`,
  ценой > 0 и `price <= median * (1 - threshold_pct / 100)`.
  `discount_pct = round((median - price) / median * 100, 1)`.
- Группа с `sample < min_sample` — `low_data: True`; её лоты всё равно
  показываются с этим флагом. Группа с `sample == 0` — рынка нет, лотов нет.
- `parts`, `is_laptop=False`, без ключа модели, с `error` — в рынок и лоты
  не попадают, считаются в `excluded`.

Возвращает:

```python
{
  "deals": [   # по discount_pct убыв.
    {"item_id", "url", "title", "price", "market_price", "discount_pct",
     "sample", "model": "Acer Nitro 5 · RTX 3060", "model_key",
     "condition", "reason", "low_data"}
  ],
  "groups": [  # по sample убыв.
    {"model_key", "model", "sample", "market_price", "min_price",
     "max_price", "low_data"}
  ],
  "excluded": {"parts": int, "not_laptop": int, "unrecognized": int, "errors": int},
  "total_items": int,
  "local_items": int,   # сколько из total_items местных
}
```

`model` — для показа: `"{brand} {series} · {gpu}"` в написании первого
объявления группы.

### `backend/resale_scan.py` — оркестрация

```python
async def run_resale_scan(
    query: str,
    city: City,
    *,
    max_items: int = 150,
    threshold_pct: float = 20,
    min_sample: int = 5,
    filters: SearchFilters | None = None,   # цена от/до — как в аналитике
    progress_cb: Callable[[str, int, int], None] | None = None,  # (stage, done, total)
) -> dict   # build_report(...) + {"topup_queries": [str], "query", "city"}
```

Стадии для `progress_cb`: `collecting`, `classifying`, `topup`, `done`.
Сбор и добор идут через собственную вкладку Chrome по CDP (как
`seller_scan._owned_page`). Разбор ИИ — синхронный subprocess, вызывать через
`asyncio.to_thread`, чтобы не держать event loop.

### API (`backend/app.py`)

| метод | путь | тело / ответ |
|---|---|---|
| POST | `/api/resale/scan` | `{query, city, threshold_pct?=20, max_items?=150, price_min?, price_max?}` → `{job_id, status: "running"}`; 422 `{errors:[{field,error}]}`; 409, если в Chrome идёт другая задача |
| GET | `/api/resale/status/{job_id}` | `{job_id, status, stage, current, total, error}`; `status`: `running`/`done`/`error`/`blocked`; 404 `{status:"not_found"}` |
| GET | `/api/resale/result/{job_id}` | статус + отчёт `run_resale_scan` |
| GET | `/api/resale/export.csv?job_id=` | CSV выгодных лотов, UTF-8 с BOM: Модель, Название, Цена, Рынок, Скидка %, Выборка, Мало данных, Состояние, Ссылка |

Валидация: `query` непустой; `city` — slug из `cities.py`;
`threshold_pct` 5..80; `max_items` 20..300; цены — как `filters._parse_price`.
Занятость Chrome — общая проверка с разбором продавца/рассылкой/публикацией.

### Фронтенд — `frontend/src/pages/ResalePage.jsx`, маршрут `/resale`

Форма: запрос, город (тот же список, что в аналитике), порог скидки %,
максимум объявлений, цена от/до. Прогресс со стадией по-русски
(«Сбор объявлений», «Разбор ИИ», «Уточняющий поиск»). Результат:
- таблица выгодных лотов (ссылка открывается в новой вкладке; «мало данных» —
  заметная пометка; `unknown` — «состояние не ясно, проверь»);
- сводка исключённых (на запчасти / не ноутбук / не распознано / ошибки ИИ);
- свёрнутая таблица рынков по моделям;
- кнопка CSV.

Пункт в навигации рядом с «Разбор продавца».

## Вне рамок первой версии

Мониторинг и уведомления; другие тематики кроме ноутбуков (промпт написан под
ноутбуки); рынок по нескольким городам; учёт возраста объявления.
