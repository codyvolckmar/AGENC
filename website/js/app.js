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
      error: "error",
      destroyed: "destroyed",
    };
    return map[status] || status;
  }

  function statusDot(status) {
    return '<span class="dot ' + esc(status) + '"></span>';
  }

  /* agent renderers ---------------------------------------------------- */
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
    var body = "<dl>" +
      row("status", statusLabel(agent.status)) +
      row("hostname", agent.hostname) +
      row("specs", agent.memory_mb + " MB RAM · " + agent.disk_gb + " GB disk · " + (agent.cores ? agent.cores : "—") + " shared cores") +
      (agent.vmid ? row("vmid", agent.vmid) : "") +
      (agent.ip ? row("ip", agent.ip + '  <code style="border:none;background:none;padding:0;">ssh agent@' + esc(agent.ip) + "</code>") : "") +
      "</dl>";
    return (
      '<h3>' + statusDot(agent.status) + " " + esc(agent.hostname) + "</h3>" +
      body +
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
    resultTitle.textContent = agent.status === "quote_required"
      ? "Quote issued — pay in " + agent.quote.asset
      : "Home requested";
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

  /* copy buttons (event delegation) */
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".copy-btn");
    if (!btn) return;
    var code = btn.closest(".copy-row").querySelector("code");
    if (code) copyText(code.textContent.replace(/^memo: /, ""), btn);
  });

  /* live health strip ----------------------------------------------------- */
  api("/api/health").then(function (res) {
    if (res && res.ok && res.free_tier) {
      var free = res.free_tier;
      var id = $("#free-badge");
      if (id) {
        if (free.limit === 0) id.textContent = "all homes free right now";
        else id.textContent = free.used >= free.limit ? "quote in USDC/USDT" : "the first home is free";
      }
    }
  }).catch(function () { /* api not up yet — form still works, server will 401 */ });
})();