"""
Стратег (Этап 3, docs/specs/agents-registry.md).

Две части, разделённые по спеке:
1. build_facts — чистый расчёт (без LLM): сравнивает снимок статистики
   Сборщика с отчётом Разведчика и оформляет их в факты для промпта.
2. call_claude_cli / parse_hypotheses — вызов Claude Code CLI и разбор ответа.

CLI запускается через subprocess.run во временном рабочем каталоге:
гипотезы строятся только из фактов в промпте, модели незачем видеть файлы
проекта. Вызывающий код (app.py) обязан запускать call_claude_cli через
asyncio.to_thread — это блокирующий вызов (до CLI_TIMEOUT_S секунд), а
asyncio-подпроцессы под Windows доступны не при любом event loop.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Таймаут вызова CLI (спека, Этап 3). Один вызов на запуск, без повторов —
# повтор стоит денег и времени, а мусорный ответ лучше сразу увидеть в журнале.
CLI_TIMEOUT_S = 180

REQUIRED_HYPOTHESIS_FIELDS = ("change", "basis", "metric", "expected_effect", "check_days")

# Оговорки о сопоставимости метрик — идут В САМИ факты, а не только в текст
# промпта: так они сохраняются в журнале вместе с числами, на которых
# строились гипотезы (см. «Решения, которых нет в спеке» брифа 06).
CAVEATS: tuple[str, ...] = (
    "avg_views_today у Разведчика — это просмотры за сегодня, а у нас — "
    "среднее в день за период. Сравнение приблизительное.",
    "Варианты внутри пакета отличаются только артикулом и зумом фото, "
    "поэтому разброс между ними отражает скорее город и выдачу, чем текст.",
)

PROMPT_TEMPLATE = """Вот факты о наших объявлениях на Авито и рыночном спросе (JSON):

{facts_json}

Сформулируй от 3 до 5 проверяемых гипотез по этим данным. Используй ТОЛЬКО
числа из фактов, ничего не придумывай и не предполагай данных, которых там нет.

Ответ — строго JSON-массив объектов, без текста до или после массива. Каждый
объект — с полями:
  change           — что меняем
  basis            — на каких цифрах основано (ссылайся на конкретные числа)
  metric           — что смотрим, чтобы проверить гипотезу
  expected_effect  — ожидаемый эффект
  check_days       — через сколько дней проверять: число 7 или 30
"""


class StrategistError(RuntimeError):
    """Понятная ошибка Стратега — оборачивается в run_failed журнала в app.py."""


# ── 1. Расчёт фактов (чистая функция, без LLM) ──────────────────────────────

def build_facts(snapshot: dict[str, Any], scout_report: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Сравнивает снимок статистики (data_json Сборщика: items/cities/variants)
    с отчётом Разведчика (список городов из кэша) и считает факты для
    промпта LLM. Ничего не отправляет — чистая функция для тестов и промпта.

    Работает и без вариантов: пакет, опубликованный до журнала, у всех
    объявлений имеет prep_id/variant_index = null — variants в снимке пуст,
    build_facts тогда отдаёт "variants": [].

    Args:
        snapshot:     data_json снимка Сборщика ({"items", "cities", "variants"})
        scout_report: список городов отчёта Разведчика (значение записи кэша:
                      city_slug, avg_views_today, local_count, top3, ...)

    Returns:
        {"cities": [...], "variants": [...], "caveats": [...]}
    """
    scout_by_slug = {
        row.get("city_slug"): row
        for row in scout_report
        if isinstance(row, dict) and row.get("city_slug")
    }

    cities_facts: list[dict[str, Any]] = []
    for city in snapshot.get("cities", []):
        slug = city.get("city_slug")
        our_avg = city.get("views_per_day_avg")
        market_row = scout_by_slug.get(slug)

        market: Optional[dict[str, Any]] = None
        if market_row is not None:
            market_avg = market_row.get("avg_views_today")
            ratio = (
                our_avg / market_avg
                if our_avg is not None and market_avg not in (None, 0)
                else None
            )
            market = {
                "avg_views_today": market_avg,
                "our_vs_market_ratio": ratio,
            }

        cities_facts.append({
            "city_slug": slug,
            "city_name": city.get("city_name"),
            "items": city.get("items"),
            "our_views_per_day_avg": our_avg,
            "contact_rate": city.get("contact_rate"),
            "market": market,  # None — город без рыночных данных (спека, Этап 3)
        })

    variants_by_prep: dict[str, list[dict[str, Any]]] = {}
    for variant in snapshot.get("variants", []):
        prep_id = variant.get("prep_id")
        if prep_id is None:
            continue
        variants_by_prep.setdefault(prep_id, []).append(variant)

    variants_facts: list[dict[str, Any]] = []
    for prep_id, variants in variants_by_prep.items():
        if len(variants) < 2:
            continue  # пакет из одного варианта — разброс не посчитать, пропускаем
        values = [v["views_per_day_avg"] for v in variants]
        variants_facts.append({
            "prep_id": prep_id,
            "variants_count": len(variants),
            "min_views_per_day_avg": min(values),
            "max_views_per_day_avg": max(values),
            "spread": max(values) - min(values),
        })

    return {
        "cities": cities_facts,
        "variants": variants_facts,
        "caveats": list(CAVEATS),
    }


