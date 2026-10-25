export const SIM_TYPES = ['eSIM', 'SIM + eSIM', 'SIM + SIM'];

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
  return errors;
}
