/* 业务改版前后对照分析 — 前端逻辑 */
"use strict";

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
const COLOR = {
  treatment: "var(--series-1)",
  control: "var(--series-2)",
  treatmentRaw: "#2a78d6",
  controlRaw: "#eb6834",
};
const FLOW_RAMP = ["var(--seq-250)", "var(--seq-350)", "var(--seq-450)", "var(--seq-550)", "var(--seq-600)"];

/* ---------- 格式化 ---------- */
const pct = (x, d = 2) => (x == null || Number.isNaN(x)) ? "—" : (x >= 0 ? "+" : "") + (x * 100).toFixed(d) + "%";
const pct0 = (x) => (x == null || Number.isNaN(x)) ? "—" : (x * 100).toFixed(1) + "%";
const fmtNum = (x) => (x == null || Number.isNaN(x)) ? "—"
  : Math.abs(x) >= 1e6 ? (x / 1e6).toFixed(2) + "M"
  : Math.abs(x) >= 1e4 ? (x / 1e3).toFixed(1) + "K"
  : Number(x).toLocaleString("zh-CN", { maximumFractionDigits: 0 });

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function svgEl(tag, attrs) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}

/* ---------- API ---------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

/* ---------- 启动 ---------- */
async function boot() {
  const [{ datasets }, { runs }] = await Promise.all([api("/api/datasets"), api("/api/runs")]);
  const sel = $("f-data-version");
  for (const d of datasets) {
    const o = el("option", null, `${d.version}（${d.row_count.toLocaleString()} 行 · ${d.note}）`);
    o.value = d.version;
    sel.appendChild(o);
  }
  if (datasets.length) sel.value = datasets[0].version;
  refreshRunList(runs);
  $("run-form").addEventListener("submit", onRun);
  $("run-select").addEventListener("change", onPickRun);
  if (runs.length) {
    $("run-select").value = runs[0].run_id;
    onPickRun();
  }
}

function refreshRunList(runs) {
  const sel = $("run-select");
  sel.innerHTML = "";
  sel.appendChild(el("option", null, "— 选择历史运行 —"));
  for (const r of runs) {
    const o = el("option", null, `${r.created_at} · ${r.name} · ${r.data_version} · #${r.result_hash.slice(0, 8)}`);
    o.value = r.run_id;
    sel.appendChild(o);
  }
}

async function onRun(ev) {
  ev.preventDefault();
  $("error").hidden = true;
  $("loading").hidden = false;
  $("run-btn").disabled = true;
  try {
    const params = {
      name: "页面运行 " + new Date().toLocaleString("zh-CN"),
      data_version: $("f-data-version").value,
      metric: $("f-metric").value,
      revamp_date: $("f-revamp").value,
      pre_days: +$("f-pre").value,
      post_days: +$("f-post").value,
      matching_window_weeks: +$("f-weeks").value,
      missing_policy: $("f-missing").value,
      event_policy: $("f-event").value,
      backfill_policy: $("f-backfill").value,
      caliber_policy: $("f-caliber").value,
      yoy_control: $("f-yoy").value === "true",
    };
    const result = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    render(result);
    const { runs } = await api("/api/runs");
    refreshRunList(runs);
    $("run-select").value = result.run_id;
  } catch (e) {
    showError(e.message);
  } finally {
    $("loading").hidden = true;
    $("run-btn").disabled = false;
  }
}

async function onPickRun() {
  const id = $("run-select").value;
  if (!id) return;
  $("error").hidden = true;
  try {
    render(await api(`/api/runs/${id}`));
  } catch (e) {
    showError(e.message);
  }
}

function showError(msg) {
  const b = $("error");
  b.textContent = "运行失败：" + msg;
  b.hidden = false;
}

/* ---------- 渲染入口 ---------- */
function render(r) {
  $("result").hidden = false;
  renderRunMeta(r);
  renderImpact(r);
  renderValidations(r);
  renderFlow(r);
  renderTrend(r);
  renderStrata(r);
  renderControls(r);
  renderSensitivity(r);
  renderEvents(r);
  renderEvidence(r);
}

function renderRunMeta(r) {
  const m = $("run-meta");
  m.textContent = "";
  m.append(
    el("span", null, `run: ${r.run_id || "—"}`), " · ",
    el("span", null, `参数哈希: ${r.param_hash}`), " · ",
    el("span", null, `数据版本: ${r.data_version.version} (${r.data_version.content_hash.slice(0, 8)})`), " · ",
    el("span", null, `代码: ${r.code_version}`),
  );
}

