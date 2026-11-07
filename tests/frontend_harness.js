"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const TOKEN = "dummy-fragment-token";
const mode = process.argv[2];
const PUSH_ENDPOINT = "https://push.example/phone";

function dummyVapid() {
  const bytes = new Uint8Array(65);
  bytes[0] = 4;
  for (let i = 1; i < bytes.length; i += 1) bytes[i] = 7;
  let binary = "";
  bytes.forEach(function (byte) { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

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
  const sandbox = { console: console, URLSearchParams: URLSearchParams, URL: URL };
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
  const location = { pathname: "/", search: search, hash: hash, origin: "https://desk.test" };
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
      if (String(url) === "/vapid_public.txt") {
        return Promise.resolve({
          ok: true,
          status: 200,
          text() { return Promise.resolve(dummyVapid()); },
        });
      }
      if (String(url) === "/api/push") {
        if (mode === "push-http") {
          return Promise.resolve({
            ok: false,
            status: 400,
            json() { return Promise.resolve({ detail: "endpoint, p256dh, and auth are required" }); },
          });
        }
        if (mode === "push-offline") return Promise.reject(new TypeError("offline"));
        return Promise.resolve({
          ok: true,
          status: 200,
          json() { return Promise.resolve({ ok: true }); },
        });
      }
      if (sandbox.networkFails > 0) {
        sandbox.networkFails -= 1;
        return Promise.reject(new TypeError("offline"));
      }
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
      const status = mode === "auth401" || mode === "paste-401" ? 401 : mode === "paste-500" ? 500 : 200;
      const body = status === 401 ? { detail: "unauthorized" } : (table[String(url)] || {});
      return Promise.resolve({
        ok: status === 200,
        status: status,
        json() { return Promise.resolve(body); },
        body: null,
      });
    },
    URL: URL,
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
    btoa: btoa,
    Uint8Array: Uint8Array,
    confirm() { return false; },
    innerWidth: 390,
    matchMedia() { return { matches: false }; },
    addEventListener() {},
  };
  sandbox.networkFails = mode === "paste-network" ? 1 : 0;
  sandbox.pushOrder = [];
  if (mode.indexOf("push-") === 0) installPushMocks(sandbox);
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
  const origin = "https://desk.test";
  const pasted = Desk.tokenFromPaste("  " + TOKEN + "  ", origin);
  if (pasted.token !== TOKEN || pasted.message) fail("trimmed token was rejected");
  const linked = Desk.tokenFromPaste(origin + "/#t=" + encodeURIComponent(TOKEN), origin);
  if (linked.token !== TOKEN) fail("same-origin link was not read");
  const foreign = Desk.tokenFromPaste("https://evil.example/desk#t=" + encodeURIComponent(TOKEN), origin);
  if (foreign.token) fail("foreign link was accepted");
  if (foreign.message.indexOf(TOKEN) !== -1 || foreign.message.indexOf("evil.example") !== -1) fail("foreign link leaked into the message");
  const queried = Desk.tokenFromPaste(origin + "/?token=" + encodeURIComponent(TOKEN) + "#t=other-token", origin);
  if (queried.token) fail("query token link was accepted");
  if (queried.message.indexOf(TOKEN) !== -1) fail("query link leaked into the message");
  const spaced = Desk.tokenFromPaste(TOKEN + " extra", origin);
  if (spaced.token) fail("spaced paste was accepted");
  const scripted = Desk.tokenFromPaste("javascript:alert(1)", origin);
  if (scripted.token) fail("script link was accepted");
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

function pastedLeak(page) {
  const gate = page.els["gate-text"] ? page.els["gate-text"].textContent : "";
  if (gate.indexOf(TOKEN) !== -1) fail("gate shows the token");
  page.urls.forEach(function (url) {
    if (String(url).indexOf(TOKEN) !== -1) fail("token stayed in the address");
  });
  page.calls.forEach(function (call) {
    if (String(call.url).indexOf(TOKEN) !== -1) fail("token stayed in the request path");
  });
  const field = page.els["gate-token"];
  if (field && field.value) fail("token stayed in the field");
}

if (mode === "paste-ok" || mode === "paste-401" || mode === "paste-500") {
  const page = browser("", "");
  page.localStorage.setItem("desk.token", "already-saved");
  setTimeout(function () {
    page.sandbox.Desk.loginWithPaste("  " + TOKEN + " ");
    setTimeout(function () {
      pastedLeak(page);
      const stored = page.localStorage.getItem("desk.token");
      if (mode === "paste-ok") {
        if (stored !== TOKEN) fail("accepted token was not stored");
        if (page.els.shell.hidden !== false) fail("desk did not open");
        if (page.els.gate.hidden !== true) fail("gate stayed open");
        const authed = page.calls.some(function (call) {
          return call.url === "/api/state" && call.options.headers && call.options.headers.Authorization === "Bearer " + TOKEN;
        });
        if (!authed) fail("paste did not call state with the bearer token");
      } else if (mode === "paste-401") {
        if (stored) fail("401 kept a credential");
        if (page.els.shell.hidden === false) fail("401 opened the desk");
        if (page.els["gate-text"].textContent.indexOf("refused") === -1) fail("401 did not refuse");
      } else {
        if (stored !== "already-saved") fail("server error changed the saved credential");
        if (page.els.shell.hidden === false) fail("server error opened the desk");
        if (page.els["gate-text"].textContent.indexOf("did not finish") === -1) fail("server error was not retryable");
      }
      process.exit(0);
    }, 40);
  }, 20);
}

if (mode === "paste-network") {
  const page = browser("", "");
  page.localStorage.setItem("desk.token", "already-saved");
  setTimeout(function () {
    page.sandbox.Desk.loginWithPaste(TOKEN);
    setTimeout(function () {
      pastedLeak(page);
      if (page.localStorage.getItem("desk.token") !== "already-saved") fail("network error changed the saved credential");
      if (page.els["gate-text"].textContent.indexOf("could not be reached") === -1) fail("offline message missing");
      page.sandbox.Desk.loginWithPaste("https://desk.test/#t=" + encodeURIComponent(TOKEN));
      setTimeout(function () {
        pastedLeak(page);
        if (page.localStorage.getItem("desk.token") !== TOKEN) fail("retry after offline did not save");
        if (page.els.gate.hidden !== true) fail("retry left the gate open");
        process.exit(0);
      }, 40);
    }, 30);
  }, 20);
}

if (mode === "paste-reject") {
  const page = browser("", "");
  setTimeout(function () {
    page.sandbox.Desk.loginWithPaste("https://evil.example/#t=" + encodeURIComponent(TOKEN));
    page.sandbox.Desk.loginWithPaste("https://desk.test/?token=" + encodeURIComponent(TOKEN));
    pastedLeak(page);
    if (page.localStorage.getItem("desk.token")) fail("rejected paste was stored");
    if (page.calls.length) fail("rejected paste called the desk");
    const gate = page.els["gate-text"].textContent;
    if (gate.indexOf("different desk") === -1 && gate.indexOf("query string") === -1) fail("rejection was not explained");
    process.exit(0);
  }, 20);
}

function installPushMocks(sandbox) {
  const registration = {
    pushManager: {
      getSubscription() {
        sandbox.pushOrder.push("getSubscription");
        return Promise.resolve(null);
      },
      subscribe(opts) {
        sandbox.pushOrder.push("subscribe");
        if (!opts || opts.userVisibleOnly !== true) fail("subscribe was not visible");
        const key = opts.applicationServerKey;
        if (!key || key.length !== 65 || key[0] !== 4) fail("applicationServerKey was not a public point");
        if (mode === "push-blocked") {
          const error = new Error("blocked");
          error.name = "NotAllowedError";
          error.message = PUSH_ENDPOINT;
          return Promise.reject(error);
        }
        return Promise.resolve({
          endpoint: PUSH_ENDPOINT,
          toJSON() {
            return { endpoint: PUSH_ENDPOINT, keys: { p256dh: "p256-dummy", auth: "auth-dummy" } };
          },
          getKey() { return null; },
        });
      },
    },
  };
  sandbox.PushManager = function PushManager() {};
  sandbox.Notification = {
    permission: "default",
    requestPermission() {
      sandbox.pushOrder.push("requestPermission");
      if (mode === "push-denied") return Promise.resolve("denied");
      this.permission = "granted";
      return Promise.resolve("granted");
    },
  };
  sandbox.navigator.standalone = true;
  sandbox.navigator.serviceWorker = {
    register() {
      sandbox.pushOrder.push("register");
      return Promise.resolve(registration);
    },
    ready: Promise.resolve(registration),
    addEventListener() {},
  };
}

function noteHasSecret(text) {
  return text.indexOf(PUSH_ENDPOINT) !== -1 || text.indexOf("p256-dummy") !== -1 || text.indexOf("auth-dummy") !== -1;
}

if (mode.indexOf("push-") === 0) {
  const page = browser("", "");
  setTimeout(function () {
    page.sandbox.Desk.loadPushKey();
    setTimeout(function () {
      page.sandbox.pushOrder = [];
      page.calls.length = 0;
      page.sandbox.Desk.enablePush();
      if (mode !== "push-denied" && mode !== "push-save" && mode !== "push-blocked" && mode !== "push-http" && mode !== "push-offline") {
        fail("unknown push mode");
      }
      if (page.sandbox.pushOrder[0] !== "requestPermission") fail("permission was not requested on the tap");
      setTimeout(function () {
        const note = page.els["account-note"] ? page.els["account-note"].textContent : "";
        if (noteHasSecret(note)) fail("subscription secret leaked into the note");
        const post = page.calls.find(function (call) { return call.url === "/api/push"; });
        if (mode === "push-denied") {
          if (page.sandbox.pushOrder.indexOf("subscribe") !== -1) fail("denied permission still subscribed");
          if (post) fail("denied permission still saved");
          if (note.indexOf("blocked") === -1) fail("denied permission was not explained");
        }
        if (mode === "push-blocked") {
          if (post) fail("blocked subscribe still saved");
          if (note.indexOf("blocked") === -1) fail("subscribe refusal was not explained");
        }
        if (mode === "push-save") {
          if (!post) fail("subscription was not posted");
          const type = post.options.headers && post.options.headers["Content-Type"];
          if (!type || type.indexOf("application/json") !== 0) fail("subscription was not sent as json");
          const body = JSON.parse(post.options.body);
          if (body.endpoint !== PUSH_ENDPOINT || body.p256dh !== "p256-dummy" || body.auth !== "auth-dummy") fail("subscription shape");
          if (body.keys) fail("subscription nested the keys");
          if (note.indexOf("Subscription saved.") === -1) fail("save was not confirmed");
        }
        if (mode === "push-http") {
          if (!post) fail("http failure never posted");
          if (note.indexOf("HTTP 400") === -1 || note.indexOf("did not save") === -1) fail("http save failure was not explained");
        }
        if (mode === "push-offline") {
          if (note.indexOf("could not be reached") === -1) fail("offline save was not explained");
          if (note.indexOf("did not create") !== -1) fail("offline save looked like a phone failure");
        }
        process.exit(0);
      }, 40);
    }, 20);
  }, 20);
}

if (mode === "sw") {
  const opened = [];
  const deleted = [];
  const listeners = {};
  let claimed = false;
  const sandbox = {
    console: console,
    URL: URL,
    fetch() { return Promise.resolve({ ok: true, clone() { return this; } }); },
    caches: {
      open(name) { opened.push(name); return Promise.resolve({ put() {} }); },
      match() { return Promise.resolve(undefined); },
      keys() { return Promise.resolve(["desk-shell-v1", "desk-shell-v2", "desk-shell-v3"]); },
      delete(name) { deleted.push(name); return Promise.resolve(true); },
    },
    self: null,
  };
  sandbox.self = {
    location: { origin: "https://desk.test" },
    addEventListener(name, fn) { listeners[name] = fn; },
    skipWaiting() {},
    clients: {
      claim() { claimed = true; return Promise.resolve(); },
      openWindow() { return Promise.resolve(); },
    },
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
  const activate = listeners.activate;
  if (typeof activate !== "function") fail("activate hook missing");
  let waited = null;
  activate({ waitUntil(work) { waited = work; } });
  Promise.resolve(waited).then(function () {
    if (deleted.indexOf("desk-shell-v1") === -1 || deleted.indexOf("desk-shell-v2") === -1) fail("old shell cache was kept");
    if (deleted.indexOf("desk-shell-v3") !== -1) fail("current shell cache was deleted");
    if (!claimed) fail("new worker did not claim the page");
    process.exit(0);
  }).catch(function (error) {
    fail(error && error.message ? error.message : "activate failed");
  });
}
