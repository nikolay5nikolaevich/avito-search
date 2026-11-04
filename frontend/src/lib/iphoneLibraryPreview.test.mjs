import test from 'node:test';
import assert from 'node:assert/strict';
import * as library from './iphoneLibrary.js';
import { buildPublishFormData } from './publish.js';
import { restoreDraftForm } from './draftPersistence.js';

test('moving a library photo changes its position without mutating the input', () => {
  const order = Object.freeze(['03.heic', '01.png', '02.jpg']);
  assert.equal(typeof library.moveLibraryPhoto, 'function');
  assert.deepEqual(library.moveLibraryPhoto(order, 0, 2), ['01.png', '02.jpg', '03.heic']);
  assert.deepEqual(library.moveLibraryPhoto(order, 2, 0), ['02.jpg', '03.heic', '01.png']);
  assert.deepEqual(order, ['03.heic', '01.png', '02.jpg']);
  assert.notEqual(library.moveLibraryPhoto(order, 0, 2), order);
});

test('invalid photo indices preserve the original order', () => {
  assert.equal(typeof library.moveLibraryPhoto, 'function');
  const order = Object.freeze(['first.jpg', 'second.jpg']);
  for (const [from, to] of [[-1, 0], [0, -1], [2, 0], [0, 2], [0.5, 1], [0, NaN], ['0', 1], [0, 0]]) {
    assert.deepEqual(library.moveLibraryPhoto(order, from, to), order);
  }
  assert.deepEqual(library.moveLibraryPhoto([], 0, 1), []);
});

test('prepare form serializes the selected library photo order as JSON', () => {
  const photo_order = ['03.heic', '01.png', '02.jpg'];
  const data = buildPublishFormData({
    category: 'iphone', title: 'iPhone 17 Pro Max', model: 'iPhone 17 Pro Max',
    memory: '512 ГБ', color: 'Синий', sim_type: 'eSIM', pack_id: 'Наш пак',
    drafts_count: 2, photo_order,
  }, []);
  assert.deepEqual(JSON.parse(data.get('photo_order')), photo_order);
});

const makePack = (id, names, available = true) => ({
  id, available, photos: names.map((name) => ({ id: name })),
});
const previewForm = {
  category: 'iphone', title: 'iPhone 17 Pro Max', model: 'iPhone 17 Pro Max',
  memory: '512 ГБ', color: 'Синий', sim_type: 'eSIM', drafts_count: 2,
  pack_id: 'first', library_preview_pack_id: 'first',
  photo_order: ['03.heic', '01.png', '02.jpg'],
};

test('refresh with the same photo names preserves the user order without mutating the form', () => {
  const form = structuredClone(previewForm);
  const before = structuredClone(form);
  const catalog = { packs: [makePack('first', ['01.png', '02.jpg', '03.heic'])] };
  const result = library.syncLibraryPreview(form, catalog);
  assert.deepEqual(result.photo_order, before.photo_order);
  assert.equal(result.library_preview_pack_id, 'first');
  assert.deepEqual(form, before);
});

test('a changed photo set replaces stale order with the new catalog order', () => {
  const form = structuredClone(previewForm);
  const result = library.syncLibraryPreview(form, {
    packs: [makePack('first', ['01.png', 'new.jpg', '03.heic'])],
  });
  assert.deepEqual(result.photo_order, ['01.png', 'new.jpg', '03.heic']);
  assert.equal(result.library_preview_pack_id, 'first');
  assert.deepEqual(form.photo_order, previewForm.photo_order);
});

test('a disappeared selected or random preview pack is not replaced by another pack', () => {
  for (const pack_id of ['first', '']) {
    const form = { ...previewForm, pack_id };
    const catalog = { model: form.model, descriptions_count: 1,
      packs: [makePack('another', ['a.jpg', 'b.jpg'])] };
    const result = library.syncLibraryPreview(form, catalog);
    assert.equal(result.pack_id, pack_id);
    assert.equal(result.library_preview_pack_id, 'first');
    assert.deepEqual(result.photo_order, []);
    assert.ok(library.validateIphoneLibrary(result, catalog, true).pack_id);
  }
});

test('random preview chooses an available pack and prepare serializes exactly that pack and order', () => {
  const names = ['a.jpg', 'b.heic'];
  const catalog = { packs: [makePack('broken', ['bad.jpg'], false), makePack('random-chosen', names)] };
  const initial = { ...previewForm, pack_id: '', library_preview_pack_id: '', photo_order: [] };
  const selected = library.syncLibraryPreview(initial, catalog);
  assert.equal(selected.pack_id, '');
  assert.equal(selected.library_preview_pack_id, 'random-chosen');
  assert.deepEqual(selected.photo_order, names);
  const reordered = { ...selected, photo_order: library.moveLibraryPhoto(selected.photo_order, 0, 1) };
  const data = buildPublishFormData(reordered, []);
  assert.equal(data.get('pack_id'), selected.library_preview_pack_id);
  assert.deepEqual(JSON.parse(data.get('photo_order')), ['b.heic', 'a.jpg']);
  const refreshed = library.syncLibraryPreview(reordered, {
    packs: [makePack('new-option', ['c.jpg']), ...catalog.packs],
  });
  assert.equal(refreshed.library_preview_pack_id, 'random-chosen');
  assert.deepEqual(refreshed.photo_order, ['b.heic', 'a.jpg']);
});

