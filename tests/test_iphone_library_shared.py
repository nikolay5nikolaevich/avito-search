"""Независимая приёмка общего текста и порядка фото, без живого Авито."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import publish_test_isolation  # noqa: F401 — до app/publisher
import app as api
import iphone_library as library
import photo_variation
import preparation
from PIL import Image
from test_security import multipart


class SharedLibraryAcceptance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "library" / "iphone"
        self.template = "{модель} | {память} | {цвет} | {SIM}\n\nДоставка.  Два пробела.\n"
        self.shared = self.root / "description" / "iphone.txt"
        self.shared.parent.mkdir(parents=True)
        self.shared.write_bytes(self.template.encode("utf-8"))
        self.models = ("iPhone 17 Pro Max", "iPhone 17 Pro")
        for model in self.models:
            pack = self.root / model / "512 GB" / "Наш пак"
            pack.mkdir(parents=True)
            for name, color in (("01.png", "red"), ("02.png", "blue"), ("03.png", "green")):
                Image.new("RGB", (120, 100), color).save(pack / name)
        for patcher in (
            mock.patch.object(library, "LIBRARY_ROOT", self.root),
            mock.patch.object(api, "TMP_PUBLISH_DIR", self.directory / "publish"),
            mock.patch.dict(api.PREP_JOBS, {}, clear=True),
            mock.patch.dict(api.PUBLISH_JOBS, {}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fields = dict(category="iphone", title=self.models[0], model=self.models[0],
                           memory="512 ГБ", color="Синий", sim_type="eSIM",
                           pack_id="Наш пак", drafts_count="2")

    def select(self, **changes):
        values = dict(title=self.models[0], memory="512 ГБ", color="Синий",
                      sim_type="eSIM", drafts_count=2, pack_id="Наш пак",
                      root=self.root, seed=11)
        values.update(changes)
        return library.select_materials(**values)

    def add_legacy_text(self):
        for model in self.models:
            directory = self.root / model / "descriptions"
            directory.mkdir()
            (directory / "legacy.txt").write_text("Старый текст", encoding="utf-8")

    def request(self, method, path, *, fields=None, query=None, headers=None):
        body, content_type = multipart(fields or {})
        request_headers = {"host": "localhost:7777", "x-avito-local": "1",
                           "content-type": content_type, **(headers or {})}
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path,
                 "raw_path": path.encode(), "query_string": urlencode(query or {}).encode(),
                 "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in request_headers.items()],
                 "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 7777), "extensions": {}}
        result = {"body": b"", "headers": {}}

        async def run():
            received = False

            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                if message["type"] == "http.response.start":
                    result["status"] = message["status"]
                    result["headers"] = dict(message.get("headers", []))
                elif message["type"] == "http.response.body":
                    result["body"] += message.get("body", b"")

            await api.app(scope, receive, send)
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=15)

        with mock.patch.object(api.publisher, "run_publish_job", new_callable=mock.AsyncMock) as publish:
            asyncio.run(run())
            publish.assert_not_called()
        return result

    def photo(self, **changes):
        query = dict(title=self.models[0], memory="512 ГБ", pack_id="Наш пак", photo_id="01.png")
        query.update(changes)
        return self.request("GET", "/api/publish/iphone-library/photo", query=query)

    def test_one_shared_utf8_template_serves_two_models_and_every_sim(self):
        for model in self.models:
            for sim in library.SIM_TYPES:
                with self.subTest(model=model, sim=sim):
                    selected = self.select(title=model, sim_type=sim)
                    expected = f"{model} | 512 ГБ | Синий | {sim}\n\nДоставка.  Два пробела.\n"
                    self.assertEqual(selected["descriptions"], [expected, expected])
                    self.assertEqual(selected["template_names"], ["iphone.txt", "iphone.txt"])
                    self.assertEqual(library.catalog(model, "512 ГБ", root=self.root)["descriptions_count"], 1)

    def test_shared_template_takes_precedence_over_model_templates(self):
        self.add_legacy_text()
        self.assertIn("Доставка.", self.select()["descriptions"][0])

    def test_existing_empty_shared_directory_must_not_fall_back(self):
        self.add_legacy_text()
        # Сохраняем тестовый файл вне общей папки, чтобы она осталась пустой.
        self.shared.rename(self.directory / "saved.txt")
        with self.assertRaises(ValueError):
            self.select()

    def test_two_shared_txt_files_must_not_fall_back(self):
        self.add_legacy_text()
        (self.shared.parent / "second.txt").write_text("Второй текст", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.select()

    def test_invalid_shared_templates_must_not_fall_back(self):
        self.add_legacy_text()
        for content in (b"\xff\xfe", b"   ", "{цена}".encode("utf-8")):
            with self.subTest(content=content):
                self.shared.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.select()

    def test_absent_shared_directory_supports_legacy_layout(self):
        self.add_legacy_text()
        self.shared.parent.rename(self.directory / "saved_description")
        self.assertEqual(self.select()["descriptions"], ["Старый текст"] * 2)

    def test_catalog_exposes_only_photo_ids_in_default_order(self):
        self.add_legacy_text()
        listing = library.catalog(self.models[0], "512 ГБ", root=self.root)
        self.assertEqual(listing["packs"][0]["photos"], [
            {"id": "01.png"}, {"id": "02.png"}, {"id": "03.png"},
        ])
        self.assertNotIn(str(self.root), json.dumps(listing, ensure_ascii=False))

    def test_selected_order_is_a_copy_and_exact_permutation(self):
        self.add_legacy_text()
        order = ["03.png", "01.png", "02.png"]
        selected = self.select(photo_order=order)
        self.assertEqual([path.name for path in selected["source_photos"]], order)
        self.assertEqual(order, ["03.png", "01.png", "02.png"])
        self.assertEqual([path.name for path in self.select(photo_order=None)["source_photos"]],
                         ["01.png", "02.png", "03.png"])

    def test_invalid_photo_orders_are_rejected(self):
        self.add_legacy_text()
        invalid = [["01.png"], ["01.png", "01.png", "03.png"],
                   ["01.png", "02.png", "missing.png"],
                   ["01.png", "02.png", "../03.png"],
                   ["01.png", "02.png", "..\\03.png"],
                   ["01.png", "02.png", str(self.root / "03.png")],
                   ["01.png", "02.png", 3]]
        for order in invalid:
            with self.subTest(order=order), self.assertRaises(ValueError):
                self.select(photo_order=order)

    def test_snapshot_freezes_shared_text_and_selected_photo_order(self):
        selected = self.select(photo_order=["03.png", "01.png", "02.png"])
        photos = selected.pop("source_photos")
        originals = [path.read_bytes() for path in photos]
        prep_dir = self.directory / "snapshot"
        job = {}
        asyncio.run(preparation.run_prep_job(
            "shared", job, title=selected["source_title"], description=selected["descriptions"][0],
            source_photos=photos, drafts_count=2, facts={}, base_dir=prep_dir, library=selected))
        self.assertEqual(job["status"], "done", job)
        snapshot = preparation.read_library_snapshot(prep_dir)
        self.assertEqual(snapshot["descriptions"], selected["descriptions"])
        self.assertEqual([path.read_bytes() for path in sorted((prep_dir / "source" / "photos").iterdir())], originals)
        self.shared.write_text("Изменённый текст", encoding="utf-8")
        card = preparation.regenerate_draft(prep_dir, 1)
        self.assertIn(selected["descriptions"][0].rstrip(), card["description"])
        self.assertNotIn("Изменённый текст", card["description"])
        self.assertEqual(preparation.read_library_snapshot(prep_dir), snapshot)

    def test_api_prepare_preserves_requested_order_in_source_copies(self):
        self.add_legacy_text()
        order = ["03.png", "01.png", "02.png"]
        result = self.request("POST", "/api/publish/prepare",
                              fields={**self.fields, "photo_order": json.dumps(order)})
        self.assertEqual(result["status"], 200, result["body"])
        prep_id = json.loads(result["body"])["prep_id"]
        self.assertEqual(api.PREP_JOBS[prep_id]["status"], "done")
        source_dir = self.directory / "publish" / f"prep_{prep_id}" / "source" / "photos"
        pack = self.root / self.models[0] / "512 GB" / "Наш пак"
        self.assertEqual([p.read_bytes() for p in sorted(source_dir.iterdir())],
                         [(pack / name).read_bytes() for name in order])

    def test_api_prepare_rejects_malformed_non_list_and_invalid_orders(self):
        self.add_legacy_text()
        for value in ("{bad", "null", '"01.png"', "{}", "123",
                      '["01.png"]', '["01.png", "01.png", "03.png"]',
                      '["01.png", "02.png", "../03.png"]'):
            with self.subTest(value=value):
                result = self.request("POST", "/api/publish/prepare",
                                      fields={**self.fields, "photo_order": value})
                self.assertEqual(result["status"], 422, result["body"])
                self.assertEqual(api.PREP_JOBS, {})

    def test_photo_endpoint_returns_jpeg_without_modifying_original(self):
        path = self.root / self.models[0] / "512 GB" / "Наш пак" / "01.png"
        original = path.read_bytes()
        result = self.photo()
        self.assertEqual(result["status"], 200, result["body"])
        self.assertEqual(result["headers"][b"content-type"].split(b";")[0], b"image/jpeg")
        with Image.open(io.BytesIO(result["body"])) as image:
            self.assertEqual(image.format, "JPEG")
            image.load()
        self.assertEqual(path.read_bytes(), original)

    def test_photo_endpoint_converts_actual_heic_to_jpeg(self):
        if not photo_variation.heif_available():
            self.skipTest("pillow-heif отсутствует: HEIC создать нельзя")
        path = self.root / self.models[0] / "512 GB" / "Наш пак" / "phone.heic"
        Image.new("RGB", (120, 100), "orange").save(path, format="HEIF")
        result = self.photo(photo_id="phone.heic")
        self.assertEqual(result["status"], 200, result["body"])
        self.assertEqual(result["headers"][b"content-type"].split(b";")[0], b"image/jpeg")
        with Image.open(io.BytesIO(result["body"])) as image:
            self.assertEqual(image.format, "JPEG")

    def test_photo_endpoint_rejects_paths_and_unknown_photo(self):
        sentinel = b"PRIVATE TEST FILE MUST NOT BE RETURNED"
        external = self.directory / "private.jpg"
        external.write_bytes(sentinel)
        for changes in ({"photo_id": "../private.jpg"}, {"photo_id": "..\\private.jpg"},
                        {"photo_id": str(external)}, {"photo_id": "missing.png"},
                        {"pack_id": "../Наш пак"}, {"pack_id": "..\\Наш пак"},
                        {"pack_id": str(external.parent)}):
            with self.subTest(changes=changes):
                result = self.photo(**changes)
                self.assertIn(result["status"], (400, 404, 422))
                self.assertNotIn(sentinel, result["body"])

    def test_photo_endpoint_obeys_local_host_and_origin_guards(self):
        query = dict(title=self.models[0], memory="512 ГБ", pack_id="Наш пак", photo_id="01.png")
        for headers, expected in (({"host": "evil.example"}, 400),
                                  ({"origin": "https://evil.example"}, 403)):
            with self.subTest(headers=headers):
                result = self.request("GET", "/api/publish/iphone-library/photo",
                                      query=query, headers=headers)
                self.assertEqual(result["status"], expected)


if __name__ == "__main__":
    unittest.main()
