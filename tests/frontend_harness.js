"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const TOKEN = "dummy-fragment-token";
const mode = process.argv[2];

function fail(message) {
  process.stderr.write(message + "\n");
  process.exit(1);
}

function load(file, sandbox) {
  const code = fs.readFileSync(path.join(root, file), "utf8");
  vm.runInNewContext(code, sandbox, { filename: file });
  return sandbox;
}

function pureDesk() {
  const sandbox = { console: console, URLSearchParams: URLSearchParams };
  sandbox.globalThis = sandbox;
  load("web/app.js", sandbox);
  if (!sandbox.Desk || typeof sandbox.Desk.readFragmentToken !== "function") fail("Desk helpers missing");
  return sandbox.Desk;
}

function makeEl(id) {
  const seeded = {
    "chart-empty": "Waiting for the first trade",
    "home-activity-empty": "Nothing yet tonight",
    "activity-empty": "Nothing yet tonight",
    "audit-empty": "No coin taken apart yet",
  };
  const node = {
    hidden: true,
    className: "",
    style: {},
    dataset: {},
    value: "",
    disabled: false,
    children: [],
    _text: seeded[id] || "",
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) {
      if (String(value).indexOf(TOKEN) !== -1) fail("token leaked into an attribute");
      this[name] = value;
    },
    getAttribute(name) { return this[name] == null ? null : String(this[name]); },
    addEventListener() {},
    focus() {},
    getContext() {
      const gradient = { addColorStop() {} };
      return new Proxy({}, {
        get(_target, prop) {
          if (prop === "createLinearGradient") return function () { return gradient; };
          if (prop === "measureText") return function () { return { width: 40 }; };
          return function () {};
        },
        set(target, prop, value) { target[prop] = value; return true; },
      });
    },
    getBoundingClientRect() { return { width: 358, height: 180, top: 0, left: 0 }; },
    closest() { return null; },
  };
  Object.defineProperty(node, "textContent", {
    get() { return this._text; },
    set(value) {
      const text = value == null ? "" : String(value);
      if (text.indexOf(TOKEN) !== -1) fail("token leaked into text");
      this._text = text;
      this.children = [];
    },
  });
  Object.defineProperty(node, "innerHTML", {
    get() { return ""; },
    set(value) {
      if (String(value).indexOf(TOKEN) !== -1) fail("token leaked into html");
    },
  });
  return node;
}

function browser(search, hash) {
  const els = {};
  const location = { pathname: "/", search: search, hash: hash };
  const urls = [];
  const calls = [];
  const document = {
    documentElement: makeEl(),
    getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
    createElement() { return makeEl(); },
    querySelectorAll(selector) {
      if (selector === "#range button") return [0, 1, 2, 3, 4, 5].map(makeEl);
      return [];
    },
  };
  const localStorage = {
    store: {},
    getItem(key) { return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null; },
    setItem(key, value) { this.store[key] = String(value); },
    removeItem(key) { delete this.store[key]; },
  };
  const sandbox = {
    console: {
      log() { fail("console.log is not used"); },
      error() {},
      warn() {},
    },
    document: document,
    location: location,
    history: {
      replaceState(_state, _title, url) { urls.push(url); location.hash = ""; },
      pushState(_state, _title, url) { urls.push(url); },
    },
    localStorage: localStorage,
    navigator: { serviceWorker: { register() { return Promise.resolve(); } }, standalone: false },
    fetch(url, options) {
      calls.push({ url: String(url), options: options || {} });
      if (String(url).indexOf("/api/stream") === 0) {
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('event: exit\ndata: {"mint":"Mint"}\n\n'));
          },
        });
        return Promise.resolve({ ok: true, status: 200, body: stream });
      }
      const table = {
        "/api/state": emptyState(),
        "/api/chart?range=LIVE": { range: "LIVE", start: null, points: [], marks: [] },
        "/api/activity": { rows: [] },
        "/api/account": emptyAccount(),
        "/api/settings": { groups: {} },
      };
      const status = mode === "auth401" ? 401 : 200;
      const body = status === 401 ? { detail: "unauthorized" } : (table[String(url)] || {});
      return Promise.resolve({
        ok: status === 200,
        status: status,
        json() { return Promise.resolve(body); },
        body: null,
      });
    },
    URLSearchParams: URLSearchParams,
    TextDecoder: TextDecoder,
    TextEncoder: TextEncoder,
    ReadableStream: ReadableStream,
    Intl: Intl,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    requestAnimationFrame(fn) { fn(); return 1; },
    atob: atob,
    confirm() { return false; },
    innerWidth: 390,
    matchMedia() { return { matches: false }; },
    addEventListener() {},
  };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  load("web/app.js", sandbox);
  return { sandbox: sandbox, location: location, localStorage: localStorage, urls: urls, calls: calls, els: els };
}

