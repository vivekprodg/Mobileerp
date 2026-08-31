/**
 * Background Offline Sync Worker
 * Automatically syncs queued offline sales orders once internet connection resumes.
 */
class OfflineSyncManager {
    static async syncPendingSales(csrfToken) {
        if (!navigator.onLine) return;

        const pendingList = await IDBStorageService.getAll('pending_sales');
        if (!pendingList || pendingList.length === 0) return;

        console.log(`[Sync] Found ${pendingList.length} offline orders to sync...`);

        for (const order of pendingList) {
            try {
                const response = await fetch('/sales/api/checkout/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken
                    },
                    body: JSON.stringify(order)
                });

                if (response.ok) {
                    await IDBStorageService.delete('pending_sales', order.client_temp_id);
                    console.log(`[Sync] Synced order: ${order.client_temp_id}`);
                }
            } catch (err) {
                console.error('[Sync] Order sync failed, retrying on next cycle.', err);
                break;
            }
        }
    }
}

window.addEventListener('online', () => {
    const tokenEl = document.querySelector('[name=csrfmiddlewaretoken]');
    const token = tokenEl ? tokenEl.value : '';
    OfflineSyncManager.syncPendingSales(token);
});