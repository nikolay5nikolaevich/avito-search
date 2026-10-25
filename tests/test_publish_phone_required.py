"""Обязательные свойства телефона выводятся только из явного описания."""

import os
import sys
import unittest


BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import publish_test_isolation  # noqa: E402, F401
import publisher  # noqa: E402


class PhoneRequiredOptionsAcceptanceTest(unittest.TestCase):
    def test_description_explicitly_confirms_all_three_options(self) -> None:
        descriptions = [
            "1 физическая SIM + eSIM. Телефон не активирован, запечатанный. Не вскрытый.",
            "SIM + eSIM. Неактивированный. Упаковка запечатана.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                self.assertCountEqual(publisher._phone_required_options(description), [
                    (184666, 3338066, "SIM + eSIM"),
                    (184667, 3375453, "Неактивированный"),
                    (167055, 3270714, "Запечатана"),
                ])

    def test_each_missing_fact_stops_instead_of_guessing(self) -> None:
        descriptions = [
            "",
            "Новый iPhone 17 Pro 256 ГБ.",
            "Телефон не активирован, запечатанный.",
            "1 физическая SIM + eSIM. Запечатанный.",
            "1 физическая SIM + eSIM. Не активирован.",
            "Только eSIM. Не активирован. Запечатанный.",
            "Две физические SIM. Не активирован. Запечатанный.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                with self.assertRaises(publisher.StepError):
                    publisher._phone_required_options(description)

    def test_contradictory_activation_or_opened_packaging_stops(self) -> None:
        descriptions = [
            "SIM + eSIM. Не активирован. Ранее активирован. Запечатанный.",
            "SIM + eSIM. Неактивированный. Активирован вчера. Запечатанный.",
            "SIM + eSIM. Не активирован. Запечатанный. Упаковка вскрыта.",
            "SIM + eSIM. Не активирован. Запечатанный. Вскрытый.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                with self.assertRaises(publisher.StepError):
                    publisher._phone_required_options(description)


if __name__ == "__main__":
    unittest.main()
