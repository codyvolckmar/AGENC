/* AGENCY — client for the provisioning API. Requires /js/config.js. */
(function () {
  "use strict";

  /* window.AGENC_CONFIG is written by deploy.sh (never committed). */
  var CFG = window.AGENC_CONFIG || { apiBase: "", siteSecret: "" };
  var apiBase = (CFG.apiBase || "").replace(/\/$/, "");
  var siteSecret = CFG.siteSecret || "";
  var $ = function (sel) { return document.querySelector(sel); };

  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (siteSecret) headers["X-Agency-Secret"] = siteSecret;
    if (opts.headers) Object.assign(headers, opts.headers);
    return fetch(apiBase + path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: { message: "bad response from server" } };
      });
    });
  }

  /* helpers ----------------------------------------------------------- */
  function copyText(text, btn) {
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "copied";
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, done);
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (e) { /* noop */ }
      document.body.removeChild(ta);
      done();
    }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function statusLabel(status) {
    var map = {
      quote_required: "awaiting payment (quote issued)",
      awaiting_payment: "awaiting payment",
      provisioning: "spinning up container…",
      running: "online",
      halted: "halted — credit exhausted",
      error: "error",
      destroyed: "destroyed",
    };
    return map[status] || status;
  }

  function statusDot(status) {
    return '<span class="dot ' + esc(status) + '"></span>';
  }

  /* agent renderers ---------------------------------------------------- */
  function fmtUsd(x) {
    return (x === null || x === undefined) ? "—" : "$" + (+x).toFixed(2);
  }

  function quoteBlock(quote) {
    if (!quote) return "";
    var rows =
      "<dl>" +
      row("asset", quote.asset) +
      row("amount", quote.amount + " " + quote.asset) +
      row("chain", quote.chain + " (" + quote.network + ")") +
      "</dl>" +
      '<div class="quote-box"><div class="quote-amount">' +
      esc(quote.amount) + " " + esc(quote.asset) +
      "</div><p style=\"margin:6px 0 10px;color:var(--muted);font-size:13px;\">" +
      esc("Send the exact amount to this address with memo " + quote.reference + ":") +
      "</p>" +
      '<div class="copy-row"><code>' + esc(quote.address) + "</code>" +
      '<button class="copy-btn">copy</button></div>' +
      '<div class="copy-row" style="margin-top:8px;"><code>memo: ' + esc(quote.reference) + "</code>" +
      '<button class="copy-btn">copy</button></div></div>';
    return rows;
  }

  function row(k, v) {
    return "<dt>" + esc(k) + "</dt><dd>" + esc(v) + "</dd>";
  }

  function agentPanel(agent) {
    var stackLine = "";
    if (agent.stack && agent.stack.length) {
      stackLine = row("app stack", agent.stack.map(function (s) { return stackLabels[s] || s; }).join(", "));
      if (agent.stack_status && agent.stack_status !== "done") {
        stackLine += "<p style=\"color:var(--warn);font-size:12.5px;margin-top:4px;\">curated stack " +
          (agent.stack_status || "") + " — installing after the container boots…</p>";
      }
    }
    var body = "<dl>" +
      row("status", statusLabel(agent.status)) +
      row("hostname", agent.hostname) +
      row("plan", esc(agent.plan || "—") +
        (agent.price_usd_per_month ? " · " + fmtUsd(agent.price_usd_per_month) + "/mo" : "")) +
      row("specs", agent.memory_mb + " MB RAM · " + agent.disk_gb + " GB disk · " + (agent.cores ? agent.cores : "—") + " cores") +
      row("billing", esc(agent.billing_period || "—") +
        (agent.paid_until ? " · " + (agent.on_trial ? "trial until " : "paid up to ") + dateStr(agent.paid_until) : "")) +
      (agent.vmid ? row("vmid", agent.vmid) : "") +
      (agent.ip ? row("ip", agent.ip + '  <code style="border:none;background:none;padding:0;">ssh root@' + esc(agent.ip) + "</code>") : "") +
      (agent.billed_total_usd ? row("billed to date", fmtUsd(agent.billed_total_usd)) : "") +
      stackLine +
      "</dl>";
    var warn = "";
    if (agent.status === "halted") {
      warn = '<p style="color:var(--danger);font-size:13px;">⛔ halted — prepaid period lapsed. Ask the operator to renew or destroy this home.</p>';
    } else if (agent.expiring) {
      warn = agent.on_trial
        ? '<p style="color:var(--warn);font-size:12.5px;">⚠ trial ends ' + dateStr(agent.paid_until) + " — pay before then or the home is deleted.</p>"
        : '<p style="color:var(--warn);font-size:12.5px;">⚠ prepaid period ends ' + dateStr(agent.paid_until) + " — renew before then or the home halts.</p>";
    }
    return (
      '<h3>' + statusDot(agent.status) + " " + esc(agent.hostname) + "</h3>" +
      body +
      warn +
      (agent.quote ? quoteBlock(agent.quote) : "") +
      (agent.error ? '<p style="color:var(--danger);font-size:13.5px;">' + esc(agent.error) + "</p>" : "") +
      '<p style="color:var(--muted);font-size:12.5px;margin-top:10px;">agent id: ' + esc(agent.id) + "</p>"
    );
  }

  /* provision form ------------------------------------------------------ */
  var form = $("#provision-form");
  var result = $("#result");
  var resultTitle = $("#result-title");
  var resultBody = $("#result-body");
  var formNote = $("#form-note");

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    formNote.textContent = "";
    var body = {
      hostname: form.hostname.value.trim(),
      email: form.email.value.trim(),
      purpose: form.purpose.value.trim(),
      ssh_key: form.ssh_key.value.trim(),
      stack: selectedStack(),
      plan: $("#plan-select").value || undefined,
      period: $("#period-select").value || undefined,
    };
    var btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "Requesting…";
    api("/api/agents", { method: "POST", body: body })
      .then(function (res) {
        if (!res.ok || !res.agent) {
          var msg = res.error ? res.error.message : "request failed";
          formNote.textContent = "⚠ " + msg;
          return;
        }
        showResult(res.agent);
        if (res.agent.status === "provisioning" || res.agent.status === "running") {
          pollStatus(res.agent.id, renderStatus);
        }
      })
      .catch(function () { formNote.textContent = "⚠ network error — is the API reachable?"; })
      .finally(function () { btn.disabled = false; btn.textContent = "Request home · free"; });
  });

  function showResult(agent) {
    result.hidden = false;
    result.scrollIntoView({ behavior: "smooth", block: "nearest" });
    resultTitle.textContent = agent.on_trial ? "Trial home requested" : "Home requested";
    resultBody.innerHTML = agentPanel(agent);
  }

  /* status polling ------------------------------------------------------ */
  var pollTimer = null;
  var currentAgentId = null;

  function renderStatus(agent) {
    showResult(agent);
    var panel = $("#status-body");
    panel.hidden = false;
    panel.innerHTML = agentPanel(agent);
    if (agent.status === "running") { stopPolling(); }
  }

  function pollStatus(id, render) {
    currentAgentId = id;
    stopPolling();
    var tick = function () {
      api("/api/agents/" + encodeURIComponent(id)).then(function (res) {
        if (res.agent) render(res.agent);
        if (res.agent && res.agent.status === "running") stopPolling();
      });
    };
    tick();
    pollTimer = setInterval(tick, 3000);
  }

  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  /* status lookup -------------------------------------------------------- */
  $("#lookup-btn").addEventListener("click", function () {
    var id = $("#agent-id").value.trim();
    if (!id) return;
    var panel = $("#status-body");
    panel.hidden = false;
    panel.innerHTML = "<p>Looking up <code>" + esc(id) + "</code>…</p>";
    api("/api/agents/" + encodeURIComponent(id)).then(function (res) {
      if (res.agent) {
        panel.innerHTML = agentPanel(res.agent);
        currentAgentId = res.agent.id;
        pollStatus(res.agent.id, function (a) {
          var p = $("#status-body");
          if (p) p.innerHTML = agentPanel(a);
          if (a.status === "running") stopPolling();
        });
      } else {
        panel.innerHTML = '<p style="color:var(--warn);">' + esc(res.error ? res.error.message : "not found") + "</p>";
      }
    });
  });
  $("#agent-id").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") $("#lookup-btn").click();
  });

  /* plan + period pickers ------------------------------------------------ */
  function dateStr(epoch) {
    if (!epoch) return "—";
    try { return new Date(epoch * 1000).toUTCString().slice(0, 16); }
    catch (e) { return "—"; }
  }

  var planSpecs = {};
  (function () {
    api("/api/plans").then(function (res) {
      var sel = $("#plan-select");
      if (!sel || !res.ok) return;
      var opts = ['<option value="">choose a plan…</option>'];
      (res.plans || []).forEach(function (p) {
        planSpecs[p.id] = p;
        opts.push("<option value=\"" + esc(p.id) + "\">" + esc(p.id) +
          " · " + p.memory_mb + " MB RAM · " + p.disk_gb + " GB disk · " +
          (p.cores ? p.cores + " cores" : "shared CPU") +
          " · " + fmtUsd(p.price_usd_per_month) + "/mo" + "</option>");
      });
      sel.innerHTML = opts.join("");
      if (res.default_plan && planSpecs[res.default_plan]) {
        sel.value = res.default_plan;
      }
      var note = $("#plan-note");
      if (note) {
        var trial = res.trial_days || 0;
        note.textContent = trial
          ? "Every home starts with " + trial + " days of free hosting — pay before the trial ends or the home is deleted."
          : "All homes are paid up front per period.";
      }
    }).catch(function () { /* plans down — form still works, server default applies */ });
  })();

  (function () {
    api("/api/plans").then(function (res) {
      var sel = $("#period-select");
      if (!sel || !res.ok) return;
      var opts = ['<option value="">choose a period…</option>'];
      Object.keys(res.periods || {}).forEach(function (name) {
        opts.push("<option value=\"" + esc(name) + "\">" + esc(name) +
          " · " + res.periods[name].days + " days</option>");
      });
      sel.innerHTML = opts.join("");
      if (res.default_period) sel.value = res.default_period;
    }).catch(function () {});
  })();

  /* curate-your-stack picker ----------------------------------------------- */
  var stackLabels = {};
  var stackMax = 6;

  function selectedStack() {
    return Array.prototype.map.call(
      document.querySelectorAll("#stack-options input[type=checkbox]:checked"),
      function (el) { return el.value; }
    );
  }

  function renderStackCard(opt, checked) {
    stackLabels[opt.id] = opt.label;
    return (
      '<label class="stack-card">' +
      '<input type="checkbox" value="' + esc(opt.id) + '" ' + (checked ? "checked" : "") + ">" +
      "<span class=\"stack-card-body\"><b>" + esc(opt.label) + "</b>" +
      "<small>" + esc(opt.desc) + "</small></span></label>"
    );
  }

  api("/api/stacks").then(function (res) {
    var box = $("#stack-options");
    if (!box || !res.ok) return;
    stackMax = res.max_groups || 6;
    var baked = $("#stack-baked");
    if (baked && res.baked) {
      baked.innerHTML = "Baked into the base image: " + res.baked.map(function (g) { return "<b>" + esc(g.label) + "</b>"; }).join(" · ") +
        ". Pick up to " + stackMax + " add-ons below:";
    }
    var html = (res.selectable || []).map(function (g) { return renderStackCard(g, false); }).join("");
    if (res.venv) html += renderStackCard(res.venv, false);
    box.innerHTML = html;

    box.addEventListener("change", function () {
      var checked = selectedStack();
      var over = checked.length > stackMax;
      Array.prototype.forEach.call(box.querySelectorAll("input[type=checkbox]"), function (el) {
        if (!el.checked && over) el.disabled = true;
        else el.disabled = false;
      });
      var note = $("#form-note");
      if (over) note.textContent = "⚠ max " + stackMax + " stack add-ons — uncheck one to continue";
      else note.textContent = "";
    });
  }).catch(function () { /* stacks endpoint down — form still works without the picker */ });

  /* copy buttons (event delegation) */
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".copy-btn");
    if (!btn) return;
    var code = btn.closest(".copy-row").querySelector("code");
    if (code) copyText(code.textContent.replace(/^memo: /, ""), btn);
  });

  /* live health strip ----------------------------------------------------- */
  api("/api/health").then(function (res) {
    if (res && res.ok && res.trial) {
      var id = $("#free-badge");
      if (id) {
        id.textContent = res.trial.days
          ? res.trial.days + "-day trial, then pay up front"
          : "paid up front per period";
      }
    }
  }).catch(function () { /* api not up yet — form still works, server will 401 */ });
})();