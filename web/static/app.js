/* 年报问答前端：流式接答案 + 指标浏览。没有构建步骤，改完刷新即可。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmt = (v) => {
    if (v === null || v === undefined || v === "") return "—";
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    return n.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  };

  /* ---------- 标签页 ---------- */
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".panel").forEach((p) =>
        p.classList.toggle("active", p.id === "panel-" + btn.dataset.panel));
      if (btn.dataset.panel === "browse" && !browse.loaded) browse.load();
    });
  });

  /* ---------- 健康状态 / 示例 ---------- */
  fetch("/api/health").then((r) => r.json()).then((d) => {
    $("health").textContent =
      `${d.companies} 家公司 · ${d.chunks} chunk · 指标 ${d.metrics} 条` +
      (d.bm25_ready ? "" : " · BM25 预热中");
  }).catch(() => { $("health").textContent = "服务未就绪"; });

  fetch("/api/demo").then((r) => r.json()).then((d) => {
    $("demo-chips").innerHTML = d.items.map((it) =>
      `<button class="chip" type="button">${esc(it.q)}</button>`).join("");
    $("demo-chips").querySelectorAll(".chip").forEach((c) =>
      c.addEventListener("click", () => {
        $("question").value = c.textContent;
        $("ask-form").requestSubmit();
      }));
  }).catch(() => {});

  /* ---------- 问答 ---------- */
  let asking = false;

  $("ask-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (asking) return;
    const question = $("question").value.trim();
    if (!question) return;

    asking = true;
    $("ask-btn").disabled = true;
    $("empty-hint").classList.add("hidden");
    $("result").classList.remove("hidden");
    $("answer").textContent = "";
    $("citations").innerHTML = "";
    $("evidence").innerHTML = "";
    $("facts").innerHTML = "";
    $("intent").innerHTML = "";
    $("meta-right").textContent = "";
    $("ev-count").textContent = "";
    $("answer").innerHTML = '<span class="cursor">▍</span>';

    let buf = "";
    let done = false;

    try {
      const resp = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let pending = "";

      while (true) {
        const { value, done: finished } = await reader.read();
        if (finished) break;
        pending += decoder.decode(value, { stream: true });

        let cut;
        while ((cut = pending.indexOf("\n\n")) >= 0) {
          const block = pending.slice(0, cut);
          pending = pending.slice(cut + 2);
          const ev = parseEvent(block);
          if (!ev) continue;
          if (ev.event === "meta") renderMeta(ev.data);
          else if (ev.event === "delta") {
            buf += ev.data.t || "";
            $("answer").textContent = buf;
            const cur = document.createElement("span");
            cur.className = "cursor";
            cur.textContent = "▍";
            $("answer").appendChild(cur);
          } else if (ev.event === "done") {
            done = true;
            finish(ev.data, buf);
          }
        }
      }
      if (!done) finish({}, buf);
    } catch (err) {
      $("answer").textContent = "请求失败：" + err.message;
    } finally {
      asking = false;
      $("ask-btn").disabled = false;
    }
  });

  function parseEvent(block) {
    let event = "message", data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!data) return null;
    try { return { event, data: JSON.parse(data) }; } catch { return null; }
  }

  function renderMeta(m) {
    const badges = [];
    if (m.company) badges.push(`公司 ${esc(m.company)}`);
    if (m.metric) badges.push(`指标 ${esc(m.metric)}`);
    if (m.qtype) badges.push(`类型 ${esc(m.qtype)}`);
    $("intent").innerHTML = badges.map((b) => `<span class="badge">${b}</span>`).join("");

    // 结构化事实：数值题最有价值的部分
    const facts = (m.facts || []).filter((f) => !f.rank);
    const rank = (m.ranking || []);
    let html = "";
    if (facts.length) {
      html += '<div class="src">已核实的结构化数值（取自年报标准表）</div><table>' +
        facts.map((f) => `<tr>
          <td>${esc(f.metric)}</td>
          <td class="num">${fmt(f.value)} <span class="unit">${esc(f.unit || "")}</span></td>
          <td class="src">${esc(f.year)} 年 · 第 ${esc(f.page || "?")} 页
            <span class="badge-src badge-${esc(f.source || "summary")}">${esc(f.source || "")}</span></td>
        </tr>`).join("") + "</table>";
    }
    if (rank.length) {
      const n = rank[0].n_total, all = rank[0].n_companies_total;
      html += `<div class="src">跨公司排序（${n} 家可排序${all && n < all ? `，共 ${all} 家` : ""}）</div><table>` +
        rank.map((f) => `<tr>
          <td>第 ${f.rank} 名</td>
          <td>${esc(f.name)}</td>
          <td class="num">${fmt(f.value)} <span class="unit">${esc(f.unit || "")}</span></td>
          <td class="src">第 ${esc(f.page || "?")} 页</td>
        </tr>`).join("") + "</table>";
    }
    $("facts").innerHTML = html;
    $("facts").classList.toggle("hidden", !html);

    // 证据卡片
    const evs = m.evidence || [];
    $("ev-count").textContent = evs.length ? ` 共 ${evs.length} 条` : "";
    evFull = {};
    $("evidence").innerHTML = evs.map((e) => {
      evFull[e.n] = e.full || e.snippet || "";
      return `
      <div class="ev" id="ev-${e.n}" data-n="${e.n}">
        <div class="ev-head">
          <span class="ev-n">[${e.n}]</span>
          <span>${esc(e.name)}${esc(e.year)}年报</span>
          <span class="ev-meta">p.${e.page ?? "?"} · ${e.kind === "table" ? "表格" : "正文"} · §${esc(e.section || "无章节")} · 得分 ${e.score}</span>
        </div>
        <div class="ev-snippet">${renderRich(e.snippet)}</div>
        ${(e.length > 400 || e.snippet !== evFull[e.n]) ? '<span class="ev-more">展开全文</span>' : ""}
      </div>`;
    }).join("");

    $("evidence").querySelectorAll(".ev-more").forEach((el) =>
      el.addEventListener("click", () => {
        const card = el.parentElement;
        const box = card.querySelector(".ev-snippet");
        const expanded = box.dataset.expanded === "1";
        if (expanded) {
          box.innerHTML = renderRich(card.dataset.snippet || "");
          box.dataset.expanded = "0";
          box.classList.remove("open");
          el.textContent = "展开全文";
        } else {
          card.dataset.snippet = card.dataset.snippet || box.innerHTML;
          box.innerHTML = renderRich(evFull[card.dataset.n] || "");
          box.dataset.expanded = "1";
          box.classList.add("open");
          el.textContent = "收起";
        }
      }));
  }

  function finish(d, buf) {
    const text = (d.text || buf || "").trim();
    $("answer").innerHTML = renderRich(text, true);   // 答案里也可能有表格
    bindCites($("answer"));

    const cites = d.citations || [];
    $("citations").innerHTML = cites.length
      ? cites.map((c) => `<div>[${c.n}] ${esc(c.name)}${esc(c.year)}年报 p.${c.page} §${esc((c.section || "").split(" > ").pop())} <span class="muted">${esc(c.chunk_id)}</span></div>`).join("")
      : "";

    const bits = [];
    if (d.elapsed_ms) bits.push(`${(d.elapsed_ms / 1000).toFixed(1)} 秒`);
    if (d.ok === false) bits.push("生成未完成");
    $("meta-right").textContent = bits.join(" · ");
  }

  /* ---------- 轻量 markdown 渲染（只支持管道表、粗体、引用角标） ----------
     证据里的表格正文本来就是 markdown 管道表（`| a | b |` + `|---|`），
     之前整段当纯文本转义，页面上就是一堆竖线。这里把它渲染成真表格。 */

  let evFull = {};   // 证据编号 → 完整正文，供「展开全文」用

  const SEP_CELL = /^:?-{2,}:?$/;

  function isSep(line) {
    const t = line.trim();
    if (!t.includes("-") || !t.includes("|")) return false;
    const cells = splitRow(t);
    return cells.length > 0 && cells.every((c) => SEP_CELL.test(c));
  }

  // 是不是表格的起始行：表头行 + 下一行是分隔行
  function isTableStart(lines, i) {
    if (!lines[i] || !lines[i].trim().startsWith("|")) return false;
    return i + 1 < lines.length && isSep(lines[i + 1]);
  }

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  }

  // 行内：转义 → 粗体 → 引用角标
  function inline(s, withCites) {
    let h = esc(s).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    if (withCites) {
      h = h.replace(/\[(\d{1,2})\]/g,
        (m, n) => `<sup class="cite" data-n="${n}">[${n}]</sup>`);
    }
    return h;
  }

  function renderTable(block, withCites) {
    const head = splitRow(block[0]);
    const body = block.slice(2).map(splitRow);
    const ths = head.map((c) => `<th>${inline(c, withCites)}</th>`).join("");
    const trs = body.map((r) => "<tr>" +
      r.map((c) => `<td>${inline(c, withCites)}</td>`).join("") + "</tr>").join("");
    return `<table class="md-table"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
  }

  function renderRich(text, withCites) {
    const lines = String(text ?? "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      if (isTableStart(lines, i)) {
        const block = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          block.push(lines[i]);
          i++;
        }
        out.push(renderTable(block, withCites));
      } else {
        const buf = [];
        while (i < lines.length && !isTableStart(lines, i)) {
          buf.push(lines[i]);
          i++;
        }
        out.push(inline(buf.join("\n"), withCites));
      }
    }
    return out.join("");
  }

  function bindCites(root) {
    root.querySelectorAll("sup.cite").forEach((sup) =>
      sup.addEventListener("click", () => {
        const card = document.getElementById("ev-" + sup.dataset.n);
        if (!card) return;
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("hit");
        setTimeout(() => card.classList.remove("hit"), 1600);
      }));
  }

  /* ---------- 指标浏览 ---------- */
  const browse = {
    loaded: false, metrics: [], rows: [], sortKey: null, sortDir: -1,

    async load() {
      const sel = $("year");
      const first = await fetch("/api/metrics?year=2025").then((r) => r.json());
      sel.innerHTML = first.available_years.map((y) =>
        `<option value="${y}"${y === 2025 ? " selected" : ""}>${y}</option>`).join("");
      sel.addEventListener("change", () => this.render(Number(sel.value)));
      this.apply(first);
      this.loaded = true;
    },

    async render(year) {
      const d = await fetch("/api/metrics?year=" + year).then((r) => r.json());
      this.apply(d);
    },

    apply(d) {
      this.metrics = d.metrics;
      this.rows = d.rows;
      this.year = d.year;
      this.sortKey = null;
      this.draw();
    },

    draw() {
      const filter = ($("company-filter").value || "").trim().toLowerCase();
      let rows = this.rows.filter((r) =>
        !filter || r.name.toLowerCase().includes(filter) || r.code.includes(filter));

      if (this.sortKey) {
        const key = this.sortKey;
        rows = rows.slice().sort((a, b) => {
          const av = cellVal(a, key), bv = cellVal(b, key);
          if (av === null && bv === null) return 0;
          if (av === null) return 1;
          if (bv === null) return -1;
          return (av - bv) * this.sortDir;
        });
      }

      $("metrics-head").innerHTML = "<th>公司</th>" +
        this.metrics.map((m) => `<th data-m="${esc(m)}" class="${this.sortKey === m ? "sorted" : ""}">${esc(m)}</th>`).join("");

      $("metrics-body").innerHTML = rows.map((r) => "<tr><td>" +
        esc(r.name) + ' <span class="muted">' + esc(r.code) + "</span></td>" +
        this.metrics.map((m) => {
          const c = r.cells[m];
          if (!c) return '<td class="miss">—</td>';
          const badge = c.src === "statement"
            ? '<span class="badge-src badge-statement" title="来自正式报表兜底，非标准表">报</span>' : "";
          const warn = c.suspect
            ? `<span class="badge-src badge-warn" title="${esc(c.suspect)}">?</span>` : "";
          return `<td class="num${c.suspect ? " suspect" : ""}" title="第 ${esc(c.page || "?")} 页 · ${esc(c.src)}">${fmt(c.v)}` +
            `<span class="unit">${esc(c.unit || "")}</span>${badge}${warn}</td>`;
        }).join("") + "</tr>").join("");

      $("browse-count").textContent = `${rows.length} / ${this.rows.length} 家 · ${this.year} 年`;

      $("metrics-head").querySelectorAll("th[data-m]").forEach((th) =>
        th.addEventListener("click", () => {
          const m = th.dataset.m;
          if (this.sortKey === m) this.sortDir = -this.sortDir;
          else { this.sortKey = m; this.sortDir = -1; }
          this.draw();
        }));
    },
  };

  const cellVal = (row, metric) => {
    const c = row.cells[metric];
    return c ? c.vb : null;            // 按折算成「元」的值排序，跨单位才可比
  };

  $("company-filter").addEventListener("input", () => browse.draw());
})();
