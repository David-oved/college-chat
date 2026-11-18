// Minimal service worker for the college chatbot PWA.
// Cache-first for the app shell, network-first fallback for everything else.
// Never intercepts POST/streaming API calls.

const CACHE_NAME = 'college-chat-v13';
const APP_SHELL = [
  '/',
  '/manifest.json',
  '/admin',
  '/admin-manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

// מקבל Push אמיתי מהשרת (כשversion.json ב-GitHub משתנה) ומציג התראת מערכת -
// זה רץ גם כשהאפליקציה סגורה לגמרי, כי ה-service worker מתעורר ברקע ע"י הדפדפן/מערכת ההפעלה.
self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = {};
  }
  const title = data.title || '🔄 גרסה חדשה זמינה';
  const body = data.body || 'עדכן עכשיו';
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/icons/icon-192.png',
      badge: '/icons/icon-192.png',
      dir: 'rtl',
      data: { url: '/' }
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow('/');
    })
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Only ever handle same-origin GET requests; never touch POST/streaming API calls.
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never cache/intercept API calls (auth, conversations, chat streaming, etc.)
  if (url.pathname.startsWith('/api/')) return;

  // Network first (the newest page whenever there is a connection), the saved copy only when offline.
  event.respondWith(
    fetch(request, { cache: 'no-cache' })
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});
