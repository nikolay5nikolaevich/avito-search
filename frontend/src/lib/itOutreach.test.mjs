import assert from "node:assert/strict";
import test from "node:test";
import { filterItOutreachCompanies, isItOutreachTerminalStatus, itOutreachPollDelay } from "./itOutreach.js";

const companies = [
  { company: "Почта", emails: ["hello@example.test"] },
  { company: "Без почты", emails: [] },
  { company: "Строка почты", email: "team@example.test" },
];

test("it outreach filter separates companies by published email", () => {
  assert.deepEqual(filterItOutreachCompanies(companies, "all"), companies);
  assert.deepEqual(filterItOutreachCompanies(companies, "with_email"), [companies[0], companies[2]]);
  assert.deepEqual(filterItOutreachCompanies(companies, "without_email"), [companies[1]]);
});

test("it outreach treats completed, blocked, failed and missing jobs as terminal", () => {
  for (const status of ["done", "blocked", "error", "not_found"]) {
    assert.equal(isItOutreachTerminalStatus(status), true);
  }
  assert.equal(isItOutreachTerminalStatus("running"), false);
});

test("it outreach retries a transient status error more slowly", () => {
  assert.equal(itOutreachPollDelay(), 1500);
  assert.equal(itOutreachPollDelay(true), 3000);
});
