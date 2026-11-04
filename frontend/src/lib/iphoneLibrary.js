export const SIM_TYPES = ['eSIM', 'SIM + eSIM', 'SIM + SIM'];

export function moveLibraryPhoto(order, from, to) {
  const result = [...order];
  if (!Number.isInteger(from) || !Number.isInteger(to)
    || from < 0 || to < 0 || from >= result.length || to >= result.length) return result;
  const [photo] = result.splice(from, 1);
  result.splice(to, 0, photo);
  return result;
}

export function libraryPhotoUrl(form, photoId, revision = 0) {
  const params = new URLSearchParams({
    title: form.title, memory: form.memory,
    pack_id: form.pack_id || form.library_preview_pack_id, photo_id: photoId,
    revision: String(revision),
  });
  return `/api/publish/iphone-library/photo?${params}`;
}

export function syncLibraryPreview(form, catalog) {
  const available = catalog?.packs?.filter((pack) => pack.available) ?? [];
  const previewKey = JSON.stringify([catalog?.model, form.memory]);
  const changedSource = 'library_preview_key' in form && form.library_preview_key !== previewKey;
  const selected = form.pack_id || (changedSource ? '' : form.library_preview_pack_id);
  let pack = available.find((item) => item.id === selected);
  if (!pack && !selected && available.length) pack = available[Math.floor(Math.random() * available.length)];
  const names = pack?.photos?.map((photo) => photo.id) ?? [];
  const previous = Array.isArray(form.photo_order) ? form.photo_order : [];
  const sameOrder = previous.length === names.length && new Set(previous).size === names.length
    && previous.every((name) => names.includes(name));
  return {
    ...form,
    library_preview_pack_id: pack?.id ?? selected ?? '',
    ...('library_preview_key' in form ? { library_preview_key: previewKey } : {}),
    photo_order: sameOrder ? previous : names,
  };
}

export function iphoneCatalogKey(form) {
  return JSON.stringify([form.category, form.title, form.memory]);
}

export function applyIphoneCatalog(form, requestKey, catalog, profile) {
  if (iphoneCatalogKey(form) !== requestKey) return form;
  const model = catalog.model;
  const changedModel = form.model !== model;
  const memories = profile?.model_memory?.[model] ?? profile?.memories ?? [];
  const colors = profile?.model_colors?.[model] ?? profile?.colors ?? [];
  return {
    ...form,
    model,
    condition: 'Новое',
    memory: changedModel && !memories.includes(form.memory) ? '' : form.memory,
    color: changedModel && !colors.includes(form.color) ? '' : form.color,
    pack_id: changedModel ? '' : form.pack_id,
    ...(changedModel ? { library_preview_pack_id: '', photo_order: [] } : {}),
  };
}

export function validateIphoneLibrary(form, catalog, current) {
  const errors = {};
  if (!SIM_TYPES.includes(form.sim_type)) errors.sim_type = 'Выберите SIM';
  if (!current) errors.pack_id = 'Дождитесь проверки библиотеки для текущих параметров';
  else if (catalog?.error) errors.pack_id = catalog.error;
  else if (!catalog?.descriptions_count) errors.pack_id = 'В библиотеке нет описаний этой модели';
  else if (!catalog?.packs?.some((pack) => pack.available && (!form.pack_id || pack.id === form.pack_id))) {
    errors.pack_id = 'Нет доступного фотопака для выбранных модели и памяти';
  }
  if (current && !errors.pack_id && 'library_preview_pack_id' in form) {
    const pack = catalog?.packs?.find((item) => item.available && item.id === (form.pack_id || form.library_preview_pack_id));
    const order = form.photo_order;
    const names = pack?.photos?.map((photo) => photo.id) ?? [];
    if (!pack || !names.length || !Array.isArray(order) || order.length !== names.length
      || new Set(order).size !== names.length || order.some((name) => !names.includes(name))) {
      errors.pack_id = 'Проверьте выбранный пак и порядок фотографий';
    }
  }
  return errors;
}
