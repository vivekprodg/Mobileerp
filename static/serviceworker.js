const CACHE_NAME = 'mobile-shop-erp-v3.5.0';

const STATIC_ASSETS = [
    '/offline/',
    '/static/css/bootstrap.min.css',
    '/static/css/custom.css',
    '/static/css/pos.css',
    '/static/css/print_thermal.css',
    '/static/css/print_a4.css',
    '/static/css/repair.css',
    '/static/vendor/nepali-datepicker/nepali.datepicker.v4.0.7.min.css',
    '/static/vendor/nepali-datepicker/nepali.datepicker.v4.0.7.min.js',
    '/static/js/bootstrap.bundle.min.js',
    '/static/js/global_search.js',
    '/static/js/contextual_search.js',
    '/static/js/pos_engine.js',
    '/static/js/camera_barcode_scanner.js',
    '/static/js/barcode_scanner_listener.js',
    '/static/js/idb_storage.js',
    '/static/js/offline_sync.js',
    '/static/js/nepali_calendar.js',
    '/static/js/thermal_printer.js',
    '/static/js/trade_in_calculator.js',
    '/static/js/repair_camera_capture.js',
    '/static/js/repair_pattern_lock.js',
    '/static/js/pwa_register.js',
    '/static/manifest.json'
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return Promise.all(
                STATIC_ASSETS.map((assetUrl) => {
                    return cache.add(assetUrl).catch((err) => {
                        console.warn(`[ServiceWorker] Skipped non-critical asset during precache: ${assetUrl}`, err);
                    });
                })
            );
        }).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.map((key) => {
                    if (key !== CACHE_NAME) {
                        console.log('[ServiceWorker] Purging legacy cache bucket:', key);
                        return caches.delete(key);
                    }
                })
            );
        }).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    const url = new URL(request.url);

    if (request.method !== 'GET' || !url.protocol.startsWith('http')) return;

    // 1. Static Assets (JS, CSS, Images, Fonts): Stale-While-Revalidate
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(request).then((cachedResponse) => {
                const fetchPromise = fetch(request).then((networkResponse) => {
                    if (networkResponse && networkResponse.status === 200) {
                        const responseToCache = networkResponse.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(request, responseToCache));
                    }
                    return networkResponse;
                }).catch(() => cachedResponse);
                return cachedResponse || fetchPromise;
            })
        );
        return;
    }

    // 2. Live Dynamic Autocomplete Searches (Bypasses Cache to Prevent Stale Empty Lists)
    const isLiveSearchQuery = 
        url.searchParams.has('q') || 
        url.pathname.includes('/products/api/search/') ||
        url.pathname.includes('/purchases/suppliers/api/search/') ||
        url.pathname.includes('/grn/api/search/');

    if (isLiveSearchQuery) {
        event.respondWith(
            fetch(request).catch(() => caches.match(request))
        );
        return;
    }

    // 3. Read-Only Standard Lookup APIs: Network-First with Cache Fallback
    const isLookupApi = 
        url.pathname.includes('/core/api/global-search/') ||
        url.pathname.includes('/inventory/api/') ||
        url.pathname.includes('/customers/api/') ||
        url.pathname.includes('/purchases/api/') ||
        url.pathname.includes('/sales/api/') ||
        url.pathname.includes('/accounting/api/') ||
        url.pathname.includes('/repairs/api/') ||
        url.pathname.includes('/api/convert-date/');

    if (isLookupApi) {
        event.respondWith(
            fetch(request).then((networkResponse) => {
                if (networkResponse && networkResponse.status === 200) {
                    const responseClone = networkResponse.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(request, responseClone));
                }
                return networkResponse;
            }).catch(() => caches.match(request))
        );
        return;
    }

    // 4. HTML Navigation Routes: Network-First with Offline Fallback
    if (request.mode === 'navigate' || (request.headers.get('accept') && request.headers.get('accept').includes('text/html'))) {
        event.respondWith(
            fetch(request).catch(() => {
                return caches.match('/offline/').then((fallback) => {
                    return fallback || new Response(
                        '<h2 style="font-family:sans-serif;text-align:center;margin-top:20vh;">Offline Mode</h2><p style="text-align:center;">Network connection lost. Please verify your connection.</p>',
                        { headers: { 'Content-Type': 'text/html' } }
                    );
                });
            })
        );
        return;
    }
});