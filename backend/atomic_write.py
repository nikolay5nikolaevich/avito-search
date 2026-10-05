"""Атомарная и устойчивая к сбою питания запись небольших файлов состояния.

Используется для checkpoint'ов, по которым после сбоя решается, повторять ли
денежное действие (publish_state; позже — outreach_state). Порядок важен:

1. Содержимое пишется во временный файл рядом с целевым.
2. `flush` + `os.fsync` временного файла — данные физически на диске ДО того,
   как новое имя начнёт на них указывать. Без этого после сбоя питания
   `os.replace` может оказаться на диске раньше данных: целевой файл пустой
   или обрезанный, checkpoint потерян.
3. `os.replace` — атомарная подмена: читатель видит либо старый, либо новый
   файл целиком.
4. Закрепление самого переименования (метаданных каталога):
   - POSIX: `fsync` каталога — стандартный способ сделать rename устойчивым;
   - Windows: каталог нельзя открыть через `os.open`, поэтому, как
     PostgreSQL (`durable_rename`), повторно открываем уже переименованный
     файл на запись и делаем `fsync` (FlushFileBuffers) — NTFS при этом
     сбрасывает журнал метаданных вместе с записью о переименовании.
   Ошибка на этом шаге пробрасывается: вызывающий код не должен считать
   запись надёжной (для checkpoint'а публикации это означает «денежный клик
   не делаем»). Исключение — POSIX-ФС, которые вовсе не поддерживают fsync
   каталога (EINVAL/ENOTSUP/EBADF): там закрепить rename штатно нельзя.
"""

from __future__ import annotations

import errno
import logging
import os
from pathlib import Path


logger = logging.getLogger(__name__)

# fsync каталога не поддержан файловой системой — это не сбой записи.
_DIR_FSYNC_UNSUPPORTED = frozenset(
    code
    for code in (
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "EBADF", None),
    )
    if code is not None
)


def _fsync_replaced(path: Path) -> None:
    """Закрепляет на диске переименование `path` (см. п. 4 докстринга модуля)."""
    if os.name == "nt":
        with open(path, "r+b") as handle:
            os.fsync(handle.fileno())
        return
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno not in _DIR_FSYNC_UNSUPPORTED:
            raise
        logger.debug("fsync каталога %s не поддержан ФС: %s", path.parent, exc)
    finally:
        os.close(dir_fd)


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Атомарно и durable записывает `text` в `path`.

    Любая ошибка (нет места, нет доступа, сбой fsync) пробрасывается как
    OSError: вызывающий код обязан считать запись несостоявшейся.
    """
    path = Path(path)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp_path, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Недописанный временный файл не нужен; целевой файл не тронут.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    _fsync_replaced(path)
