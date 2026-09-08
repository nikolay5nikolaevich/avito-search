import test from "node:test";
import assert from "node:assert/strict";

import {
  buildLaunchFormData,
  buildPublishFormData,
  buildPrepPhotoUrl,
  canResumePublish,
  describeResumePlan,
  extractPublishFieldErrors,
  normalizePublishProgress,
    normalizeViewPrice,
  publishButtonLabel,
  replaceDraftCard,
  resizeLocations,
  resumeUnavailableMessage,
  TERMINAL_STATUSES,
    validateLocations,
} from "./publish.js";

test("reconnect button trusts only backend resume availability", () => {
  assert.equal(canResumePublish({
    status: "needs_user_action",
    resume_available: true,
    items_total: 13,
    items_published: 9,
  }), true);
  assert.equal(canResumePublish({
    status: "needs_user_action",
    resume_available: false,
    items_total: 13,
    items_published: 9,
  }), false);
  assert.equal(canResumePublish({
    status: "failed",
    items_total: 13,
    items_published: 9,
  }), false);
  assert.equal(canResumePublish({
    status: "needs_user_action",
    resume_available: true,
    items_total: 13,
    items_published: 13,
  }), false);
});

test("resume is available on the red 'failed' status too, not only the yellow user_action one", () => {
  // Боевой кейс: fill_view_price упал со статусом "failed" (не user_action),
  // объявление уже создано на Авито — кнопка должна показаться в режиме skip_item.
  assert.equal(canResumePublish({
    status: "failed",
    resume_available: true,
    items_total: 13,
    items_published: 6,
  }), true);
});

test("describeResumePlan renders retry_item and skip_item texts from resume_plan alone", () => {
  assert.equal(describeResumePlan({ resume_plan: null }), null);

  const retry = describeResumePlan({
    resume_plan: { mode: "retry_item", start_index: 7, skipped_item: null, items_total: 13 },
  });
  assert.equal(retry.buttonLabel, "Продолжить публикацию");
  assert.match(retry.description, /№7 из 13/);

  const skip = describeResumePlan({
    resume_plan: { mode: "skip_item", start_index: 8, skipped_item: 7, items_total: 13 },
  });
  assert.equal(skip.buttonLabel, "Продолжить со следующего");
  assert.match(skip.description, /№8 из 13/);
  assert.match(skip.description, /№7 уже создано на Авито/);
});

test("resumeUnavailableMessage only fires when resume is genuinely unavailable with items left", () => {
  assert.equal(resumeUnavailableMessage({
    status: "failed",
    resume_available: false,
    items_total: 13,
    items_published: 12,
  }), "Продолжение недоступно: остановка пришлась на последнее объявление — завершите его вручную.");

  // Всё отправлено — это не тот случай, объяснение не нужно
  assert.equal(resumeUnavailableMessage({
    status: "failed",
    resume_available: false,
    items_total: 13,
    items_published: 13,
  }), null);

  // Возобновление доступно — объяснение не нужно
  assert.equal(resumeUnavailableMessage({
    status: "failed",
    resume_available: true,
    items_total: 13,
    items_published: 6,
  }), null);
});

test("interrupted publish is terminal until user explicitly resumes it", () => {
  assert.equal(TERMINAL_STATUSES.has("interrupted"), true);
});

test("normalizePublishProgress prefers published contract and supports legacy fallback", () => {
  assert.deepEqual(normalizePublishProgress(null), {
    itemsTotal: null,
    itemIndex: null,
    itemsPublished: 0,
    publishedUrls: [],
    appliedViewPrices: [],
    addressWarnings: [],
    brandSelected: null,
    skippedItems: [],
  });

  assert.deepEqual(normalizePublishProgress({
    items_total: 3,
    item_index: 2,
    items_published: 1,
    published_urls: ["https://www.avito.ru/test_100"],
    drafts_total: 99,
    draft_index: 99,
    drafts_saved: 99,
    saved_urls: ["stale"],
    applied_view_prices: [{ item_index: 1, price: "1.7" }],
    address_warnings: [{ item_index: 1, requested: "Москва, Арбат, 1", applied: "Москва" }],
    brand_selected: "Без бренда",
    skipped_items: [2],
  }), {
    itemsTotal: 3,
    itemIndex: 2,
    itemsPublished: 1,
    publishedUrls: ["https://www.avito.ru/test_100"],
    appliedViewPrices: [{ item_index: 1, price: "1.7" }],
    addressWarnings: [{ item_index: 1, requested: "Москва, Арбат, 1", applied: "Москва" }],
    brandSelected: "Без бренда",
    skippedItems: [2],
  });

  assert.deepEqual(normalizePublishProgress({
    drafts_total: 2,
    draft_index: 1,
    drafts_saved: 0,
    saved_urls: [],
  }), {
    itemsTotal: 2,
    itemIndex: 1,
    itemsPublished: 0,
    publishedUrls: [],
    appliedViewPrices: [],
    addressWarnings: [],
    brandSelected: null,
    skippedItems: [],
  });
});

