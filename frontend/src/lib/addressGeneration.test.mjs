import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./api.js", import.meta.url), "utf8");
const pageSource = readFileSync(new URL("../pages/DraftPage.jsx", import.meta.url), "utf8");
assert.match(source, /export async function generateAddresses\(cities, usedLocations = \[\]\)/);
assert.match(source, /fetch\("\/api\/addresses\/generate", \{/);
assert.match(source, /used_locations: usedLocations/);
assert.match(pageSource, /targets\.forEach\(\(index\) => \{ delete next\[`locations\.\$\{index \+ 1\}\.address`\]; \}\)/);
assert.doesNotMatch(pageSource, /if \(result\?\.address\) \{\s*clearError/);
assert.match(pageSource, /catch \(err\) \{\s*setAddressGeneration\(\{ loading: \[\], error: missingErrors,/);
console.log("address generation API helper source: OK");
