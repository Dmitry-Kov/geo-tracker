# -*- coding: utf-8 -*-
"""
HTML dashboard generator for the GEO tracker.
A single self-contained dashboard.html: data is inlined as JSON,
charts are Chart.js from a CDN (opening the file requires internet).
"""

import json
from datetime import datetime
from pathlib import Path

# Brand colors for domains on charts (stable across reports)
DOMAIN_COLORS = ["#FF5C1F", "#1F7A8C", "#6A4C93", "#2A9D8F", "#E0A100", "#D62246"]

# A trend point is drawn for a date only if that date covers at least this
# fraction of the busiest date's ok-answer count. Keeps gap-fill days (a small
# single-engine top-up) from rendering as false collapses in the trend.
TREND_MIN_FRACTION = 0.5


def _hit(row: dict, domain: str) -> bool:
    return row.get(f"src_{domain}") == "1" or row.get(f"txt_{domain}") == "1"


def _ts(row: dict) -> str:
    # run_ts is ISO ("2026-06-12T17:14:00Z"), sortable as a string; fall back to date
    return row.get("run_ts") or row.get("run_date") or ""


def _latest_per_pair(ok: list) -> list:
    """Newest ok answer for each (engine, niche, query) pair, regardless of date.

    This is the "current visibility" snapshot: each pair contributes its freshest
    measurement, so a partial gap-fill day doesn't wipe out pairs last run earlier.
    """
    latest = {}
    for r in ok:
        key = (r["engine"], r["niche"], r["query"])
        if key not in latest or _ts(r) >= _ts(latest[key]):
            latest[key] = r
    return list(latest.values())


def _sov_rows(rows: list, domains: list, niches: dict) -> dict:
    """{niche: {domain: % of the niche's rows with a hit}} over the given rows."""
    out = {}
    for niche in niches:
        subset = [r for r in rows if r["niche"] == niche]
        if not subset:
            continue
        out[niche] = {d: round(100 * sum(_hit(r, d) for r in subset) / len(subset), 1)
                      for d in domains}
    return out


