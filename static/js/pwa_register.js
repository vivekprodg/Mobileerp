/**
 * PWA Service Worker Registration & Installation Prompt Handler
 */
(function() {
    'use strict';

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/static/serviceworker.js')
                .then((registration) => {
                    console.log('[PWA] ServiceWorker registered with scope:', registration.scope);
                })
                .catch((error) => {
                    console.error('[PWA] ServiceWorker registration failed:', error);
                });
        });
    }

    let deferredInstallPrompt = null;
    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();
        deferredInstallPrompt = event;

        const installBtn = document.getElementById('pwaInstallBtn');
        if (installBtn) {
            installBtn.classList.remove('d-none');
            installBtn.addEventListener('click', async () => {
                if (deferredInstallPrompt) {
                    deferredInstallPrompt.prompt();
                    const choice = await deferredInstallPrompt.userChoice;
                    if (choice.outcome === 'accepted') {
                        console.log('[PWA] User accepted installation prompt');
                    }
                    deferredInstallPrompt = null;
                    installBtn.classList.add('d-none');
                }
            });
        }
    });
})();