/* ---------- 影响范围 ---------- */
function renderImpact(r) {
  const ir = r.impact_range;
  $("impact-metric").textContent = `指标 ${r.params.metric} · 窗口 pre ${r.windows.pre[0]}~${r.windows.pre[1]} / post ${r.windows.post[0]}~${r.windows.post[1]}`;
  $("hero-point").textContent = pct(ir.point_pct);
  const v = $("hero-verdict");
  v.textContent = ir.verdict;
  $("tile-ci").textContent = ir.ci_pct ? `${pct(ir.ci_pct[0])} ~ ${pct(ir.ci_pct[1])}` : "—";
  $("tile-did").textContent = ir.did_pct != null
    ? pct(ir.did_pct) + (ir.did_ci_pct ? `（${pct(ir.did_ci_pct[0])}~${pct(ir.did_ci_pct[1])}）` : "") : "—";
  $("tile-net").textContent = ir.net_of_yoy_pct != null ? pct(ir.net_of_yoy_pct) : "—";
  $("tile-nvar").textContent = r.sensitivity.n_variants;
  $("tile-sens").textContent = `${pct(ir.sensitivity_pct[0])} ~ ${pct(ir.sensitivity_pct[1])}`;
}

/* ---------- 验证 ---------- */
function renderValidations(r) {
  const fill = (id, checks) => {
    const ul = $(id);
    ul.innerHTML = "";
    for (const c of checks) {
      const li = el("li", c.passed ? "check-pass" : (c.severity === "warning" ? "check-warn" : "check-fail"));
      li.append(
        el("span", "check-icon", c.passed ? "✓" : (c.severity === "warning" ? "!" : "✗")),
        el("span", null, c.name + " "),
        el("span", "check-detail", c.detail),
      );
      ul.appendChild(li);
    }
  };
  fill("boundary-checks", r.validations.time_boundaries);
  fill("backfill-checks", r.validations.backfill);
  const box = $("backfill-outliers");
  box.innerHTML = "";
  if (r.backfill_outliers.length) {
    const div = el("div", "outlier-box");
    div.append(el("strong", null, `⚠ 检出 ${r.backfill_outliers.length} 行离群回补：`),
      document.createTextNode(r.backfill_outliers.map(o =>
        `${o.date} ${o.stratum}/${o.segment} 值=${fmtNum(o.value)} z=${o.robust_z}`).join("；")));
    box.appendChild(div);
  }
}

/* ---------- 样本流 ---------- */
function renderFlow(r) {
  const host = $("flow-chart");
  host.innerHTML = "";
  const stages = r.sample_flow;
  const max = Math.max(...stages.map(s => s.out), 1);
  const W = 720, rowH = 34, labelW = 170, barMax = W - labelW - 130;
  const svg = svgEl("svg", { width: W, height: stages.length * rowH + 8, role: "img",
    "aria-label": "样本流：各阶段样本量" });
  stages.forEach((s, i) => {
    const y = i * rowH + 4;
    const w = Math.max(2, (s.out / max) * barMax);
    const t = el("text");
    // 阶段名
    const name = svgEl("text", { x: 0, y: y + 17 });
    name.textContent = s.label;
    svg.appendChild(name);
    // 条形（序数色带）
    svg.appendChild(svgEl("rect", {
      x: labelW, y: y + 3, width: w, height: 22, rx: 4,
      fill: FLOW_RAMP[Math.min(i, FLOW_RAMP.length - 1)],
    }));
    // 数值标签（条外）
    const val = svgEl("text", { x: labelW + w + 8, y: y + 18 });
    val.textContent = s.out.toLocaleString() + (s.dropped ? `（剔 ${s.dropped.toLocaleString()}）` : "");
    svg.appendChild(val);
  });
  host.appendChild(svg);

  const tb = $("flow-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const s of stages) {
    const tr = el("tr");
    tr.append(el("td", null, s.label), el("td", "num", s.in.toLocaleString()),
      el("td", "num", s.out.toLocaleString()), el("td", "num", s.dropped.toLocaleString()),
      el("td", null, s.note));
    tb.appendChild(tr);
  }
}

