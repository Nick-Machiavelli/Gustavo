const CACHE_NAME = 'gustavo-cache-v4';
const ASSETS_TO_CACHE = [
    './',
    './index.html'
];

// Install Event: Cache the UI Shell
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(ASSETS_TO_CACHE);
        })
    );
    self.skipWaiting();
});

// Activate Event: Cleanup old caches
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
            );
        }).then(() => self.clients.claim())
    );
});

// Fetch Event: Network-First for Data, Cache-First for UI
self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Handle navigation requests (loading the website itself)
    // FIX: Network-first so mobile always gets latest index.html (was cache-first causing stale)
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request)
                .then((response) => {
                    const cln = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put('./index.html', cln));
                    return response;
                })
                .catch(() => caches.match('./index.html').then(r => r || caches.match('./')))
        );
        return;
    }

    // Data handling (JSON files)
    if (url.pathname.endsWith('.json')) {
        event.respondWith(
            fetch(event.request)
                .then((response) => {
                    const cln = response.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, cln));
                    return response;
                })
                .catch(async () => {
                    const cachedResponse = await caches.match(event.request);
                    if (cachedResponse) {
                        console.log('Serving JSON from cache (offline)');
                        return cachedResponse;
                    }
                    // Return a valid empty JSON response instead of undefined to avoid TypeErrors
                    return new Response(JSON.stringify([]), { 
                        status: 200, 
                        headers: { 'Content-Type': 'application/json' } 
                    });
                })
        );
        return;
    }

    // UI and Assets handling (Cache-First)
    event.respondWith(
        caches.match(event.request).then((cachedResponse) => {
            if (cachedResponse) return cachedResponse;
            
            return fetch(event.request).then((networkResponse) => {
                // Cache external CDNs dynamically as they are requested
                if (event.request.url.includes('cdn') || event.request.url.includes('cloudflare') || event.request.url.includes('weserv.nl') || event.request.url.includes('unsplash')) {
                    const cln = networkResponse.clone();
                    caches.open(CACHE_NAME).then(cache => cache.put(event.request, cln));
                }
                return networkResponse;
            }).catch(() => {
                // Fallback for failed asset fetches (like being offline with empty cache)
                return new Response('Network error occurred', { status: 408 });
            });
        })
    );
});