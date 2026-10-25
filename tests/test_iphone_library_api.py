"""Приёмка HTTP-контракта без сети, CDP и денежных действий."""
import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import publish_test_isolation  # noqa: F401, E402
import app as api
import preparation
import publish_state
from test_security import multipart


class IphoneApiAcceptance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.library_root = self.directory / "library"
        model = self.library_root / "iPhone 17 Pro Max"
        (model / "descriptions").mkdir(parents=True)
        (model / "descriptions" / "01.txt").write_text("{модель} {память}. {SIM}.\n\nДоставка по договорённости.", encoding="utf-8")
        pack = model / "512 GB" / "Наш пак"
        pack.mkdir(parents=True)
        Image.new("RGB", (120, 100), "blue").save(pack / "01.png")
        for patcher in [mock.patch.object(api, "TMP_PUBLISH_DIR", self.directory / "publish"), mock.patch.dict(api.PREP_JOBS, {}, clear=True), mock.patch.dict(api.PUBLISH_JOBS, {}, clear=True)]:
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            library = importlib.import_module("iphone_library")
        except ModuleNotFoundError:
            pass  # RED: маршруты проверяются и до появления модуля.
        else:
            patcher = mock.patch.object(library, "LIBRARY_ROOT", self.library_root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fields = dict(category="iphone", title="17 Pro Max iPhone", model="iPhone 17 Pro Max", memory="512 ГБ", color="Синий", sim_type="eSIM", pack_id="Наш пак", drafts_count="2", condition="Новое", trade_type="Продаю своё", price="120000", view_price_max="5", locations_json=json.dumps([{"city": "Москва", "address": "ул. Арбат, 1"}] * 2))

    def request(self, method, path, *, fields=None, query=None, files=None):
        body, content_type = multipart(fields or {}, files)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": urlencode(query or {}).encode(), "root_path": "", "headers": [(b"host", b"localhost:7777"), (b"x-avito-local", b"1"), (b"content-type", content_type.encode())], "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 7777), "extensions": {}}
        result = {"body": b""}

        async def run():
            sent = False
            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()
            async def send(message):
                if message["type"] == "http.response.start":
                    result["status"] = message["status"]
                elif message["type"] == "http.response.body":
                    result["body"] += message.get("body", b"")
            await api.app(scope, receive, send)
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=15)

        with mock.patch.object(api.publisher, "run_publish_job", new_callable=mock.AsyncMock) as publish:
            asyncio.run(run())
            publish.assert_not_called()
        return result["status"], json.loads(result["body"])

    def prepare(self):
        status, payload = self.request("POST", "/api/publish/prepare", fields=self.fields)
        self.assertEqual(status, 200, payload)
        prep_id = payload["prep_id"]
        self.assertEqual(api.PREP_JOBS[prep_id]["status"], "done", api.PREP_JOBS[prep_id])
        return prep_id

    def test_catalog_resolves_title_and_refreshes_disk(self):
        query = {"title": self.fields["title"], "memory": "512 ГБ"}
        status, payload = self.request("GET", "/api/publish/iphone-library", query=query)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["model"], "iPhone 17 Pro Max")
        self.assertEqual([p["id"] for p in payload["packs"]], ["Наш пак"])
        (self.library_root / "iPhone 17 Pro Max" / "512 GB" / "Пустой").mkdir()
        status, payload = self.request("GET", "/api/publish/iphone-library", query=query)
        self.assertEqual(status, 200)
        empty = next(p for p in payload["packs"] if p["id"] == "Пустой")
        self.assertFalse(empty["available"])
        self.assertTrue(empty["error"])

    def test_unknown_model_is_422(self):
        status, _ = self.request("GET", "/api/publish/iphone-library", query={"title": "iPhone 18", "memory": "512 ГБ"})
        self.assertEqual(status, 422)

    def test_catalog_retains_model_for_empty_or_stale_memory(self):
        for memory in ["", "128 ГБ"]:
            with self.subTest(memory=memory):
                status, payload = self.request("GET", "/api/publish/iphone-library", query={"title": "iPhone 17 Pro Max", "memory": memory})
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["model"], "iPhone 17 Pro Max")
                self.assertEqual(payload["packs"], [])
                self.assertTrue(payload["error"])

    def test_prepare_without_upload_or_description_and_public_snapshot(self):
        prep_id = self.prepare()
        status, result = self.request("GET", f"/api/publish/prepare/result/{prep_id}")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["library"]["sim_type"], "eSIM")
        self.assertEqual(result["library"]["pack_id"], "Наш пак")
        self.assertNotIn("descriptions", result["library"])
        self.assertNotIn("source_photos", result["library"])
        self.assertNotIn(str(self.library_root), json.dumps(result, ensure_ascii=False))
        self.assertEqual(api.PUBLISH_JOBS, {})

    def test_required_sim_and_manual_upload_rejected(self):
        for changes in [{"sim_type": ""}, {"sim_type": "auto"}]:
            status, _ = self.request("POST", "/api/publish/prepare", fields={**self.fields, **changes})
            self.assertEqual(status, 422)
        photo = next(self.library_root.rglob("*.png"))
        status, _ = self.request("POST", "/api/publish/prepare", fields=self.fields, files=[("photos", "photo.png", "image/png", photo.read_bytes())])
        self.assertEqual(status, 422)
        self.assertEqual(api.PREP_JOBS, {})

    def test_start_rejects_missing_preview(self):
        status, payload = self.request("POST", "/api/publish/start", fields=self.fields)
        self.assertEqual(status, 422, payload)
        self.assertIn("prep_id", {error["field"] for error in payload["errors"]})
        self.assertEqual(api.PUBLISH_JOBS, {})

    def test_start_rejects_changed_snapshot_fields_before_scheduling(self):
        prep_id = self.prepare()
        for changes in [{"title": "iPhone 17 Pro"}, {"model": "iPhone 17 Pro"}, {"memory": "1024 ГБ"}, {"color": "Оранжевый"}, {"sim_type": "SIM + SIM"}, {"drafts_count": "1"}, {"pack_id": "Другой пак"}]:
            with self.subTest(changes=changes):
                status, payload = self.request("POST", "/api/publish/start", fields={**self.fields, "prep_id": prep_id, **changes})
                self.assertEqual(status, 422, payload)
                self.assertEqual(api.PUBLISH_JOBS, {})

    def test_tampered_card_description_blocks_start(self):
        prep_id = self.prepare()
        path = self.directory / "publish" / f"prep_{prep_id}" / "draft_02" / "text.txt"
        path.write_text("Упаковка вскрыта. Активирован вчера.", encoding="utf-8")
        status, payload = self.request("POST", "/api/publish/start", fields={**self.fields, "prep_id": prep_id})
        self.assertEqual(status, 422, payload)
        self.assertEqual(api.PUBLISH_JOBS, {})

    def test_start_uses_prepared_snapshot_and_saves_sim_checkpoint(self):
        prep_id = self.prepare()
        prep_dir = self.directory / "publish" / f"prep_{prep_id}"
        card = preparation.build_result(prep_dir, 2)[0]
        with mock.patch.object(api, "_schedule_publish") as schedule:
            status, payload = self.request("POST", "/api/publish/start", fields={**self.fields, "prep_id": prep_id})
        self.assertEqual(status, 200, payload)
        schedule.assert_called_once()
        job_id, draft, job_dir, scheduled_prep_id = schedule.call_args.args
        self.assertEqual(job_id, payload["job_id"])
        self.assertEqual(scheduled_prep_id, prep_id)
        self.assertEqual(draft.description, card["description"])
        self.assertEqual(draft.sim_type, "eSIM")
        self.assertEqual(draft.model, "iPhone 17 Pro Max")
        self.assertEqual(draft.memory, "512 ГБ")
        self.assertTrue(draft.photo_paths)
        prepared_photos = [path.read_bytes() for path in (prep_dir / "draft_01").rglob("*") if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
        self.assertEqual([Path(path).read_bytes() for path in draft.photo_paths], prepared_photos)
        saved_job, restored = publish_state.load_publish_state(job_dir)
        self.assertEqual(restored, draft)
        self.assertEqual(restored.sim_type, "eSIM")
        self.assertEqual(saved_job["prep_id"], prep_id)


if __name__ == "__main__":
    unittest.main()