/* ---------- 趋势图 ---------- */
function renderTrend(r) {
  const host = $("trend-chart");
  host.innerHTML = "";
  host.style.position = "relative";
  const data = r.daily_series;
  const W = Math.max(760, host.clientWidth || 760), H = 300, padL = 64, padR = 90, padT = 14, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  const vals = [];
  for (const d of data) { if (d.treatment != null) vals.push(d.treatment); if (d.control != null) vals.push(d.control); }
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const span = hi - lo || 1;
  const y0 = lo - span * 0.08, y1 = hi + span * 0.08;
  const x = (i) => padL + (i / (data.length - 1)) * plotW;
  const y = (v) => padT + (1 - (v - y0) / (y1 - y0)) * plotH;

  const svg = svgEl("svg", { width: W, height: H, role: "img", "aria-label": "处理组与对照组日指标趋势" });

  // 网格与 y 轴刻度
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = y0 + ((y1 - y0) * i) / ticks;
    const yy = y(v);
    svg.appendChild(svgEl("line", { class: "grid", x1: padL, y1: yy, x2: W - padR, y2: yy }));
    const t = svgEl("text", { x: padL - 8, y: yy + 4, "text-anchor": "end" });
    t.textContent = fmtNum(v);
    svg.appendChild(t);
  }
  // x 轴日期刻度
  const step = Math.ceil(data.length / 8);
  data.forEach((d, i) => {
    if (i % step) return;
    const t = svgEl("text", { x: x(i), y: H - 8, "text-anchor": "middle" });
    t.textContent = d.date.slice(5);
    svg.appendChild(t);
  });
  svg.appendChild(svgEl("line", { class: "axis", x1: padL, y1: H - padB, x2: W - padR, y2: H - padB }));

  // 上线日分隔线
  const revIdx = data.findIndex(d => d.period === "post");
  if (revIdx > 0) {
    const rx = (x(revIdx - 1) + x(revIdx)) / 2;
    svg.appendChild(svgEl("line", { class: "divider", x1: rx, y1: padT, x2: rx, y2: H - padB }));
    const t = svgEl("text", { x: rx + 4, y: padT + 10 });
    t.textContent = "上线 " + r.params.revamp_date;
    svg.appendChild(t);
  }

  // 两条线
  const line = (key, color) => {
    const pts = data.map((d, i) => d[key] == null ? null : `${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`);
    // 缺失处断开
    let dAttr = "", started = false;
    pts.forEach((p, i) => {
      if (p == null) { started = false; return; }
      dAttr += (started ? "L" : "M") + p;
      started = true;
    });
    svg.appendChild(svgEl("path", { class: "series-line", d: dAttr, stroke: color }));
  };
  line("treatment", COLOR.treatment);
  line("control", COLOR.control);

  // 末端直接标注
  const lastIdx = data.length - 1;
  for (const [key, color, name] of [["treatment", COLOR.treatment, "处理组"], ["control", COLOR.control, "对照组"]]) {
    let i = lastIdx;
    while (i >= 0 && data[i][key] == null) i--;
    if (i < 0) continue;
    const t = svgEl("text", { x: x(i) + 6, y: y(data[i][key]) + 4, fill: "var(--text-secondary)" });
    t.textContent = name;
    svg.appendChild(t);
    svg.appendChild(svgEl("circle", { cx: x(i), cy: y(data[i][key]), r: 4, fill: color, stroke: "var(--surface-1)", "stroke-width": 2 }));
  }

  // 事件 / 回补 / 缺失标记
  data.forEach((d, i) => {
    if (d.is_event && d.treatment != null) {
      const m = svgEl("rect", { x: x(i) - 4, y: y(d.treatment) - 4, width: 8, height: 8,
        fill: "var(--critical)", stroke: "var(--surface-1)", "stroke-width": 2,
        transform: `rotate(45 ${x(i)} ${y(d.treatment)})` });
      m.appendChild(svgEl("title"));
      m.querySelector("title").textContent = `事件 ${d.event_name || ""}`;
      svg.appendChild(m);
    }
    if (d.is_backfilled) {
      svg.appendChild(svgEl("rect", { x: x(i) - 4, y: H - padB - 10, width: 8, height: 8,
        fill: "var(--warning)", stroke: "var(--surface-1)", "stroke-width": 2 }));
    }
    if (d.is_missing) {
      svg.appendChild(svgEl("circle", { cx: x(i), cy: H - padB - 6, r: 4,
        fill: "none", stroke: "var(--text-muted)", "stroke-width": 1.5 }));
    }
  });

  // 十字线 + 提示框
  const cross = svgEl("line", { x1: 0, y1: padT, x2: 0, y2: H - padB, stroke: "var(--baseline)", "stroke-width": 1, visibility: "hidden" });
  svg.appendChild(cross);
  const tip = el("div", "tooltip");
  host.appendChild(tip);
  svg.addEventListener("pointermove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const i = Math.round(((px - padL) / plotW) * (data.length - 1));
    if (i < 0 || i >= data.length) { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; return; }
    const d = data[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    cross.setAttribute("visibility", "visible");
    tip.innerHTML = "";
    tip.appendChild(el("div", "tt-title", d.date + (d.period === "post" ? "（post）" : "（pre）")));
    for (const [key, color, name] of [["treatment", COLOR.treatmentRaw, "处理组"], ["control", COLOR.controlRaw, "对照组"]]) {
      const row = el("div", "tt-row");
      row.append(el("span", "tt-key", ""), el("span", null, name), el("span", "tt-val", fmtNum(d[key])));
      row.firstChild.style.background = color;
      tip.appendChild(row);
    }
    const flags = [d.is_event && `事件:${d.event_name}`, d.is_backfilled && "回补", d.is_missing && "缺失"].filter(Boolean);
    if (flags.length) tip.appendChild(el("div", "tt-title", flags.join(" · ")));
    tip.style.display = "block";
    tip.style.left = Math.min(px + 14, rect.width - 200) + "px";
    tip.style.top = (ev.clientY - rect.top + 10) + "px";
  });
  svg.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; });

  host.appendChild(svg);
  // 图例
  const lg = $("trend-legend");
  lg.innerHTML = "";
  for (const [color, name] of [[COLOR.treatment, "处理组"], [COLOR.control, "对照组"]]) {
    const k = el("span", "key");
    const sw = el("span", "swatch");
    sw.style.background = color;
    k.append(sw, el("span", null, name));
    lg.appendChild(k);
  }
  lg.appendChild(el("span", null, "◆事件 ■回补 ○缺失"));

  // 表格视图
  const tb = $("trend-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const d of data) {
    const tr = el("tr");
    const flags = [d.is_event && `事件:${d.event_name}`, d.is_backfilled && "回补", d.is_missing && "缺失"].filter(Boolean).join("；");
    tr.append(el("td", null, d.date), el("td", null, d.period), el("td", "num", fmtNum(d.treatment)),
      el("td", "num", fmtNum(d.control)), el("td", null, flags));
    tb.appendChild(tr);
  }
}

