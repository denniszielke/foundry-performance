"""Generate a self-contained interactive dashboard from benchmark result files.

By default, this reads ``results/benchmark-*.json`` and writes
``results/benchmark-dashboard.html``::

    python -m scripts.generate_benchmark_dashboard
    python -m scripts.generate_benchmark_dashboard --results-dir results --out results/report.html
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts._helpers import ROOT


ERROR_MARKERS = (
    "service failed",
    "api version not supported",
    "error code:",
    "traceback",
    "internal server error",
)


def _quality_summary(turns: list[dict[str, Any]]) -> dict[str, int]:
    suspicious = 0
    for turn in turns:
        if not turn.get("ok"):
            continue
        text = str(turn.get("text") or "").strip().lower()
        if not text or any(marker in text for marker in ERROR_MARKERS):
            suspicious += 1
    return {"turns": len(turns), "suspicious": suspicious}


def _load_run(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read benchmark file {path}: {exc}") from exc

    required = ("datetime", "agent-type", "model-hosting", "model-deployment", "results")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"Benchmark file {path} is missing: {', '.join(missing)}")
    if not isinstance(data["results"], list):
        raise ValueError(f"Benchmark file {path} has a non-array 'results' value")

    turns = data.get("turns", [])
    if not isinstance(turns, list):
        turns = []
    return {
        "source": path.name,
        "datetime": data["datetime"],
        "agent-type": data["agent-type"],
        "model-hosting": data["model-hosting"],
        "model-deployment": data["model-deployment"],
        "tool-mode": data.get("tool-mode", "unknown"),
        "iterations": data.get("iterations"),
        "query": data.get("query"),
        "base-url": data.get("base-url"),
        "quality": _quality_summary(turns),
        "results": data["results"],
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Foundry Agent Performance</title>
  <script>
    (() => {
      const param = new URLSearchParams(window.location.search).get("scoutTheme");
      const theme =
        param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.setAttribute("data-theme", theme);
    })();
  </script>
  <style>
    :root {
      color-scheme: light;
      --cp-bg: #f7f4ef;
      --cp-bg-elevated: #fcfbf8;
      --cp-surface: #ffffff;
      --cp-surface-soft: #f5f5f5;
      --cp-border: #dedede;
      --cp-border-strong: #919191;
      --cp-text: #242424;
      --cp-text-muted: #5c5c5c;
      --cp-text-soft: #6f6f6f;
      --cp-accent: #b11f4b;
      --cp-accent-hover: #9a1a41;
      --cp-accent-soft: rgba(177, 31, 75, 0.08);
      --cp-accent-fg: #ffffff;
      --cp-success: #16a34a;
      --cp-danger: #dc2626;
      --cp-warning: #f59e0b;
      --cp-link: #0078d4;
      --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.12);
      --cp-overlay: rgba(255, 255, 255, 0.8);
      --cp-panel: rgba(255, 255, 255, 0.86);
      --cp-panel-strong: rgba(255, 255, 255, 0.96);
      --cp-sheen: rgba(255, 255, 255, 0.55);
      --cp-highlight: rgba(177, 31, 75, 0.12);
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --cp-bg: #3d3b3a;
      --cp-bg-elevated: #343231;
      --cp-surface: #292929;
      --cp-surface-soft: #2e2e2e;
      --cp-border: #474747;
      --cp-border-strong: #5f5f5f;
      --cp-text: #dedede;
      --cp-text-muted: #919191;
      --cp-text-soft: #b0b0b0;
      --cp-accent: #fd8ea1;
      --cp-accent-hover: #fb7b91;
      --cp-accent-soft: rgba(253, 142, 161, 0.14);
      --cp-accent-fg: #1a1a1a;
      --cp-success: #4ade80;
      --cp-danger: #f87171;
      --cp-warning: #fbbf24;
      --cp-link: #4da6ff;
      --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
      --cp-overlay: rgba(41, 41, 41, 0.88);
      --cp-panel: rgba(41, 41, 41, 0.72);
      --cp-panel-strong: rgba(41, 41, 41, 0.96);
      --cp-sheen: rgba(255, 255, 255, 0.04);
      --cp-highlight: rgba(253, 142, 161, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--cp-bg);
      color: var(--cp-text);
      font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
    }
    button, input, select { font: inherit; }
    .shell { width: min(1480px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 48px; }
    header { display: flex; justify-content: space-between; align-items: end; gap: 24px; margin-bottom: 24px; }
    .eyebrow { color: var(--cp-accent); font-size: 12px; font-weight: 700; letter-spacing: 0; text-transform: uppercase; }
    h1 { margin: 4px 0 6px; font-size: clamp(28px, 4vw, 48px); line-height: 1; letter-spacing: 0; }
    .subtitle { margin: 0; color: var(--cp-text-muted); }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    button, .file-button {
      min-height: 38px; padding: 8px 12px; border: 1px solid var(--cp-border-strong); border-radius: 8px;
      background: var(--cp-surface); color: var(--cp-text); cursor: pointer; display: inline-flex; align-items: center; gap: 8px;
    }
    button:hover, .file-button:hover { border-color: var(--cp-accent); color: var(--cp-accent); }
    .file-button input { display: none; }
    .panel { background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 8px; }
    .filters { display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 12px; padding: 16px; margin-bottom: 16px; }
    label { display: grid; gap: 6px; color: var(--cp-text-muted); font-size: 12px; font-weight: 600; }
    select { width: 100%; min-height: 38px; padding: 7px 28px 7px 9px; border: 1px solid var(--cp-border); border-radius: 6px; background: var(--cp-bg-elevated); color: var(--cp-text); }
    .phase-filter { margin: 0; padding: 0; border: 0; min-width: 0; }
    .phase-filter legend { margin-bottom: 6px; color: var(--cp-text-muted); font-size: 12px; font-weight: 600; }
    .phase-options { display: flex; min-height: 38px; border: 1px solid var(--cp-border); border-radius: 6px; overflow: hidden; }
    .phase-option { display: flex; flex: 1; align-items: center; justify-content: center; gap: 5px; padding: 7px 5px; background: var(--cp-bg-elevated); color: var(--cp-text); font-size: 11px; cursor: pointer; white-space: nowrap; }
    .phase-option + .phase-option { border-left: 1px solid var(--cp-border); }
    .phase-option:has(input:not(:checked)) { background: var(--cp-surface-soft); color: var(--cp-text-muted); text-decoration: line-through; }
    .phase-option input { margin: 0; accent-color: var(--cp-accent); }
    .kpis { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
    .kpi { padding: 16px; min-height: 105px; border-top: 3px solid var(--cp-accent); }
    .kpi.warning { border-top-color: var(--cp-warning); }
    .kpi.danger { border-top-color: var(--cp-danger); }
    .kpi-label { color: var(--cp-text-muted); font-size: 12px; }
    .kpi-value { margin: 7px 0 2px; font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }
    .kpi-detail { color: var(--cp-text-soft); font-size: 12px; }
    .workspace { display: grid; grid-template-columns: minmax(0, 1.65fr) minmax(280px, .55fr); gap: 16px; margin-bottom: 16px; }
    .section-head { display: flex; justify-content: space-between; gap: 16px; align-items: center; padding: 16px 16px 0; }
    h2 { margin: 0; font-size: 17px; letter-spacing: 0; }
    .section-note { color: var(--cp-text-muted); font-size: 12px; }
    #chart { min-height: 390px; padding: 20px 16px 16px; display: grid; align-content: start; gap: 12px; }
    .bar-row { display: grid; grid-template-columns: minmax(150px, 220px) minmax(120px, 1fr) 96px; align-items: center; gap: 12px; }
    .bar-label { min-width: 0; }
    .bar-label strong, .bar-label span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .bar-label span { color: var(--cp-text-muted); font-size: 11px; margin-top: 2px; }
    .track { height: 22px; background: var(--cp-surface-soft); border: 1px solid var(--cp-border); border-radius: 4px; overflow: hidden; }
    .bar { height: 100%; min-width: 2px; background: var(--cp-accent); }
    .bar.bad { background: var(--cp-danger); }
    .bar-value { text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; }
    .runs { padding: 10px 16px 16px; max-height: 390px; overflow: auto; }
    .run { padding: 12px 0; border-bottom: 1px solid var(--cp-border); }
    .run:last-child { border-bottom: 0; }
    .run-top { display: flex; justify-content: space-between; gap: 8px; }
    .run-name { font-weight: 700; }
    .run-meta, .run-file { color: var(--cp-text-muted); font-size: 11px; margin-top: 4px; overflow-wrap: anywhere; }
    .badge { border: 1px solid var(--cp-border); border-radius: 999px; padding: 2px 7px; font-size: 10px; white-space: nowrap; }
    .badge.warn { color: var(--cp-warning); border-color: var(--cp-warning); }
    .table-panel { overflow: hidden; }
    .table-wrap { overflow: auto; max-height: 520px; margin-top: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { position: sticky; top: 0; z-index: 1; background: var(--cp-surface-soft); color: var(--cp-text-muted); text-align: left; cursor: pointer; white-space: nowrap; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--cp-border); }
    td.number { text-align: right; font-variant-numeric: tabular-nums; }
    tr.flagged td:first-child { box-shadow: inset 3px 0 var(--cp-warning); }
    .error { color: var(--cp-danger); font-weight: 700; }
    .empty { color: var(--cp-text-muted); padding: 36px 16px; text-align: center; }
    .drop-active { outline: 3px solid var(--cp-accent); outline-offset: -8px; }
    footer { color: var(--cp-text-muted); font-size: 12px; margin-top: 14px; }
    @media (max-width: 980px) {
      .filters { grid-template-columns: repeat(3, 1fr); }
      .kpis { grid-template-columns: repeat(3, 1fr); }
      .workspace { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .shell { width: min(100% - 20px, 1480px); padding-top: 18px; }
      header { align-items: start; flex-direction: column; }
      .filters { grid-template-columns: repeat(2, 1fr); }
      .kpis { grid-template-columns: repeat(2, 1fr); }
      .bar-row { grid-template-columns: 110px minmax(80px, 1fr) 72px; gap: 7px; }
      .kpi-value { font-size: 22px; }
    }
  </style>
</head>
<body>
  <main class="shell" id="app">
    <header>
      <div>
        <div class="eyebrow">Benchmark explorer</div>
        <h1>Foundry agent performance</h1>
        <p class="subtitle" id="subtitle"></p>
      </div>
      <div class="actions">
        <label class="file-button" title="Add benchmark JSON files">+ Add JSON<input id="file-input" type="file" accept=".json,application/json" multiple></label>
        <button id="reset" type="button">Reset filters</button>
      </div>
    </header>

    <section class="panel filters" aria-label="Dashboard filters">
      <label>Agent type<select id="agent-type"></select></label>
      <label>Model hosting<select id="model-hosting"></select></label>
      <label>Model deployment<select id="model-deployment"></select></label>
      <label>Tool mode<select id="tool-mode"></select></label>
      <label>Protocol<select id="protocol"></select></label>
      <fieldset class="phase-filter"><legend>Include phases</legend><div class="phase-options">
        <label class="phase-option"><input type="checkbox" name="phase" value="cold" checked>Cold</label>
        <label class="phase-option"><input type="checkbox" name="phase" value="warm" checked>Warm</label>
        <label class="phase-option"><input type="checkbox" name="phase" value="followup" checked>Follow-up</label>
      </div></fieldset>
      <label>Metric<select id="metric">
        <option value="mean-ms">Mean latency</option><option value="p50">P50 latency</option>
        <option value="p95">P95 latency</option><option value="ttfb">TTFB</option><option value="error-rate">Error rate</option>
      </select></label>
    </section>

    <section class="kpis" id="kpis"></section>

    <section class="workspace">
      <div class="panel">
        <div class="section-head"><h2>Comparison</h2><span class="section-note" id="chart-note"></span></div>
        <div id="chart"></div>
      </div>
      <aside class="panel">
        <div class="section-head"><h2>Included runs</h2><span class="section-note" id="run-count"></span></div>
        <div class="runs" id="runs"></div>
      </aside>
    </section>

    <section class="panel table-panel">
      <div class="section-head"><h2>Measurements</h2><span class="section-note">Select a heading to sort</span></div>
      <div class="table-wrap"><table>
        <thead><tr>
          <th data-key="agent-type">Agent</th><th data-key="model-hosting">Hosting</th><th data-key="model-deployment">Model</th><th data-key="tool-mode">Tool mode</th>
          <th data-key="protocol">Protocol</th><th data-key="phase">Phase</th><th data-key="n">N</th><th data-key="err">Errors</th>
          <th data-key="mean-ms">Mean</th><th data-key="p50">P50</th><th data-key="p95">P95</th><th data-key="ttfb">TTFB</th><th data-key="datetime">Recorded</th>
        </tr></thead><tbody id="table-body"></tbody>
      </table></div>
    </section>
    <footer>Drop benchmark JSON files anywhere on this page to add them to the current comparison. Imported data remains local to your browser.</footer>
  </main>

  <script>
    const INITIAL_RUNS = __RUN_DATA__;
    const filterIds = ["agent-type", "model-hosting", "model-deployment", "tool-mode", "protocol"];
    const state = { runs: INITIAL_RUNS, sortKey: "mean-ms", sortDirection: 1 };
    const byId = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[char]));
    const number = (value) => value === null || value === undefined || value === "" ? null : (Number.isFinite(Number(value)) ? Number(value) : null);
    const formatMs = (value) => number(value) === null ? "--" : `${number(value).toLocaleString(undefined, {maximumFractionDigits: 1})} ms`;
    const formatRate = (value) => `${(value * 100).toFixed(value ? 1 : 0)}%`;
    const qualityFor = (turns) => {
      const markers = ["service failed", "api version not supported", "error code:", "traceback", "internal server error"];
      const suspicious = turns.filter((turn) => turn.ok && (!String(turn.text || "").trim() || markers.some((marker) => String(turn.text || "").toLowerCase().includes(marker)))).length;
      return {turns: turns.length, suspicious};
    };
    function normalizeRun(data, source) {
      const required = ["datetime", "agent-type", "model-hosting", "model-deployment", "results"];
      const missing = required.filter((key) => !(key in data));
      if (missing.length || !Array.isArray(data.results)) throw new Error(`${source}: invalid benchmark data${missing.length ? `; missing ${missing.join(", ")}` : ""}`);
      return {source, datetime: data.datetime, "agent-type": data["agent-type"], "model-hosting": data["model-hosting"],
        "model-deployment": data["model-deployment"], "tool-mode": data["tool-mode"] || "unknown", iterations: data.iterations, query: data.query, "base-url": data["base-url"],
        quality: qualityFor(Array.isArray(data.turns) ? data.turns : []), results: data.results};
    }
    function rows() {
      return state.runs.flatMap((run) => run.results.map((row) => ({...row, "tool-mode": row["tool-mode"] || run["tool-mode"] || "unknown", source: run.source, datetime: run.datetime, quality: run.quality})));
    }
    function filteredRows() {
      const phases = new Set([...document.querySelectorAll('input[name="phase"]:checked')].map((input) => input.value));
      return rows().filter((row) => phases.has(String(row.phase)) && filterIds.every((id) => byId(id).value === "all" || String(row[id]) === byId(id).value));
    }
    function fillFilters() {
      const current = Object.fromEntries(filterIds.map((id) => [id, byId(id).value || "all"]));
      const allRows = rows();
      filterIds.forEach((id) => {
        const values = [...new Set(allRows.map((row) => row[id]).filter((value) => value !== null && value !== undefined))].sort();
        byId(id).innerHTML = `<option value="all">All</option>${values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}`;
        byId(id).value = values.map(String).includes(current[id]) ? current[id] : "all";
      });
    }
    function aggregate(items) {
      const samples = items.reduce((sum, row) => sum + (number(row.n) || 0), 0);
      const errors = items.reduce((sum, row) => sum + (number(row.err) || 0), 0);
      const weighted = (key) => {
        const valid = items.filter((row) => number(row[key]) !== null && number(row.n) > 0);
        const weight = valid.reduce((sum, row) => sum + number(row.n), 0);
        return weight ? valid.reduce((sum, row) => sum + number(row[key]) * number(row.n), 0) / weight : null;
      };
      return {samples, errors, mean: weighted("mean-ms"), p95: weighted("p95"), ttfb: weighted("ttfb")};
    }
    function renderKpis(items) {
      const stats = aggregate(items);
      const warningRuns = new Set(items.filter((row) => row.quality.suspicious).map((row) => row.source)).size;
      const cards = [
        ["Samples", stats.samples.toLocaleString(), `${items.length} aggregate rows`, ""],
        ["Mean latency", formatMs(stats.mean), "weighted by sample count", ""],
        ["P95 latency", formatMs(stats.p95), "weighted aggregate", ""],
        ["Error rate", stats.samples ? formatRate(stats.errors / stats.samples) : "--", `${stats.errors} errors`, stats.errors ? "danger" : ""],
        ["Quality warnings", warningRuns.toLocaleString(), "runs with suspicious output", warningRuns ? "warning" : ""]
      ];
      byId("kpis").innerHTML = cards.map(([label, value, detail, cls]) => `<div class="panel kpi ${cls}"><div class="kpi-label">${label}</div><div class="kpi-value">${value}</div><div class="kpi-detail">${detail}</div></div>`).join("");
    }
    function renderChart(items) {
      const metric = byId("metric").value;
      const title = byId("metric").selectedOptions[0].text;
      const plotted = items.map((row) => ({row, value: metric === "error-rate" ? (number(row.n) ? number(row.err) / number(row.n) : null) : number(row[metric])})).filter((item) => item.value !== null).sort((a, b) => a.value - b.value).slice(0, 30);
      const max = Math.max(...plotted.map((item) => item.value), 0);
      byId("chart-note").textContent = `${title} · ${plotted.length}${items.length > 30 ? " of " + items.length : ""} rows`;
      if (!plotted.length) { byId("chart").innerHTML = `<div class="empty">No values are available for this metric and filter set.</div>`; return; }
      byId("chart").innerHTML = plotted.map(({row, value}) => {
        const errorRate = number(row.n) ? number(row.err) / number(row.n) : 0;
        const display = metric === "error-rate" ? formatRate(value) : formatMs(value);
        return `<div class="bar-row" title="${escapeHtml(row.source)}"><div class="bar-label"><strong>${escapeHtml(row["agent-type"])}</strong><span>${escapeHtml(row["tool-mode"])} · ${escapeHtml(row.protocol)} · ${escapeHtml(row.phase)}</span></div><div class="track"><div class="bar ${errorRate ? "bad" : ""}" style="width:${max ? Math.max(0.5, value / max * 100) : 0}%"></div></div><div class="bar-value">${display}</div></div>`;
      }).join("");
    }
    function renderRuns(items) {
      const sources = new Set(items.map((row) => row.source));
      const runs = state.runs.filter((run) => sources.has(run.source)).sort((a, b) => String(b.datetime).localeCompare(String(a.datetime)));
      byId("run-count").textContent = `${runs.length} of ${state.runs.length}`;
      byId("runs").innerHTML = runs.length ? runs.map((run) => `<div class="run"><div class="run-top"><span class="run-name">${escapeHtml(run["agent-type"])}</span><span class="badge ${run.quality.suspicious ? "warn" : ""}">${run.quality.suspicious ? `${run.quality.suspicious} warnings` : "clean"}</span></div><div class="run-meta">${escapeHtml(run["model-hosting"])} · ${escapeHtml(run["model-deployment"])} · ${escapeHtml(run["tool-mode"])} · ${new Date(run.datetime).toLocaleString()}</div><div class="run-file">${escapeHtml(run.source)}</div></div>`).join("") : `<div class="empty">No runs match the filters.</div>`;
    }
    function renderTable(items) {
      const direction = state.sortDirection;
      const sorted = [...items].sort((a, b) => {
        const left = a[state.sortKey], right = b[state.sortKey];
        if (number(left) !== null && number(right) !== null) return (number(left) - number(right)) * direction;
        return String(left ?? "").localeCompare(String(right ?? "")) * direction;
      });
      byId("table-body").innerHTML = sorted.length ? sorted.map((row) => `<tr class="${row.quality.suspicious ? "flagged" : ""}" title="${escapeHtml(row.source)}"><td>${escapeHtml(row["agent-type"])}</td><td>${escapeHtml(row["model-hosting"])}</td><td>${escapeHtml(row["model-deployment"])}</td><td>${escapeHtml(row["tool-mode"])}</td><td>${escapeHtml(row.protocol)}</td><td>${escapeHtml(row.phase)}</td><td class="number">${number(row.n) ?? "--"}</td><td class="number ${number(row.err) ? "error" : ""}">${number(row.err) ?? "--"}</td><td class="number">${formatMs(row["mean-ms"])}</td><td class="number">${formatMs(row.p50)}</td><td class="number">${formatMs(row.p95)}</td><td class="number">${formatMs(row.ttfb)}</td><td>${new Date(row.datetime).toLocaleString()}</td></tr>`).join("") : `<tr><td colspan="13" class="empty">No measurements match the filters.</td></tr>`;
    }
    function render() {
      const items = filteredRows();
      byId("subtitle").textContent = `${state.runs.length} runs · ${rows().length} aggregate measurements · generated __GENERATED_AT__`;
      renderKpis(items); renderChart(items); renderRuns(items); renderTable(items);
    }
    async function addFiles(files) {
      const additions = [];
      const failures = [];
      for (const file of files) {
        try { additions.push(normalizeRun(JSON.parse(await file.text()), file.name)); }
        catch (error) { failures.push(error.message); }
      }
      const existing = new Set(state.runs.map((run) => run.source));
      state.runs.push(...additions.filter((run) => !existing.has(run.source)));
      fillFilters(); render();
      if (failures.length) console.error("Skipped benchmark files:", failures);
    }
    filterIds.concat("metric").forEach((id) => byId(id).addEventListener("change", render));
    document.querySelectorAll('input[name="phase"]').forEach((input) => input.addEventListener("change", render));
    byId("reset").addEventListener("click", () => { filterIds.forEach((id) => byId(id).value = "all"); document.querySelectorAll('input[name="phase"]').forEach((input) => input.checked = true); byId("metric").value = "mean-ms"; render(); });
    byId("file-input").addEventListener("change", (event) => addFiles([...event.target.files]));
    document.querySelectorAll("th[data-key]").forEach((heading) => heading.addEventListener("click", () => {
      state.sortDirection = state.sortKey === heading.dataset.key ? -state.sortDirection : 1; state.sortKey = heading.dataset.key; render();
    }));
    document.addEventListener("dragover", (event) => { event.preventDefault(); byId("app").classList.add("drop-active"); });
    document.addEventListener("dragleave", () => byId("app").classList.remove("drop-active"));
    document.addEventListener("drop", (event) => { event.preventDefault(); byId("app").classList.remove("drop-active"); addFiles([...event.dataTransfer.files].filter((file) => file.name.endsWith(".json"))); });
    fillFilters(); render();
  </script>
</body>
</html>
'''


def generate_dashboard(results_dir: Path, pattern: str, output: Path) -> int:
    paths = sorted(path for path in results_dir.glob(pattern) if path.is_file())
    if not paths:
        raise ValueError(f"No benchmark files matched {results_dir / pattern}")
    runs = [_load_run(path) for path in paths]
    embedded = json.dumps(runs, separators=(",", ":"), ensure_ascii=True).replace("</", "<\\/")
    generated_at = max(str(run["datetime"]) for run in runs)
    html = HTML_TEMPLATE.replace("__RUN_DATA__", embedded).replace("__GENERATED_AT__", generated_at)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return len(runs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=ROOT / "results", help="Directory containing benchmark JSON files")
    parser.add_argument("--pattern", default="benchmark-*.json", help="Glob used within the results directory")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "benchmark-dashboard.html", help="Generated HTML path")
    args = parser.parse_args()
    try:
        count = generate_dashboard(args.results_dir, args.pattern, args.out)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Generated {args.out} from {count} benchmark files.")


if __name__ == "__main__":
    main()