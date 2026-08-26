const CACHE = 'hq-v4';
const SHELL = ['/static/style.css', '/static/board.js', '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).catch(function(){}));
  self.skipWaiting();
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }));
  self.clients.claim();
});

self.addEventListener('fetch', function (e) {
  if (e.request.method !== 'GET') return;
  var url = new URL(e.request.url);
  if (url.origin === location.origin && url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(function (r) { return r || fetch(e.request); }));
  }
});

// reminders arrive here even when the app is closed
self.addEventListener('push', function (e) {
  var d = {title: 'Shimon HQ', body: '', url: '/'};
  try { d = Object.assign(d, e.data ? e.data.json() : {}); }
  catch (err) { if (e.data) { d.body = e.data.text(); } }
  e.waitUntil(self.registration.showNotification(d.title, {
    body: d.body,
    icon: '/static/icon-192.png',
    badge: '/static/icon-192.png',
    tag: d.tag || undefined,
    data: {url: d.url || '/'},
    requireInteraction: false
  }));
});

self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].url.indexOf(self.location.origin) === 0 && 'focus' in list[i]) {
        list[i].navigate(target);
        return list[i].focus();
      }
    }
    return clients.openWindow(target);
  }));
});