/* ---------- 分层结果 ---------- */
function renderStrata(r) {
  const tb = $("strata-table").querySelector("tbody");
  tb.innerHTML = "";
  const compByStratum = Object.fromEntries(r.composition.strata.map(c => [c.stratum, c]));
  for (const s of r.effects.strata) {
    const c = compByStratum[s.stratum] || {};
    const tr = el("tr");
    const flagTd = el("td");
    if (c.flag) flagTd.append(el("span", "flag-bad", "⚠ 构成显著变化"));
    else flagTd.append(el("span", "flag-ok", "✓ 稳定"));
    tr.append(
      el("td", null, s.stratum),
      el("td", "num", fmtNum(s.treatment.pre_mean)),
      el("td", "num", fmtNum(s.treatment.post_mean)),
      el("td", "num", pct(s.treatment.effect_pct)),
      el("td", "num", s.treatment.ci_pct ? `${pct(s.treatment.ci_pct[0])}~${pct(s.treatment.ci_pct[1])}` : "—"),
      el("td", "num", pct(s.did.effect_pct)),
      el("td", "num", c.pre_share != null ? `${pct0(c.pre_share)} → ${pct0(c.post_share)}` : "—"),
      flagTd,
    );
    tb.appendChild(tr);
  }
  const note = $("composition-note");
  note.textContent = "";
  const o = r.effects.overall;
  note.append(
    el("span", null, `构成调整总体 ${pct(o.adjusted.effect_pct)}；朴素总体 ${pct(o.naive.effect_pct)}；构成影响 ${o.composition_impact_pp >= 0 ? "+" : ""}${o.composition_impact_pp.toFixed(2)}pp。`),
    document.createTextNode(" "),
    el("span", r.composition.overall_flag ? "flag-bad" : "flag-ok",
      r.composition.overall_flag ? `⚠ 检出样本构成变化（χ²=${r.composition.chi2_stat.toFixed(4)}），以构成调整值为准。` : "✓ 样本构成稳定。"),
  );
}

