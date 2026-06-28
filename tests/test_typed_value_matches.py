"""
Юнит-тесты функции publisher._typed_value_matches.

Запуск:
    .venv\\Scripts\\python.exe tests\\test_typed_value_matches.py

Тестируемое поведение:
  - «2 990» (неразрывный пробел) совпадает с «2990» → True
  - «2 990» (обычный пробел U+0020) совпадает с «2990» → True
  - «10 000» совпадает с «10000» → True
  - Пустая строка («Цена не указана» или просто не заполнено) НЕ совпадает
    с «2990» → False
  - Одинаковые строки без форматирования → True
"""

import os
import sys

# backend/ и корень проекта — в sys.path
_tests_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_tests_dir)
_backend_dir = os.path.join(_project_root, "backend")
for _p in (_tests_dir, _project_root, _backend_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import publisher  # noqa: E402


def _check(label: str, result: bool, expected: bool) -> None:
    status = "PASS" if result == expected else "FAIL"
    mark = "  " if result == expected else "!!"
    print(f"{mark} {status}: {label}")
    if result != expected:
        raise AssertionError(
            f"{label}: ожидали {expected}, получили {result}"
        )


def run_tests() -> None:
    fn = publisher._typed_value_matches  # поднимет AttributeError, если не создана

    checks = [
        # (описание, current, expected_value, ожидаемый_результат)
        (
            "неразрывный пробел: «2\\u00a0990» == «2990»",
            "2 990", "2990", True,
        ),
        (
            "обычный пробел: «2 990» == «2990»",
            "2 990", "2990", True,
        ),
        (
            "шестизначная цена: «10 000» == «10000»",
            "10 000", "10000", True,
        ),
        (
            "без пробелов: «2990» == «2990»",
            "2990", "2990", True,
        ),
        (
            "пустое поле (не заполнено) != цена",
            "", "2990", False,
        ),
        (
            "«Цена не указана» (плейсхолдер) != цена",
            "Цена не указана", "2990", False,
        ),
        (
            "совпадающие строки без цифр: «foo» == «foo»",
            "foo", "foo", True,
        ),
        (
            "разные значения: «bar» != «baz»",
            "bar", "baz", False,
        ),
    ]

    passed = 0
    for label, current, expected_value, want in checks:
        result = fn(current, expected_value)
        _check(label, result, want)
        passed += 1

    print(f"\n=== _typed_value_matches: OK: {passed} проверок ===")


if __name__ == "__main__":
    try:
        run_tests()
        sys.exit(0)
    except AttributeError as e:
        print(f"\n[FAIL] Функция не найдена: {e}", file=sys.stderr)
        sys.exit(1)
    except AssertionError as e:
        print(f"\n[FAIL] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
