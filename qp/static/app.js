/* QuoteProof UI behaviour. Same-origin script (CSP unchanged). All page data is inserted with textContent, never innerHTML. */
(function () {
  "use strict";
  var KEY = "qp.comparisons.v1";

  // ---------- small helpers ----------
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "cls") e.className = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c != null) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return e;
  }
  function icon(name, cls) {
    var ns = "http://www.w3.org/2000/svg", s = document.createElementNS(ns, "svg"), u = document.createElementNS(ns, "use");
    s.setAttribute("class", "icon " + (cls || "")); s.setAttribute("aria-hidden", "true"); u.setAttribute("href", "#i-" + name);
    s.appendChild(u); return s;
  }
  function chip(cls, text, ic) { return el("span", { cls: "chip " + cls }, [ic ? icon(ic) : null, text]); }
  function load() { try { var v = JSON.parse(localStorage.getItem(KEY) || "[]"); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
  function save(list) { try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, 50))); } catch (e) { /* storage blocked: pages still work */ } }
  var UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;
  function expiresIn(t) {
    var left = t - Date.now() / 1000; if (left <= 0) return "expired";
    var h = Math.floor(left / 3600), m = Math.floor(left % 3600 / 60); return h ? h + " h " + m + " min" : m + " min";
  }

  // ---------- mobile navigation drawer ----------
  function drawer() {
    var b = document.getElementById("nav-toggle"), scrim = document.getElementById("scrim"), side = document.getElementById("sidebar");
    if (!b || !side) return;
    function set(open) {
      document.body.classList.toggle("nav-open", open); b.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) { var first = side.querySelector("a"); if (first) first.focus(); } else b.focus();
    }
    b.addEventListener("click", function () { set(!document.body.classList.contains("nav-open")); });
    if (scrim) scrim.addEventListener("click", function () { set(false); });
    document.addEventListener("keydown", function (e) {
      if (!document.body.classList.contains("nav-open")) return;
      if (e.key === "Escape") set(false);
      if (e.key === "Tab") { // keep focus inside the open drawer
        var f = side.querySelectorAll("a"), first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
  }

  // ---------- browser-side comparison list ----------
  function remember() {
    var n = document.getElementById("qp-cmp"); if (!n) return;
    var c; try { c = JSON.parse(n.textContent); } catch (e) { return; }
    var list = load().filter(function (x) { return x.id !== c.id; });
    var prev = load().filter(function (x) { return x.id === c.id; })[0] || {};
    list.unshift({ id: c.id, name: c.name, currency: c.currency, expires_at: c.expires_at, opened: Date.now(), s: prev.s || null });
    save(list);
  }
  function queueBadge() {
    var b = document.getElementById("nav-queue-count"); if (!b) return;
    var n = load().reduce(function (t, x) { return t + ((x.s && x.s.open_findings) || 0); }, 0);
    if (n) { b.textContent = String(n); b.hidden = false; } else b.hidden = true;
  }
  function refreshAll() {
    var list = load();
    return Promise.all(list.map(function (x) {
      return fetch("/p/" + x.id + "/summary", { cache: "no-store" }).then(function (r) {
        if (r.status === 404) return { id: x.id, gone: true };
        if (!r.ok) return { id: x.id, s: x.s, stale: true };
        return r.json().then(function (s) { return { id: x.id, s: s }; });
      }).catch(function () { return { id: x.id, s: x.s, stale: true }; });
    })).then(function (res) {
      var by = {}; res.forEach(function (r) { by[r.id] = r; });
      var kept = list.filter(function (x) { return !(by[x.id] && by[x.id].gone); }).map(function (x) {
        var r = by[x.id]; if (r && r.s) { x.s = r.s; x.name = r.s.name; x.currency = r.s.currency; x.expires_at = r.s.expires_at; }
        x.stale = !!(r && r.stale); return x;
      });
      save(kept); queueBadge(); return kept;
    });
  }

  function gateChip(s) {
    if (!s) return chip("neutral", "Unavailable");
    if (!s.documents) return chip("neutral", "No documents yet");
    if (s.assurance.headline === "Complete") return chip("ok", "Complete", "check");
    return chip("warn", s.assurance.headline, "clock");
  }
  function layerChip(n, label, ok, total, warnCls) {
    var cls = !total ? "neutral" : (ok === total ? "ok" : warnCls);
    return el("span", { cls: "chip " + cls, title: label }, [String(ok) + "/" + String(total)]);
  }
  function emptyState(box, title, text) {
    box.textContent = "";
    box.appendChild(el("div", { cls: "empty" }, [icon("folder"), el("h3", { text: title }), el("p", { text: text }),
      el("div", { cls: "btn-row" }, [el("a", { cls: "btn", href: "/new" }, [icon("plus"), "New comparison"])])]));
  }

  function renderComparisons(list, box, compact) {
    box.textContent = "";
    if (!list.length) return emptyState(box, "No comparisons in this browser yet",
      "Create one, or add one you opened elsewhere with its link.");
    var t = el("table", { cls: "stack-mobile" }), thead = el("thead"), tb = el("tbody");
    var cols = compact ? ["Comparison", "Docs", "Report", "Expires"] : ["Comparison", "Currency", "Docs", "1 · Source", "2 · Arithmetic", "3 · Equivalence", "Report", "Expires", ""];
    var hr = el("tr"); cols.forEach(function (h) { hr.appendChild(el("th", { cls: h === "Docs" ? "num" : "", text: h })); });
    thead.appendChild(hr); t.appendChild(thead); t.appendChild(tb);
    list.forEach(function (x) {
      var s = x.s, a = s && s.assurance, tr = el("tr");
      tr.appendChild(el("td", { "data-l": "Comparison" }, [el("a", { href: "/p/" + x.id, cls: "strong", text: x.name || "Untitled comparison" }),
        compact ? el("span", { cls: "cur", style: "margin-left:8px", text: x.currency || "" }) : null,
        s && s.processing ? el("div", { cls: "small muted" }, ["Processing " + s.processing + " document(s)…"]) : null,
        x.stale ? el("div", { cls: "small muted", text: "Could not refresh just now" }) : null]));
      if (!compact) tr.appendChild(el("td", { "data-l": "Currency" }, [el("span", { cls: "cur", text: x.currency || "" })]));
      tr.appendChild(el("td", { cls: "num", "data-l": "Documents", text: s ? String(s.documents) : "–" }));
      if (compact) {
        tr.appendChild(el("td", { "data-l": "Report status" }, [gateChip(s)]));
        tr.appendChild(el("td", { "data-l": "Expires", cls: "nowrap small", text: x.expires_at ? expiresIn(x.expires_at) : "" }));
        tb.appendChild(tr); return;
      }
      tr.appendChild(el("td", { "data-l": "Source verification" }, [a ? layerChip(1, "Source verification", a.source.verified, a.source.total, "warn") : null]));
      tr.appendChild(el("td", { "data-l": "Arithmetic reconciliation" }, [a ? layerChip(2, "Arithmetic reconciliation", a.arithmetic.reconciled, a.arithmetic.total, "bad") : null]));
      tr.appendChild(el("td", { "data-l": "Human-confirmed equivalence" }, [a ? el("span", { cls: "chip " + (!a.equivalence.total_items ? "neutral" : (a.equivalence.confirmed_items && !a.equivalence.proposed_groups ? "ok" : "warn")), text: a.equivalence.confirmed_items + "/" + a.equivalence.total_items + " confirmed" }) : null]));
      tr.appendChild(el("td", { "data-l": "Report status" }, [gateChip(s)]));
      tr.appendChild(el("td", { "data-l": "Expires", cls: "nowrap small", text: x.expires_at ? expiresIn(x.expires_at) : "" }));
      var forget = el("button", { cls: "ghost sm", type: "button", title: "Remove from this browser's list (does not delete the comparison)" }, [icon("trash", "sm"), "Forget"]);
      forget.addEventListener("click", function () { save(load().filter(function (y) { return y.id !== x.id; })); renderComparisons(load(), box); queueBadge(); });
      tr.appendChild(el("td", { cls: "nowrap" }, [el("a", { cls: "btn secondary sm", href: "/p/" + x.id + "/report" }, ["Report"]), " ", forget]));
      tb.appendChild(tr);
    });
    box.appendChild(el("div", { cls: "table-wrap" }, [t]));
  }

  function renderDashboard(list) {
    var set = function (id, v) { var n = document.getElementById(id); if (n) n.textContent = v; };
    var open = 0, sec = 0, drafts = 0, done = 0, proposals = 0, processing = 0;
    list.forEach(function (x) {
      var s = x.s; if (!s) return;
      open += s.open_findings; sec += s.security_open; processing += s.processing;
      proposals += s.assurance.equivalence.proposed_groups;
      if (s.assurance.headline === "Complete") done++; else drafts++;
    });
    set("st-count", String(list.length)); set("st-open", String(open)); set("st-sec", sec ? sec + " security finding(s) among them" : "No security findings open");
    set("st-reports", done + " / " + list.length); set("st-drafts", drafts + " draft" + (drafts === 1 ? "" : "s") + (processing ? " · " + processing + " processing" : ""));
    var openTile = document.getElementById("tile-open"); if (openTile) openTile.classList.toggle(sec ? "alert" : "attn", open > 0);
    var att = document.getElementById("attention"); if (att) {
      att.textContent = "";
      var need = list.filter(function (x) { return x.s && (x.s.open_findings || x.s.assurance.equivalence.proposed_groups || x.s.processing); })
        .sort(function (a, b) { return (b.s.security_open - a.s.security_open) || (b.s.open_findings - a.s.open_findings); });
      if (!need.length) att.appendChild(el("div", { cls: "empty" }, [icon("check"), el("h3", { text: list.length ? "Nothing needs your attention" : "No comparisons yet" }),
        el("p", { text: list.length ? "Every comparison in this browser is processed and reviewed." : "Start a comparison to upload supplier quotes." })]));
      need.slice(0, 6).forEach(function (x) {
        var s = x.s, why = [];
        if (s.security_open) why.push(chip("bad", s.security_open + " security finding" + (s.security_open === 1 ? "" : "s"), "shield-alert"));
        if (s.open_findings - s.security_open > 0) why.push(chip("warn", (s.open_findings - s.security_open) + " item" + (s.open_findings - s.security_open === 1 ? "" : "s") + " to review", "alert"));
        if (s.assurance.equivalence.proposed_groups) why.push(chip("warn", s.assurance.equivalence.proposed_groups + " match proposal(s)", "scale"));
        if (s.processing) why.push(chip("info", "Processing", "clock"));
        att.appendChild(el("div", { cls: "spread", style: "padding:14px 20px;border-bottom:1px solid var(--line)" }, [
          el("div", {}, [el("a", { href: "/p/" + x.id, cls: "strong", text: x.name }), el("div", { cls: "row", style: "margin-top:6px;gap:6px" }, why)]),
          el("a", { cls: "btn secondary sm", href: "/p/" + x.id + (s.processing ? "" : "/review") }, [s.processing ? "View progress" : "Review now", icon("arrow", "sm")])]));
      });
    }
    var rec = document.getElementById("recent"); if (rec) renderComparisons(list.slice(0, 5), rec, true);
  }

  function renderQueue(list, box) {
    box.textContent = "";
    var items = list.filter(function (x) { return x.s && (x.s.open_findings || x.s.assurance.equivalence.proposed_groups); })
      .sort(function (a, b) { return (b.s.security_open - a.s.security_open) || (b.s.open_findings - a.s.open_findings); });
    if (!items.length) {
      box.appendChild(el("div", { cls: "empty" }, [icon("inbox"), el("h3", { text: "Your review queue is empty" }),
        el("p", { text: list.length ? "No open findings or match proposals in the comparisons in this browser." : "Comparisons you open in this browser appear here when they need a decision." })]));
      return;
    }
    items.forEach(function (x) {
      var s = x.s, rows = [];
      if (s.security_open) rows.push(el("div", { cls: "callout bad", style: "padding:10px 12px" }, [icon("shield-alert"),
        el("div", {}, [el("h4", { text: s.security_open + " security finding" + (s.security_open === 1 ? "" : "s") }), el("p", { text: "Possible prompt injection detected in an uploaded quote. Confirm or dismiss with a reason." })])]));
      var stats = el("div", { cls: "row", style: "gap:6px" }, [
        s.open_findings - s.security_open > 0 ? chip("warn", (s.open_findings - s.security_open) + " verification finding(s)", "alert") : null,
        s.assurance.equivalence.proposed_groups ? chip("warn", s.assurance.equivalence.proposed_groups + " AI match proposal(s) to confirm", "scale") : null,
        gateChip(s)]);
      box.appendChild(el("div", { cls: "card", style: "display:flex;flex-direction:column;gap:12px" }, [
        el("div", { cls: "spread" }, [el("div", {}, [el("a", { href: "/p/" + x.id, cls: "strong", text: x.name }), " ", el("span", { cls: "cur", text: x.currency })]),
          el("a", { cls: "btn sm", href: "/p/" + x.id + "/review" + (s.open_findings ? "#findings" : "#matches") }, ["Open Human Review", icon("arrow", "sm")])]),
        rows[0] || null, stats]));
    });
  }

  function globalPages() {
    var mode = document.body.getAttribute("data-page"); if (!mode) return;
    var box = document.getElementById("list");
    refreshAll().then(function (list) {
      if (mode === "dashboard") renderDashboard(list);
      if (mode === "comparisons") renderComparisons(list, box);
      if (mode === "queue") renderQueue(list, box);
    });
    var f = document.getElementById("add-by-link");
    if (f) f.addEventListener("submit", function (e) {
      e.preventDefault();
      var msg = document.getElementById("add-msg"), m = (f.elements.link.value || "").toLowerCase().match(UUID);
      if (!m) { msg.textContent = "That doesn't look like a QuoteProof comparison link."; msg.className = "small"; msg.style.color = "var(--bad)"; return; }
      fetch("/p/" + m[0] + "/summary", { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error(r.status === 404 ? "No comparison found at that link (it may have expired)." : "Could not load that comparison.");
        return r.json();
      }).then(function (s) {
        var list = load().filter(function (x) { return x.id !== s.id; });
        list.unshift({ id: s.id, name: s.name, currency: s.currency, expires_at: s.expires_at, opened: Date.now(), s: s }); save(list);
        msg.textContent = "Added “" + s.name + "”."; msg.style.color = "var(--ok)"; f.reset(); globalPages.rerun();
      }).catch(function (err) { msg.textContent = err.message; msg.style.color = "var(--bad)"; });
    });
  }
  globalPages.rerun = function () {
    var mode = document.body.getAttribute("data-page"), box = document.getElementById("list"), list = load();
    if (mode === "dashboard") renderDashboard(list); if (mode === "comparisons") renderComparisons(list, box); if (mode === "queue") renderQueue(list, box);
  };

  // ---------- upload drop zone ----------
  function dropzones() {
    document.querySelectorAll(".drop").forEach(function (z) {
      var inp = z.querySelector("input[type=file]"), out = z.querySelector(".chosen");
      ["dragenter", "dragover"].forEach(function (ev) { z.addEventListener(ev, function () { z.classList.add("over"); }); });
      ["dragleave", "drop"].forEach(function (ev) { z.addEventListener(ev, function () { z.classList.remove("over"); }); });
      if (inp && out) inp.addEventListener("change", function () { out.textContent = inp.files.length ? "Selected: " + inp.files[0].name : ""; });
    });
  }

  // ---------- processing timeline (same /events polling and reload rule as before) ----------
  var TITLES = {
    upload: "File received", sandbox_index: "Indexed inside the sandbox", index_validation: "Index validated by trusted code",
    injection_check: "Prompt-injection check", llm_write_extractor: "AI writes an extractor (Vultr Serverless Inference)",
    sandbox_run_extractor: "AI-written extractor runs in the sandbox", extraction_schema: "Output schema check",
    verification_feedback: "Verification feedback", layer1_source: "Layer 1 · Source verification",
    layer2_arithmetic: "Layer 2 · Arithmetic reconciliation", ai_matching: "AI item-matching proposals", explain: "AI summary"
  };
  var KEYS = ["attempt", "model", "runtime", "container_id", "exit_code", "status", "duration_ms", "timed_out", "oom_killed", "removed", "line_items", "charges", "error", "reason", "flags", "pattern_hits", "classifier_hits", "amounts_verified", "amounts_total", "unaccounted", "document_currency", "comparable", "checks", "proposed_groups", "test", "bytes", "isolation_mode", "note", "latency_ms"];
  function raw(d) {
    var out = []; KEYS.forEach(function (k) {
      if (d[k] !== undefined && d[k] !== null && d[k] !== "") { var v = d[k]; if (k === "container_id" && typeof v === "string") v = v.slice(0, 12); if (typeof v === "object") v = JSON.stringify(v); out.push(k + "=" + v); }
    }); return out.join(" · ");
  }
  function facts(step, d) {
    var f = [];
    if (d.attempt) f.push("attempt " + d.attempt);
    if (d.model) f.push("model " + d.model);
    if (d.runtime) f.push(d.runtime === "runsc" ? "gVisor (runsc)" : "runtime " + d.runtime);
    if (d.exit_code !== undefined && d.exit_code !== null) f.push("exit " + d.exit_code);
    if (d.duration_ms !== undefined) f.push(d.duration_ms + " ms");
    if (d.latency_ms !== undefined) f.push(d.latency_ms + " ms");
    if (d.container_id) f.push("container " + String(d.container_id).slice(0, 12));
    if (d.removed === true) f.push("container removed");
    if (d.timed_out === true) f.push("timed out");
    if (d.oom_killed === true) f.push("out of memory");
    if (d.pattern_hits !== undefined) f.push(d.pattern_hits + " pattern hit(s)");
    if (d.classifier_hits !== undefined) f.push(d.classifier_hits + " classifier hit(s)");
    if (d.line_items !== undefined) f.push(d.line_items + " line items, " + (d.charges || 0) + " charges");
    if (d.amounts_total !== undefined) f.push(d.amounts_verified + "/" + d.amounts_total + " amounts match source");
    if (d.unaccounted !== undefined) f.push(d.unaccounted + " unaccounted");
    if (d.document_currency) f.push("currency " + d.document_currency + (d.comparable === false ? " (other currency)" : ""));
    if (step === "layer2_arithmetic" && d.status) f.push(d.status === "RECONCILES" ? "all checks reconcile" : "status " + d.status);
    if (d.proposed_groups !== undefined) f.push(d.proposed_groups + " group(s) proposed");
    if (d.bytes !== undefined) f.push(d.bytes + " bytes");
    if (d.reason) f.push(String(d.reason));
    if (d.error) f.push(String(d.error));
    return f.join(" · ");
  }
  function cls(s) { return ["done", "passed", "clean", "PASS", "received"].indexOf(s) >= 0 ? "ok" : (["failed", "rejected", "FAIL", "flagged"].indexOf(s) >= 0 ? "bad" : "warn"); }
  function timeline() {
    var box = document.getElementById("timeline"); if (!box) return;
    var pid = box.getAttribute("data-pid"), after = 0, sawRunning = false, groups = {}, live = document.getElementById("tl-live");
    var empty = document.getElementById("tl-empty");
    function group(name) {
      if (groups[name]) return groups[name];
      var d = el("details", { cls: "doc-run" }), sm = el("summary"), ul = el("ol", { cls: "steps" });
      var badge = el("span", { cls: "chip neutral", text: "…" }), last = el("span", { cls: "small muted", text: "" });
      sm.appendChild(icon(name === "Comparison" ? "layers" : "file")); sm.appendChild(el("span", { cls: "name", text: name }));
      sm.appendChild(badge); sm.appendChild(last); sm.appendChild(icon("chevron", "chev"));
      d.appendChild(sm); d.appendChild(ul); box.appendChild(d);
      var g = { d: d, ul: ul, badge: badge, last: last, flagged: false, failed: false, done: false, user: false, pending: {} };
      sm.addEventListener("click", function () { g.user = true; });
      groups[name] = g; return g;
    }
    function add(e) {
      var g = group(e.document || "Comparison"), c = cls(e.status), d = e.detail || {};
      var key = e.step + "|" + (d.attempt || ""), r = raw(d), line = e.step + " " + e.status + (r ? " · " + r : "");
      var prev = g.pending[key];
      // a step's "started" row is completed in place by its result, so each step reads as one line; raw evidence keeps both events
      if (prev && e.status !== "started") { prev.li.remove(); delete g.pending[key]; }
      var li = el("li", { cls: "step " + c });
      li.appendChild(el("span", { cls: "node" }, [icon(c === "ok" ? "check" : (c === "bad" ? (e.step === "injection_check" ? "shield-alert" : "x") : "clock"))]));
      var body = el("div", {}, [el("div", { cls: "st" }, [el("b", { text: TITLES[e.step] || e.step }), el("span", { cls: "chip " + c, text: e.status }),
        el("span", { cls: "time", text: new Date(e.ts * 1000).toLocaleTimeString() })])]);
      var fx = facts(e.step, d); if (fx) body.appendChild(el("div", { cls: "facts", text: fx }));
      var lines = (prev ? prev.lines : []).concat([line]);
      body.appendChild(el("details", { cls: "raw" }, [el("summary", { text: "Show raw evidence" }), el("code", { text: lines.join("\n") })]));
      li.appendChild(body); g.ul.appendChild(li);
      if (e.status === "started") g.pending[key] = { li: li, lines: lines };
      if (e.status === "flagged") g.flagged = true;
      if (c === "bad" && e.status !== "flagged") g.failed = true;
      if (e.step === "layer2_arithmetic" || (e.step === "ai_matching" && e.status !== "started") || g.failed) g.done = true;
      g.last.textContent = TITLES[e.step] || e.step;
      if (g.failed) { g.badge.className = "chip bad"; g.badge.textContent = "Failed"; }
      else if (g.flagged) { g.badge.className = "chip bad"; g.badge.textContent = "Injection flagged"; }
      else if (g.done) { g.badge.className = "chip ok"; g.badge.textContent = e.document ? "Verified" : "Done"; }
      else { g.badge.className = "chip info"; g.badge.textContent = "In progress"; }
    }
    function settle() { // open what needs attention or is still running; collapse finished, clean documents
      Object.keys(groups).forEach(function (k) { var g = groups[k]; if (!g.user) g.d.open = g.flagged || g.failed || !g.done; });
    }
    function poll() {
      fetch("/p/" + pid + "/events?after=" + after, { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
        j.events.forEach(function (e) { after = Math.max(after, e.id); add(e); });
        settle();
        if (empty) empty.hidden = !!Object.keys(groups).length;
        if (live) live.hidden = !j.jobs_running;
        // refresh the page once, when processing that this page saw running has finished
        if (j.jobs_running) { sawRunning = true; } else if (sawRunning) { sawRunning = false; setTimeout(function () { location.reload(); }, 800); return; }
        setTimeout(poll, 1500);
      }).catch(function () { setTimeout(poll, 1500); });
    }
    poll();
  }

  // ---------- reviewer name: copied into every review form on submit (unchanged behaviour) ----------
  function reviewerForms() {
    var rv = document.getElementById("rv"); if (!rv) return;
    document.querySelectorAll("form.rv").forEach(function (f) {
      f.addEventListener("submit", function (e) {
        if (!rv.value.trim()) { e.preventDefault(); rv.focus(); rv.setAttribute("aria-invalid", "true"); var w = document.getElementById("rv-warn"); if (w) w.hidden = false; return; }
        f.querySelectorAll("input[name=reviewer]").forEach(function (i) { i.value = rv.value; });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    drawer(); remember(); queueBadge(); globalPages(); dropzones(); timeline(); reviewerForms();
  });
})();