/* ---------- 对照组 ---------- */
function renderControls(r) {
  const o = r.effects.overall;
  const rows = [
    ["处理组效应（构成调整）", pct(o.adjusted.effect_pct)],
    ["对照组同期变化", pct(o.control_effect_pct)],
    ["DiD 净效应", pct(o.did.effect_pct)],
    ["DiD 95% CI", o.did.ci_pct ? `${pct(o.did.ci_pct[0])} ~ ${pct(o.did.ci_pct[1])}` : "—"],
  ];
  const tb = $("did-table");
  tb.innerHTML = "";
  for (const [k, v] of rows) { const tr = el("tr"); tr.append(el("td", null, k), el("td", "num", v)); tb.appendChild(tr); }

  const yb = $("yoy-table");
  yb.innerHTML = "";
  if (r.effects.yoy) {
    const yrows = [
      ["去年同期同窗口变化", pct(r.effects.yoy.effect_pct)],
      ["去年同期 95% CI", r.effects.yoy.ci_pct ? `${pct(r.effects.yoy.ci_pct[0])} ~ ${pct(r.effects.yoy.ci_pct[1])}` : "—"],
      ["剔除季节性后净效应", pct(r.impact_range.net_of_yoy_pct)],
    ];
    for (const [k, v] of yrows) { const tr = el("tr"); tr.append(el("td", null, k), el("td", "num", v)); yb.appendChild(tr); }
  } else {
    yb.appendChild(el("tr", null, "未启用同期对照"));
  }
}

/* ---------- 敏感性 ---------- */
function renderSensitivity(r) {
  const host = $("sens-chart");
  host.innerHTML = "";
  host.style.position = "relative";
  const vs = r.sensitivity.variants;
  const base = r.impact_range.point_pct;
  const ci = r.impact_range.ci_pct;
  const lo = Math.min(...vs.map(v => v.effect_pct), ci ? ci[0] : 0, 0);
  const hi = Math.max(...vs.map(v => v.effect_pct), ci ? ci[1] : 0);
  const span = hi - lo || 1;
  const W = Math.max(720, host.clientWidth || 720), H = 120, padL = 56, padR = 24, axisY = 86;
  const x = (v) => padL + ((v - lo - span * 0.05) / (span * 1.1)) * (W - padL - padR);

  const svg = svgEl("svg", { width: W, height: H, role: "img", "aria-label": "敏感性：各参数变体的效应估计分布" });
  // CI 带
  if (ci) {
    svg.appendChild(svgEl("rect", { x: x(ci[0]), y: 18, width: x(ci[1]) - x(ci[0]), height: axisY - 26,
      fill: "var(--series-1)", opacity: 0.10 }));
  }
  // 零线
  svg.appendChild(svgEl("line", { class: "axis", x1: x(0), y1: 14, x2: x(0), y2: axisY }));
  const zt = svgEl("text", { x: x(0), y: axisY + 16, "text-anchor": "middle" });
  zt.textContent = "0";
  svg.appendChild(zt);
  // 轴与刻度
  svg.appendChild(svgEl("line", { class: "axis", x1: padL, y1: axisY, x2: W - padR, y2: axisY }));
  for (let i = 0; i <= 5; i++) {
    const v = lo - span * 0.05 + (span * 1.1 * i) / 5;
    const t = svgEl("text", { x: x(v), y: axisY + 16, "text-anchor": "middle" });
    t.textContent = pct(v, 1);
    svg.appendChild(t);
  }
  // 范围须线
  const smin = r.impact_range.sensitivity_pct[0], smax = r.impact_range.sensitivity_pct[1];
  svg.appendChild(svgEl("line", { x1: x(smin), y1: 30, x2: x(smax), y2: 30, stroke: "var(--text-muted)", "stroke-width": 1 }));
  for (const v of [smin, smax]) svg.appendChild(svgEl("line", { x1: x(v), y1: 24, x2: x(v), y2: 36, stroke: "var(--text-muted)", "stroke-width": 1 }));

  const tip = el("div", "tooltip");
  host.appendChild(tip);
  // 变体点（抖动避免重叠）
  vs.forEach((v, i) => {
    const cy = 52 + ((i * 7) % 3) * 8;
    const hit = svgEl("circle", { cx: x(v.effect_pct), cy, r: 12, fill: "transparent" });
    const dot = svgEl("circle", { cx: x(v.effect_pct), cy, r: 4, fill: "var(--series-1)",
      stroke: "var(--surface-1)", "stroke-width": 2 });
    hit.addEventListener("pointerenter", () => {
      tip.innerHTML = "";
      tip.appendChild(el("div", "tt-title", `效应 ${pct(v.effect_pct)} · DiD ${pct(v.did_pct)}`));
      tip.appendChild(el("div", null, Object.entries(v.params).map(([k, val]) => `${k}=${val}`).join("，")));
      tip.style.display = "block";
      tip.style.left = Math.min(x(v.effect_pct) + 12, W - 240) + "px";
      tip.style.top = "0px";
    });
    hit.addEventListener("pointerleave", () => { tip.style.display = "none"; });
    svg.append(dot, hit);
  });
  // 基准点
  svg.appendChild(svgEl("circle", { cx: x(base), cy: 30, r: 6, fill: "var(--series-2)",
    stroke: "var(--surface-1)", "stroke-width": 2 }));
  const bt = svgEl("text", { x: x(base), y: 14, "text-anchor": "middle", fill: "var(--text-secondary)" });
  bt.textContent = `基准 ${pct(base)}`;
  svg.appendChild(bt);
  host.appendChild(svg);

  // 变体表
  $("sens-count").textContent = `${vs.length} 组`;
  const tb = $("sens-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const v of [...vs].sort((a, b) => a.effect_pct - b.effect_pct)) {
    const tr = el("tr");
    tr.append(
      el("td", "num", String(v.params.matching_window_weeks)),
      el("td", null, v.params.missing_policy),
      el("td", null, v.params.event_policy),
      el("td", null, v.params.backfill_policy),
      el("td", null, v.params.caliber_policy),
      el("td", "num", pct(v.effect_pct)),
      el("td", "num", pct(v.did_pct)),
    );
    tb.appendChild(tr);
  }
}

