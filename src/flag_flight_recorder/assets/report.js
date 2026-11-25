"use strict";

const evidence = JSON.parse(document.getElementById("evidence").textContent);
const viewIds = ["overview", "regression", "capture"];
const tabs = Array.from(document.querySelectorAll("[data-view]"));
const view = document.getElementById("view");
let selected = evidence.cases[0];
let activeView = "overview";

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== null && text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

function text(id, value) {
  document.getElementById(id).textContent = value;
}

function shortId(value) {
  return String(value || "").slice(0, 12);
}

function dataText(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function dateText(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return date.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

function outcomeText(outcome) {
  if (outcome.status === "error") return outcome.type + ": " + outcome.message;
  return dataText(outcome.result);
}

function flagText(read) {
  if (!read) return "No read";
  const parts = [];
  if (Object.hasOwn(read, "enabled")) parts.push(read.enabled ? "Enabled" : "Disabled");
  if (Object.hasOwn(read, "value")) parts.push(dataText(read.value));
  return parts.join(" · ");
}

function attributes(rows, className) {
  const list = element("dl", null, className);
  for (const [label, value] of rows) {
    const group = element("div");
    group.append(element("dt", label), element("dd", value));
    list.append(group);
  }
  return list;
}

function table(labels, rows, extraClass) {
  const grid = element("table", null, "tabular" + (extraClass ? " " + extraClass : ""));
  const head = element("thead");
  const top = element("tr");
  for (const label of labels) {
    const cell = element("th", label);
    cell.scope = "col";
    top.append(cell);
  }
  head.append(top);
  const body = element("tbody");
  for (const row of rows) {
    const line = element("tr");
    row.forEach((item, column) => {
      const cell = element(column === 0 ? "th" : "td");
      if (column === 0) cell.scope = "row";
      if (item instanceof Node) cell.append(item);
      else cell.textContent = item;
      line.append(cell);
    });
    body.append(line);
  }
  grid.append(head, body);
  return grid;
}

function heading(title, details) {
  const row = element("div", null, "panel-heading");
  row.append(element("h2", title));
  if (details) row.append(element("p", details, "panel-subtitle"));
  return row;
}

function runCard(title, action, stage) {
  const card = element("article", null, "run");
  const error = stage.outcome.status === "error";
  card.append(
    element("h3", title),
    element("p", action || (error ? "Application error" : "Succeeded"), error ? "state error" : "state"),
    element("code", shortId(stage.snapshot_sha256)),
    element("pre", outcomeText(stage.outcome))
  );
  return card;
}

function overview() {
  const result = selected.comparison;
  const network = result.isolation;
  const comparison = heading("Execution comparison", "Same operation, identity and input");
  const runs = element("div", null, "runs");
  runs.append(
    runCard("Original capture", null, result.recorded || result.historical),
    runCard(selected.transition && selected.transition.method ? "After live edit" : "Current snapshot", null, result.current),
    runCard("Offline replay", result.historical.outcome.status === "error" ? "Original error reproduced" : "Original result reproduced", result.historical)
  );
  const isolation = element("p", null, "isolation");
  isolation.append(
    document.createTextNode("Replay ran in an isolated network namespace with loopback only."),
  );
  if (network.connection_error) {
    isolation.append(document.createTextNode(" Live listener probe returned "), element("code", network.connection_error), ".");
  }
  const flags = element("section", null, "section");
  const edit = selected.transition;
  const editDetail = edit && edit.method ? edit.segment + " · Flagsmith " + edit.method + " · HTTP " + edit.http_status : "";
  flags.append(heading("Evaluated flag changes", editDetail));
  if (result.flag_changes.length) {
    flags.append(table(["Flag", "Historical", "Current"], result.flag_changes.map((change) => [
      element("code", change.name),
      flagText(change.before),
      flagText(change.after)
    ])));
  } else {
    flags.append(element("p", "Both snapshots produced the same observed flag values.", "empty"));
  }
  view.append(comparison, runs, isolation, flags);
}

function regression() {
  const results = selected.regression;
  view.append(heading("Exported regression test", "Historical snapshot and input"));
  if (!results) {
    view.append(element("p", "Attach the generated test results to inspect fix verification.", "empty"));
    return;
  }
  const pair = element("div", null, "testing");
  for (const [label, result] of [["Original application", results.before], ["Fixed application", results.after]]) {
    const run = element("article");
    const verdict = result.passed ? "Passed" : "Failed";
    const title = Number.isInteger(result.pytest_exit) ? "pytest · " + verdict : verdict;
    run.append(
      element("h3", label),
      element("p", title, result.passed ? "test-result" : "test-result error"),
      element("p", "App version " + result.app_version, "panel-subtitle"),
      element("pre", outcomeText(result.outcome))
    );
    pair.append(run);
  }
  view.append(pair);
  const expected = element("section", null, "section");
  expected.append(heading("Expected result"), element("pre", dataText(results.after.expected)));
  if (selected.fix) expected.append(element("p", selected.fix, "description"));
  const invariants = attributes([
    ["Snapshot", results.before.snapshot_sha256],
    ["Input", results.before.event_sha256],
    ["Flag conditions", results.before.flags_match && results.after.flags_match ? "Matched in both runs" : "Review the flag reads"]
  ], "detail-list");
  view.append(expected, invariants);
}

function captureDetails() {
  const result = selected.comparison;
  const values = [
    ["Capture ID", result.capture_id],
    ["Operation", result.operation],
    ["Original version", result.app_version || "Original"],
    ["Application digest", result.app_sha256],
    ["Historical snapshot", result.historical.snapshot_sha256],
    ["Current snapshot", result.current.snapshot_sha256],
    ["Input digest", result.event_sha256],
    ["Identity", result.identity],
    ["Traits", JSON.stringify(result.traits || {})],
    ["Network namespace", result.isolation.namespace || "Isolated"],
  ];
  if (selected.regression && selected.regression.test_sha256) values.push(["Exported test digest", selected.regression.test_sha256]);
  view.append(heading("Capture provenance"), attributes(values, "detail-list"));
  const checks = Array.isArray(selected.sdk_checks) ? selected.sdk_checks : [];
  if (checks.length) {
    const section = element("section", null, "section");
    section.append(heading("SDK verification"));
    section.append(table(["Control", "Identity", "Result"], checks.map((check) => [
      check.label,
      element("code", check.identity),
      check.parity + (check.variant ? " · " + check.variant : ""),
    ]), "checks"));
    view.append(section);
  }
}

const panels = { overview, regression, capture: captureDetails };

function draw() {
  for (const item of document.querySelectorAll(".case-button")) {
    item.setAttribute("aria-pressed", String(item.dataset.case === selected.id));
  }
  text("capture-label", "Capture " + shortId(selected.comparison.capture_id));
  text("case-state", "Offline replay verified");
  text("title", selected.title);
  text("description", selected.description);
  const result = selected.comparison;
  const groups = attributes([
    ["Identity", result.identity],
    ["Original snapshot", shortId(result.historical.snapshot_sha256)],
    ["Captured", selected.occurred_at ? dateText(selected.occurred_at) : result.operation]
  ], "context");
  document.getElementById("context").replaceChildren(...groups.children);
  for (const tab of tabs) {
    const active = tab.dataset.view === activeView;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  view.replaceChildren();
  view.setAttribute("aria-labelledby", "tab-" + activeView);
  panels[activeView]();
  document.title = selected.title + " · Flag Flight Recorder";
}

function choose(incident, panel, updateHash) {
  selected = incident;
  activeView = panel;
  if (updateHash) {
    const hash = "#" + encodeURIComponent(selected.id) + "/" + activeView;
    if (location.hash !== hash) history.replaceState(null, "", hash);
  }
  draw();
}

function loadHash() {
  try {
    const [id, viewName] = location.hash.slice(1).split("/");
    const incident = evidence.cases.find((item) => item.id === decodeURIComponent(id));
    choose(incident || evidence.cases[0], viewIds.includes(viewName) ? viewName : "overview", false);
  } catch (error) {
    if (!(error instanceof URIError)) throw error;
    choose(evidence.cases[0], "overview", false);
  }
}

for (const incident of evidence.cases) {
  const control = element("button", incident.title, "case-button");
  control.type = "button";
  control.dataset.case = incident.id;
  control.addEventListener("click", () => choose(incident, activeView, true));
  document.getElementById("cases").append(control);
}

for (const tab of tabs) {
  tab.addEventListener("click", () => choose(selected, tab.dataset.view, true));
  tab.addEventListener("keydown", (event) => {
    const index = viewIds.indexOf(activeView);
    const step = { ArrowLeft: -1, ArrowRight: 1 };
    let target;
    if (event.key === "Home") target = 0;
    else if (event.key === "End") target = tabs.length - 1;
    else if (Object.hasOwn(step, event.key)) target = (index + step[event.key] + tabs.length) % tabs.length;
    else return;
    event.preventDefault();
    choose(selected, tabs[target].dataset.view, true);
    tabs[target].focus();
  });
}

document.getElementById("download").addEventListener("click", () => {
  const bytes = new Blob([JSON.stringify(evidence, null, 2) + "\n"], { type: "application/json" });
  const url = URL.createObjectURL(bytes);
  const link = document.createElement("a");
  link.href = url;
  link.download = "flight-recorder-evidence.json";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

text("engine", evidence.flagsmith_release ? "Flagsmith " + evidence.flagsmith_release : "Flagsmith evidence");
if (evidence.generated_at) {
  const time = element("time", dateText(evidence.generated_at));
  time.dateTime = evidence.generated_at;
  document.getElementById("run-meta").append("Report generated", time);
}
window.addEventListener("hashchange", loadHash);
loadHash();