test("publishButtonLabel uses Russian announcement plural forms", () => {
  assert.equal(publishButtonLabel(1), "Опубликовать 1 объявление");
  assert.equal(publishButtonLabel(2), "Опубликовать 2 объявления");
  assert.equal(publishButtonLabel(5), "Опубликовать 5 объявлений");
  assert.equal(publishButtonLabel(21), "Опубликовать 21 объявление");
});

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
    view_price_max: " 2,0 ",
    locations: [
      { city: "  Москва ", address: "  Арбат, 1 " },
      { city: "Одинцово", address: "ул. Центральная, 7" },
      { city: "Москва", address: "Арбат, 1" },
    ],
    drafts_count: 3,
    category: "sneakers",
  };

  const photos = [new File(["photo"], "photo-1.png", { type: "image/png" })];
  const formData = buildPublishFormData(form, photos);

  assert.equal(formData.get("category"), "sneakers");
  assert.equal(formData.get("title"), "Костюм Hugo Boss");
  assert.equal(formData.get("trade_type"), "Продаю своё");
  assert.equal(formData.get("ad_type"), null);
  assert.equal(formData.get("brand"), "Hugo Boss");
  assert.equal(formData.get("price"), "15000");
  assert.equal(formData.get("view_price_max"), "2.0");
  assert.deepEqual(JSON.parse(formData.get("locations_json")), [
    { city: "Москва", address: "Арбат, 1" },
    { city: "Одинцово", address: "ул. Центральная, 7" },
    { city: "Москва", address: "Арбат, 1" },
  ]);
  assert.equal(formData.get("city"), null);
  assert.equal(formData.get("address"), null);
  assert.equal(formData.get("drafts_count"), "3");
  assert.equal(formData.getAll("photos").length, 1);
});

test("resizeLocations preserves order, appends blanks, and drops inactive rows", () => {
  const first = { city: "Москва", address: "Тверская, 1" };
  const second = { city: "Одинцово", address: "Центральная, 7" };

  const increased = resizeLocations([first], 3);
  assert.deepEqual(increased, [
    first,
    { city: "", address: "" },
    { city: "", address: "" },
  ]);
  assert.notEqual(increased[0], first);

  const decreased = resizeLocations([first, second, { city: "Тула", address: "Ленина, 2" }], 2);
  assert.deepEqual(decreased, [first, second]);
  assert.notEqual(decreased[0], first);
  assert.notEqual(decreased[1], second);
});

test("normalizeViewPrice keeps decimal digits exact and rejects non-positive or ambiguous input", () => {
  assert.equal(normalizeViewPrice(" 2,250 "), "2.250");
  assert.equal(normalizeViewPrice("0.5"), "0.5");
  assert.equal(normalizeViewPrice("1"), "1");
  assert.equal(normalizeViewPrice("0"), null);
  assert.equal(normalizeViewPrice("0,00"), null);
  assert.equal(normalizeViewPrice("-1"), null);
  assert.equal(normalizeViewPrice("1e3"), null);
  assert.equal(normalizeViewPrice("1,2,3"), null);
  assert.equal(normalizeViewPrice(""), null);
});

test("validateLocations reports one-based errors for the matching announcement", () => {
  assert.deepEqual(validateLocations([
    { city: "Москва", address: "Тверская, 1" },
    { city: "  ", address: "" },
  ], 2), {
    "locations.2.city": "Укажите город для объявления №2",
    "locations.2.address": "Укажите улицу для объявления №2",
  });
  assert.deepEqual(validateLocations([
    { city: "Москва", address: "Тверская, 1" },
  ], 2), {
    locations: "Количество геолокаций должно совпадать с количеством объявлений",
  });
});

test("validateLocations rejects an address without street structure", () => {
  assert.deepEqual(validateLocations([
    { city: "Санкт-Петербург", address: "королева 26" },
    { city: "Москва", address: "Тверская, 10" },
    { city: "Казань", address: "улица Баумана 7" },
    { city: "Челябинск", address: "Российская улица" },
    { city: "Омск", address: "улица Избышева," },
  ], 5), {
    "locations.1.address": "Добавьте тип улицы (например, «Тверская улица») или отделите дом запятой",
  });
});

test("extractPublishFieldErrors reads custom backend 422 payload", () => {
  const fieldErrors = extractPublishFieldErrors({
    errors: [
      { field: "trade_type", error: "Вид объявления: недопустимое значение ''" },
      { field: "drafts_count", error: "Сколько черновиков: целое число от 1 до 20" },
    ],
  });

  assert.deepEqual(fieldErrors, {
    trade_type: "Вид объявления: недопустимое значение ''",
    drafts_count: "Сколько черновиков: целое число от 1 до 20",
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
    view_price_max: "3",
    locations: [
      { city: "Москва", address: "Арбат, 1" },
      { city: "Одинцово", address: "Центральная, 7" },
      { city: "Тула", address: "Ленина, 2" },
    ],
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
  assert.equal(formData.get("view_price_max"), "3");
  assert.deepEqual(JSON.parse(formData.get("locations_json")), form.locations);
  assert.equal(formData.get("city"), null);
  assert.equal(formData.get("address"), null);
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