function emptyState() {
  return {
    balance: 200,
    today_pct: 0,
    multiplier: 1,
    mode: "PAPER",
    open_positions: 0,
    fixture: false,
    agents: [
      { name: "scanner", running: false, paused: false, count: 0, line: "Listening for graduations", feed: [] },
      { name: "auditor", running: false, paused: false, count: 0, line: "No coin taken apart yet", layers: [], rejected: 0 },
      { name: "analyst", running: false, paused: false, count: 0, score: null, min_score: 70 },
      { name: "executor", running: false, paused: false, count: 0, stack_pct: 8, last_size_usd: null, last_status: null },
      { name: "risk", running: false, paused: false, count: 0, stop_pct: 6, trail_pct: 12, pause_locked: false },
    ],
  };
}

function emptyAccount() {
  return {
    mode: "PAPER",
    stack: 200,
    profile_url: null,
    signup_url: "https://example.test/invite",
    analyst_mode: "rules",
    integrations: {
      email: { ready: false, detail: "Email delivery is not configured." },
      telegram: { ready: false, detail: "Telegram was skipped." },
      web_push: { ready: false, detail: "Sending notifications is not running yet." },
    },
    jev: { configured: false, host_locked: false, detail: "No Jev key is saved." },
  };
}

if (mode === "pure") {
  const Desk = pureDesk();
  const parsed = Desk.readFragmentToken("#t=" + encodeURIComponent(TOKEN));
  if (parsed.token !== TOKEN) fail("fragment token was not read");
  if (parsed.hash !== "") fail("fragment was not consumed");
  if (!Desk.queryHasToken("?token=" + TOKEN)) fail("query token was not detected");
  if (Desk.stripTokenQuery("?token=" + TOKEN + "&range=LIVE") !== "?range=LIVE") fail("query token was not stripped");
  const sse = Desk.parseSse('event: balance\ndata: {"value":1}\n\nleftover');
  if (sse.events.length !== 1 || sse.events[0].event !== "balance" || sse.events[0].data.value !== 1) fail("sse parse failed");
  if (sse.rest !== "leftover") fail("sse rest failed");
  const stopped = Desk.markView({ kind: "stopped", title: "Stopped X", subtitle: null, value: null, ts: 1 });
  if (!stopped || stopped.text.indexOf("$0") !== -1 || stopped.text !== "Stopped X") fail("missing money became a number");
  const bought = Desk.markView({ kind: "bought", title: "Bought X", ts: 2 });
  if (!bought || bought.text !== "Bought X" || bought.color !== "#0a84ff") fail("bought mark");
  process.exit(0);
}

