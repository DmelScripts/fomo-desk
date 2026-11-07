/* App shell only. Authenticated responses and token URLs are never cached. */
var SHELL = "desk-shell-v3";

function deskCacheAllowed(request) {
  var url;
  try {
    url = new URL(request.url);
  } catch (error) {
    return false;
  }
  if (url.origin !== self.location.origin) return false;
  if (request.method && request.method !== "GET") return false;
  if (request.headers && request.headers.has && request.headers.has("authorization")) return false;
  var path = url.pathname;
  if (path === "/api" || path.indexOf("/api/") === 0) return false;
  if (path === "/vapid_public.txt") return false;
  var banned = ["t", "token", "app_token", "APP_TOKEN"];
  for (var i = 0; i < banned.length; i++) {
    if (url.searchParams.has(banned[i])) return false;
  }
  if (path === "/" || path === "/index.html" || path === "/manifest.json") return true;
  return path.indexOf("/icons/") === 0 || /\.(css|js|png)$/.test(path);
}

self.deskCacheAllowed = deskCacheAllowed;

self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (key) {
      if (key === SHELL) return Promise.resolve(false);
      return caches.delete(key);
    }));
  }).then(function () {
    return self.clients.claim();
  }));
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (!deskCacheAllowed(request)) {
    event.respondWith(fetch(request, { cache: "no-store" }));
    return;
  }
  event.respondWith(
    fetch(request, { cache: "no-cache" }).then(function (response) {
      if (response && response.ok) {
        var copy = response.clone();
        caches.open(SHELL).then(function (cache) {
          cache.put(request, copy);
        });
      }
      return response;
    }).catch(function () {
      return caches.match(request).then(function (hit) {
        return hit || caches.match("/index.html");
      });
    })
  );
});

function deskNotificationUrl(data) {
  if (data && typeof data.url === "string" && data.url) return data.url;
  return "/";
}

self.deskNotificationUrl = deskNotificationUrl;

self.addEventListener("push", function (event) {
  var payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (error) {
    payload = {};
  }
  var title = payload.title || "Grok Bot";
  var body = payload.body || "";
  var url = deskNotificationUrl(payload);
  event.waitUntil(self.registration.showNotification(title, {
    body: body,
    data: { url: url },
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = deskNotificationUrl(event.notification && event.notification.data);
  event.waitUntil(self.clients.openWindow(url));
});