def _prepare(rows: list, domains: list, niches: dict) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    errors = [r for r in rows if r["status"] != "ok"]
    dates = sorted({r["run_date"] for r in rows})
    engines = sorted({r["engine"] for r in ok})
    latest = dates[-1]

    # Current snapshot: freshest ok answer per pair, used by the matrix,
    # the engine breakdown, the leaderboard and the hits list.
    current = _latest_per_pair(ok)

    # Trend: overall domain SOV by date — but only for dates whose coverage is
    # comparable to the busiest date, so gap-fill days are left as gaps (null).
    date_counts = {date: sum(1 for r in ok if r["run_date"] == date) for date in dates}
    threshold = TREND_MIN_FRACTION * (max(date_counts.values()) if date_counts else 0)
    trend = {d: [] for d in domains}
    trend_hidden = 0
    for date in dates:
        day = [r for r in ok if r["run_date"] == date]
        if day and len(day) >= threshold:
            for d in domains:
                trend[d].append(round(100 * sum(_hit(r, d) for r in day) / len(day), 1))
        else:
            for d in domains:
                trend[d].append(None)
            trend_hidden += 1

    # Per-engine breakdown over the current snapshot
    by_engine = {}
    for e in engines:
        sub = [r for r in current if r["engine"] == e]
        if sub:
            by_engine[e] = {d: round(100 * sum(_hit(r, d) for r in sub) / len(sub), 1)
                            for d in domains}

    leaderboard = sorted(
        ((d, round(100 * sum(_hit(r, d) for r in current) / len(current), 1)
          if current else 0.0) for d in domains),
        key=lambda x: -x[1])

    # Hits from the current snapshot, freshest first
    current_hits = []
    for r in sorted(current, key=_ts, reverse=True):
        hit_domains = [d for d in domains if _hit(r, d)]
        if hit_domains:
            current_hits.append({"engine": r["engine"], "niche": r["niche"],
                                 "query": r["query"], "domains": hit_domains})
        if len(current_hits) >= 12:
            break

    return {
        "generated": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "dates": dates,
        "latest": latest,
        "engines": engines,
        "domains": domains,
        "colors": dict(zip(domains, DOMAIN_COLORS)),
        "niches": {k: {"title": v["title"], "primary": v["primary_domain"]}
                   for k, v in niches.items()},
        "n_answers": len(ok),
        "n_errors": len(errors),
        "n_pairs": len(current),
        "trend_hidden": trend_hidden,
        "sov_current": _sov_rows(current, domains, niches),
        "trend": trend,
        "by_engine": by_engine,
        "leaderboard": leaderboard,
        "current_hits": current_hits,
    }


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GEO Monitor · Optimize.uz</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js"></script>
<style>
:root{
  --paper:#F6F7F4; --card:#FFFFFF; --ink:#16181D; --muted:#6B7180;
  --line:#E4E6E0; --accent:#FF5C1F; --accent-soft:#FFF0E9;
  --mono:'JetBrains Mono',ui-monospace,monospace;
}
*{box-sizing:border-box;margin:0}
body{background:var(--paper);color:var(--ink);font:15px/1.55 'Inter',system-ui,sans-serif;padding:0 0 64px}
.wrap{max-width:1100px;margin:0 auto;padding:0 24px}
header{border-bottom:2px solid var(--ink);padding:28px 0 20px;margin-bottom:28px}
.brand{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px}
h1{font:700 26px/1.1 'Space Grotesk',sans-serif;letter-spacing:-.02em}
h1 .dot{color:var(--accent)}
.meta{font-family:var(--mono);font-size:12px;color:var(--muted)}
h2{font:700 17px 'Space Grotesk',sans-serif;margin:36px 0 6px}
.sub{color:var(--muted);font-size:13px;margin-bottom:14px}
.kpis{display:flex;gap:0;border:1px solid var(--line);border-radius:10px;background:var(--card);overflow:hidden;flex-wrap:wrap}
.kpi{flex:1 1 140px;padding:16px 20px;border-right:1px solid var(--line)}
.kpi:last-child{border-right:0}
.kpi b{display:block;font-family:var(--mono);font-size:24px;font-weight:700}
.kpi b.acc{color:var(--accent)}
.kpi span{font-size:12px;color:var(--muted)}
table.matrix{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.matrix th,.matrix td{padding:10px 8px;text-align:center;border-bottom:1px solid var(--line);font-size:13px}
.matrix th{font:600 12px 'Inter';color:var(--muted);text-transform:none}
.matrix th.dom{font-family:var(--mono);font-size:11px}
.matrix td.niche{text-align:left;font-weight:600;padding-left:14px;white-space:nowrap}
.cell{font-family:var(--mono);font-weight:700;border-radius:6px;padding:6px 0;display:block;min-width:54px}
.cell.primary{outline:2px solid var(--ink);outline-offset:-2px}
.legend{font-size:12px;color:var(--muted);margin-top:8px}
.legend .pr{display:inline-block;width:11px;height:11px;border:2px solid var(--ink);border-radius:3px;vertical-align:-1px;margin-right:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:840px){.grid2{grid-template-columns:1fr}}
canvas{max-height:340px}
ul.hits{list-style:none}
ul.hits li{padding:10px 0;border-bottom:1px solid var(--line);font-size:13px}
ul.hits li:last-child{border-bottom:0}
.tag{font-family:var(--mono);font-size:11px;background:var(--accent-soft);color:var(--accent);border-radius:4px;padding:1px 6px;margin-left:4px;white-space:nowrap}
.eng{font-family:var(--mono);font-size:11px;color:var(--muted)}
footer{margin-top:40px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}
</style>
</head>
<body>
<div class="wrap">
<header><div class="brand">
  <h1>Optimize<span class="dot">.</span>uz&ensp;GEO Monitor</h1>
  <div class="meta" id="meta"></div>
</div></header>

<div class="kpis" id="kpis"></div>

<h2>Visibility by niche</h2>
<div class="sub">Share of the niche's queries where the domain appeared in the answer's sources or text. Current snapshot: the freshest answer per pair (engine × niche × query), regardless of run date.</div>
<table class="matrix" id="matrix"></table>
<div class="legend"><span class="pr"></span>niche's primary domain · fill intensity — visibility level</div>

<h2>Share of voice over time</h2>
<div class="sub" id="trendSub">Overall visibility of each domain by run date (all niches and engines).</div>
<div class="card"><canvas id="trendChart"></canvas></div>

<div class="grid2">
  <div>
    <h2>Engine breakdown</h2>
    <div class="sub">Current snapshot: where each domain is visible (freshest answer per pair).</div>
    <div class="card"><canvas id="engineChart"></canvas></div>
  </div>
  <div>
    <h2>Recent hits</h2>
    <div class="sub">Queries from the current snapshot that surfaced our domains.</div>
    <div class="card"><ul class="hits" id="hits"></ul></div>
  </div>
</div>

<footer id="foot"></footer>
</div>

<script>
const D = __DATA__;

document.getElementById('meta').textContent =
  `period ${D.dates[0]} — ${D.latest} · generated ${D.generated}`;

/* Trend caption: warn about hidden partial dates */
if (D.trend_hidden > 0) {
  document.getElementById('trendSub').textContent +=
    ` Dates with partial coverage (gap-fills) are not shown: ${D.trend_hidden} of ${D.dates.length}.`;
}

/* KPI */
const lead = D.leaderboard[0];
document.getElementById('kpis').innerHTML = [
  [D.dates.length, 'runs in history'],
  [D.n_answers, 'AI answers analyzed'],
  [D.engines.join(' · '), 'engines'],
  [`${lead[0]} — ${lead[1]}%`, 'visibility leader (current snapshot)', true],
].map(([v, l, acc]) =>
  `<div class="kpi"><b class="${acc ? 'acc' : ''}">${v}</b><span>${l}</span></div>`).join('');

/* Niche × domain matrix */
const shade = p => p === 0 ? 'transparent'
  : `rgba(255,92,31,${(0.12 + 0.78 * p / 100).toFixed(2)})`;
const fg = p => p > 55 ? '#fff' : 'var(--ink)';
let html = '<tr><th></th>' +
  D.domains.map(d => `<th class="dom">${d}</th>`).join('') + '</tr>';
for (const [niche, info] of Object.entries(D.niches)) {
  const row = D.sov_current[niche];
  if (!row) continue;
  html += `<tr><td class="niche">${info.title}</td>` + D.domains.map(d => {
    const p = row[d], pr = d === info.primary ? ' primary' : '';
    return `<td><span class="cell${pr}" style="background:${shade(p)};color:${fg(p)}">${p}%</span></td>`;
  }).join('') + '</tr>';
}
document.getElementById('matrix').innerHTML = html;

/* Trend */
new Chart(document.getElementById('trendChart'), {
  type: 'line',
  data: {
    labels: D.dates,
    datasets: D.domains.map(d => ({
      label: d, data: D.trend[d], borderColor: D.colors[d],
      backgroundColor: D.colors[d], tension: .3, spanGaps: true,
      pointRadius: 3, borderWidth: 2,
    })),
  },
  options: {
    responsive: true,
    scales: { y: { beginAtZero: true, ticks: { callback: v => v + '%' } } },
    plugins: { legend: { labels: { usePointStyle: true, boxWidth: 8 } } },
  },
});

/* By engine */
new Chart(document.getElementById('engineChart'), {
  type: 'bar',
  data: {
    labels: Object.keys(D.by_engine),
    datasets: D.domains.map(d => ({
      label: d, data: Object.values(D.by_engine).map(e => e[d]),
      backgroundColor: D.colors[d],
    })),
  },
  options: {
    responsive: true,
    scales: { y: { beginAtZero: true, max: 100, ticks: { callback: v => v + '%' } } },
    plugins: { legend: { labels: { usePointStyle: true, boxWidth: 8 } } },
  },
});

/* Hits */
document.getElementById('hits').innerHTML = D.current_hits.length
  ? D.current_hits.map(h =>
      `<li><span class="eng">${h.engine} · ${D.niches[h.niche].title}</span><br>` +
      `“${h.query}”` +
      h.domains.map(d => `<span class="tag">${d}</span>`).join('') + '</li>').join('')
  : '<li>No domains found in answers in the current snapshot.</li>';

document.getElementById('foot').textContent =
  `Pairs in current snapshot: ${D.n_pairs}. API errors across all history: ${D.n_errors}. ` +
  `Data source: results.csv · geo_tracker.py`;
</script>
</body>
</html>
"""


def build_dashboard(rows: list, domains: list, niches: dict, out_path: Path) -> None:
    data = _prepare(rows, domains, niches)
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    Path(out_path).write_text(html, encoding="utf-8")
