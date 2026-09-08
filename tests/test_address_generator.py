import json
import math
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import address_generator as generator  # noqa: E402
import app as app_module  # noqa: E402


class AddressGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        generator._cache.clear()
        generator._areas.clear()
        generator._last_nominatim_request = 0.0

    def test_groups_city_requests_and_excludes_used_addresses(self) -> None:
        responses = [
            [{"osm_type": "relation", "osm_id": "123", "boundingbox": ["1", "2", "3", "4"]}],
            {"elements": [
                {"tags": {"addr:street": "ул. Ленина", "addr:housenumber": "1"}},
                {"tags": {"addr:street": "ул. Ленина", "addr:housenumber": "2"}},
            ]},
        ]
        with mock.patch.object(generator, "_request_json", side_effect=responses) as request:
            result = generator.generate_addresses(
                ["Москва", "Москва"], [{"city": "Москва", "address": "ул. Ленина, 1"}], rng=lambda values: values[0],
            )

        self.assertEqual([item.get("address") for item in result], ["ул. Ленина, 2", None])
        self.assertIn("не осталось уникальных", result[1]["error"])
        self.assertEqual(request.call_count, 2)

    def test_cached_addresses_skip_network(self) -> None:
        generator._cache["москва"] = (generator.time.time() + 60, ["ул. Тверская, 1"])
        with mock.patch.object(generator, "_request_json") as request:
            result = generator.generate_addresses(["Москва"], [], rng=lambda values: values[0])

        self.assertEqual(result[0]["address"], "ул. Тверская, 1")
        request.assert_not_called()

    def test_overpass_query_uses_relation_area(self) -> None:
        query = generator._overpass_query(generator.CityArea("relation", 77, (1, 2, 3, 4)))
        self.assertIn("area(3600000077)->.city", query)
        self.assertIn("addr:street", query)

    def test_rng_can_choose_not_first_candidate(self) -> None:
        generator._cache["москва"] = (generator.time.time() + 60, ["ул. Ленина, 1", "ул. Ленина, 2"])
        result = generator.generate_addresses(["Москва"], [], rng=lambda values: values[-1])
        self.assertEqual(result[0]["address"], "ул. Ленина, 2")

    def test_same_address_is_allowed_in_different_cities(self) -> None:
        generator._cache["москва"] = (generator.time.time() + 60, ["ул. Ленина, 1"])
        generator._cache["тула"] = (generator.time.time() + 60, ["ул. Ленина, 1"])
        result = generator.generate_addresses(
            ["Москва", "Тула"], [{"city": "Москва", "address": "ул. Ленина, 1"}], rng=lambda values: values[0],
        )
        self.assertIn("error", result[0])
        self.assertEqual(result[1]["address"], "ул. Ленина, 1")

    def test_relation_is_preferred_over_first_node(self) -> None:
        payload = [
            {"osm_type": "node", "osm_id": "1", "boundingbox": ["1", "2", "3", "4"]},
            {"osm_type": "relation", "osm_id": "2", "boundingbox": ["1", "2", "3", "4"]},
        ]
        with mock.patch.object(generator, "_request_json", return_value=payload):
            area = generator._find_city("Москва")
        self.assertEqual((area.osm_type, area.osm_id), ("relation", 2))

    def test_bbox_filters_explicitly_other_city(self) -> None:
        generator._areas["москва"] = generator.CityArea("node", 1, (1, 2, 3, 4))
        with mock.patch.object(generator, "_request_json", return_value={"elements": [
            {"tags": {"addr:street": "ул. Ленина", "addr:housenumber": "1", "addr:city": "Тула"}},
        ]}):
            with self.assertRaises(generator.AddressGenerationError):
                generator._load_addresses("Москва")

    def test_failed_nominatim_attempt_still_enforces_next_delay(self) -> None:
        valid = [{"osm_type": "relation", "osm_id": "1", "boundingbox": ["1", "2", "3", "4"]}]
        with mock.patch.object(generator, "_request_json", side_effect=[generator.AddressGenerationError("fail"), valid]), \
             mock.patch.object(generator.time, "monotonic", side_effect=[10, 10, 10.2, 11]), \
             mock.patch.object(generator.time, "sleep") as sleep:
            with self.assertRaises(generator.AddressGenerationError):
                generator._find_city("Москва")
            generator._find_city("Тула")
        sleep.assert_called_once()
        self.assertTrue(math.isclose(sleep.call_args.args[0], 0.8))

    def test_endpoint_rejects_non_string_city_and_handles_unexpected_error(self) -> None:
        class Request:
            def __init__(self, payload): self.payload = payload
            async def json(self): return self.payload

        response = __import__("asyncio").run(app_module.api_generate_addresses(Request({"cities": [{"bad": 1}]})))
        self.assertEqual(response.status_code, 422)
        with mock.patch.object(app_module.address_generator, "generate_addresses", side_effect=RuntimeError("boom")):
            response = __import__("asyncio").run(app_module.api_generate_addresses(Request({"cities": ["Москва"]})))
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
