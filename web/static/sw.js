/* Clip Engine — Service Worker v3 (revamp)
 *
 * Strategy:
 *   - Static shell assets: cache-first (install precache, serve from cache, update in background)
 *   - /api/*: network-only (never cache API responses — state lives on server)
 *   - clip video + thumb endpoints: network-only
 */

const CACHE_NAME = 'clip-engine-static-v12';

const PRECACHE = [
  '/',
  '/index.html',
  '/styles.css',
  '/app.js',
  '/api.js',
  '/queue.js',
  '/campaigns.js',
  '/analytics.js',
  '/sources.js',
  '/fixtures.js',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon.svg',
];

// ── Install: precache static shell ──────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      // addAll fails if any request fails; wrap each individually so a
      // missing asset does not block the entire install.
      return Promise.allSettled(
        PRECACHE.map((url) =>
          cache.add(url).catch(() => { /* non-fatal: missing asset at install time */ })
        )
      );
    })
  );
  // Take control immediately; do not wait for tabs to reload.
  self.skipWaiting();
});

// ── Activate: purge stale caches ─────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 1. Never intercept API calls, non-GET, cross-origin requests (presigned
  //    R2 media), media elements, or Range requests. iOS Safari refuses to
  //    play <video> whose range requests are answered through a service
  //    worker respondWith() — the hero video renders blank on mobile.
  if (
    event.request.method !== 'GET' ||
    url.origin !== self.location.origin ||
    url.pathname.startsWith('/api/') ||
    event.request.headers.has('range') ||
    event.request.destination === 'video' ||
    event.request.destination === 'audio'
  ) {
    // Network-only — let the request fall through without touching cache.
    return;
  }

  // 2. Cache-first for static shell assets.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) {
        // Serve from cache; revalidate in background.
        fetch(event.request)
          .then((fresh) => {
            if (fresh && fresh.ok) {
              caches.open(CACHE_NAME).then((c) => c.put(event.request, fresh.clone()));
            }
          })
          .catch(() => {/* offline — cache already served */});
        return cached;
      }
      // Not in cache — fetch, cache, return.
      return fetch(event.request).then((response) => {
        if (response && response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((c) => c.put(event.request, clone));
        }
        return response;
      });
    })
  );
});
