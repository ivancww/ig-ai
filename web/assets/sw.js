const CACHE = 'ig-ai-shell-v1';
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(['/', '/index.html', '/assets/app.css', '/assets/app.js', '/manifest.webmanifest']))));
self.addEventListener('fetch', event => { if (event.request.url.includes('/api/')) return; event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request))); });