/* ---------- 事件与口径 ---------- */
function renderEvents(r) {
  const tb = $("events-table").querySelector("tbody");
  tb.innerHTML = "";
  for (const e of r.events) {
    const tr = el("tr");
    tr.append(
      el("td", null, e.name), el("td", null, e.type),
      el("td", null, `${e.start} ~ ${e.end}`),
      el("td", "num", String(e.days_in_post_window)),
      el("td", "num", e.excess_pct != null ? `${pct(e.excess_pct)}（${fmtNum(e.excess_abs)}）` : "—"),
    );
    tb.appendChild(tr);
  }
  const info = $("caliber-info");
  info.innerHTML = "";
  const c = r.caliber;
  if (!c.changed) {
    info.appendChild(el("p", "note", `指标 ${c.metric} 窗口内口径一致（${c.calibers.join("/")}），无需调整。`));
  } else {
    const ul = el("ul", "check-list");
    const items = [
      `口径切换日：${c.switch_date}（${c.calibers.join(" → ")}）`,
      `调整策略：${c.policy}` + (c.adjustment_factor ? `，换算因子 ${c.adjustment_factor.toFixed(4)}（${c.method}，重叠 ${c.overlap_days} 天）` : ""),
      `重标定行数：${c.rescaled_rows}`,
    ];
    for (const it of items) { const li = el("li"); li.append(el("span", null, it)); ul.appendChild(li); }
    info.appendChild(ul);
    for (const [cal, def] of Object.entries(c.definitions || {})) {
      info.appendChild(el("p", "note", `${cal}：${def.definition}（${def.effective_from} ~ ${def.effective_to || "至今"}）`));
    }
  }
}

/* ---------- 证据链 ---------- */
function renderEvidence(r) {
  const ol = $("evidence-list");
  ol.innerHTML = "";
  for (const s of r.evidence) {
    const li = el("li");
    li.append(el("span", null, s.name), el("span", "ev-hash", "#" + s.hash));
    const det = el("div", "ev-detail", JSON.stringify(s.detail));
    li.appendChild(det);
    ol.appendChild(li);
  }
  const h = $("run-hashes");
  h.textContent = `结果哈希 ${r.result_hash} · 参数哈希 ${r.param_hash} · 数据版本 ${r.data_version.version} (内容哈希 ${r.data_version.content_hash.slice(0, 12)}) · 代码 ${r.code_version} · 相同参数+数据版本重跑应得到相同结果哈希`;
}

boot().catch(e => showError(e.message));
