/*
 * Crossword app service worker.
 *
 * Goal: make repeat visits nearly free. It caches ONLY things that are safe to
 * share between users and rarely change:
 *   - /static/*  (icons, progress SVGs, sounds, manifest)
 *   - the Tailwind CDN script (big, third-party, same on every page)
 *
 * It deliberately does NOT touch HTML pages or /api/* - those contain
 * per-user data and always go to the network.
 *
 * To force everyone to re-download static assets after you change them,
 * bump VERSION below (then rebuild/restart the web container).
 */
const VERSION = 'v1';
const CACHE = `crossword-static-${VERSION}`;

const CDN_HOSTS = ['cdn.tailwindcss.com'];

// Fetched (best effort) when the worker installs. Anything missing is skipped.
const PRECACHE = [
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/site.webmanifest',
  'https://cdn.tailwindcss.com',
];

function isSameOrigin(url) {
  return url.origin === self.location.origin;
}

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.allSettled(PRECACHE.map(async (u) => {
      const url = new URL(u, self.location.origin);
      // Same-origin: bypass the HTTP cache so a version bump really refetches.
      // Cross-origin: no-cors (script tags load it that way) -> opaque response.
      const req = isSameOrigin(url)
        ? new Request(url, { cache: 'reload' })
        : new Request(url, { mode: 'no-cors' });
      const res = await fetch(req);
      if (res.ok || res.type === 'opaque') await cache.put(url.href, res);
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k.startsWith('crossword-static-') && k !== CACHE)
          .map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

async function cacheFirst(request, url) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(request, { ignoreVary: true });
  if (hit) return hit;

  // Same-origin misses: revalidate with the server rather than trusting a
  // (possibly stale) HTTP-cache entry from before a version bump.
  const req = isSameOrigin(url) ? new Request(request, { cache: 'no-cache' }) : request;
  const res = await fetch(req);
  if (res && (res.ok || res.type === 'opaque')) {
    cache.put(request, res.clone()).catch(() => {});
  }
  return res;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  // Media elements use Range requests; answering those with a full 200 from
  // the cache breaks playback in Safari, so let the network handle them.
  if (request.headers.has('range')) return;

  const url = new URL(request.url);
  const isStatic = isSameOrigin(url) && url.pathname.startsWith('/static/');
  const isCdn = CDN_HOSTS.includes(url.hostname);
  if (!isStatic && !isCdn) return;          // pages + API: straight to network

  event.respondWith(cacheFirst(request, url));
});
