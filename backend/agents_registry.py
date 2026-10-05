"""
Реестр агентов сервиса.

Единый источник правды о том, какие агенты есть, что они делают и какими
инструментами могут пользоваться. Фронт получает реестр через GET /api/agents
и ничего не хардкодит; journal.py использует его для проверки прав
(actor должен быть агентом отсюда или "human", tool — входить в allowed_tools).

Человек в реестр не вносится: его решения попадают в журнал с actor = "human".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentSpec:
    """Карточка одного агента."""

    id: str
    name: str
    role: str                  # одна фраза
    kind: str                  # "code" | "code_llm"
    status: str                # "active" | "planned"
    inputs: tuple[str, ...] = field(default_factory=tuple)
    outputs: tuple[str, ...] = field(default_factory=tuple)
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    modules: tuple[str, ...] = field(default_factory=tuple)       # файлы кода
    hands_off_to: tuple[str, ...] = field(default_factory=tuple)  # id агента или "human"


AGENTS: dict[str, AgentSpec] = {
    "scout": AgentSpec(
        id="scout",
        name="Разведчик",
        role="Парсит выдачу Авито по городам и считает спрос",
        kind="code",
        status="active",
        inputs=("запрос", "города"),
        outputs=("отчёт спроса (город → avg_views_today, local_count, top3)",),
        allowed_tools=("browser_cdp", "avito_search_parse", "sqlite_cache"),
        modules=("parser.py", "analytics.py", "cache.py", "browser.py"),
        hands_off_to=("strategist",),
    ),
    "preparer": AgentSpec(
        id="preparer",
        name="Подготовщик",
        role="Готовит N уникальных вариантов черновика (текст + фото)",
        kind="code",
        status="active",
        inputs=("черновик", "фото", "N"),
        outputs=("пакет вариантов (prep_id)",),
        allowed_tools=("text_variation", "photo_variation", "tmp_files"),
        modules=("preparation.py", "text_variation.py", "photo_variation.py"),
        hands_off_to=("publisher",),
    ),
    "publisher": AgentSpec(
        id="publisher",
        name="Публикатор",
        role="Публикует варианты на Авито через живой Chrome по CDP",
        kind="code",
        status="active",
        inputs=("prep_id", "черновик"),
        outputs=("опубликованные объявления (listings)",),
        allowed_tools=("browser_cdp", "avito_additem", "listings_db"),
        modules=("publisher.py", "browser.py", "publish_state.py", "category_profiles.py"),
        hands_off_to=("stats_collector",),
    ),
    "stats_collector": AgentSpec(
        id="stats_collector",
        name="Сборщик статистики",
        role="Собирает статистику просмотров и контактов своих объявлений",
        kind="code",
        status="active",
        inputs=("период (7 или 30 дней)",),
        outputs=("снимок статистики своих объявлений",),
        allowed_tools=("avito_api", "listings_db", "stats_db"),
        modules=("stats_collector.py",),
        hands_off_to=("strategist",),
    ),
    "strategist": AgentSpec(
        id="strategist",
        name="Стратег",
        role="Формулирует проверяемые гипотезы по статистике и отчёту Разведчика",
        kind="code_llm",
        status="active",
        inputs=("снимок статистики", "отчёт Разведчика"),
        outputs=("3–5 гипотез",),
        allowed_tools=("stats_db", "sqlite_cache", "llm_claude_cli"),
        modules=("strategist.py",),
        hands_off_to=("human",),
    ),
}


def get_agent(agent_id: str) -> AgentSpec | None:
    """Возвращает карточку агента по id или None, если такого агента нет."""
    return AGENTS.get(agent_id)


def list_agents() -> list[AgentSpec]:
    """Возвращает все карточки агентов в порядке объявления."""
    return list(AGENTS.values())
