/*
 * Crossword app service worker.
 *
 * Goal: make repeat visits nearly free. It caches ONLY things that are safe to
 * share between users and rarely change:
 *   - /static/*  (icons, progress SVGs, sounds, manifest)
 *   - the Tailwind CDN script (big, third-party, same on every page)
 *   - /shell/*   (empty game page HTML shells - no per-user or puzzle data)
 *
 * When navigating to a game page (/wordle/..., /spelling-bee/...,
 * /connections/..., /play/...), the cached shell is served immediately and the
 * page fetches only the small JSON payload for that puzzle + saved progress.
 *
 * VERSION is stamped automatically from a hash of sw.js + game templates when
 * app.py serves /sw.js, so any template or worker change invalidates old caches
 * on the next reload.
 */
const VERSION = '__SW_VERSION__';
const CACHE = `crossword-static-${VERSION}`;

const CDN_HOSTS = ['cdn.tailwindcss.com'];

// Fetched (best effort) when the worker installs. Anything missing is skipped.
const PRECACHE = [
  '/static/icon/apple-touch-icon.png',
  '/static/icon/192.png',
  '/static/icon/512.png',
  '/static/site.webmanifest',
  'https://cdn.tailwindcss.com',
  '/shell/wordle',
  '/shell/spelling-bee',
  '/shell/connections',
  '/shell/play',
];

const SHELL_ROUTES = [
  { pattern: /^\/wordle\/\d{4}\/\d{1,2}\/\d{1,2}\/?$/, shell: '/shell/wordle' },
  { pattern: /^\/spelling-bee\/\d{4}\/\d{1,2}\/\d{1,2}\/?$/, shell: '/shell/spelling-bee' },
  { pattern: /^\/connections\/\d{4}\/\d{1,2}\/\d{1,2}\/?$/, shell: '/shell/connections' },
  { pattern: /^\/play\/[^/]+\/\d{4}\/\d{1,2}\/[^/]+\/?$/, shell: '/shell/play' },
];

function isSameOrigin(url) {
  return url.origin === self.location.origin;
}

function matchShell(pathname) {
  for (const r of SHELL_ROUTES) {
    if (r.pattern.test(pathname)) return r.shell;
  }
  return null;
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

async function handleGameNavigation(request, shellPath) {
  const cache = await caches.open(CACHE);
  const shellUrl = new URL(shellPath, self.location.origin).href;
  const hit = await cache.match(shellUrl);
  if (hit) return hit;

  // Shell not cached yet: let the normal SSR request serve the full page, and
  // warm the shell in the background for next time.
  cache.add(new Request(shellUrl, { cache: 'reload' })).catch(() => {});
  return fetch(request);
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  // Media elements use Range requests; answering those with a full 200 from
  // the cache breaks playback in Safari, so let the network handle them.
  if (request.headers.has('range')) return;

  const url = new URL(request.url);

  if (request.mode === 'navigate' && isSameOrigin(url)) {
    const shell = matchShell(url.pathname);
    if (shell) {
      event.respondWith(handleGameNavigation(request, shell));
      return;
    }
  }

  const isStatic = isSameOrigin(url) && url.pathname.startsWith('/static/');
  const isCdn = CDN_HOSTS.includes(url.hostname);
  if (!isStatic && !isCdn) return;          // other pages + API: straight to network

  event.respondWith(cacheFirst(request, url));
});