if (mode === "fragment" || mode === "auth401") {
  const page = browser("", "#t=" + encodeURIComponent(TOKEN));
  setTimeout(function () {
    const stored = page.localStorage.getItem("desk.token");
    const gate = page.els["gate-text"] ? page.els["gate-text"].textContent : "";
    if (gate.indexOf(TOKEN) !== -1) fail("gate shows the token");
    page.urls.forEach(function (url) {
      if (String(url).indexOf(TOKEN) !== -1) fail("token stayed in the address");
    });
    if (mode === "fragment") {
      if (stored !== TOKEN) fail("fragment token was not stored");
      if (page.location.hash.indexOf("t=") !== -1) fail("fragment was not removed");
    }
    if (mode === "auth401") {
      if (stored) fail("refused token was kept");
      if (gate.indexOf("refused") === -1) fail("401 did not show the gate");
      const authed = page.calls.some(function (call) {
        const header = call.options.headers && call.options.headers.Authorization;
        return header === "Bearer " + TOKEN;
      });
      if (!authed) fail("401 request did not use the bearer header");
      const streamed = page.calls.some(function (call) { return String(call.url).indexOf("EventSource") !== -1; });
      if (streamed) fail("EventSource was used");
    }
    process.exit(0);
  }, 30);
}

if (mode === "query") {
  const page = browser("?token=" + encodeURIComponent(TOKEN), "");
  setTimeout(function () {
    if (page.localStorage.getItem("desk.token")) fail("query token was stored");
    const gate = page.els["gate-text"] ? page.els["gate-text"].textContent : "";
    if (gate.indexOf(TOKEN) !== -1) fail("query token was shown");
    if (!page.urls.length || String(page.urls[0]).indexOf("token=") !== -1) fail("query token was left in the address");
    if (page.calls.length) fail("query token triggered an API call");
    process.exit(0);
  }, 20);
}

if (mode === "sse") {
  const page = browser("", "#t=" + encodeURIComponent(TOKEN));
  setTimeout(function () {
    const stream = page.calls.find(function (call) { return call.url === "/api/stream"; });
    if (!stream) fail("stream was not opened with fetch");
    if (!stream.options.headers || stream.options.headers.Authorization !== "Bearer " + TOKEN) fail("stream missed the bearer header");
    if (stream.options.headers.Authorization.indexOf("EventSource") !== -1) fail("EventSource");
    const dot = page.els["bell-dot"];
    if (!dot || dot.hidden !== false) fail("exit event did not mark the bell");
    const home = page.els["home-activity-empty"];
    if (!home || home.textContent !== "Nothing yet tonight") fail("empty activity copy");
    const chart = page.els["chart-empty"];
    if (!chart || chart.hidden !== false || chart.textContent !== "Waiting for the first trade") fail("empty chart copy");
    process.exit(0);
  }, 40);
}

if (mode === "sw") {
  const opened = [];
  const sandbox = {
    console: console,
    URL: URL,
    fetch() { return Promise.resolve({ ok: true, clone() { return this; } }); },
    caches: { open(name) { opened.push(name); return Promise.resolve({ put() {} }); }, match() { return Promise.resolve(undefined); } },
    self: null,
  };
  sandbox.self = {
    location: { origin: "https://desk.test" },
    addEventListener() {},
    skipWaiting() {},
    clients: { claim() { return Promise.resolve(); }, openWindow() { return Promise.resolve(); } },
    registration: { showNotification() { return Promise.resolve(); } },
  };
  sandbox.globalThis = sandbox;
  load("web/sw.js", sandbox);
  const allow = sandbox.self.deskCacheAllowed;
  if (typeof allow !== "function") fail("cache gate missing");
  function request(url, header) {
    return {
      url: url,
      method: "GET",
      headers: { has(name) { return header === name; } },
    };
  }
  if (allow(request("https://desk.test/api/state"))) fail("api response is cacheable");
  if (allow(request("https://desk.test/app.js?t=" + TOKEN))) fail("token url is cacheable");
  if (allow(request("https://desk.test/app.js?token=1"))) fail("token query is cacheable");
  if (allow(request("https://desk.test/", "authorization"))) fail("authorized navigation is cacheable");
  if (allow(request("https://desk.test/vapid_public.txt"))) fail("push key is cacheable");
  if (!allow(request("https://desk.test/app.css"))) fail("stylesheet was refused");
  if (opened.length) fail("cache opened during the check");
  process.exit(0);
}
