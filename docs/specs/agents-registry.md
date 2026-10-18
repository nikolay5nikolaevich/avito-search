# Спека: реестр агентов и общий журнал событий (MVP)

Дата: 14.09.2026. Статус: согласовано, к реализации.

## Цель

Превратить сервис в систему из пяти агентов с общим журналом событий и реестром.
Цифры считает код, LLM формулирует гипотезы, решения принимает человек.

Главное — **быстро и просто**. Всё, что не перечислено в разделе «Входит», в MVP не входит.

## Агенты

| id | Имя | Тип | Статус | Вход → Выход | Инструменты | Передаёт |
|---|---|---|---|---|---|---|
| `scout` | Разведчик | код | работает | запрос + города → отчёт спроса (город → `avg_views_today`, `local_count`, `top3`) | `browser_cdp`, `avito_search_parse`, `sqlite_cache` | strategist |
| `preparer` | Подготовщик | код | работает | черновик + фото + N → пакет вариантов (`prep_id`) | `text_variation`, `photo_variation`, `tmp_files` | publisher |
| `publisher` | Публикатор | код | работает | `prep_id` + черновик → опубликованные объявления (`listings`) | `browser_cdp`, `avito_additem`, `listings_db` | stats_collector |
| `stats_collector` | Сборщик статистики | код | этап 2 | период 7/30 дней → снимок статистики своих объявлений | `avito_api`, `listings_db`, `stats_db` | strategist |
| `strategist` | Стратег | код + LLM | этап 3 | снимок статистики + отчёт Разведчика → 3–5 гипотез | `stats_db`, `sqlite_cache`, `llm_claude_cli` | человек |

Человек в реестр **не** вносится. Его решения попадают в журнал с `actor = "human"`.

Поля карточки: `id`, `name`, `role` (одна фраза), `kind` (`code` / `code_llm`),
`status` (`active` / `planned`), `inputs[]`, `outputs[]`, `allowed_tools[]`,
`modules[]` (файлы кода), `hands_off_to[]`.

Когда этап 2 или 3 готов, `status` агента меняется на `active` прямо в реестре.

## Архитектура

### Реестр — `backend/agents_registry.py`

- Единый источник правды: словарь `AGENTS` из dataclass `AgentSpec`.
- Функции `get_agent(agent_id)` и `list_agents()`.
- Фронт получает реестр через `GET /api/agents` и сам ничего не хардкодит.

### Журнал — `backend/journal.py`, база `agents.db`

Отдельный файл: `cache.db` не подходит, его удаляют при смене метрик.
`agents.db` добавить в `.gitignore`.

```sql
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,          -- ISO-8601 UTC
    run_id       TEXT,                   -- job_id / prep_id / id запуска агента
    actor        TEXT NOT NULL,          -- id агента или "human"
    type         TEXT NOT NULL,          -- см. ниже
    tool         TEXT,                   -- инструмент, если применимо
    summary      TEXT NOT NULL,          -- строка по-русски для UI
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS listings (
    item_id        TEXT PRIMARY KEY,
    publish_job_id TEXT,
    prep_id        TEXT,                 -- NULL, если публиковали без подготовки
    variant_index  INTEGER,              -- номер варианта в пакете, с 1
    city_slug      TEXT,
    city_name      TEXT,
    category       TEXT,
    title          TEXT,
    published_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stats_snapshots (
    id          TEXT PRIMARY KEY,        -- = run_id Сборщика
    created_at  TEXT NOT NULL,
    period_days INTEGER NOT NULL,        -- 7 или 30
    date_from   TEXT NOT NULL,
    date_to     TEXT NOT NULL,
    data_json   TEXT NOT NULL            -- см. этап 2
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,      -- запуск Стратега
    created_at       TEXT NOT NULL,
    snapshot_id      TEXT NOT NULL,
    scout_report_key TEXT NOT NULL,
    change           TEXT NOT NULL,      -- что меняем
    basis            TEXT NOT NULL,      -- на каких цифрах основано
    metric           TEXT NOT NULL,      -- что смотрим
    expected_effect  TEXT NOT NULL,
    check_days       INTEGER NOT NULL,   -- 7 или 30
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected
    decided_at       TEXT
);
```

**Типы событий:**

| type | Когда |
|---|---|
| `run_started` | запуск агента |
| `run_finished` | успешное завершение; в payload — краткие итоги |
| `run_failed` | ошибка; в payload — текст ошибки |
| `artifact` | агент что-то произвёл: отчёт, пакет, объявление (`item_id`), снимок, гипотезы |
| `handoff` | агент взял выход другого; payload `{from, artifact}` |
| `violation` | агент заявил инструмент не из `allowed_tools` |
| `decision` | человек принял или отклонил гипотезу (`actor = "human"`) |

Пошаговые события Публикатора (форма, фото, цена) в журнал **не** пишем, они остаются в `app.log`.

**API модуля:**

```python
log_event(actor: str, type: str, summary: str, *, run_id: str | None = None,
          tool: str | None = None, payload: dict | None = None) -> None
list_events(actor: str | None = None, limit: int = 100) -> list[dict]
```

