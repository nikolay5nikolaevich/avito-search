import { extractPublishFieldErrors } from "./publish";

async function readJson(response) {
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};

  if (!response.ok) {
    const message = data.error || `HTTP ${response.status}`;
    throw new Error(message);
  }

  return data;
}

export async function fetchBootstrap() {
  const response = await fetch("/api/bootstrap");
  return readJson(response);
}

export async function startSearch(payload) {
  const response = await fetch("/api/search", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return readJson(response);
}

export async function fetchStatus(jobId) {
  const response = await fetch(`/api/status/${jobId}`);
  return readJson(response);
}

export async function fetchResults(jobId) {
  const response = await fetch(`/api/results/${jobId}`);
  return readJson(response);
}

// ─── Publish API ──────────────────────────────────────────────────────────────

export async function startPublish(formData) {
  // Отправляем multipart/form-data — браузер сам выставит Content-Type с boundary
  const response = await fetch("/api/publish/start", {
    method: "POST",
    body: formData,
  });

  if (response.status === 422) {
    const data = await response.json().catch(() => ({}));
    const fieldErrors = extractPublishFieldErrors(data);
    const error = new Error("Ошибка валидации");
    error.fieldErrors = fieldErrors;
    throw error;
  }

  return readJson(response);
}

export async function fetchPublishStatus(jobId) {
  const response = await fetch(`/api/publish/status/${jobId}`);
  return readJson(response);
}

export async function fetchPublishResult(jobId) {
  const response = await fetch(`/api/publish/result/${jobId}`);
  return readJson(response);
}

// ─── Prepare API (фаза превью вариантов) ─────────────────────────────────────

export async function startPrepare(formData) {
  // Отправляем multipart/form-data — браузер сам выставит Content-Type с boundary
  const response = await fetch("/api/publish/prepare", {
    method: "POST",
    body: formData,
  });
  return readJson(response);
}

export async function getPrepareStatus(prepId) {
  const response = await fetch(`/api/publish/prepare/status/${prepId}`);
  return readJson(response);
}

export async function getPrepareResult(prepId) {
  const response = await fetch(`/api/publish/prepare/result/${prepId}`);
  return readJson(response);
}

export async function regenerateDraft(prepId, draftIndex) {
  const response = await fetch("/api/publish/prepare/regenerate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prep_id: prepId, draft_index: draftIndex }),
  });
  return readJson(response);
}

// Строит URL к миниатюре фото черновика (не fetch — просто строка для <img src>)
export function prepPhotoUrl(prepId, draftIndex, photoIndex) {
  return `/api/publish/prepare/photo/${prepId}/${draftIndex}/${photoIndex}`;
}
