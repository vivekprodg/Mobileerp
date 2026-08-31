/**
 * PWA Service Worker for Mobile Shop & Optical Store ERP (Nepal)
 * Cache Version: v3.1.0 (Camera Barcode Scanner & Offline Hardware Architecture)
 * 
 * Features:
 * - Pre-caches core styles, camera barcode scanner engine, audio chime feedback, and offline pages.
 * - Stale-While-Revalidate caching strategy for static scripts and CSS assets.
 * - Network-First strategy with resilient cache fallback for catalog search APIs and date converters.
 * - Offline navigation fallback to /offline/ when network connection is unavailable.
 */

const CACHE_NAME = 'mobile-shop-erp-v3.1.0';

const STATIC_ASSETS = [
    '/',
    '/static/css/custom.css',
    '/static/css/pos.css',
    '/static/css/print_thermal.css',
    '/static/css/print_a4.css',
    '/static/css/repair.css',
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
    '/static/manifest.json',
    '/offline/'
];

// =============================================================================
// INSTALL EVENT: Pre-cache shell assets and camera scanning engine
// =============================================================================
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(STATIC_ASSETS);
        }).then(() => self.skipWaiting())
    );
});

// =============================================================================
// ACTIVATE EVENT: Clean up deprecated cache versions
// =============================================================================
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

// =============================================================================
// FETCH EVENT: Intelligent Caching & Offline Routing
// =============================================================================
self.addEventListener('fetch', (event) => {
    const request = event.request;
    const url = new URL(request.url);

    // Skip non-GET requests (e.g. POST checkout or API commits)
    if (request.method !== 'GET' || !url.protocol.startsWith('http')) return;

    // 1. Static JavaScript & CSS Assets: Stale-While-Revalidate
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

    // 2. Read-Only Search & Date Converter APIs: Network-First with Stale Cache Fallback
    if (url.pathname.includes('/api/search/') || url.pathname.includes('/api/convert-date/')) {
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

    // 3. HTML Navigation: Network-First with Fallback to /offline/
    event.respondWith(
        fetch(request).catch(() => {
            return caches.match(request).then((response) => {
                return response || caches.match('/offline/');
            });
        })
    );
});