import test from 'node:test';
import assert from 'node:assert/strict';
import { applyIphoneCatalog, iphoneCatalogKey, validateIphoneLibrary } from './iphoneLibrary.js';
import { buildPublishFormData } from './publish.js';

const profile = { model_memory: { 'iPhone 17': ['256 GB'] }, model_colors: { 'iPhone 17': ['Black'] } };
const form = { category: 'iphone', title: 'iPhone 17', model: 'iPhone 17', memory: '256 GB', color: 'Black', sim_type: 'eSIM', pack_id: 'one' };
const catalog = { model: 'iPhone 17', packs: [{ id: 'one', available: true }], descriptions_count: 1 };

test('late catalog cannot overwrite a newer memory, title or category', () => {
  for (const change of [{ memory: '512 GB' }, { title: 'iPhone 17 Pro' }, { category: 'jackets' }]) {
    const current = { ...form, ...change };
    assert.equal(applyIphoneCatalog(current, iphoneCatalogKey(form), catalog, profile), current);
  }
});
test('catalog refresh preserves a missing chosen pack and requires explicit reselection', () => {
  assert.equal(applyIphoneCatalog(form, iphoneCatalogKey(form), catalog, profile).memory, '256 GB');
  const result = applyIphoneCatalog(form, iphoneCatalogKey(form), { ...catalog, packs: [] }, profile);
  assert.equal(result.pack_id, 'one');
  assert.equal(result.color, 'Black');
  assert.ok(validateIphoneLibrary(result, { ...catalog, packs: [{ id: 'other', available: true }] }, true).pack_id);
});
test('a changed model clears incompatible memory, color and pack even with same pack name', () => {
  const current = { ...form, model: 'iPhone 16', memory: '128 GB', color: 'Blue' };
  const result = applyIphoneCatalog(current, iphoneCatalogKey(current), catalog, profile);
  assert.equal(result.memory, '');
  assert.equal(result.color, '');
  assert.equal(result.pack_id, '');
});
test('SIM and current available materials are mandatory', () => {
  assert.deepEqual(validateIphoneLibrary(form, catalog, true), {});
  assert.ok(validateIphoneLibrary({ ...form, sim_type: '' }, catalog, true).sim_type);
  assert.ok(validateIphoneLibrary(form, catalog, false).pack_id);
  assert.ok(validateIphoneLibrary(form, { ...catalog, descriptions_count: 0 }, true).pack_id);
  assert.ok(validateIphoneLibrary(form, { ...catalog, packs: [{ id: 'one', available: false }] }, true).pack_id);
});
test('iPhone serialization carries SIM/pack and excludes manual materials', () => {
  const data = buildPublishFormData({ ...form, description: 'old clothing text', drafts_count: 1 }, [new Blob(['old'])]);
  assert.equal(data.get('sim_type'), 'eSIM');
  assert.equal(data.get('pack_id'), 'one');
  assert.equal(data.get('condition'), 'Новое');
  assert.equal(data.has('photos'), false);
  assert.equal(data.has('description'), false);
});