test('restoring a saved draft accepts only a unique list of photo names', () => {
  const defaults = { photo_order: [], drafts_count: 1, locations: [{ city: '', address: '' }] };
  for (const photo_order of [null, {}, '01.png', 3, [3], [null], [{}], [''], ['01.png', '01.png']]) {
    const restored = restoreDraftForm(defaults, { version: 2, form: { photo_order } });
    assert.deepEqual(restored.photo_order, [], `invalid order: ${JSON.stringify(photo_order)}`);
  }
  const order = ['03.heic', '01.png', '02.jpg'];
  const restored = restoreDraftForm(defaults, { version: 2, form: { photo_order: order } });
  assert.deepEqual(restored.photo_order, order);
  assert.deepEqual(defaults.photo_order, []);
});

test('changing model with the same memory clears the previous pack and photo order', () => {
  const form = {
    ...structuredClone(previewForm), title: 'iPhone 17 Pro',
  };
  const before = structuredClone(form);
  const catalog = {
    model: 'iPhone 17 Pro', descriptions_count: 1,
    packs: [makePack('first', ['01.png', '02.jpg', '03.heic'])],
  };
  const profile = {
    model_memory: { 'iPhone 17 Pro': ['512 ГБ'] },
    model_colors: { 'iPhone 17 Pro': ['Синий'] },
  };

  const applied = library.applyIphoneCatalog(
    form, library.iphoneCatalogKey(form), catalog, profile,
  );

  assert.equal(applied.model, 'iPhone 17 Pro');
  assert.equal(applied.memory, '512 ГБ');
  assert.equal(applied.pack_id, '');
  assert.equal(applied.library_preview_pack_id, '');
  assert.deepEqual(applied.photo_order, []);
  assert.deepEqual(form, before);
  const preview = library.syncLibraryPreview(applied, catalog);
  assert.deepEqual(preview.photo_order, ['01.png', '02.jpg', '03.heic']);
});

test('catalog error remains exact when the previous preview pack and photo order are invalid', () => {
  const form = {
    ...structuredClone(previewForm),
    library_preview_pack_id: 'disappeared', photo_order: ['missing.jpg'],
  };
  const catalog = {
    model: form.model, descriptions_count: 1, packs: [],
    error: 'В паке «Основной» должно быть от 1 до 10 фото; найдено 11',
  };

  const errors = library.validateIphoneLibrary(form, catalog, true);

  assert.equal(errors.pack_id, catalog.error);
});

test('an untracked saved random preview migrates to the current model direct pack', () => {
  const form = {
    ...structuredClone(previewForm), title: 'iPhone 17', model: 'iPhone 17',
    memory: '256 ГБ', pack_id: '', library_preview_key: '',
    library_preview_pack_id: 'old-model-pack', photo_order: ['old-model.jpg'],
  };
  const before = structuredClone(form);
  const catalog = {
    model: 'iPhone 17', descriptions_count: 1,
    packs: [makePack('__memory__', ['new-first.jpg', 'new-second.jpg'])],
  };

  const result = library.syncLibraryPreview(form, catalog);

  assert.equal(result.pack_id, '');
  assert.equal(result.library_preview_pack_id, '__memory__');
  assert.equal(result.library_preview_key, JSON.stringify(['iPhone 17', '256 ГБ']));
  assert.deepEqual(result.photo_order, ['new-first.jpg', 'new-second.jpg']);
  assert.deepEqual(form, before);
});

test('a disappeared random preview from the same source remains blocked', () => {
  const key = JSON.stringify(['iPhone 17', '256 ГБ']);
  const form = {
    ...structuredClone(previewForm), title: 'iPhone 17', model: 'iPhone 17',
    memory: '256 ГБ', pack_id: '', library_preview_key: key,
    library_preview_pack_id: 'disappeared', photo_order: ['old-photo.jpg'],
  };
  const catalog = {
    model: 'iPhone 17', descriptions_count: 1,
    packs: [makePack('__memory__', ['new-photo.jpg'])],
  };

  const result = library.syncLibraryPreview(form, catalog);

  assert.equal(result.pack_id, '');
  assert.equal(result.library_preview_pack_id, 'disappeared');
  assert.equal(result.library_preview_key, key);
  assert.deepEqual(result.photo_order, []);
  assert.ok(library.validateIphoneLibrary(result, catalog, true).pack_id);
});
