const CACHE = 'hq-v1';
const SHELL = ['/static/style.css', '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }));
  self.skipWaiting();
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }));
});

self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') return;
  var url = new URL(e.request.url);
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(function (r) { return r || fetch(e.request); }));
  } else {
    e.respondWith(fetch(e.request).catch(function () { return caches.match(e.request); }));
  }
});