**Проверка прав** — единственная точка контракта:
- `actor` не из реестра и не `"human"` → `ValueError` (это ошибка программиста);
- `tool` задан и не входит в `allowed_tools` агента → вместо исходного события пишется
  `violation` с payload `{attempted_type, tool, summary}`, в лог — `warning`, исключения нет.

**Правило безопасности (деньги):** `log_event` и запись в `listings` **никогда не бросают
исключения наружу**. Любая ошибка SQLite ловится и уходит в `logger.error`. Журнал не должен
останавливать публикацию и не должен провоцировать повтор финансовых кликов.
`ValueError` про неизвестного `actor` ловится тестами, а не в бою.

## Этапы

Каждый этап законченный: после него всё работает и показывается.

### Этап 0 — разведка API статистики (делает пользователь, под РУ-IP)

Скрипт `debug/avito_stats_probe.py` (в духе `diag.py`), env `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET`:
1. `POST https://api.avito.ru/token` (client_credentials) — уже проверено в n8n.
2. `GET /core/v1/accounts/self` → `user_id`.
3. `GET /core/v1/items?per_page=100&status=active` → список своих объявлений (id, title, address, category).
4. `POST /stats/v1/accounts/{user_id}/items` с телом
   `{dateFrom, dateTo, fields: ["uniqViews","uniqContacts","uniqFavorites"], itemIds: [...], periodGrouping: "day"}`
   для 2–3 объявлений за 7 дней.

Сырые ответы — в `debug/stats_probe/*.json`, токен в файлы не писать.
Пункты 3–4 написаны по памяти о документации: **реальные имена полей и эндпоинтов берём
из ответов**, этап 2 без этого не начинаем.

Готово, когда: в `debug/stats_probe/` лежат ответы всех четырёх запросов.

### Этап 1 — реестр, журнал, страница

Бэкенд:
- `agents_registry.py`, `journal.py` (инициализация БД в `on_startup`).
- Хуки в существующих агентах. В `app.py` — вокруг фоновых задач:
  - Разведчик (`run_job`, а также ответ из кэша): `run_started` / `run_finished`
    (payload: query, число городов, `cached`) / `run_failed`, плюс `artifact` с ключом кэша.
  - Подготовщик (задача `prepare`): `run_started` / `run_finished` (`prep_id`, N) / `run_failed`.
  - Публикатор (задача `publish`): `run_started` (плюс `handoff` от preparer, если есть `prep_id`),
    `run_finished` / `run_failed`.
- В `publisher.py`, в точке подтверждённой отправки (после `_save_checkpoint()`, рядом с логом
  «Объявление %d/%d отправлено Авито»): строка в `listings` и событие `artifact` с `item_id`,
  `tool="avito_additem"`. Порядок финансового сценария не трогаем.
- `GET /api/agents` → `list_agents()`.
- `GET /api/journal?actor=&limit=100` → события, новые сверху.

Фронт:
- `frontend/src/pages/AgentsPage.jsx`, маршрут `/agents` в `App.jsx`.
- Ссылка «Агенты» в `links` у `Topbar` на страницах Workspace и Draft, как у остальных разделов.
- Сетка из пяти карточек: имя, бейджи типа (`код` / `код + LLM`) и статуса, роль,
  «вход → выход», инструменты чипами, модули, «передаёт: …».
- Ниже блок «Журнал»: таблица (время, агент, тип, summary), фильтр по агенту
  (клик по карточке = фильтр, повторный клик снимает), опрос раз в 5 секунд.

Тесты:
- `tests/test_journal.py`: запись и чтение, фильтр по actor, `violation` при чужом
  инструменте, `ValueError` на неизвестном actor, сбой БД не бросает исключение.
- Прогнать существующие `publish_smoke_test.py`, `prepare_smoke_test.py`,
  `test_publish_financial_flow.py` — должны пройти без изменений.

Готово, когда: `/agents` показывает пять карточек, прогон аналитики и подготовки оставляет
события в журнале, тесты зелёные.

### Этап 2 — Сборщик статистики

`backend/stats_collector.py`:
- `run(period_days: 7 | 30) -> snapshot_id`. Даты: `date_to` = вчера, `date_from` = `date_to − (period_days − 1)`.
- Список объявлений: все активные из API (`/core/v1/items`), обогащённые данными из
  `listings` по `item_id` (prep_id, вариант, город). Если объявления нет в `listings`,
  город берём из адреса API через сопоставление с `cities.py`, вариант = `null`.
- Статистика пачками по 200 `item_id` (уточнить лимит на этапе 0).
- Безопасные GET/POST статистики можно ретраить (2 попытки, пауза).
- `data_json` снимка:
  ```json
  {
    "items":  [{"item_id", "title", "city_slug", "prep_id", "variant_index",
                "views", "contacts", "favorites", "views_per_day"}],
    "cities": [{"city_slug", "city_name", "items", "views_per_day_avg", "contact_rate"}],
    "variants": [{"prep_id", "variant_index", "items", "views_per_day_avg", "contact_rate"}]
  }
  ```
  Все агрегаты считает код. `contact_rate = contacts / views` (при `views = 0` → `null`).
