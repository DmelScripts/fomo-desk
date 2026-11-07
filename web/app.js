/* Grok Bot. The login token arrives only in the #t= fragment. */
(function (root) {
  "use strict";

  var TOKEN_KEY = "desk.token";
  var state = {
    token: "",
    screen: "home",
    mint: "",
    range: "LIVE",
    data: null,
    chart: null,
    activity: null,
    account: null,
    settings: null,
    decisions: {},
    pendingDecisions: {},
    coin: null,
    audit: null,
    unread: 0,
    retry: 5,
    loginPending: false,
    workerWatch: false,
    reloaded: false,
    pushBusy: false,
    pushKey: null,
    pushKeyLoading: false,
    timer: 0,
    searchTimer: 0,
    stream: null,
    offline: false,
    lastUpdate: null,
    seenBalance: "",
    commandDraft: "",
    commandReply: "",
  };

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, text, className) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function clear(node) {
    if (node) node.textContent = "";
  }

  function readFragmentToken(hash) {
    var text = hash || "";
    if (text.charAt(0) === "#") text = text.slice(1);
    var parts = text.split("&");
    var token = "";
    var kept = [];
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i];
      if (part.indexOf("t=") === 0) {
        try { token = decodeURIComponent(part.slice(2)); } catch (error) { token = ""; }
      } else if (part) kept.push(part);
    }
    return { token: token, hash: kept.length ? "#" + kept.join("&") : "" };
  }

  function queryHasToken(search) {
    var params = new URLSearchParams(search || "");
    return params.has("t") || params.has("token") || params.has("app_token") || params.has("APP_TOKEN");
  }

  function stripTokenQuery(search) {
    var params = new URLSearchParams(search || "");
    ["t", "token", "app_token", "APP_TOKEN"].forEach(function (key) { params.delete(key); });
    var rest = params.toString();
    return rest ? "?" + rest : "";
  }

  function parseSse(buffer) {
    var blocks = buffer.split("\n\n");
    var rest = blocks.pop();
    var events = [];
    blocks.forEach(function (block) {
      if (!block) return;
      var name = "message";
      var data = "";
      block.split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) name = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
      });
      if (!data) return;
      var payload = null;
      try { payload = JSON.parse(data); } catch (error) { payload = null; }
      events.push({ event: name, data: payload });
    });
    return { events: events, rest: rest };
  }

  function markView(mark) {
    if (!mark) return null;
    if (mark.kind === "bought") return { text: mark.title || "Bought", color: "#0a84ff", ts: mark.ts };
    if (mark.kind === "sold") return { text: mark.value || mark.title || "Sold", color: "#00d35a", ts: mark.ts };
    if (mark.kind === "stopped") {
      var stop = mark.subtitle ? String(mark.subtitle).replace(/^Risk · /, "") : "";
      var money = mark.value || "";
      var text = [stop, money].filter(Boolean).join(" · ");
      return { text: text || mark.title || "Stopped", color: "#ff453a", ts: mark.ts };
    }
    return null;
  }

  function tokenFromPaste(raw, origin) {
    var text = String(raw == null ? "" : raw).replace(/^\s+|\s+$/g, "");
    if (!text) return { token: "", message: "Paste the token or the private link." };
    if (/\s/.test(text) || text.length > 2000) {
      return { token: "", message: "Paste only the token or one private link." };
    }
    if (/^(javascript|data|vbscript|blob|file):/i.test(text)) {
      return { token: "", message: "That link was not accepted." };
    }
    var asLink = text.indexOf("://") !== -1 || text.indexOf("#") !== -1 || text.charAt(0) === "/" || text.indexOf("?") !== -1;
    if (!asLink) return { token: text, message: "" };
    var base = origin || "https://invalid.local";
    var url;
    try { url = new URL(text, base); } catch (error) {
      return { token: "", message: "That link was not accepted." };
    }
    if (url.username || url.password || (url.protocol !== "https:" && url.protocol !== "http:")) {
      return { token: "", message: "That link was not accepted." };
    }
    if (origin && url.origin !== origin) return { token: "", message: "That link is for a different desk." };
    if (queryHasToken(url.search)) return { token: "", message: "A token in the query string is ignored." };
    var parsed = readFragmentToken(url.hash);
    var token = parsed.token ? parsed.token.replace(/^\s+|\s+$/g, "") : "";
    if (!token) return { token: "", message: "That link has no private token." };
    return { token: token, message: "" };
  }

  root.Desk = {
    readFragmentToken: readFragmentToken,
    queryHasToken: queryHasToken,
    stripTokenQuery: stripTokenQuery,
    tokenFromPaste: tokenFromPaste,
    parseSse: parseSse,
    markView: markView,
  };

  if (typeof document === "undefined") return;

  function reduced() {
    return (" " + document.documentElement.className + " ").indexOf(" reduce ") !== -1;
  }

  function num(value) {
    return typeof value === "number" && isFinite(value);
  }

  function usd(value) {
    if (!num(value)) return "—";
    return "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function pct(value) {
    if (!num(value)) return "—";
    var sign = value > 0 || value === 0 ? "+" : "";
    return sign + value.toFixed(2) + "%";
  }

  function clock(ts) {
    if (!num(ts)) return "—";
    try {
      return new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "Asia/Ho_Chi_Minh",
      }).format(new Date(ts * 1000));
    } catch (error) {
      return "—";
    }
  }

  function ageText(minutes) {
    if (!num(minutes)) return "";
    if (minutes >= 60) return Math.round(minutes / 60) + "h old";
    return Math.round(minutes) + "m old";
  }

  function liqText(value) {
    if (!num(value)) return "";
    if (Math.abs(value) >= 1000) return "$" + (value / 1000).toFixed(1) + "K";
    return "$" + value.toFixed(0);
  }

  function httpsUrl(url) {
    return typeof url === "string" && url.indexOf("https://") === 0;
  }

  function fomoAnchor(url, label) {
    var node = el("a", label, "fomo-btn");
    node.setAttribute("data-external", "fomo");
    if (httpsUrl(url)) {
      node.href = url;
      node.target = "_blank";
      node.rel = "noopener noreferrer";
    } else {
      node.setAttribute("aria-disabled", "true");
    }
    return node;
  }

  function showGate(text) {
    $("shell").hidden = true;
    $("gate").hidden = false;
    $("gate-text").textContent = text;
    var form = $("gate-form");
    if (form) form.hidden = false;
    var field = $("gate-token");
    if (field) field.value = "";
  }

  function refuse(text) {
    state.token = "";
    state.loginPending = false;
    try { localStorage.removeItem(TOKEN_KEY); } catch (error) { /* private mode */ }
    if (state.stream && state.stream.cancel) state.stream.cancel();
    showGate(text || "This desk link was refused. Open a fresh link.");
  }

  function showError() {
    var when = state.lastUpdate ? clock(state.lastUpdate.getTime() / 1000) : "—";
    $("error").hidden = false;
    $("error").textContent = "Desk offline · retrying in " + state.retry + "s · last update " + when;
  }

  function armRetry() {
    if (state.timer) root.clearInterval(state.timer);
    state.retry = 5;
    showError();
    state.timer = root.setInterval(function () {
      state.retry -= 1;
      if (state.retry <= 0) {
        root.clearInterval(state.timer);
        state.timer = 0;
        loadAll();
        return;
      }
      showError();
    }, 1000);
  }

  function call(path, options) {
    var opts = options || {};
    var headers = { Accept: "application/json" };
    if (opts.body) headers["Content-Type"] = "application/json";
    if (state.token) headers.Authorization = "Bearer " + state.token;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: "no-store",
    }).then(function (res) {
      if (res.status === 401 || res.status === 503) {
        refuse(res.status === 503 ? "The desk is not accepting a login right now." : "This desk link was refused. Open a fresh link.");
        var authError = new Error("auth");
        authError.auth = true;
        throw authError;
      }
      return res.json().then(function (body) {
        if (!res.ok) {
          var httpError = new Error("http");
          httpError.status = res.status;
          httpError.body = body;
          throw httpError;
        }
        return body;
      });
    });
  }

  function loadAll() {
    if (!state.token) return Promise.resolve();
    return Promise.all([
      call("/api/state"),
      call("/api/chart?range=" + encodeURIComponent(state.range)),
      call("/api/activity"),
      call("/api/account"),
      call("/api/settings"),
    ]).then(function (parts) {
      state.data = parts[0];
      state.chart = parts[1];
      state.activity = parts[2];
      state.account = parts[3];
      state.settings = parts[4];
      state.offline = false;
      state.lastUpdate = new Date();
      $("error").hidden = true;
      render();
    }).catch(function (error) {
      if (error && error.auth) return;
      state.offline = true;
      armRetry();
    });
  }

  function connectStream() {
    if (!state.token || !root.ReadableStream) return;
    var buffer = "";
    fetch("/api/stream", {
      headers: { Authorization: "Bearer " + state.token, Accept: "text/event-stream" },
      cache: "no-store",
    }).then(function (res) {
      if (res.status === 401 || res.status === 503) {
        refuse(res.status === 503 ? "The desk is not accepting a login right now." : "This desk link was refused. Open a fresh link.");
        return null;
      }
      if (!res.ok || !res.body) throw new Error("stream");
      var reader = res.body.getReader();
      state.stream = reader;
      var decoder = new TextDecoder();
      function pump() {
        return reader.read().then(function (result) {
          if (result.done) throw new Error("closed");
          buffer += decoder.decode(result.value, { stream: true });
          var parsed = parseSse(buffer);
          buffer = parsed.rest;
          parsed.events.forEach(onStream);
          return pump();
        });
      }
      return pump();
    }).catch(function (error) {
      if (error && error.auth) return;
      if (state.token) armRetry();
    });
  }

  function onStream(item) {
    if (!item) return;
    if (item.event === "signal" || item.event === "exit") {
      state.unread += 1;
      $("bell-dot").hidden = state.unread === 0;
    }
    if (item.event === "audit_layer" && state.screen === "audit" && state.mint) {
      openAudit(state.mint, true);
      return;
    }
    loadAll();
  }

  function renderOdometer(node, text) {
    var previous = node.getAttribute("data-value") || "";
    clear(node);
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      var old = previous.length === text.length ? previous.charAt(i) : ch;
      var col = el("span", null, "od-col");
      if (!reduced() && old !== ch && /\d/.test(ch) && /\d/.test(old)) {
        col.className = "od-col roll";
        col.style.animationDelay = ((text.length - 1 - i) * 30) + "ms";
        var stack = el("span", null, "od-stack");
        stack.appendChild(el("span", old, "od-num"));
        stack.appendChild(el("span", ch, "od-num"));
        col.appendChild(stack);
      } else {
        col.appendChild(el("span", ch, "od-num"));
      }
      node.appendChild(col);
    }
    node.setAttribute("data-value", text);
  }

  function activityRow(row) {
    var button = el("button", null, "activity-row");
    button.type = "button";
    var grow = el("span", null, "grow");
    grow.appendChild(el("strong", row.title || "—"));
    grow.appendChild(el("span", row.subtitle || "", "subline"));
    button.appendChild(grow);
    button.appendChild(el("span", row.value || "", "value num"));
    button.addEventListener("click", function () {
      if (!row.mint) return;
      if (row.kind === "rejected") openAudit(row.mint);
      else openCoin(row.mint);
    });
    return button;
  }

  function drawHomeChart() {
    var canvas = $("chart-home");
    var chart = state.chart || { points: [], marks: [] };
    var empty = !chart.points || !chart.points.length;
    $("chart-empty").hidden = !empty;
    var marks = (chart.marks || []).map(markView).filter(Boolean);
    if (root.DeskCharts) {
      root.DeskCharts.line(canvas, {
        points: chart.points || [],
        start: chart.start,
        marks: marks,
        motion: !reduced(),
      });
    }
  }

  function renderHome() {
    var data = state.data;
    if (!data) return;
    $("home-skel").hidden = true;
    $("home-body").hidden = false;
    $("fixture").hidden = !data.fixture;
    renderOdometer($("balance"), usd(data.balance));
    var today = $("today");
    today.textContent = pct(data.today_pct);
    today.className = "num today" + (num(data.today_pct) && data.today_pct < 0 ? " down" : "");
    $("multiplier").textContent = num(data.multiplier) ? data.multiplier.toFixed(2) + "×" : "—";
    var unrealized = $("unrealized");
    if (unrealized) {
      if (data.open_positions > 0 && num(data.unrealized)) {
        unrealized.hidden = false;
        unrealized.textContent = "Unrealized " + usd(data.unrealized);
      } else if (data.open_positions > 0) {
        unrealized.hidden = false;
        unrealized.textContent = "Unrealized n/a";
      } else {
        unrealized.hidden = true;
        unrealized.textContent = "";
      }
    }
    $("paper-badge").hidden = data.mode !== "PAPER";
    var running = (data.agents || []).filter(function (card) { return card.running; }).length;
    $("agents-label").textContent = "Agents · " + running + " running";
    drawHomeChart();
    document.querySelectorAll("#range button").forEach(function (button) {
      button.className = button.getAttribute("data-range") === state.range ? "on" : "";
    });
    var cards = $("cards");
    clear(cards);
    (data.agents || []).forEach(function (card) {
      cards.appendChild(agentFace(card, true));
    });
    var list = $("home-activity");
    clear(list);
    var rows = (state.activity && state.activity.rows) || [];
    $("home-activity-empty").hidden = rows.length > 0;
    rows.slice(0, 5).forEach(function (row) { list.appendChild(activityRow(row)); });
  }

  function agentFace(card, carousel) {
    var width = Math.max(240, (root.innerWidth || 390) - 32);
    var node = el("article", null, "agent-card");
    if (carousel) {
      node.style.width = width + "px";
      node.style.aspectRatio = "504 / 470";
      node.style.borderRadius = (42 * width / 504) + "px";
    } else {
      node.style.borderRadius = (42 * width / 504) + "px";
    }
    var head = el("header");
    var title = el("div", null);
    title.appendChild(el("div", labelFor(card.name), "agent-name"));
    title.style.color = colorFor(card.name);
    head.appendChild(title);
    head.appendChild(el("span", card.paused ? "Paused" : (card.running ? "Running" : "Off"), "muted"));
    node.appendChild(head);
    node.appendChild(el("p", countLine(card)));
    if (card.line) node.appendChild(el("p", card.line));
    if (card.name === "scanner") node.appendChild(scannerFeed(card));
    if (card.name === "auditor") node.appendChild(auditorStrips(card));
    if (card.name === "analyst") node.appendChild(scoreBlock(card));
    if (card.name === "executor") node.appendChild(executorBlock(card));
    if (card.name === "risk") node.appendChild(riskPills(card));
    return node;
  }

  function labelFor(name) {
    return name ? name.charAt(0).toUpperCase() + name.slice(1) : "";
  }

  function colorFor(name) {
    if (name === "scanner") return "#64d2ff";
    if (name === "analyst") return "#ffd60a";
    if (name === "executor") return "#00d35a";
    if (name === "risk") return "#ff6b9a";
    return "#fff";
  }

  function countLine(card) {
    var n = card.count;
    if (card.name === "scanner") return (num(n) ? n : "—") + " pools";
    if (card.name === "auditor") return (num(n) ? n : "—") + " taken apart";
    if (card.name === "analyst") return "Scoring";
    if (card.name === "executor") return (num(n) ? n : "—") + " trades";
    return "Guarding";
  }

  function scannerFeed(card) {
    var wrap = el("div");
    var feed = card.feed || [];
    if (!feed.length && card.line) return wrap;
    feed.forEach(function (row) {
      var line = el("div", null, "feed-row" + (row.verdict === "kill" ? " killed" : ""));
      var liq = liqText(row.liquidity_usd);
      var left = row.verdict === "pass"
        ? (row.name || "Coin") + (liq ? " · liq " + liq : "") + " · → auditor"
        : (row.name || "Coin");
      line.appendChild(el("span", left));
      if (row.verdict === "kill") line.appendChild(el("span", row.reason || ""));
      wrap.appendChild(line);
    });
    return wrap;
  }

  function auditorStrips(card) {
    var wrap = el("div", null, "strips");
    var layers = card.layers || [];
    for (var i = 0; i < 8; i++) {
      var layer = layers[i];
      var strip = el("div", null, "strip");
      if (layer && layer.pass === true) strip.className = "strip ok";
      if (layer && layer.pass === false && layer.value !== "not checked") strip.className = "strip bad";
      wrap.appendChild(strip);
    }
    if (num(card.rejected)) wrap.appendChild(el("p", card.rejected + " rejected"));
    return wrap;
  }

  function scoreBlock(card) {
    var wrap = el("div");
    var canvas = el("canvas", null, "ring");
    wrap.appendChild(canvas);
    root.requestAnimationFrame(function () {
      if (root.DeskCharts) root.DeskCharts.ring(canvas, { score: card.score, min: card.min_score });
    });
    if (!num(card.score)) wrap.appendChild(el("p", "No score yet"));
    return wrap;
  }

  function executorBlock(card) {
    var wrap = el("div");
    var size = num(card.last_size_usd) ? usd(card.last_size_usd) : "";
    var pctText = num(card.last_stack_pct) ? card.last_stack_pct + "% of stack" : (num(card.stack_pct) ? card.stack_pct + "% of stack" : "");
    wrap.appendChild(el("p", [size, pctText].filter(Boolean).join(" · ") || "No trade yet"));
    if (card.last_status === "sent" || card.last_status === "bought") {
      wrap.appendChild(el("p", "✓ Filled"));
    } else if (card.last_status) {
      wrap.appendChild(el("p", card.last_status));
    }
    return wrap;
  }

  function riskPills(card) {
    var wrap = el("div", null, "pills");
    if (num(card.stop_pct)) wrap.appendChild(el("span", "stop -" + card.stop_pct + "%", "pill stop mono"));
    if (num(card.trail_pct)) wrap.appendChild(el("span", "trail " + card.trail_pct + "%", "pill trail mono"));
    return wrap;
  }

  function renderAgents() {
    var list = $("agents-list");
    clear(list);
    var agents = (state.data && state.data.agents) || [];
    agents.forEach(function (card) {
      var panel = el("article", null, "agent-panel");
      panel.id = "agent-" + card.name;
      panel.appendChild(agentFace(card, false));
      var row = el("div", null, "link-row");
      var status = el("span", card.paused ? "Paused" : "On", "grow");
      row.appendChild(status);
      var sw = el("button", null, "switch" + (card.paused ? " on" : ""));
      sw.type = "button";
      sw.setAttribute("aria-label", "Pause " + card.name);
      sw.disabled = card.pause_locked === true;
      sw.appendChild(el("i"));
      sw.addEventListener("click", function () { togglePause(card); });
      row.appendChild(sw);
      panel.appendChild(row);
      if (card.pause_locked) panel.appendChild(el("p", "Risk stays on while a position is open.", "muted"));
      var decisions = (state.decisions[card.name] && state.decisions[card.name].decisions) || [];
      decisions.forEach(function (item) { panel.appendChild(decisionRow(item)); });
      var shortcut = el("button", "Settings", "text-btn");
      shortcut.type = "button";
      shortcut.addEventListener("click", function () {
        go("account");
        var target = $("group-" + card.name);
        if (target && target.scrollIntoView) target.scrollIntoView();
      });
      panel.appendChild(shortcut);
      list.appendChild(panel);
    });
    agents.forEach(function (card) {
      if (state.decisions[card.name] || state.pendingDecisions[card.name]) return;
      state.pendingDecisions[card.name] = true;
      call("/api/agents/" + card.name + "/decisions").then(function (body) {
        state.decisions[card.name] = body;
        state.pendingDecisions[card.name] = false;
        if (state.screen === "agents") renderAgents();
      }).catch(function (error) {
        state.pendingDecisions[card.name] = false;
        if (error && error.auth) return;
      });
    });
  }

  function decisionRow(item) {
    var row = el("div", null, "decision");
    var grow = el("span", null, "grow");
    grow.appendChild(el("strong", (item.name || item.mint || "Coin") + " · " + (item.verdict || "")));
    grow.appendChild(el("span", clock(item.ts) + (item.reason ? " · " + item.reason : ""), "subline"));
    row.appendChild(grow);
    if (httpsUrl(item["coin_url"])) {
      var link = el("a", "FOMO", "text-btn");
      link.href = item["coin_url"];
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute("data-external", "fomo");
      row.appendChild(link);
    }
    row.addEventListener("click", function (event) {
      if (event.target && event.target.getAttribute && event.target.getAttribute("data-external")) return;
      if (item.mint) openCoin(item.mint);
    });
    return row;
  }

  function togglePause(card) {
    var path = "/api/agents/" + card.name + "/" + (card.paused ? "resume" : "pause");
    call(path, { method: "POST", body: {} }).then(function () {
      state.decisions = {};
      return loadAll();
    }).catch(function (error) {
      if (error && error.auth) return;
      var detail = error && error.body && error.body.detail;
      var note = $("account-note");
      if (note && detail) note.textContent = detail;
      if (state.screen === "agents" && detail) {
        var panel = $("agent-" + card.name);
        if (panel) panel.appendChild(el("p", detail, "muted"));
      }
    });
  }

  function renderActivity() {
    var rows = (state.activity && state.activity.rows) || [];
    $("activity-empty").hidden = rows.length > 0;
    var list = $("activity-list");
    clear(list);
    rows.forEach(function (row) { list.appendChild(activityRow(row)); });
    $("activity-more").hidden = rows.length < 30;
  }

  function renderAccount() {
    var account = state.account || {};
    var mode = $("account-mode");
    clear(mode);
    mode.appendChild(el("p", "Mode", "muted"));
    var switches = el("div", null, "ranges");
    ["PAPER", "LIVE"].forEach(function (name) {
      var button = el("button", name);
      button.type = "button";
      button.className = account.mode === name ? "on" : "";
      button.addEventListener("click", function () {
        if (name === "PAPER") {
          call("/api/mode", { method: "POST", body: { mode: "PAPER" } }).then(loadAll).catch(noteError);
          return;
        }
        $("live-confirm").hidden = false;
      });
      switches.appendChild(button);
    });
    mode.appendChild(switches);
    var stack = $("account-stack");
    clear(stack);
    stack.appendChild(el("p", "Stack", "muted"));
    var input = el("input");
    input.type = "number";
    input.className = "stack-field";
    if (num(account.stack)) input.value = String(account.stack);
    input.addEventListener("change", function () {
      saveSetting("paper_start_stack", input.value);
    });
    stack.appendChild(input);
    var profile = $("account-profile");
    clear(profile);
    profile.appendChild(el("p", "Your FOMO profile", "muted"));
    if (httpsUrl(account.profile_url)) {
      var link = el("a", account.profile_url);
      link.href = account.profile_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute("data-external", "fomo");
      profile.appendChild(link);
    } else {
      profile.appendChild(el("p", "No FOMO username yet."));
    }
    var invite = $("account-invite");
    clear(invite);
    var share = el("button", "Invite a friend", "text-btn");
    share.type = "button";
    share.addEventListener("click", function () { shareInvite(account.signup_url); });
    invite.appendChild(share);
    if (account.signup_url) invite.appendChild(el("p", account.signup_url, "muted"));
    renderSettings();
    renderIntegrations();
    renderKey();
    renderPush();
    renderPhantom();
    renderQuotes();
    renderCommand();
  }

  function renderPhantom() {
    var node = $("account-phantom");
    if (!node) return;
    clear(node);
    var row = (state.account && state.account.phantom) || {};
    node.appendChild(el("h2", "Phantom"));
    node.appendChild(el("p", "PAPER application control. Not a wallet policy and not live authority.", "muted"));
    node.appendChild(el("p", "Bankroll $" + (row.bankroll_usd || "100.00") + " · one ticket $" + (row.size_usd || "8.00") + ". The $47.50 FOMO bump does not apply."));
    node.appendChild(el("p", "Transport " + (row.transport || "BLOCKED") + " · " + (row.auth || "AWAITING_OWNER_AUTH_PROCEDURE")));
    node.appendChild(el("p", row.halted ? "Kill switch on" : "Kill switch off"));
    node.appendChild(el("p", "Send off. A quote needs the bound wallet id and Solana address. The recorded quote is not executable."));
    node.appendChild(el("p", "Scanner, auditor, and Jev signals do not start an order. Exits are not scheduled from here.", "muted"));
  }

  function renderQuotes() {
    var node = $("account-quotes");
    if (!node) return;
    clear(node);
    node.appendChild(el("h2", "Quotes"));
    node.appendChild(el("p", "PAPER · manually imported. The Phantom bridge is not connected. A recorded quote is not an order.", "muted"));
    var rows = (state.account && state.account.quotes) || [];
    if (!rows.length) {
      node.appendChild(el("p", "No quote has been recorded.", "muted"));
      return;
    }
    rows.forEach(function (row) {
      var line = row.label + " · " + row.status + " · " + row.provider;
      line += " · " + row.provenance + " · bridge " + row.bridge;
      line += " · " + (row.wallet || "wallet pending");
      line += " · slippage " + row.slippage;
      if (row.provider_expiry_unknown) line += " · provider expiry unknown";
      else if (row.expiry) line += " · provider expires " + new Date(row.expiry * 1000).toISOString();
      else line += " · no provider expiry";
      line += row.local_ttl ? " · local ttl " + new Date(row.local_ttl * 1000).toISOString() : " · no local ttl";
      if (!row.usable) line += " · unusable";
      if (row.executable === false) line += " · not executable";
      if (row.observed_at) line += " · observed " + new Date(row.observed_at * 1000).toISOString();
      if (row.route_provider) line += " · route " + row.route_provider;
      if (row.fees && row.fees.length) {
        line += " · fees";
        row.fees.forEach(function (fee) { line += " " + fee.name + " " + fee.amount; });
      }
      if (row.transaction_material_omitted) line += " · transaction payload omitted";
      line += " · " + row.sell_chain + " " + row.amount + " " + row.amount_unit;
      if (row.buy_mint) line += " → " + row.buy_mint;
      if (row.out_amount) line += " → " + row.out_amount;
      if (row.provider_quote_id) line += " · " + row.provider_quote_id;
      if (row.failure) line += " · " + row.failure;
      node.appendChild(el("p", line));
    });
  }

  function renderCommand() {
    var node = $("account-command");
    if (!node) return;
    clear(node);
    node.appendChild(el("h2", "Desk"));
    node.appendChild(el("p", "Type a command. Nothing here places an order.", "muted"));
    var input = el("input");
    input.id = "command-text";
    input.type = "text";
    input.placeholder = "status";
    input.autocomplete = "off";
    input.value = state.commandDraft || "";
    input.addEventListener("input", function () { state.commandDraft = input.value; });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") sendCommand(input);
    });
    var button = el("button", "Send", "fomo-btn");
    button.type = "button";
    button.addEventListener("click", function () { sendCommand(input); });
    var reply = el("p", state.commandReply || "", "note");
    reply.id = "command-reply";
    node.appendChild(input);
    node.appendChild(button);
    node.appendChild(reply);
  }

  function sendCommand(input) {
    var text = (input.value || "").trim();
    if (!text) return;
    call("/api/command", { method: "POST", body: { text: text } }).then(function (body) {
      state.commandReply = body && body.text ? body.text : "";
      state.commandDraft = "";
      input.value = "";
      renderCommand();
    }).catch(noteError);
  }

  function shareInvite(url) {
    if (!httpsUrl(url)) {
      $("account-note").textContent = "No invite link is available.";
      return;
    }
    if (navigator.share) {
      navigator.share({ url: url }).catch(function () { copyInvite(url); });
      return;
    }
    copyInvite(url);
  }

  function copyInvite(url) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () {
        $("account-note").textContent = "Link copied";
      }).catch(function () {
        $("account-note").textContent = "Copy is not available in this browser.";
      });
      return;
    }
    $("account-note").textContent = "Copy is not available in this browser.";
  }

  function renderSettings() {
    var rootNode = $("account-settings");
    clear(rootNode);
    var groups = (state.settings && state.settings.groups) || {};
    Object.keys(groups).forEach(function (name) {
      var block = el("section", null, "card block");
      block.id = "group-" + name;
      block.appendChild(el("h2", labelFor(name)));
      groups[name].forEach(function (item) {
        if (item.key === "mode") {
          block.appendChild(el("p", "mode · use the switch above", "muted"));
          return;
        }
        block.appendChild(settingControl(item));
      });
      rootNode.appendChild(block);
    });
    var ticket = state.settings && state.settings.ticket;
    var note = el("p", ticket || "", "note");
    note.id = "settings-ticket";
    if (!ticket) note.hidden = true;
    rootNode.insertBefore(note, rootNode.firstChild);
  }

  function settingControl(item) {
    var row = el("div", null, "setting");
    var title = el("span", null, "grow");
    title.appendChild(el("strong", item.label || item.key));
    title.appendChild(el("span", " " + item.key, "mono muted"));
    if (item.note) title.appendChild(el("span", item.note, "sub"));
    if (item.min != null && item.max != null) {
      title.appendChild(el("span", "Range " + item.min + " to " + item.max, "sub"));
    }
    row.appendChild(title);
    if (item.values) {
      item.values.forEach(function (value) {
        var button = el("button", value, "text-btn");
        button.type = "button";
        if (String(item.value) === value) button.className = "text-btn on";
        button.addEventListener("click", function () { saveSetting(item.key, value); });
        row.appendChild(button);
      });
      return row;
    }
    if (item.value === "true" || item.value === "false") {
      var on = item.value === "true";
      var sw = el("button", null, "switch" + (on ? " on" : ""));
      sw.type = "button";
      sw.appendChild(el("i"));
      sw.addEventListener("click", function () {
        if (item.key === "enabled" && on && !askTwice("Turn off Take apart before buying?")) return;
        saveSetting(item.key, on ? "false" : "true");
      });
      row.appendChild(sw);
      return row;
    }
    if (item.min != null && item.max != null) {
      var range = el("input");
      range.type = "range";
      range.min = String(item.min);
      range.max = String(item.max);
      range.step = stepFor(item);
      range.value = item.value == null ? "" : String(item.value);
      var exact = el("input");
      exact.type = "number";
      exact.min = range.min;
      exact.max = range.max;
      exact.step = range.step;
      exact.value = range.value;
      range.addEventListener("input", function () { exact.value = range.value; });
      exact.addEventListener("change", function () { saveSetting(item.key, exact.value); });
      range.addEventListener("change", function () { saveSetting(item.key, range.value); });
      row.appendChild(range);
      row.appendChild(exact);
      return row;
    }
    var text = el("input");
    text.type = "text";
    text.value = item.value == null ? "" : String(item.value);
    text.addEventListener("change", function () { saveSetting(item.key, text.value); });
    row.appendChild(text);
    return row;
  }

  function stepFor(item) {
    var key = item.key || "";
    if (key.indexOf("pct") >= 0 && item.max <= 1) return "0.01";
    if (item.max <= 1) return "0.01";
    if (String(item.value).indexOf(".") >= 0) return "0.01";
    return "1";
  }

  function askTwice(message) {
    var ask = root["confirm"];
    if (typeof ask !== "function") return false;
    return ask(message) && ask("Confirm once more. " + message);
  }

  function saveSetting(key, value) {
    call("/api/settings", { method: "POST", body: { key: key, value: value } }).then(function (body) {
      if (body && body.ticket) $("account-note").textContent = body.ticket;
      return loadAll();
    }).catch(noteError);
  }

  function noteError(error) {
    if (error && error.auth) return;
    var detail = error && error.body && error.body.detail;
    $("account-note").textContent = detail || "That change was refused.";
  }

  function renderIntegrations() {
    var node = $("account-integrations");
    clear(node);
    node.appendChild(el("h2", "Notifications"));
    var integrations = (state.account && state.account.integrations) || {};
    ["email", "telegram", "web_push"].forEach(function (name) {
      var item = integrations[name] || {};
      var title = name === "web_push" ? "Web push" : labelFor(name);
      node.appendChild(el("p", title));
      node.appendChild(el("p", item.detail || "Not available.", "muted"));
    });
    var push = integrations.web_push || {};
    var test = el("button", "Send test push", "text-btn");
    test.type = "button";
    test.disabled = !push.ready;
    if (push.ready) test.addEventListener("click", sendTestPush);
    node.appendChild(test);
  }

  function sendTestPush() {
    call("/api/push/test", { method: "POST", body: {} }).then(function (body) {
      var sent = body && body.sent ? body.sent : 0;
      var failed = body && body.failed ? body.failed : 0;
      var removed = body && body.removed ? body.removed : 0;
      var note;
      if (failed) {
        note = "Test push failed for " + failed + (failed === 1 ? " phone." : " phones.");
        if (sent) note += " " + sent + (sent === 1 ? " accepted it." : " accepted it.");
        var err = body.errors && body.errors[0];
        if (err && typeof err.status === "number") note += " Provider status " + err.status + ".";
        if (err && err.reason && /^[A-Za-z]{1,40}$/.test(err.reason)) note += " Reason " + err.reason + ".";
        if (err && err.category && /^[a-z_]{1,40}$/.test(err.category)) note += " Category " + err.category + ".";
      } else if (sent) {
        note = "Test push sent to " + sent + (sent === 1 ? " phone." : " phones.");
      } else {
        note = "No phone is subscribed, so nothing was sent.";
      }
      if (removed) note += " " + removed + " expired subscription" + (removed === 1 ? " was" : "s were") + " removed.";
      $("account-note").textContent = note;
    }).catch(noteError);
  }

  function renderKey() {
    var node = $("account-key");
    clear(node);
    var jev = (state.account && state.account.jev) || {};
    node.appendChild(el("h2", "Jev key"));
    node.appendChild(el("p", "Analyst mode " + ((state.account && state.account.analyst_mode) || "—"), "muted"));
    node.appendChild(el("p", jev.detail || "", "muted"));
    if (jev.host_locked) return;
    var field = el("input");
    field.type = "password";
    field.autocomplete = "off";
    field.placeholder = "Paste a new key";
    var save = el("button", "Save key");
    save.type = "button";
    save.addEventListener("click", function () {
      var value = field.value;
      field.value = "";
      if (!value) return;
      call("/api/account/key", { method: "POST", body: { name: "TYPESAFE_API_KEY", value: value } }).then(function () {
        $("account-note").textContent = "Jev key saved. It is not shown again.";
        return loadAll();
      }).catch(noteError);
    });
    var remove = el("button", "Remove key", "text-btn");
    remove.type = "button";
    remove.addEventListener("click", function () {
      call("/api/account/key", { method: "POST", body: { name: "TYPESAFE_API_KEY", clear: true } }).then(function () {
        $("account-note").textContent = "Jev key removed.";
        return loadAll();
      }).catch(noteError);
    });
    node.appendChild(field);
    node.appendChild(save);
    node.appendChild(remove);
  }

  function renderPush() {
    var node = $("account-push");
    clear(node);
    node.appendChild(el("h2", "This phone"));
    var standalone = root.navigator.standalone === true ||
      (root.matchMedia && root.matchMedia("(display-mode: standalone)").matches);
    if (!standalone) {
      node.appendChild(el("p", "Add this app to your iPhone home screen, then open it from the icon to allow notifications.", "muted"));
      return;
    }
    var button = el("button", "Enable notifications");
    button.id = "enable-push";
    button.type = "button";
    button.addEventListener("click", enablePush);
    node.appendChild(button);
    node.appendChild(el("p", "Tap once. This phone will ask to allow notifications.", "muted"));
    loadPushKey();
  }

  function loadPushKey() {
    if (state.pushKey || state.pushKeyLoading) return;
    state.pushKeyLoading = true;
    fetch("/vapid_public.txt", { cache: "no-store" }).then(function (res) {
      if (!res.ok) throw new Error("http");
      return res.text();
    }).then(function (raw) {
      var key = urlBase64ToUint8Array(String(raw || "").replace(/^\s+|\s+$/g, ""));
      if (key.length !== 65 || key[0] !== 4) throw new Error("shape");
      state.pushKey = key;
    }).catch(function () {
      state.pushKey = null;
      if ($("account-note")) $("account-note").textContent = "This desk has no usable notification key.";
    }).then(function () {
      state.pushKeyLoading = false;
    });
  }

  function enablePush() {
    if (state.pushBusy) return;
    if (!("serviceWorker" in navigator) || !("PushManager" in root) || !root.Notification) {
      $("account-note").textContent = "This browser cannot subscribe to notifications.";
      return;
    }
    if (!state.pushKey) {
      $("account-note").textContent = "The notification key is not on this phone yet. Tap Enable once more.";
      loadPushKey();
      return;
    }
    state.pushBusy = true;
    var permissionNow = root.Notification.permission;
    var asked = permissionNow === "granted"
      ? Promise.resolve("granted")
      : root.Notification.requestPermission();
    var ready = workerReady();
    asked.then(function (permission) {
      if (permission !== "granted") {
        state.pushBusy = false;
        $("account-note").textContent = permission === "denied"
          ? "Notifications are blocked."
          : "Notifications were not allowed.";
        return null;
      }
      return ready;
    }).then(function (reg) {
      if (!reg) return null;
      return reg.pushManager.getSubscription().then(function (existing) {
        if (existing) return existing;
        return reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: state.pushKey,
        });
      });
    }).then(function (sub) {
      if (!sub) return null;
      var keys = subscriptionKeys(sub);
      if (!keys.endpoint || !keys.p256dh || !keys.auth) {
        var shape = new Error("shape");
        shape.name = "DataError";
        throw shape;
      }
      return call("/api/push", { method: "POST", body: keys }).then(function (body) {
        return body;
      }).catch(function (error) {
        if (error) error.stage = "save";
        throw error;
      });
    }).then(function (body) {
      state.pushBusy = false;
      if (!body || !body.ok) return;
      var known = state.account && state.account.integrations && state.account.integrations.web_push;
      $("account-note").textContent = known && !known.ready
        ? "Subscription saved. Sending stays off until web push is configured."
        : "Subscription saved.";
    }).catch(function (error) {
      state.pushBusy = false;
      pushNote(error);
    });
  }

  function workerReady() {
    return new Promise(function (resolve, reject) {
      var worker = navigator.serviceWorker;
      if (!worker || !worker.ready) {
        var missing = new Error("worker");
        missing.name = "InvalidStateError";
        reject(missing);
        return;
      }
      var settled = false;
      var timer = root.setTimeout(function () {
        if (settled) return;
        settled = true;
        var late = new Error("worker");
        late.name = "InvalidStateError";
        reject(late);
      }, 4000);
      worker.ready.then(function (reg) {
        if (settled) return;
        settled = true;
        root.clearTimeout(timer);
        resolve(reg);
      }).catch(function () {
        if (settled) return;
        settled = true;
        root.clearTimeout(timer);
        var failed = new Error("worker");
        failed.name = "InvalidStateError";
        reject(failed);
      });
    });
  }

  function subscriptionKeys(sub) {
    var json = {};
    try { json = sub && sub.toJSON ? sub.toJSON() : {}; } catch (error) { json = {}; }
    var endpoint = (json && json.endpoint) || (sub && sub.endpoint) || "";
    var p256dh = json && json.keys ? json.keys.p256dh : "";
    var auth = json && json.keys ? json.keys.auth : "";
    if ((!p256dh || !auth) && sub && sub.getKey) {
      try {
        var pub = sub.getKey("p256dh");
        var secret = sub.getKey("auth");
        if (pub) p256dh = bytesToUrlBase64(pub);
        if (secret) auth = bytesToUrlBase64(secret);
      } catch (error) { /* keep the json keys */ }
    }
    return { endpoint: String(endpoint || ""), p256dh: String(p256dh || ""), auth: String(auth || "") };
  }

  function bytesToUrlBase64(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return root.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function pushNote(error) {
    if (!error || error.auth) return;
    if (error.stage === "save") {
      if (typeof error.status === "number") {
        $("account-note").textContent = "The phone created a subscription, but the desk did not save it (HTTP " + error.status + ").";
        return;
      }
      $("account-note").textContent = "The phone created a subscription, but the desk could not be reached.";
      return;
    }
    var name = error.name && /^[A-Za-z]{1,40}$/.test(error.name) ? error.name : "";
    if (name === "NotAllowedError") {
      $("account-note").textContent = "Notifications are blocked. Tap Enable once from this home screen app.";
      return;
    }
    if (name === "InvalidStateError") {
      $("account-note").textContent = "The notification worker is not active yet. Leave the app, open the icon, and tap Enable once.";
      return;
    }
    if (name === "AbortError") {
      $("account-note").textContent = "The phone cancelled the subscription. Tap Enable once more.";
      return;
    }
    if (name === "InvalidAccessError" || name === "DataError") {
      $("account-note").textContent = "The phone rejected the notification key.";
      return;
    }
    if (name) {
      $("account-note").textContent = "The phone did not create a subscription (" + name + ").";
      return;
    }
    $("account-note").textContent = "The subscription was not saved.";
  }

  function urlBase64ToUint8Array(value) {
    var padding = "=".repeat((4 - (value.length % 4)) % 4);
    var base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = root.atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function openCoin(mint) {
    state.mint = mint;
    state.screen = "coin";
    state.coin = null;
    writeRoute();
    showOnly("view-coin");
    $("coin-body").hidden = true;
    $("coin-missing").hidden = true;
    call("/api/coin/" + encodeURIComponent(mint)).then(function (body) {
      state.coin = body;
      renderCoin();
    }).catch(function (error) {
      if (error && error.auth) return;
      if (error && error.status === 404) {
        $("coin-title").textContent = "Coin";
        $("coin-missing").hidden = false;
        return;
      }
      armRetry();
    });
  }

  function renderCoin() {
    var coin = state.coin;
    if (!coin) return;
    $("coin-missing").hidden = true;
    $("coin-body").hidden = false;
    $("coin-title").textContent = coin.name || coin.symbol || coin.mint || "Coin";
    var bits = [coin.symbol || "", "Solana", ageText(coin.age_min)].filter(Boolean);
    $("coin-sub").textContent = bits.join(" · ");
    $("coin-shield").hidden = !coin.shield;
    $("coin-shield").textContent = coin.shield || "";
    var paper = state.data && state.data.mode === "PAPER";
    $("coin-paper").hidden = !paper;
    $("coin-price").textContent = num(coin.price) ? usd(coin.price) : "—";
    var change = $("coin-change");
    if (num(coin.change_since_entry_pct)) {
      change.hidden = false;
      change.textContent = (coin.change_since_entry_pct >= 0 ? "▲ " : "▼ ") + Math.abs(coin.change_since_entry_pct).toFixed(2) + "% since entry";
      change.style.color = coin.change_since_entry_pct >= 0 ? "#00d35a" : "#ff453a";
    } else {
      change.hidden = true;
      change.textContent = "";
    }
    var candles = coin.candles || [];
    $("coin-empty-chart").hidden = candles.length > 0;
    var exitPrice = coin.exit_lines && num(coin.exit_lines.current) ? coin.exit_lines.current : null;
    if (root.DeskCharts) {
      root.DeskCharts.candles($("chart-coin"), {
        candles: candles,
        exitPrice: exitPrice,
        exitLabel: exitPrice != null && coin.exit_lines ? coin.exit_lines.label : "",
      });
    }
    var stats = $("coin-stats");
    clear(stats);
    stat(stats, "Market cap", coin.stats && coin.stats.market_cap != null ? usd(coin.stats.market_cap) : "n/a");
    stat(stats, "Liquidity", coin.stats && num(coin.stats.liquidity_usd) ? usd(coin.stats.liquidity_usd) : "n/a");
    stat(stats, "24h volume", coin.stats && coin.stats.volume_h24 != null ? usd(coin.stats.volume_h24) : "n/a");
    var holders = coin.stats ? coin.stats.holders : null;
    stat(stats, "Holders", holders == null || holders === "" ? "n/a" : String(holders));
    var position = $("coin-position");
    var exit = $("coin-exit");
    clear(position);
    clear(exit);
    var open = coin.position && coin.position.open;
    if (open) {
      position.appendChild(el("p", "Your position · " + usd(coin.position.size_usd) + " in · Executor"));
      var card = el("div", null, "card block");
      var stop = coin.exit_lines ? coin.exit_lines.stop : null;
      var trail = coin.exit_lines ? coin.exit_lines.trail : null;
      var carried = num(coin.position.stop_pct) ? " · -" + Number(coin.position.stop_pct).toFixed(2) + "%" : "";
      card.appendChild(el("p", "Stop " + (num(stop) ? usd(stop) : "—") + carried + " carried"));
      var unreal = coin.position.unrealized_usd;
      card.appendChild(el("p", num(unreal) ? "Unrealized " + usd(unreal) : "Unrealized n/a"));
      card.appendChild(el("p", "Trail " + (num(trail) ? usd(trail) : "not trailing")));
      var left = coin.position.timeout_left_s;
      var leftText = "Time left —";
      if (num(left)) {
        if (left < 0) leftText = "Past the timeout window";
        else leftText = "Time left " + Math.ceil(left / 60) + "m";
      }
      card.appendChild(el("p", leftText));
      exit.appendChild(card);
    }
    var fomo = $("coin-fomo");
    clear(fomo);
    fomo.appendChild(fomoAnchor(coin["coin_url"], open ? "Sell on FOMO" : "Buy on FOMO"));
  }

  function stat(parent, name, value) {
    var node = el("div", null, "stat");
    node.appendChild(el("span", name));
    node.appendChild(el("strong", value, "num"));
    parent.appendChild(node);
  }

  function openAudit(mint, keep) {
    state.mint = mint;
    state.screen = "audit";
    if (!keep) state.audit = null;
    writeRoute();
    showOnly("view-audit");
    call("/api/audit/" + encodeURIComponent(mint)).then(function (body) {
      state.audit = body;
      renderAudit();
    }).catch(function (error) {
      if (error && error.auth) return;
      armRetry();
    });
  }

  function renderAudit() {
    var audit = state.audit || { layers: [] };
    $("audit-title").textContent = state.mint || "Auditor";
    var layers = audit.layers || [];
    $("audit-empty").hidden = layers.length > 0;
    $("audit-rows").hidden = layers.length === 0;
    var discs = $("audit-discs");
    var rows = $("audit-rows");
    clear(discs);
    clear(rows);
    layers.forEach(function (layer, index) {
      var disc = el("div", null, "disc");
      disc.appendChild(el("span", String(layer.n || index + 1) + " " + (layer.name || "")));
      disc.appendChild(el("span", layer.pass === true ? "✓" : (layer.pass === false ? "×" : "")));
      discs.appendChild(disc);
      var row = el("div", null, "link-row");
      row.appendChild(el("span", layer.name || "", "grow"));
      row.appendChild(el("span", layer.value || "—", "mono"));
      rows.appendChild(row);
    });
    paintDiscs(layers);
    var result = $("audit-result");
    clear(result);
    if (layers.length && audit.passed) {
      result.appendChild(el("p", "Cleared · 8/8 parts"));
      var sent = "Sent to Executor";
      if (num(audit.size_usd)) sent += " · " + usd(audit.size_usd);
      result.appendChild(el("p", sent));
    } else if (layers.length) {
      result.appendChild(el("p", "Rejected · " + (audit.reason || "stopped")));
      if (audit.name) result.appendChild(el("p", "Grok Bot won't buy " + audit.name));
    }
    var toggle = $("audit-toggle");
    clear(toggle);
    var enabled = settingValue("enabled");
    toggle.appendChild(el("span", "Take apart before buying", "grow"));
    var sw = el("button", null, "switch" + (enabled === "true" ? " on" : ""));
    sw.type = "button";
    sw.appendChild(el("i"));
    sw.addEventListener("click", function () {
      if (enabled === "true" && !askTwice("Turn off Take apart before buying?")) return;
      saveSetting("enabled", enabled === "true" ? "false" : "true");
    });
    toggle.appendChild(sw);
    var fomo = $("audit-fomo");
    clear(fomo);
    fomo.appendChild(fomoAnchor(audit["coin_url"], "Open on FOMO"));
  }

  function discClass(layer) {
    if (layer.pass === true) return "disc ok";
    if (layer.pass === false && layer.value !== "not checked") return "disc bad";
    return "disc";
  }

  function paintDiscs(layers) {
    var discs = $("audit-discs").children;
    function apply(index) {
      if (index >= layers.length) return;
      var disc = discs[index];
      var layer = layers[index];
      if (!disc || !layer) return;
      if (reduced()) {
        disc.className = discClass(layer);
        apply(index + 1);
        return;
      }
      disc.className = "disc beam";
      root.setTimeout(function () {
        disc.className = discClass(layer);
        root.setTimeout(function () { apply(index + 1); }, 150);
      }, 350);
    }
    apply(0);
  }

  function settingValue(key) {
    var groups = (state.settings && state.settings.groups) || {};
    var names = Object.keys(groups);
    for (var i = 0; i < names.length; i++) {
      var items = groups[names[i]] || [];
      for (var j = 0; j < items.length; j++) {
        if (items[j].key === key) return items[j].value;
      }
    }
    return null;
  }

  function showOnly(id) {
    ["view-home", "view-agents", "view-activity", "view-account", "view-audit", "view-coin"].forEach(function (name) {
      $(name).hidden = name !== id;
    });
    var tab = id.replace("view-", "");
    ["home", "agents", "activity", "account"].forEach(function (name) {
      $("tab-" + name).className = name === tab ? "on" : "";
    });
  }

  function render() {
    if (state.screen === "home") {
      showOnly("view-home");
      renderHome();
    } else if (state.screen === "agents") {
      showOnly("view-agents");
      renderAgents();
    } else if (state.screen === "activity") {
      showOnly("view-activity");
      renderActivity();
    } else if (state.screen === "account") {
      showOnly("view-account");
      renderAccount();
    } else if (state.screen === "coin" && state.coin) {
      showOnly("view-coin");
      renderCoin();
    } else if (state.screen === "audit" && state.audit) {
      showOnly("view-audit");
      renderAudit();
    }
  }

  function go(screen) {
    state.screen = screen;
    state.mint = "";
    writeRoute();
    render();
    if (screen === "agents") renderAgents();
  }

  function writeRoute() {
    var hash = "#/" + state.screen;
    if (state.mint) hash += "/" + encodeURIComponent(state.mint);
    if (location.hash !== hash) history.pushState({ screen: state.screen }, "", location.pathname + location.search + hash);
  }

  function readRoute() {
    var body = (location.hash || "").replace(/^#/, "");
    if (!body || body.indexOf("t=") === 0) return;
    var parts = body.split("/");
    var screen = parts[1] || "home";
    var allowed = { home: 1, agents: 1, activity: 1, account: 1, coin: 1, audit: 1 };
    if (!allowed[screen]) return;
    state.screen = screen;
    state.mint = parts[2] ? decodeURIComponent(parts[2]) : "";
    if (screen === "coin" && state.mint) openCoin(state.mint);
    else if (screen === "audit" && state.mint) openAudit(state.mint);
    else render();
  }

  function registerWorker() {
    var worker = navigator.serviceWorker;
    if (!worker || !worker.register) return;
    if (!state.workerWatch && worker.addEventListener) {
      state.workerWatch = true;
      worker.addEventListener("controllerchange", function () {
        if (state.reloaded || !location.reload) return;
        state.reloaded = true;
        location.reload();
      });
    }
    worker.register("/sw.js").catch(function () {});
  }

  function openDesk() {
    $("gate").hidden = true;
    var form = $("gate-form");
    if (form) form.hidden = true;
    var field = $("gate-token");
    if (field) field.value = "";
    $("shell").hidden = false;
    $("home-skel").hidden = false;
    registerWorker();
    bind();
    loadAll().then(function () {
      connectStream();
      readRoute();
    }).catch(function (error) {
      if (error && error.auth) return;
      armRetry();
    });
  }

  function rememberedToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (error) { return ""; }
  }

  function loginWithPaste(raw) {
    var field = $("gate-token");
    var value = raw == null ? (field ? field.value : "") : raw;
    if (field) field.value = "";
    if (state.loginPending) return;
    var parsed = tokenFromPaste(value, location.origin);
    if (!parsed.token) {
      showGate(parsed.message);
      return;
    }
    state.loginPending = true;
    state.token = parsed.token;
    call("/api/state").then(function () {
      try { localStorage.setItem(TOKEN_KEY, parsed.token); } catch (error) { /* private mode */ }
      state.loginPending = false;
      openDesk();
    }).catch(function (error) {
      state.loginPending = false;
      if (field) field.value = "";
      if (error && error.auth) return;
      state.token = rememberedToken();
      if (error && error.status) {
        showGate("The desk did not finish login. Paste the token and try again.");
        return;
      }
      showGate("The desk could not be reached. Paste the token and try again.");
    });
  }

  function bindGate() {
    var form = $("gate-form");
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    form.addEventListener("submit", function (event) {
      if (event && event.preventDefault) event.preventDefault();
      loginWithPaste(null);
    });
  }

  function boot() {
    if (root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      document.documentElement.classList.add("reduce");
    }
    bindGate();
    registerWorker();
    if (queryHasToken(location.search)) {
      history.replaceState(null, "", location.pathname + stripTokenQuery(location.search));
      showGate("Open the desk link that puts the token after #. A token in the query string is ignored.");
      return;
    }
    var parsed = readFragmentToken(location.hash);
    if (parsed.token) {
      try { localStorage.setItem(TOKEN_KEY, parsed.token); } catch (error) { /* private mode */ }
      history.replaceState(null, "", location.pathname + location.search + parsed.hash);
    }
    var saved = rememberedToken();
    if (!saved) {
      showGate("This phone keeps its own login. Paste the token, or the private link that ends with #t=. It is saved only on this device.");
      return;
    }
    state.token = saved;
    openDesk();
  }

  root.Desk.loginWithPaste = loginWithPaste;
  root.Desk.loadPushKey = loadPushKey;
  root.Desk.enablePush = enablePush;
  root.Desk.subscriptionKeys = subscriptionKeys;

  function bind() {
    if (state.bound) return;
    state.bound = true;
    $("avatar").addEventListener("click", function () { go("account"); });
    $("brand").addEventListener("click", function () { go("home"); });
    $("bell").addEventListener("click", function () {
      state.unread = 0;
      $("bell-dot").hidden = true;
      go("activity");
    });
    $("see-all").addEventListener("click", function () { go("activity"); });
    $("search-open").addEventListener("click", function () {
      $("search-panel").hidden = !$("search-panel").hidden;
      if (!$("search-panel").hidden) $("search").focus();
    });
    $("search").addEventListener("input", function () {
      root.clearTimeout(state.searchTimer);
      var q = $("search").value.trim();
      state.searchTimer = root.setTimeout(function () { runSearch(q); }, 200);
    });
    $("range").addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest("button") : event.target;
      if (!button || !button.getAttribute) return;
      var range = button.getAttribute("data-range");
      if (!range) return;
      state.range = range;
      call("/api/chart?range=" + encodeURIComponent(range)).then(function (body) {
        state.chart = body;
        drawHomeChart();
        renderHome();
      }).catch(function (error) { if (!(error && error.auth)) armRetry(); });
    });
    $("tab-home").addEventListener("click", function () { go("home"); });
    $("tab-agents").addEventListener("click", function () { go("agents"); });
    $("tab-activity").addEventListener("click", function () { go("activity"); });
    $("tab-account").addEventListener("click", function () { go("account"); });
    $("activity-more").addEventListener("click", function () {
      var rows = (state.activity && state.activity.rows) || [];
      if (!rows.length) return;
      call("/api/activity?before=" + encodeURIComponent(rows[rows.length - 1].ts)).then(function (body) {
        state.activity.rows = rows.concat(body.rows || []);
        renderActivity();
      }).catch(function (error) { if (!(error && error.auth)) armRetry(); });
    });
    $("live-go").addEventListener("click", function () {
      var typed = $("live-text").value;
      if (typed !== "LIVE") {
        $("account-note").textContent = "Type LIVE to confirm. Nothing was changed.";
        return;
      }
      call("/api/mode", { method: "POST", body: { mode: "LIVE", confirm: typed } }).then(function () {
        $("live-text").value = "";
        $("live-confirm").hidden = true;
        return loadAll();
      }).catch(function (error) {
        if (error && error.auth) return;
        if (error && error.status === 409) {
          $("account-note").textContent = "LIVE locked. A full PAPER night is not on record.";
          return;
        }
        noteError(error);
      });
    });
    $("audit-back").addEventListener("click", function () { go("home"); });
    $("coin-back").addEventListener("click", function () { go("home"); });
    root.addEventListener("hashchange", readRoute);
    root.addEventListener("resize", function () {
      if (state.screen === "home") drawHomeChart();
      if (state.screen === "coin") renderCoin();
    });
  }

  function runSearch(q) {
    var box = $("search-results");
    clear(box);
    if (!q) return;
    call("/api/search?q=" + encodeURIComponent(q)).then(function (body) {
      var rows = body.rows || [];
      if (!rows.length) {
        box.appendChild(el("p", "No match", "muted"));
        return;
      }
      rows.forEach(function (row) {
        var button = el("button", (row.symbol || row.name || "Coin") + " · " + (row.mint || ""), "search-hit");
        button.type = "button";
        button.addEventListener("click", function () {
          $("search-panel").hidden = true;
          openCoin(row.mint);
        });
        box.appendChild(button);
      });
    }).catch(function (error) { if (!(error && error.auth)) armRetry(); });
  }

  boot();
})(typeof globalThis !== "undefined" ? globalThis : this);
