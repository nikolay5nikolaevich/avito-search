import { MAX_DRAFTS, resizeLocations } from "./publish.js";

export const DRAFT_STORAGE_KEY = "avito.draft.form";
const DRAFT_STORAGE_VERSION = 2;
const LEGACY_DRAFT_STORAGE_VERSION = 1;
const DRAFT_PHOTOS_DB = "avito-draft-photos";
const DRAFT_PHOTOS_STORE = "photos";
const MAX_SAVED_PHOTOS = 10;

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isDraftValue(key, value, defaultValue) {
  if (key === "drafts_count") {
    const count = Number(value);
    return (
      ((typeof value === "string" && /^\d+$/.test(value))
        || (typeof value === "number" && Number.isFinite(value)))
      && Number.isInteger(count)
      && count >= 1
      && count <= MAX_DRAFTS
    );
  }
  return typeof value === typeof defaultValue;
}

export function restoreDraftForm(defaults, savedDraft) {
  const restored = { ...defaults };
  if (
    !isRecord(savedDraft)
    || ![LEGACY_DRAFT_STORAGE_VERSION, DRAFT_STORAGE_VERSION].includes(savedDraft.version)
    || !isRecord(savedDraft.form)
  ) {
    return restored;
  }

  for (const [key, defaultValue] of Object.entries(defaults)) {
    if (key === "locations") continue;
    const value = savedDraft.form[key];
    if (isDraftValue(key, value, defaultValue)) restored[key] = value;
  }

  const count = Number(restored.drafts_count);
  if (savedDraft.version === LEGACY_DRAFT_STORAGE_VERSION) {
    const city = typeof savedDraft.form.city === "string"
      ? savedDraft.form.city.trim()
      : "";
    const address = typeof savedDraft.form.address === "string"
      ? savedDraft.form.address.trim()
      : "";
    restored.locations = resizeLocations([{ city, address }], count);
  } else {
    const savedLocations = Array.isArray(savedDraft.form.locations)
      ? savedDraft.form.locations.map((location) => (
        isRecord(location)
        && typeof location.city === "string"
        && typeof location.address === "string"
          ? { city: location.city, address: location.address }
          : { city: "", address: "" }
      ))
      : defaults.locations;
    restored.locations = resizeLocations(savedLocations, count);
  }

  return restored;
}

export function createDraftStorage(storage) {
  return {
    load(defaults) {
      const raw = storage.getItem(DRAFT_STORAGE_KEY);
      if (!raw) return { ...defaults };
      try {
        return restoreDraftForm(defaults, JSON.parse(raw));
      } catch {
        return { ...defaults };
      }
    },
    save(form) {
      storage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({
        version: DRAFT_STORAGE_VERSION,
        form,
      }));
    },
    clear() {
      storage.removeItem(DRAFT_STORAGE_KEY);
    },
  };
}

export function createPersistenceController({ draftStorage, photoStorage }) {
  let queue = Promise.resolve();
  let acceptsSaves = true;

  function enqueue(operation) {
    const result = queue.catch(() => undefined).then(operation);
    queue = result;
    return result;
  }

  return {
    saveForm(form) {
      if (!acceptsSaves) return Promise.resolve(false);
      return enqueue(() => draftStorage.save(form)).then(() => true);
    },
    savePhotos(files) {
      if (!acceptsSaves) return Promise.resolve(false);
      return enqueue(() => photoStorage.save(files)).then(() => true);
    },
    clear() {
      acceptsSaves = false;
      return enqueue(async () => {
        const results = await Promise.allSettled([
          Promise.resolve().then(() => draftStorage.clear()),
          Promise.resolve().then(() => photoStorage.clear()),
        ]);
        const failure = results.find((result) => result.status === "rejected");
        if (failure) throw failure.reason;
      }).then(() => {
        acceptsSaves = true;
      }).catch((error) => {
        acceptsSaves = true;
        throw error;
      });
    },
  };
}

function requestAsPromise(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Не удалось открыть хранилище фото"));
  });
}

function transactionAsPromise(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error || new Error("Не удалось сохранить фото"));
    transaction.onabort = () => reject(transaction.error || new Error("Операция с фото отменена"));
  });
}

async function openPhotoDatabase(indexedDb) {
  if (!indexedDb) throw new Error("Браузер не поддерживает локальное сохранение фото");
  const request = indexedDb.open(DRAFT_PHOTOS_DB, 1);
  request.onupgradeneeded = () => {
    if (!request.result.objectStoreNames.contains(DRAFT_PHOTOS_STORE)) {
      request.result.createObjectStore(DRAFT_PHOTOS_STORE, { keyPath: "id" });
    }
  };
  return requestAsPromise(request);
}

export function createPhotoStorage(indexedDb) {
  return {
    async load() {
      const db = await openPhotoDatabase(indexedDb);
      try {
        const transaction = db.transaction(DRAFT_PHOTOS_STORE, "readonly");
        const rows = await requestAsPromise(transaction.objectStore(DRAFT_PHOTOS_STORE).getAll());
        return rows
          .filter((row) => isRecord(row) && row.blob instanceof Blob && typeof row.name === "string")
          .sort((left, right) => left.order - right.order)
          .slice(0, MAX_SAVED_PHOTOS)
          .map((row) => new File([row.blob], row.name, {
            type: typeof row.type === "string" ? row.type : row.blob.type,
            lastModified: Number.isFinite(row.lastModified) ? row.lastModified : Date.now(),
          }));
      } finally {
        db.close();
      }
    },
    async save(files) {
      const db = await openPhotoDatabase(indexedDb);
      try {
        const transaction = db.transaction(DRAFT_PHOTOS_STORE, "readwrite");
        const store = transaction.objectStore(DRAFT_PHOTOS_STORE);
        store.clear();
        files.slice(0, MAX_SAVED_PHOTOS).forEach((file, order) => {
          store.put({
            id: `photo-${order}`,
            blob: file,
            name: file.name,
            type: file.type,
            lastModified: file.lastModified,
            order,
          });
        });
        await transactionAsPromise(transaction);
      } finally {
        db.close();
      }
    },
    async clear() {
      const db = await openPhotoDatabase(indexedDb);
      try {
        const transaction = db.transaction(DRAFT_PHOTOS_STORE, "readwrite");
        transaction.objectStore(DRAFT_PHOTOS_STORE).clear();
        await transactionAsPromise(transaction);
      } finally {
        db.close();
      }
    },
  };
}