- События: `run_started`, `artifact` (`tool="avito_api"`, snapshot_id, сколько объявлений),
  `run_finished` / `run_failed`.

API: `POST /api/agents/stats_collector/run {period_days}` → `{run_id}`;
`GET /api/agents/runs/{run_id}` → статус (in-memory `AGENT_RUNS`, как `JOBS`);
`GET /api/stats/latest` → последний снимок.

UI на карточке: переключатель «7 дней / 30 дней», кнопка «Запустить», статус запуска,
краткие итоги последнего снимка (дата, объявлений, средние просмотры в день).

Тесты: `tests/test_stats_collector.py` — HTTP замокан фикстурами из этапа 0; проверка
агрегатов по городам и вариантам, `views = 0`, объявление без `listings`.

Готово, когда: кнопка под РУ-IP создаёт снимок, в журнале есть события.

### Этап 3 — Стратег

`backend/strategist.py`, две части.

**1. Расчёт (чистые функции, без LLM):** `build_facts(snapshot, scout_report) -> dict`
- по городам: наши `views_per_day_avg` против рыночного `avg_views_today`, отношение;
  города без рыночных данных помечаются `market: null`;
- по вариантам внутри одного `prep_id`: min / max / разброс `views_per_day_avg`;
  если в пакете меньше двух вариантов, пакет пропускается;
- конверсия в контакт по городам.

Оговорка в фактах для LLM:
- `avg_views_today` у Разведчика — это просмотры за сегодня, а у нас — среднее в день
  за период. Сравнение приблизительное.
- Варианты различаются только артикулом и зумом фото, поэтому разброс между ними
  отражает скорее город и выдачу, чем текст.

**2. Формулировка (LLM):** вызов Claude Code CLI через `subprocess`:
- `shutil.which("claude")` (на Windows это `claude.cmd`), аргументы `-p --output-format json`;
  промпт подаётся в stdin, таймаут 180 с. Флаги пропуска разрешений не использовать.
- Промпт по-русски: «вот факты (JSON), сформулируй 3–5 проверяемых гипотез, используй только
  числа из фактов, ответ — строго JSON-массив объектов с полями
  `change, basis, metric, expected_effect, check_days` (7 или 30)».
- Из ответа CLI берём поле `result`, из него извлекаем JSON-массив и валидируем поля.
  Невалидный ответ → `run_failed`, сырой текст — в `logs/`, повтора нет.
- Нужен не-РУ IP. Запуск Стратега идёт отдельно от Сборщика — это обычный цикл VPN.

События: `run_started`, два `handoff` (`from: scout`, ключ отчёта; `from: stats_collector`,
snapshot_id), `artifact` (`tool="llm_claude_cli"`, число гипотез), `run_finished` / `run_failed`.

API:
- `GET /api/scout/reports` → записи `cache.db` (ключ разобран на query и города, `created_at`).
  Устаревшие по TTL тоже показываем, с возрастом. Нужна новая функция `cache.list_entries()`.
- `POST /api/agents/strategist/run {scout_report_key}` — берёт последний снимок статистики.
- `GET /api/hypotheses` → гипотезы последнего запуска.
- `POST /api/hypotheses/{id}/decision {decision: "accepted" | "rejected"}` → обновить статус
  и записать событие `decision` с `actor = "human"`. **Никаких действий на Авито.**

UI на карточке: выпадающий список отчётов Разведчика, «Запустить», под карточкой — список
гипотез (что меняем / основание / метрика / эффект / срок) с кнопками «Принять» / «Отклонить».

Тесты: `tests/test_strategist.py` — `build_facts` на фикстурах; разбор ответа LLM
(валидный, мусор вокруг JSON, неверные поля); вызов CLI замокан.

Готово, когда: запуск даёт гипотезы, решение по ним видно в журнале.

## Не входит в MVP

- Автодействия после «Принять», автоматическая проверка гипотез по сроку.
- Карточка человека в реестре, авторизация, несколько аккаунтов Авито.
- Расписание запусков: всё запускается вручную.
- Пошаговые события Публикатора, повторы запроса к LLM, календарь периода.
- Песочницы и принудительная изоляция инструментов (проверка — только при записи в журнал).

## Env

`AVITO_CLIENT_ID`, `AVITO_CLIENT_SECRET` — для Сборщика. Claude CLI должен быть установлен
и авторизован (`claude` в PATH).

## Для резюме (формулировка)

> Мультиагентная система автовыкладки и аналитики объявлений: 5 агентов
> (парсинг рынка, подготовка вариантов, публикация через CDP, сбор статистики через API,
> стратег на LLM) с реестром ролей и разрешённых инструментов, общим журналом событий
> и передачей артефактов между агентами. Цифры считает код, LLM формулирует проверяемые
> гипотезы, решения подтверждает человек.
