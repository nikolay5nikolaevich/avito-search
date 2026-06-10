import test from "node:test";
import assert from "node:assert/strict";

import {
  buildPublishFormData,
  extractPublishFieldErrors,
} from "./publish.js";

test("buildPublishFormData uses backend field names for publish form", () => {
  const form = {
    title: "  Костюм Hugo Boss  ",
    trade_type: "Продаю своё",
    condition: "Отличное",
    size: "50 (L)",
    brand: "  Hugo Boss ",
    color: "Чёрный",
    description: "  Почти не носили ",
    price: "15000",
    city: "  Москва ",
    address: "  Арбат, 1 ",
    drafts_count: 3,
  };

  const photos = [new File(["photo"], "photo-1.png", { type: "image/png" })];
  const formData = buildPublishFormData(form, photos);

  assert.equal(formData.get("title"), "Костюм Hugo Boss");
  assert.equal(formData.get("trade_type"), "Продаю своё");
  assert.equal(formData.get("ad_type"), null);
  assert.equal(formData.get("brand"), "Hugo Boss");
  assert.equal(formData.get("price"), "15000");
  assert.equal(formData.get("city"), "Москва");
  assert.equal(formData.get("address"), "Арбат, 1");
  assert.equal(formData.get("drafts_count"), "3");
  assert.equal(formData.getAll("photos").length, 1);
});

test("extractPublishFieldErrors reads custom backend 422 payload", () => {
  const fieldErrors = extractPublishFieldErrors({
    errors: [
      { field: "trade_type", error: "Вид объявления: недопустимое значение ''" },
      { field: "drafts_count", error: "Сколько черновиков: целое число от 1 до 10" },
    ],
  });

  assert.deepEqual(fieldErrors, {
    trade_type: "Вид объявления: недопустимое значение ''",
    drafts_count: "Сколько черновиков: целое число от 1 до 10",
  });
});

test("extractPublishFieldErrors keeps FastAPI detail compatibility", () => {
  const fieldErrors = extractPublishFieldErrors({
    detail: [
      { loc: ["body", "photos"], msg: "field required" },
    ],
  });

  assert.deepEqual(fieldErrors, {
    photos: "field required",
  });
});
