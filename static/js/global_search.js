/**
 * Global Command Palette (Ctrl + K) Search Engine
 * Enterprise ERP - Mobile Shop & Optical Store Management System
 * 
 * Features:
 * - Debounced asynchronous querying (200ms) to /core/api/global-search/
 * - In-flight network request cancellation via AbortController
 * - High-speed keyboard navigation (ArrowDown, ArrowUp, Enter, Escape)
 * - Domain categorization (Products, IMEI/Serials, Customers, Suppliers, Invoices, GRN, Repairs, Accounts)
 * - Active filter chip toggles & automated UI state transitions
 */

(function () {
    'use strict';

    // Domain display configurations and icon mappings
    const DOMAIN_CONFIG = {
        products: {
            label: 'Smartphones & Products',
            icon: 'fas fa-mobile-screen',
            iconBg: 'bg-primary bg-opacity-10 text-primary',
            badgeClass: 'badge-domain-product'
        },
        imei: {
            label: 'IMEI & Serial Devices',
            icon: 'fas fa-barcode',
            iconBg: 'bg-purple bg-opacity-10 text-purple',
            badgeClass: 'badge-domain-imei'
        },
        customers: {
            label: 'Customers & Debtors',
            icon: 'fas fa-users',
            iconBg: 'bg-success bg-opacity-10 text-success',
            badgeClass: 'badge-domain-customer'
        },
        invoices: {
            label: 'Sales Invoices & Bills',
            icon: 'fas fa-file-invoice',
            iconBg: 'bg-info bg-opacity-10 text-info',
            badgeClass: 'badge-domain-invoice'
        },
        repairs: {
            label: 'Workshop & Job Sheets',
            icon: 'fas fa-screwdriver-wrench',
            iconBg: 'bg-warning bg-opacity-10 text-warning',
            badgeClass: 'badge-domain-repair'
        },
        suppliers: {
            label: 'Suppliers & Vendors',
            icon: 'fas fa-truck-ramp-box',
            iconBg: 'bg-secondary bg-opacity-10 text-secondary',
            badgeClass: 'badge-domain-supplier'
        },
        grn: {
            label: 'Goods Received Notes (GRN)',
            icon: 'fas fa-boxes-packing',
            iconBg: 'bg-danger bg-opacity-10 text-danger',
            badgeClass: 'badge-domain-grn'
        },
        accounts: {
            label: 'General Ledger Accounts',
            icon: 'fas fa-book-journal-whills',
            iconBg: 'bg-dark bg-opacity-10 text-dark',
            badgeClass: 'badge-domain-account'
        }
    };

    class GlobalSearchEngine {
        constructor() {
            this.modal = document.getElementById('globalSearchModal');
            this.input = document.getElementById('globalSearchInput');
            this.spinner = document.getElementById('globalSearchSpinner');
            this.clearBtn = document.getElementById('globalSearchClearBtn');
            this.chipsContainer = document.querySelector('.global-search-chips');
            this.filterChips = document.querySelectorAll('.search-filter-chip');
            this.resultsContainer = document.getElementById('globalSearchResultsContainer');
            this.initialState = document.getElementById('globalSearchInitialState');
            this.dynamicResults = document.getElementById('globalSearchDynamicResults');
            this.emptyState = document.getElementById('globalSearchEmptyState');

            this.activeFilter = 'all';
            this.debounceTimer = null;
            this.abortController = null;
            this.selectedIndex = -1;
            this.cachedResults = null;

            this.init();
        }

        init() {
            if (!this.modal || !this.input) return;

            // Debounced search input handler
            this.input.addEventListener('input', () => this.handleInput());

            // Clear input button handler
            if (this.clearBtn) {
                this.clearBtn.addEventListener('click', () => this.clearSearch());
            }

            // Keyboard navigation inside command palette
            this.modal.addEventListener('keydown', (e) => this.handleKeydown(e));

            // Domain chip filter buttons
            this.filterChips.forEach(chip => {
                chip.addEventListener('click', () => {
                    this.filterChips.forEach(c => c.classList.remove('active'));
                    chip.classList.add('active');
                    this.activeFilter = chip.dataset.filter || 'all';
                    if (this.cachedResults) {
                        this.renderResults(this.cachedResults);
                    } else if (this.input.value.trim().length >= 1) {
                        this.performSearch(this.input.value.trim());
                    }
                });
            });

            // Modal Lifecycle reset events
            this.modal.addEventListener('hidden.bs.modal', () => this.resetState());
        }

        handleInput() {
            const query = this.input.value.trim();

            if (this.clearBtn) {
                this.clearBtn.style.display = query.length > 0 ? 'flex' : 'none';
            }

            clearTimeout(this.debounceTimer);

            if (query.length === 0) {
                this.resetToInitial();
                return;
            }

            this.debounceTimer = setTimeout(() => {
                this.performSearch(query);
            }, 200);
        }

        async performSearch(query) {
            if (this.abortController) {
                this.abortController.abort();
            }
            this.abortController = new AbortController();

            this.setLoading(true);

            try {
                const endpoint = `/core/api/global-search/?q=${encodeURIComponent(query)}&category=${encodeURIComponent(this.activeFilter)}`;
                const response = await fetch(endpoint, {
                    method: 'GET',
                    headers: {
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    signal: this.abortController.signal
                });

                if (!response.ok) {
                    throw new Error(`HTTP Error status: ${response.status}`);
                }

                const data = await response.json();
                this.cachedResults = data.results || data;
                this.renderResults(this.cachedResults);
            } catch (err) {
                if (err.name === 'AbortError') {
                    // Suppress aborted request logs
                    return;
                }
                console.error('[GlobalSearch] Search query failed:', err);
                this.renderError();
            } finally {
                this.setLoading(false);
            }
        }

        renderResults(data) {
            this.selectedIndex = -1;
            this.dynamicResults.innerHTML = '';

            let hasMatches = false;
            const normalizedData = this.normalizeResults(data);

            const activeDomains = this.activeFilter === 'all' 
                ? Object.keys(normalizedData) 
                : [this.activeFilter];

            activeDomains.forEach(domainKey => {
                const items = normalizedData[domainKey] || [];
                if (items.length > 0) {
                    hasMatches = true;
                    const groupContainer = this.createGroupElement(domainKey, items);
                    this.dynamicResults.appendChild(groupContainer);
                }
            });

            if (hasMatches) {
                this.initialState.classList.add('d-none');
                this.emptyState.classList.add('d-none');
                this.dynamicResults.classList.remove('d-none');
            } else {
                this.initialState.classList.add('d-none');
                this.dynamicResults.classList.add('d-none');
                this.emptyState.classList.remove('d-none');
            }
        }

        normalizeResults(data) {
            const normalized = {};

            if (Array.isArray(data)) {
                data.forEach(item => {
                    const category = item.category || item.domain || 'products';
                    if (!normalized[category]) normalized[category] = [];
                    normalized[category].push(item);
                });
            } else if (typeof data === 'object' && data !== null) {
                Object.keys(data).forEach(key => {
                    if (Array.isArray(data[key])) {
                        normalized[key] = data[key];
                    }
                });
            }

            return normalized;
        }

        createGroupElement(domainKey, items) {
            const config = DOMAIN_CONFIG[domainKey] || {
                label: domainKey.toUpperCase(),
                icon: 'fas fa-circle-dot',
                iconBg: 'bg-secondary bg-opacity-10 text-secondary',
                badgeClass: 'badge-soft-secondary'
            };

            const container = document.createElement('div');
            container.className = 'search-group-container';

            const header = document.createElement('div');
            header.className = 'search-group-heading';
            header.innerHTML = `
                <div class="d-flex align-items-center gap-2">
                    <i class="${config.icon}"></i>
                    <span>${config.label}</span>
                </div>
                <span class="badge rounded-pill bg-light text-muted border font-mono fs-2xs">${items.length}</span>
            `;
            container.appendChild(header);

            items.forEach(item => {
                const row = document.createElement('a');
                row.href = item.url || '#';
                row.className = 'search-result-row';
                row.dataset.searchUrl = item.url || '#';

                const subtitle = item.subtitle || item.meta_desc || '';
                const badgeText = item.badge || item.status || '';
                const badgeClass = item.badge_class || config.badgeClass;

                row.innerHTML = `
                    <div class="d-flex align-items-center gap-3 overflow-hidden flex-grow-1">
                        <div class="search-result-icon ${config.iconBg}">
                            <i class="${item.icon || config.icon}"></i>
                        </div>
                        <div class="search-result-content">
                            <div class="search-result-title">${this.escapeHtml(item.title || item.name)}</div>
                            ${subtitle ? `<div class="search-result-subtitle">${this.escapeHtml(subtitle)}</div>` : ''}
                        </div>
                    </div>
                    <div class="search-result-meta">
                        ${badgeText ? `<span class="badge ${badgeClass} fs-2xs">${this.escapeHtml(badgeText)}</span>` : ''}
                        ${item.extra ? `<span class="font-mono text-muted fs-2xs mt-1">${this.escapeHtml(item.extra)}</span>` : ''}
                    </div>
                `;

                container.appendChild(row);
            });

            return container;
        }

        handleKeydown(e) {
            const rows = this.dynamicResults.querySelectorAll('.search-result-row');
            if (!rows.length) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                this.selectedIndex = (this.selectedIndex + 1) >= rows.length ? 0 : this.selectedIndex + 1;
                this.updateActiveRow(rows);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                this.selectedIndex = (this.selectedIndex - 1) < 0 ? rows.length - 1 : this.selectedIndex - 1;
                this.updateActiveRow(rows);
            } else if (e.key === 'Enter') {
                if (this.selectedIndex >= 0 && this.selectedIndex < rows.length) {
                    e.preventDefault();
                    rows[this.selectedIndex].click();
                }
            }
        }

        updateActiveRow(rows) {
            rows.forEach((row, index) => {
                if (index === this.selectedIndex) {
                    row.classList.add('active');
                    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                } else {
                    row.classList.remove('active');
                }
            });
        }

        setLoading(isLoading) {
            if (this.spinner) {
                this.spinner.style.display = isLoading ? 'block' : 'none';
            }
            if (this.clearBtn && isLoading) {
                this.clearBtn.style.display = 'none';
            }
        }

        clearSearch() {
            this.input.value = '';
            this.input.focus();
            if (this.clearBtn) this.clearBtn.style.display = 'none';
            this.resetToInitial();
        }

        resetToInitial() {
            this.cachedResults = null;
            this.selectedIndex = -1;
            this.dynamicResults.innerHTML = '';
            this.dynamicResults.classList.add('d-none');
            this.emptyState.classList.add('d-none');
            this.initialState.classList.remove('d-none');
        }

        resetState() {
            clearTimeout(this.debounceTimer);
            if (this.abortController) {
                this.abortController.abort();
            }
            this.setLoading(false);
            this.clearSearch();
        }

        renderError() {
            this.dynamicResults.innerHTML = `
                <div class="search-empty-state">
                    <i class="fas fa-triangle-exclamation text-danger"></i>
                    <h6 class="fw-bold text-dark mb-1">Search Service Unavailable</h6>
                    <p class="fs-xs text-muted mb-0">Unable to retrieve ERP records. Please check your connectivity.</p>
                </div>
            `;
            this.initialState.classList.add('d-none');
            this.emptyState.classList.add('d-none');
            this.dynamicResults.classList.remove('d-none');
        }

        escapeHtml(string) {
            if (!string) return '';
            const str = String(string);
            return str
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }
    }

    // Auto-instantiate on document ready
    document.addEventListener('DOMContentLoaded', () => {
        window.globalSearchInstance = new GlobalSearchEngine();
    });
})();