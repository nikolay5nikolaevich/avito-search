"""
Загрузка секретов из файла .env в корне проекта (без сторонних зависимостей).

Формат — строки KEY=VALUE; пустые строки и строки с # пропускаются, кавычки
вокруг значения снимаются. Уже заданные переменные окружения не перезаписываются:
$env:... в PowerShell важнее файла.

Значения никогда не пишутся в лог — только имена загруженных ключей.
"""

from __future__ import annotations

import logging
import os
import pathlib

logger = logging.getLogger(__name__)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_env_file(path: pathlib.Path = ENV_PATH) -> list[str]:
    """
    Читает .env и кладёт значения в os.environ.

    Args:
        path: путь к файлу; по умолчанию .env в корне проекта

    Returns:
        имена загруженных ключей (пустые значения не загружаются)
    """
    if not path.is_file():
        logger.info("Файл %s не найден — секреты берутся только из окружения", path.name)
        return []

    loaded: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key or not value or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)

    logger.info("Из %s загружены ключи: %s", path.name, ", ".join(loaded) or "нет")
    return loaded
