/**
 * IndexedDB Local Storage Wrapper for Offline POS Caching
 */
class IDBStorageService {
    static DB_NAME = 'MobileShopOfflineDB';
    static DB_VERSION = 1;

    static async openDB() {
        return new Promise((resolve, reject) => {
            const request = indexedDB.open(this.DB_NAME, this.DB_VERSION);

            request.onupgradeneeded = (event) => {
                const db = event.target.result;
                if (!db.objectStoreNames.contains('products')) {
                    db.createObjectStore('products', { keyPath: 'id' });
                }
                if (!db.objectStoreNames.contains('pending_sales')) {
                    db.createObjectStore('pending_sales', { keyPath: 'client_temp_id' });
                }
                if (!db.objectStoreNames.contains('held_carts')) {
                    db.createObjectStore('held_carts', { keyPath: 'hold_reference' });
                }
            };

            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    static async set(storeName, value) {
        const db = await this.openDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, 'readwrite');
            const store = tx.objectStore(storeName);
            store.put(value);
            tx.oncomplete = () => resolve(true);
            tx.onerror = () => reject(tx.error);
        });
    }

    static async getAll(storeName) {
        const db = await this.openDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, 'readonly');
            const store = tx.objectStore(storeName);
            const request = store.getAll();
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
    }

    static async delete(storeName, key) {
        const db = await this.openDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction(storeName, 'readwrite');
            const store = tx.objectStore(storeName);
            store.delete(key);
            tx.oncomplete = () => resolve(true);
            tx.onerror = () => reject(tx.error);
        });
    }
}