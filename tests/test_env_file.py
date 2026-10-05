"""Тесты загрузчика .env (backend/env_file.py) — на временном файле, не на реальном .env."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

import env_file  # noqa: E402


class LoadEnvFileTests(unittest.TestCase):
    def _write(self, text: str) -> pathlib.Path:
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return pathlib.Path(tmp.name)

    def test_parses_values_comments_quotes_and_skips_empty(self) -> None:
        path = self._write(
            "# комментарий\n"
            "\n"
            "TEST_ENV_A=abc\n"
            'TEST_ENV_B="в кавычках"\n'
            "export TEST_ENV_C = 'x=y'\n"
            "TEST_ENV_EMPTY=\n"
            "мусор без равно\n"
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            loaded = env_file.load_env_file(path)
            self.assertEqual(loaded, ["TEST_ENV_A", "TEST_ENV_B", "TEST_ENV_C"])
            self.assertEqual(os.environ["TEST_ENV_A"], "abc")
            self.assertEqual(os.environ["TEST_ENV_B"], "в кавычках")
            self.assertEqual(os.environ["TEST_ENV_C"], "x=y")
            self.assertNotIn("TEST_ENV_EMPTY", os.environ)

    def test_existing_environment_wins(self) -> None:
        path = self._write("TEST_ENV_A=from_file\n")
        with mock.patch.dict(os.environ, {"TEST_ENV_A": "from_shell"}, clear=True):
            self.assertEqual(env_file.load_env_file(path), [])
            self.assertEqual(os.environ["TEST_ENV_A"], "from_shell")

    def test_missing_file_is_not_an_error(self) -> None:
        missing = pathlib.Path(tempfile.gettempdir()) / "нет_такого_файла.env"
        self.assertEqual(env_file.load_env_file(missing), [])

    def test_values_are_not_logged(self) -> None:
        path = self._write("TEST_ENV_SECRET=очень-секретно\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertLogs(env_file.logger, level="INFO") as logs:
                env_file.load_env_file(path)
        self.assertFalse(any("очень-секретно" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
