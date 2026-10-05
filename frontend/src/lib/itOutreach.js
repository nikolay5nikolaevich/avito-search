function hasEmail(company) {
  return Boolean(company?.email || company?.emails?.length);
}

export function filterItOutreachCompanies(companies, filter) {
  if (filter === "with_email") return companies.filter(hasEmail);
  if (filter === "without_email") return companies.filter((company) => !hasEmail(company));
  return companies;
}

export function isItOutreachTerminalStatus(status) {
  return ["done", "blocked", "error", "not_found"].includes(status);
}

export function itOutreachPollDelay(afterError = false) {
  return afterError ? 3000 : 1500;
}
