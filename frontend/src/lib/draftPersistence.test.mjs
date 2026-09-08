import test from "node:test";
import assert from "node:assert/strict";

import {
  DRAFT_STORAGE_KEY,
  createDraftStorage,
  createPersistenceController,
  createPhotoStorage,
  restoreDraftForm,
} from "./draftPersistence.js";

const defaults = {
  category: "jackets",
  title: "",
  price: "",
  view_price_max: "",
  drafts_count: 1,
  locations: [{ city: "", address: "" }],
  item_type: "",
};

function createMemoryIndexedDb({ failTransactions = false } = {}) {
  const rows = new Map();
  let created = false;

  function makeStore(transaction) {
    return {
      clear() {
        if (!failTransactions) rows.clear();
      },
      put(value) {
        if (!failTransactions) rows.set(value.id, value);
      },
      getAll() {
        const request = {};
        queueMicrotask(() => {
          if (failTransactions) {
            request.error = new Error("indexeddb read failed");
            request.onerror?.();
          } else {
            request.result = Array.from(rows.values());
            request.onsuccess?.();
          }
        });
        return request;
      },
    };
  }

  const database = {
    objectStoreNames: { contains: () => created },
    createObjectStore() { created = true; },
    transaction() {
      const transaction = {
        error: failTransactions ? new Error("indexeddb transaction failed") : null,
        objectStore() { return makeStore(transaction); },
      };
      queueMicrotask(() => {
        if (failTransactions) transaction.onerror?.();
        else transaction.oncomplete?.();
      });
      return transaction;
    },
    close() {},
  };

  return {
    open() {
      const request = { result: database };
      queueMicrotask(() => {
        if (!created) request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

test("restoreDraftForm merges a current-version draft over defaults", () => {
  const restored = restoreDraftForm(defaults, {
    version: 2,
    form: {
      title: "Пиджак",
      price: "3500",
      view_price_max: "0,5",
      drafts_count: "2",
      locations: [
        { city: "Москва", address: "Тверская, 1" },
        { city: "Одинцово", address: "Центральная, 7" },
      ],
      unknown_field: "ignore me",
    },
  });

  assert.deepEqual(restored, {
    category: "jackets",
    title: "Пиджак",
    price: "3500",
    view_price_max: "0,5",
    drafts_count: "2",
    locations: [
      { city: "Москва", address: "Тверская, 1" },
      { city: "Одинцово", address: "Центральная, 7" },
    ],
    item_type: "",
  });
});

test("restoreDraftForm keeps a twenty-announcement draft", () => {
  const locations = Array.from({ length: 20 }, (_, index) => ({
    city: `Город ${index + 1}`,
    address: `Улица ${index + 1}`,
  }));
  const restored = restoreDraftForm(defaults, {
    version: 2,
    form: {
      ...defaults,
      drafts_count: "20",
      locations,
    },
  });

  assert.equal(restored.drafts_count, "20");
  assert.deepEqual(restored.locations, locations);
});

test("restoreDraftForm migrates version 1 city and address into the first ordered location", () => {
  const restored = restoreDraftForm(defaults, {
    version: 1,
    form: {
      title: "Пиджак",
      drafts_count: "3",
      city: "  Москва  ",
      address: "  ул. Тверская, 10  ",
    },
  });

  assert.equal(restored.title, "Пиджак");
  assert.equal(restored.drafts_count, "3");
  assert.equal(restored.view_price_max, "");
  assert.deepEqual(restored.locations, [
    { city: "Москва", address: "ул. Тверская, 10" },
    { city: "", address: "" },
    { city: "", address: "" },
  ]);
  assert.equal(Object.hasOwn(restored, "city"), false);
  assert.equal(Object.hasOwn(restored, "address"), false);
});

test("restoreDraftForm ignores malformed, obsolete, and invalid field values", () => {
  assert.deepEqual(restoreDraftForm(defaults, null), defaults);
  assert.deepEqual(restoreDraftForm(defaults, { version: 999, form: { title: "old" } }), defaults);
  assert.deepEqual(
    restoreDraftForm(defaults, {
      version: 2,
      form: {
        title: 42,
        drafts_count: {},
        category: "coats",
        view_price_max: 0.5,
        locations: [
          { city: "Москва", address: 42 },
          "не объект",
        ],
      },
    }),
    { ...defaults, category: "coats" },
  );
});

test("draft storage saves versioned data and restores it safely", () => {
  const records = new Map();
  const storage = {
    getItem: (key) => records.get(key) ?? null,
    setItem: (key, value) => records.set(key, value),
    removeItem: (key) => records.delete(key),
  };
  const draftStorage = createDraftStorage(storage);

  draftStorage.save({ ...defaults, title: "Пальто" });

  assert.deepEqual(JSON.parse(records.get(DRAFT_STORAGE_KEY)), {
    version: 2,
    form: { ...defaults, title: "Пальто" },
  });
  assert.deepEqual(draftStorage.load(defaults), { ...defaults, title: "Пальто" });

  records.set(DRAFT_STORAGE_KEY, "{");
  assert.deepEqual(draftStorage.load(defaults), defaults);
});

test("draft storage surfaces browser storage errors to the caller", () => {
  const quotaError = new Error("quota exceeded");
  const draftStorage = createDraftStorage({
    getItem: () => null,
    setItem: () => { throw quotaError; },
    removeItem: () => { throw quotaError; },
  });

  assert.throws(() => draftStorage.save(defaults), quotaError);
  assert.throws(() => draftStorage.clear(), quotaError);
});

test("photo storage keeps only ten files and restores metadata in input order", async () => {
  const photoStorage = createPhotoStorage(createMemoryIndexedDb());
  const files = Array.from({ length: 12 }, (_, index) => new File(
    [`photo-${index}`],
    `photo-${index}.png`,
    { type: "image/png", lastModified: 1000 + index },
  ));

  await photoStorage.save(files);
  const restored = await photoStorage.load();

  assert.equal(restored.length, 10);
  assert.deepEqual(restored.map((file) => file.name), files.slice(0, 10).map((file) => file.name));
  assert.deepEqual(restored.map((file) => file.type), Array(10).fill("image/png"));
  assert.deepEqual(restored.map((file) => file.lastModified), files.slice(0, 10).map((file) => file.lastModified));
  assert.equal(await restored[4].text(), "photo-4");
});

test("photo storage clear removes every saved file", async () => {
  const photoStorage = createPhotoStorage(createMemoryIndexedDb());
  await photoStorage.save([new File(["one"], "one.jpg", { type: "image/jpeg" })]);

  await photoStorage.clear();

  assert.deepEqual(await photoStorage.load(), []);
});

test("photo storage reports IndexedDB transaction errors", async () => {
  const photoStorage = createPhotoStorage(createMemoryIndexedDb({ failTransactions: true }));

  await assert.rejects(photoStorage.save([new File(["x"], "x.png")]), /indexeddb transaction failed/);
  await assert.rejects(photoStorage.load(), /indexeddb read failed/);
  await assert.rejects(photoStorage.clear(), /indexeddb transaction failed/);
});

test("persistence controller isolates old saves across clear and accepts new post-clear saves", async () => {
  const saveStarted = deferred();
  const releaseSave = deferred();
  const events = [];
  const controller = createPersistenceController({
    draftStorage: {
      save: async (form) => {
        events.push(`save-form:${form.title}`);
        saveStarted.resolve();
        await releaseSave.promise;
      },
      clear: async () => { events.push("clear-form"); },
    },
    photoStorage: {
      save: async () => { events.push("save-photos"); },
      clear: async () => { events.push("clear-photos"); },
    },
  });

  const saving = controller.saveForm({ title: "old" });
  await saveStarted.promise;
  const clearing = controller.clear();
  const ignoredOldGeneration = controller.saveForm({ title: "late old" });
  releaseSave.resolve();

  await Promise.all([saving, clearing, ignoredOldGeneration]);
  const savedNewGeneration = await controller.saveForm({ title: "new" });

  assert.equal(await ignoredOldGeneration, false);
  assert.equal(savedNewGeneration, true);
  assert.deepEqual(events, ["save-form:old", "clear-form", "clear-photos", "save-form:new"]);
});

test("persistence controller attempts both clear operations when one storage fails", async () => {
  const events = [];
  const controller = createPersistenceController({
    draftStorage: {
      save: async () => {},
      clear: () => {
        events.push("clear-form");
        throw new Error("localStorage clear failed");
      },
    },
    photoStorage: {
      save: async () => {},
      clear: async () => { events.push("clear-photos"); },
    },
  });

  await assert.rejects(controller.clear(), /localStorage clear failed/);
  assert.deepEqual(events, ["clear-form", "clear-photos"]);

  const savedAfterFailure = await controller.saveForm({ title: "retry" });
  assert.equal(savedAfterFailure, true);
});
