"""Город цены должен подтверждаться выбранным и применённым адресом."""

import copy
import unittest

import publish_test_isolation  # noqa: F401
import publisher


class MoscowGeoCityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.location = publisher.LocationData("Московская область", "Шараповская улица")
        self.geo = {
            "requested": self.location.full_address(),
            "selected": (
                "Шараповская улица\n\nтерритория Жуковская, "
                "муниципальный округ Чехов, Московская область"
            ),
            "applied": "Московская область, Чехов, Шараповская улица",
            "location_id": "test-location-id",
        }

    def test_known_chekhov_is_confirmed_by_selected_and_applied_geo(self) -> None:
        self.assertTrue(publisher._geo_city_matches(self.location, "Чехов", self.geo))

    def test_unknown_city_is_rejected(self) -> None:
        self.assertFalse(publisher._geo_city_matches(self.location, "Мытищи", self.geo))

    def test_other_region_is_rejected(self) -> None:
        for field in ("selected", "applied"):
            with self.subTest(field=field):
                geo = copy.deepcopy(self.geo)
                geo[field] = geo[field].replace("Московская область", "Калужская область")
                self.assertFalse(publisher._geo_city_matches(self.location, "Чехов", geo))

    def test_other_street_is_rejected(self) -> None:
        for field in ("selected", "applied"):
            with self.subTest(field=field):
                geo = copy.deepcopy(self.geo)
                geo[field] = geo[field].replace("Шараповская улица", "Лесная улица")
                self.assertFalse(publisher._geo_city_matches(self.location, "Чехов", geo))

    def test_empty_location_id_is_rejected(self) -> None:
        self.assertFalse(publisher._geo_city_matches(
            self.location, "Чехов", {**self.geo, "location_id": ""},
        ))

    def test_missing_geo_is_rejected(self) -> None:
        for geo in (None, {}):
            with self.subTest(geo=geo):
                self.assertFalse(publisher._geo_city_matches(self.location, "Чехов", geo))

    def test_exact_city_preserves_existing_normalized_comparison(self) -> None:
        location = publisher.LocationData("Чехов", "Шараповская улица")
        self.assertTrue(publisher._geo_city_matches(location, "  ЧЕХОВ  ", None))

    def test_conflicting_applied_city_cannot_confirm_selected_city(self) -> None:
        geo = {**self.geo, "applied": "Московская область, Мытищи, Шараповская улица"}
        self.assertFalse(publisher._geo_city_matches(self.location, "Чехов", geo))


if __name__ == "__main__":
    unittest.main()