def build_prompt(facts: dict[str, Any]) -> str:
    """Собирает промпт из шаблона и фактов (JSON с русскими буквами без escape)."""
    facts_json = json.dumps(facts, ensure_ascii=False, indent=2)
    return PROMPT_TEMPLATE.format(facts_json=facts_json)


# ── 2. Вызов CLI и разбор ответа ────────────────────────────────────────────

def call_claude_cli(prompt: str) -> str:
    """
    Запускает Claude Code CLI неинтерактивно, без инструментов и без доступа
    к файлам проекта — гипотезы строятся только из фактов в промпте.

    `--tools ""` отключает у модели все инструменты (в т.ч. файловые) —
    это и защита (ей незачем видеть проект), и гарантия, что вызов не
    зависнет на запросе разрешений (спрашивать разрешение не на что).
    Промпт подаётся в stdin, флаги пропуска разрешений НЕ используются
    (спека, Этап 3) — они и не нужны при отключённых инструментах.

    Args:
        prompt: текст промпта (кириллица, кодируется в utf-8 явно)

    Returns:
        Сырой stdout CLI (JSON-конверт --output-format json, ещё не разобран).

    Raises:
        StrategistError: claude не найден в PATH, таймаут CLI_TIMEOUT_S,
                          ненулевой код возврата, ошибка запуска процесса.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        raise StrategistError(
            "Claude CLI не найден в PATH (shutil.which('claude') вернул None). "
            "Установите и авторизуйте Claude CLI, затем повторите запуск Стратега."
        )

    with tempfile.TemporaryDirectory(prefix="strategist-cli-") as cwd:
        try:
            proc = subprocess.run(
                [claude_path, "-p", "--output-format", "json", "--tools", ""],
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                timeout=CLI_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise StrategistError(
                f"Claude CLI не ответил за {CLI_TIMEOUT_S} с (таймаут)"
            ) from exc
        except OSError as exc:
            raise StrategistError(f"Не удалось запустить Claude CLI: {exc}") from exc

    if proc.returncode != 0:
        stderr_tail = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise StrategistError(
            f"Claude CLI вернул код {proc.returncode}: {stderr_tail}"
        )

    return proc.stdout.decode("utf-8", errors="replace")


def _extract_json_array(text: str) -> Optional[list[Any]]:
    """
    Достаёт JSON-массив из текста ответа модели.

    Сначала пробует распарсить текст целиком (штатный случай — промпт просит
    строго массив без обрамления). Если не вышло — ищет первый "[" и
    последний "]" и парсит срез между ними (модель иногда добавляет
    пояснение до/после массива, вопреки промпту).

    Returns:
        Список гипотез (сырые dict, ещё не провалидированные) или None,
        если валидного JSON-массива найти не удалось.
    """
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None

    try:
        parsed = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, list) else None


def parse_hypotheses(raw_response: str) -> list[dict[str, Any]]:
    """
    Разбирает сырой ответ CLI (--output-format json) в список гипотез.

    Конверт CLI: {"result": "<текст ответа модели>", ...} — берём поле
    result, из него извлекаем JSON-массив (может быть с текстом вокруг) и
    валидируем поля каждой гипотезы.

    Args:
        raw_response: сырой stdout call_claude_cli

    Returns:
        Список валидных гипотез: {change, basis, metric, expected_effect,
        check_days (int, 7 или 30)}.

    Raises:
        StrategistError: конверт не JSON / не объект, нет строкового поля
                          result, в result нет JSON-массива, гипотеза не
                          объект / без нужных полей, check_days не 7 и не 30,
                          итоговый список гипотез пуст.
    """
    try:
        envelope = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise StrategistError(f"Ответ CLI не является JSON: {exc}") from exc

    # Живой прогон 15.09.2026: CLI отдаёт не один объект, а список событий
    # сессии (system/assistant/.../result) — нужный конверт последний с
    # type == "result". Одиночный объект тоже поддерживаем.
    if isinstance(envelope, list):
        envelope = next(
            (
                event for event in reversed(envelope)
                if isinstance(event, dict) and event.get("type") == "result"
            ),
            None,
        )

    if not isinstance(envelope, dict) or "result" not in envelope:
        raise StrategistError("В конверте ответа CLI нет поля result")

    if envelope.get("is_error"):
        raise StrategistError(f"Claude CLI вернул ошибку: {envelope.get('result')}")

    result_text = envelope["result"]
    if not isinstance(result_text, str):
        raise StrategistError("Поле result в ответе CLI не строка")

    raw_array = _extract_json_array(result_text)
    if raw_array is None:
        raise StrategistError("В поле result нет валидного JSON-массива гипотез")

    hypotheses: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_array, start=1):
        if not isinstance(raw, dict):
            raise StrategistError(f"Гипотеза №{index} — не JSON-объект")

        missing = [field for field in REQUIRED_HYPOTHESIS_FIELDS if field not in raw]
        if missing:
            raise StrategistError(f"Гипотеза №{index}: не хватает полей {missing}")

        check_days = raw["check_days"]
        if check_days not in (7, 30):
            raise StrategistError(
                f"Гипотеза №{index}: check_days должен быть 7 или 30, получено {check_days!r}"
            )

        hypotheses.append({
            "change": str(raw["change"]),
            "basis": str(raw["basis"]),
            "metric": str(raw["metric"]),
            "expected_effect": str(raw["expected_effect"]),
            "check_days": int(check_days),
        })

    if not hypotheses:
        raise StrategistError("Claude CLI вернул пустой список гипотез")

    return hypotheses
