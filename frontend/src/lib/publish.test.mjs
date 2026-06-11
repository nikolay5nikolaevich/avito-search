import test from "node:test";
import assert from "node:assert/strict";

import {
  buildLaunchFormData,
  buildPublishFormData,
  buildPrepPhotoUrl,
  extractPublishFieldErrors,
  replaceDraftCard,
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

// ─── Тесты фазы превью ────────────────────────────────────────────────────────

test("buildLaunchFormData: полная форма + фото + prep_id для запуска из превью", () => {
  const form = {
    title: "Костюм Hugo Boss",
    trade_type: "Продаю своё",
    condition: "Отличное",
    size: "50 (L)",
    brand: "Hugo Boss",
    color: "Чёрный",
    description: "Почти не носили",
    price: "15000",
    city: "Москва",
    address: "Арбат, 1",
    drafts_count: 3,
  };
  const photos = [
    new File(["photo"], "photo-1.png", { type: "image/png" }),
    new File(["photo"], "photo-2.png", { type: "image/png" }),
  ];

  const formData = buildLaunchFormData(form, photos, "prep-abc-123");

  // prep_id уходит вместе с формой
  assert.equal(formData.get("prep_id"), "prep-abc-123");

  // Полный набор полей формы — как при обычном запуске
  assert.equal(formData.get("title"), "Костюм Hugo Boss");
  assert.equal(formData.get("trade_type"), "Продаю своё");
  assert.equal(formData.get("condition"), "Отличное");
  assert.equal(formData.get("size"), "50 (L)");
  assert.equal(formData.get("brand"), "Hugo Boss");
  assert.equal(formData.get("color"), "Чёрный");
  assert.equal(formData.get("description"), "Почти не носили");
  assert.equal(formData.get("price"), "15000");
  assert.equal(formData.get("city"), "Москва");
  assert.equal(formData.get("address"), "Арбат, 1");
  assert.equal(formData.get("drafts_count"), "3");

  // Фото тоже уходят (бэкенд валидирует их даже при наличии prep_id)
  assert.equal(formData.getAll("photos").length, 2);
});

test("buildPrepPhotoUrl строит правильный путь (1-based индексы контракта)", () => {
  assert.equal(
    buildPrepPhotoUrl("abc123", 2, 1),
    "/api/publish/prepare/photo/abc123/2/1",
  );
  assert.equal(
    buildPrepPhotoUrl("xyz-99", 1, 5),
    "/api/publish/prepare/photo/xyz-99/1/5",
  );
});

test("replaceDraftCard заменяет карточку по полю index (1-based) и не мутирует исходный массив", () => {
  const original = [
    { index: 1, title: "Оригинал" },
    { index: 2, title: "Вариант 2" },
    { index: 3, title: "Вариант 3" },
  ];

  const updated = replaceDraftCard(original, { index: 2, title: "Новый вариант 2" });

  // Заменена именно карточка с index=2 (позиция 1 в массиве)
  assert.equal(updated[1].title, "Новый вариант 2");
  assert.equal(updated[1].index, 2);

  // Соседние карточки 1 и 3 не тронуты (те же самые объекты)
  assert.equal(updated[0], original[0]);
  assert.equal(updated[2], original[2]);

  // Исходный массив не мутирован
  assert.equal(original[1].title, "Вариант 2");

  // Длина сохранена
  assert.equal(updated.length, 3);
});

test("replaceDraftCard с index последней карточки заменяет последний элемент", () => {
  const original = [
    { index: 1, title: "Оригинал" },
    { index: 2, title: "Вариант 2" },
    { index: 3, title: "Вариант 3" },
  ];
  const updated = replaceDraftCard(original, { index: 3, title: "Обновлено" });
  assert.equal(updated[2].title, "Обновлено");
  assert.equal(updated[0].title, "Оригинал");
  assert.equal(updated[1].title, "Вариант 2");
});
