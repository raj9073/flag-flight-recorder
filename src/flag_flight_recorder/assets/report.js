"use strict";
const evidence = JSON.parse(document.getElementById("evidence").textContent);
const byId = (id) => document.getElementById(id);
const element = (tag, text) => {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};
const display = (value) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);
const short = (value) => (value ? String(value).slice(0, 12) : "—");
const setting = (value) =>
  typeof value === "boolean" ? (value ? "On" : "Off") : display(value);
const observation = (read) => {
  if (!read) return "No read";
  const parts = [];
  if (Object.hasOwn(read, "enabled")) parts.push(setting(read.enabled));
  if (Object.hasOwn(read, "value")) parts.push(setting(read.value));
  return parts.join(" · ");
};
const resultText = (outcome) =>
  outcome.status === "error"
    ? outcome.type + ": " + outcome.message
    : display(outcome.result);
let incident, stages, selected;
function fields(list, data) {
  list.replaceChildren(
    ...data
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => {
        const row = element("div");
        row.append(element("dt", k), element("dd", v));
        return row;
      }),
  );
}
function download(name, blob) {
  const url = URL.createObjectURL(blob),
    a = element("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function jsonDownload(name, data) {
  download(
    name,
    new Blob([JSON.stringify(data, null, 2) + "\n"], {
      type: "application/json",
    }),
  );
}
function route(key, push) {
  const hash = "#" + encodeURIComponent(incident.id) + "/" + key;
  if (push && location.hash !== hash) history.pushState(null, "", hash);
}
function stageList() {
  const c = incident.comparison,
    live = incident.live_requests,
    test = incident.regression;
  const original = c.recorded || c.historical;
  const underEdit = live ? live.retry : c.current;
  const out = (run) =>
    run.outcome.status === "error" ? "App failed" : "Returned result";
  const check =
    c.historical.outcome.status === "error"
      ? "Failure matched"
      : "Result matched";
  const list = [
    {
      id: "original",
      title: "Capture",
      result: original,
      status: out(original),
      kind: "Recorded operation",
      explanation: incident.description,
      receipt: live?.original,
      version: c.app_version,
    },
    {
      id: "updated",
      title: live ? "Live retry" : "Updated rules",
      result: underEdit,
      status: out(underEdit),
      kind: live
        ? "Live application response"
        : "Offline rerun with updated rules",
      explanation: live
        ? "The same request reached the application after the Flagsmith edit. Its response also matched the independent offline comparison."
        : "The same code ran against the selected updated document inside the offline worker.",
      receipt: live?.retry,
      version: c.app_version,
    },
    {
      id: "replay",
      title: "Offline replay",
      result: c.historical,
      status: check,
      kind: "Network-isolated replay",
      explanation:
        "The captured code, flag reads and result matched under the original rules.",
      version: c.app_version,
    },
  ];
  if (test) {
    const verified = test.binding === "verified",
      fix =
        verified &&
        !test.before.passed &&
        test.after.passed &&
        test.before.app_sha256 !== test.after.app_sha256;
    list.push({
      id: "fix",
      title: fix ? "Code fix" : "Candidate",
      result: test.after,
      status: verified
        ? test.after.passed
          ? "Test passed"
          : "Test failed"
        : "Legacy result",
      kind: verified
        ? "Historical regression verification"
        : "Archived verification",
      explanation: verified
        ? incident.fix || test.after.detail
        : "This archive predates linked specification receipts. The two reported runs have unverified test identity.",
      version: test.after.app_version,
    });
  }
  return list;
}
function selectStage(index, push = false) {
  selected = index;
  const s = stages[index],
    c = incident.comparison,
    t = incident.regression;
  for (const b of document.querySelectorAll("[data-stage]"))
    b.setAttribute("aria-pressed", String(+b.dataset.stage === index));
  for (const cell of document.querySelectorAll("[data-col]"))
    cell.classList.toggle("selected", +cell.dataset.col === index);
  byId("stage-kind").textContent = s.kind;
  byId("output-title").textContent = s.status;
  byId("output").textContent = resultText(s.result.outcome);
  byId("output").className =
    s.result.outcome.status === "success" ? "success" : "";
  byId("explain").textContent = s.explanation;
  byId("proof-title").textContent = s.title + " evidence";
  let data = [
    ["Snapshot", short(s.result.snapshot_sha256)],
    ["Application", s.version],
  ];
  if (s.receipt)
    data.push(
      [
        "HTTP receipt",
        s.receipt.method + " " + s.receipt.path + " · " + s.receipt.http_status,
      ],
      ["Capture ID", s.receipt.capture_id],
      ["Received", s.receipt.occurred_at],
    );
  if (s.id === "updated" && incident.transition)
    data.push([
      "Flagsmith edit",
      incident.transition.segment +
        " · " +
        incident.transition.method +
        " " +
        incident.transition.http_status,
    ]);
  if (s.id === "replay")
    data.push(
      ["Network", c.isolation.interfaces.join(", ") + " · loopback only"],
      ["Live server probe", c.isolation.connection_error],
      ["Flag reads", "Matched capture"],
    );
  if (s.id === "fix")
    data.push(
      ["Original code", testVerdict(t.before)],
      ["Candidate code", testVerdict(t.after)],
      [
        "Test identity",
        t.binding === "verified"
          ? short(t.after.spec_sha256)
          : "Unlinked archive",
      ],
    );
  fields(byId("proof"), data);
  route(s.id, push);
}
function testVerdict(run) {
  const name = Object.hasOwn(run, "pytest_exit")
    ? "pytest exit " + run.pytest_exit
    : "Verification";
  return name + " · " + (run.passed ? "Passed" : "Failed");
}
function stageButton(i, mobile) {
  const s = stages[i],
    b = element("button");
  b.type = "button";
  b.className = mobile ? "m-stage" : "stage";
  b.dataset.stage = i;
  b.setAttribute("aria-label", s.title + " · " + s.status);
  if (mobile) {
    const label = element("span", s.status);
    label.className = statusClass(s);
    b.append(element("strong", s.title), label);
  } else b.textContent = s.title;
  b.addEventListener("click", () => selectStage(i, true));
  b.addEventListener("keydown", (e) => {
    const n = stages.length,
      next = {
        ArrowRight: (i + 1) % n,
        ArrowLeft: (i + n - 1) % n,
        Home: 0,
        End: n - 1,
      };
    if (Object.hasOwn(next, e.key)) {
      e.preventDefault();
      selectStage(next[e.key], true);
      b.closest(mobile ? ".m-stages" : "table")
        .querySelector('[data-stage="' + selected + '"]')
        .focus();
    }
  });
  return b;
}
function statusClass(s) {
  if (s.id === "replay") return "good";
  if (s.id === "fix")
    return incident.regression.binding === "verified"
      ? incident.regression.after.passed
        ? "good"
        : "bad"
      : "neutral";
  return s.result.outcome.status === "error" ? "bad" : "good";
}
function buildTrace() {
  const c = incident.comparison,
    changes = c.flag_changes.map((x) => x.name),
    keys = [
      ...new Set([
        ...changes,
        ...stages.flatMap((s) => Object.keys(s.result.reads)),
      ]),
    ];
  const head = element("thead"),
    hr = element("tr"),
    label = element("th", "Inspect a run");
  label.scope = "col";
  hr.append(label);
  stages.forEach((s, i) => {
    const th = element("th");
    th.scope = "col";
    th.dataset.col = i;
    th.append(stageButton(i, false));
    hr.append(th);
  });
  head.append(hr);
  const body = element("tbody");
  const rows = keys.map((name) => ({
    name,
    values: stages.map((s) => observation(s.result.reads[name])),
  }));
  rows.push(
    {
      name: "Application code",
      values: stages.map((s) => s.version || "Original"),
    },
    { name: "Outcome", values: stages.map((s) => s.status) },
  );
  for (const row of rows) {
    const line = element("tr"),
      th = element("th", row.name);
    th.scope = "row";
    line.append(th);
    const waveRow =
      changes.includes(row.name) &&
      row.values.every((v) => v === "On" || v === "Off");
    row.values.forEach((v, i) => {
      const td = element("td", v);
      td.dataset.col = i;
      if (row.name === "Outcome")
        td.className = "status " + statusClass(stages[i]);
      if (waveRow) {
        td.className = "level";
        const wave = element("span");
        wave.className =
          "wave" +
          (v === "Off" ? " low" : "") +
          (i && row.values[i - 1] !== v ? " transition" : "");
        wave.setAttribute("aria-hidden", "true");
        const marker = element("i");
        marker.className = "sample";
        wave.append(marker);
        td.append(wave);
      }
      line.append(td);
    });
    body.append(line);
  }
  byId("matrix").replaceChildren(head, body);
  byId("m-stages").replaceChildren(
    ...stages.map((_, i) => stageButton(i, true)),
  );
  const lines = keys.map((name) => {
    const line = element("span");
    line.className = "held";
    line.append(
      element("code", name),
      document.createTextNode(
        "  " +
          observation(c.historical.reads[name]) +
          (changes.includes(name)
            ? " → " + observation(c.current.reads[name])
            : " · held across snapshots"),
      ),
    );
    return line;
  });
  byId("delta").replaceChildren(...lines);
}
function inspectCell(cell, i) {
  for (const b of byId("grid").querySelectorAll("button"))
    b.setAttribute("aria-pressed", String(+b.dataset.index === i));
  const matrix = incident.exploration;
  byId("cell-title").textContent =
    cell.settings
      .map((v, j) => matrix.axes[j].flag + " " + setting(v))
      .join(" / ") +
    (JSON.stringify(cell.settings) === JSON.stringify(matrix.baseline_settings)
      ? " · Captured settings"
      : "");
  const nodes = [];
  for (const [key, version] of [
    ["original", matrix.original_version],
    ["candidate", matrix.candidate_version],
  ]) {
    const run = cell[key],
      details = element("details");
    details.append(
      element("summary", "Observed flag reads"),
      element("pre", display(run.reads)),
    );
    nodes.push(
      element(
        "h4",
        version +
          " · " +
          (run.outcome.status === "error" ? "Raised an error" : "Returned"),
      ),
      element("pre", resultText(run.outcome)),
      details,
    );
  }
  byId("cell-output").replaceChildren(...nodes);
}
function buildGrid() {
  const m = incident.exploration;
  byId("explorer").hidden = !m;
  byId("jump").hidden = !m;
  if (!m) return;
  const [a, b] = m.axes,
    table = element("table");
  table.className = "axes";
  table.append(
    element("caption", "Rows · " + a.flag + " / Columns · " + b.flag),
  );
  const headers = element("tr");
  headers.append(element("th", ""));
  b.values.forEach((v) => {
    const th = element("th", setting(v));
    th.scope = "col";
    headers.append(th);
  });
  const thead = element("thead");
  thead.append(headers);
  table.append(thead);
  const tbody = element("tbody");
  a.values.forEach((x) => {
    const tr = element("tr"),
      th = element("th", setting(x));
    th.scope = "row";
    tr.append(th);
    b.values.forEach((y) => {
      const i = m.cells.findIndex(
          (cell) => JSON.stringify(cell.settings) === JSON.stringify([x, y]),
        ),
        cell = m.cells[i];
      const failure = cell.original.outcome.status === "error",
        td = element("td"),
        btn = element("button", failure ? "App failed" : "Returned");
      btn.type = "button";
      btn.className = failure ? "failure" : "";
      btn.dataset.index = i;
      btn.setAttribute(
        "aria-label",
        a.flag +
          " " +
          setting(x) +
          ", " +
          b.flag +
          " " +
          setting(y) +
          ", " +
          btn.textContent,
      );
      btn.onclick = () => inspectCell(cell, i);
      td.append(btn);
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(tbody);
  byId("grid").replaceChildren(
    table,
    element(
      "p",
      "Cells show the original code. Select one to compare the candidate. These runs use hypothetical effective values.",
    ),
  );
  byId("grid").lastChild.className = "lab-note";
  const index = Math.max(
    0,
    m.cells.findIndex((cell) => cell.original.outcome.status === "error"),
  );
  inspectCell(m.cells[index], index);
}
function buildDetails() {
  const c = incident.comparison,
    t = incident.regression,
    a = incident.artifacts;
  const checks = (incident.sdk_checks || []).map((x) => [
    x.label,
    x.parity + (x.variant ? " · " + x.variant : ""),
  ]);
  fields(byId("origins"), [
    ["Capture ID", c.capture_id],
    ["Captured", incident.occurred_at || c.occurred_at],
    ["Identity", c.identity],
    ["Traits", c.traits && display(c.traits)],
    ["Input SHA-256", c.event_sha256],
    ["Captured code", c.app_sha256],
    ["Original snapshot", c.historical.snapshot_sha256],
    ["Updated snapshot", c.current.snapshot_sha256],
    ["Runtime", c.runtime && display(c.runtime)],
    ...Object.entries(c.reasons || {}).map(([k, v]) => [
      "SDK reason · " + k,
      v,
    ]),
    ...checks,
  ]);
  byId("regression").hidden = !t;
  if (t) {
    const list = element("dl");
    list.className = "signal";
    fields(list, [
      ["Original verdict", testVerdict(t.before)],
      ["Candidate verdict", testVerdict(t.after)],
      ["Original code SHA-256", t.before.app_sha256],
      ["Candidate code SHA-256", t.after.app_sha256],
      ["Specification SHA-256", t.after.spec_sha256],
      ["Test file SHA-256", t.test_sha256],
      [
        "Observed flags",
        t.after.flags_match ? "Match the historical reads" : t.after.detail,
      ],
    ]);
    const nodes = [
      list,
      element("p", "Expected application result"),
      element("pre", display(t.after.expected)),
    ];
    if (a) {
      const button = element("button", "Download runnable synthetic pytest");
      button.type = "button";
      button.onclick = () =>
        download(
          a.filename,
          new Blob(
            [Uint8Array.from(atob(a.zip_base64), (v) => v.charCodeAt(0))],
            { type: "application/zip" },
          ),
        );
      nodes.push(
        element("h3", "Application change"),
        element("pre", a.patch),
        element(
          "p",
          "The downloadable fixture contains synthetic effective values and its own snapshot. Use CPython " +
            a.runtime.python +
            " on Linux. The archive includes the exact test commands.",
        ),
        button,
      );
    }
    byId("regression-body").replaceChildren(...nodes);
  }
  const importable = /^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$/.test(
    c.registry || "",
  );
  byId("replay-command").hidden = !(
    importable && /^[a-f0-9]{24}$/.test(c.capture_id)
  );
  if (!byId("replay-command").hidden)
    byId("command").textContent =
      "flight-recorder replay " + c.capture_id + " --registry " + c.registry;
}
function render(inc, key, push = false) {
  incident = inc;
  stages = stageList();
  byId("case").value = inc.id;
  byId("case-id").textContent = "Capture " + short(inc.comparison.capture_id);
  document.title = inc.title + " · Flag Flight Recorder";
  byId("headline").textContent = inc.headline || inc.title;
  byId("dek").textContent = inc.description;
  buildTrace();
  buildGrid();
  buildDetails();
  byId("provenance").open = key === "capture";
  byId("regression").open = key === "regression";
  const mapped =
    { overview: "replay", regression: "fix", capture: "replay" }[key] || key;
  const index = stages.findIndex((s) => s.id === mapped);
  selectStage(index < 0 ? 2 : index, false);
  route(key || "replay", push);
  if (key === "combinations" && inc.exploration) byId("explorer").focus();
  byId("meta").textContent = [
    evidence.flagsmith_release && "Flagsmith " + evidence.flagsmith_release,
    evidence.sample === "synthetic-orders"
      ? "Synthetic order examples"
      : "Recorded evidence",
  ]
    .filter(Boolean)
    .join(" · ");
}
function loadRoute() {
  if (location.hash === "#investigation" && incident) return;
  let id, key;
  try {
    [id, key] = location.hash.slice(1).split("/");
    id = decodeURIComponent(id);
  } catch {
    id = "";
  }
  render(
    evidence.cases.find((c) => c.id === id) || evidence.cases[0],
    key || "replay",
  );
}
for (const item of evidence.cases) {
  const opt = element("option", item.title);
  opt.value = item.id;
  byId("case").append(opt);
}
byId("case").onchange = (e) =>
  render(
    evidence.cases.find((c) => c.id === e.target.value),
    "replay",
    true,
  );
byId("jump").onclick = () => {
  route("combinations", true);
  byId("explorer").focus();
  byId("explorer").scrollIntoView({ block: "start" });
};
byId("save").onclick = () =>
  jsonDownload("flight-recorder-evidence.json", evidence);
byId("summary").onclick = () =>
  jsonDownload("flight-recorder-summary.json", evidence.share_summary);
byId("copy").onclick = async () => {
  try {
    await navigator.clipboard.writeText(byId("command").textContent);
    byId("copy").textContent = "Copied";
    setTimeout(() => (byId("copy").textContent = "Copy command"), 1200);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(byId("command"));
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
};
document.querySelector(".skip").onclick = (e) => {
  e.preventDefault();
  byId("investigation").focus();
};
window.addEventListener("hashchange", loadRoute);
loadRoute();
