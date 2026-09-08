import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";


const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, "..");
const pageSource = fs.readFileSync(
  path.join(projectRoot, "src", "pages", "DraftPage.jsx"),
  "utf8",
);

for (const step of [
  "continue_listing",
  "fill_view_price",
  "continue_view_price",
  "skip_services",
]) {
  assert.match(pageSource, new RegExp(`key: "${step}"`));
}

assert.match(pageSource, /normalizePublishProgress/);
// Кнопка возобновления обязана зависеть только от resume_plan/canResume, а не
// от того, что она нарисована внутри блока конкретного статуса (needs_user_action)
assert.match(pageSource, /describeResumePlan/);
assert.match(pageSource, /resumeUnavailableMessage/);
assert.match(pageSource, /canResume\s*&&\s*resumePlan/);
assert.doesNotMatch(pageSource, /verify_published/);
assert.doesNotMatch(pageSource, /Проверка публикации/);
assert.match(pageSource, /Отправлено \{itemsPublished\} из \{itemsTotal\}/);
assert.match(pageSource, /Наличие объявления во вкладке «Активные» не проверялось/);
assert.match(pageSource, /publishedUrls\.map/);
assert.match(pageSource, /minimum_view_price/);
assert.match(pageSource, /\{`Вариант \$\{draft\.index\}`\}/);
assert.doesNotMatch(pageSource, /isOriginal/);
assert.doesNotMatch(pageSource, /Вариант 1 — оригинал/);

for (const obsoleteText of [
  "Сохранить черновик на Авито",
  "Все черновики сохранены",
  "Мои объявления → Черновики",
]) {
  assert.doesNotMatch(pageSource, new RegExp(obsoleteText));
}

console.log("publish UI source check passed");
