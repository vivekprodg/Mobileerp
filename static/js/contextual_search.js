(function () {
    'use strict';

    class ContextualSearchDropdown {
        constructor(targetElement) {
            this.target = targetElement;
            this.isContainer = !['INPUT', 'SELECT', 'TEXTAREA'].includes(this.target.tagName);
            
            this.wrapper = null;
            this.input = null;
            this.hiddenInput = null;
            this.dropdown = null;
            this.chip = null;
            this.spinner = null;
            this.clearBtn = null;

            this.url = '';
            this.minChars = 0;
            this.debounceDelay = 200;
            this.placeholder = 'Type or click to search...';
            this.extraParams = {};

            this.debounceTimer = null;
            this.abortController = null;
            this.selectedIndex = -1;
            this.currentResults = [];

            this.resolveConfiguration();
            this.setupDOM();
            this.bindEvents();
        }

        resolveConfiguration() {
            const el = this.target;
            const parentWrapper = el.closest ? el.closest('.contextual-search-wrapper, .async-search-group') : null;

            this.url = el.dataset.searchUrl || 
                       el.dataset.autocompleteUrl || 
                       (parentWrapper ? (parentWrapper.dataset.searchUrl || parentWrapper.dataset.autocompleteUrl) : '') || 
                       '';

            this.minChars = parseInt(el.dataset.minChars || (parentWrapper ? parentWrapper.dataset.minChars : '0') || '0', 10);
            this.debounceDelay = parseInt(el.dataset.debounce || (parentWrapper ? parentWrapper.dataset.debounce : '200') || '200', 10);
            this.placeholder = el.dataset.placeholder || el.getAttribute('placeholder') || (parentWrapper ? parentWrapper.dataset.placeholder : '') || 'Type or click to search...';

            const rawExtraParams = el.dataset.extraParams || (parentWrapper ? parentWrapper.dataset.extraParams : null);
            if (rawExtraParams) {
                try {
                    this.extraParams = JSON.parse(rawExtraParams);
                } catch (e) {
                    this.extraParams = {};
                }
            }
        }

        setupDOM() {
            if (this.isContainer) {
                this.wrapper = this.target;
                if (!this.wrapper.classList.contains('async-search-wrapper')) {
                    this.wrapper.classList.add('async-search-wrapper');
                }

                this.hiddenInput = this.wrapper.querySelector('input[type="hidden"]');
                this.input = this.wrapper.querySelector('input[type="text"]:not([type="hidden"])');

                if (!this.input) {
                    this.input = document.createElement('input');
                    this.input.type = 'text';
                    this.input.className = 'form-control async-search-input';
                    this.input.placeholder = this.placeholder;
                    this.input.autocomplete = 'off';
                    this.input.spellcheck = false;
                    this.wrapper.appendChild(this.input);
                }
            } else {
                this.input = this.target;
                if (!this.url) {
                    this.url = this.input.dataset.searchUrl || this.input.dataset.autocompleteUrl || '';
                }

                if (this.input.parentElement && this.input.parentElement.classList.contains('async-search-wrapper')) {
                    this.wrapper = this.input.parentElement;
                } else {
                    this.wrapper = document.createElement('div');
                    this.wrapper.className = 'async-search-wrapper';
                    this.input.parentNode.insertBefore(this.wrapper, this.input);
                    this.wrapper.appendChild(this.input);
                }

                const targetHiddenId = this.input.dataset.targetHidden;
                if (targetHiddenId) {
                    this.hiddenInput = document.getElementById(targetHiddenId);
                }

                if (!this.hiddenInput) {
                    const originalName = this.input.getAttribute('name');
                    if (originalName && !originalName.endsWith('_display')) {
                        this.hiddenInput = document.createElement('input');
                        this.hiddenInput.type = 'hidden';
                        this.hiddenInput.name = originalName;
                        this.hiddenInput.value = this.input.dataset.initialId || '';

                        this.input.setAttribute('name', `${originalName}_display`);
                        this.wrapper.appendChild(this.hiddenInput);
                    }
                }
            }

            if (!this.input.classList.contains('async-search-input')) {
                this.input.classList.add('async-search-input');
            }

            if (this.url && !this.input.dataset.searchUrl) {
                this.input.dataset.searchUrl = this.url;
            }

            let controlsWrap = this.wrapper.querySelector('.async-controls-wrap');
            if (controlsWrap) {
                controlsWrap.remove();
            }
            controlsWrap = document.createElement('div');
            controlsWrap.className = 'async-controls-wrap position-absolute end-0 top-50 translate-middle-y me-2 d-flex align-items-center gap-1';
            controlsWrap.style.zIndex = '5';
            this.wrapper.style.position = 'relative';
            this.wrapper.appendChild(controlsWrap);

            this.spinner = document.createElement('span');
            this.spinner.className = 'spinner-border spinner-border-sm text-primary d-none';
            this.spinner.setAttribute('role', 'status');
            controlsWrap.appendChild(this.spinner);

            this.clearBtn = document.createElement('button');
            this.clearBtn.type = 'button';
            this.clearBtn.className = 'btn btn-link text-muted p-0 text-decoration-none d-none';
            this.clearBtn.innerHTML = '<i class="fas fa-times-circle"></i>';
            this.clearBtn.title = 'Clear selection';
            controlsWrap.appendChild(this.clearBtn);

            let existingDropdown = this.wrapper.querySelector('.async-search-dropdown');
            if (existingDropdown) {
                existingDropdown.remove();
            }
            this.dropdown = document.createElement('div');
            this.dropdown.className = 'async-search-dropdown shadow-lg';
            this.wrapper.appendChild(this.dropdown);

            let existingChip = this.wrapper.querySelector('.async-selected-chip');
            if (existingChip) {
                existingChip.remove();
            }
            this.chip = document.createElement('div');
            this.chip.className = 'async-selected-chip d-none';
            this.chip.innerHTML = `
                <span class="chip-label text-truncate" style="max-width: 280px;"></span>
                <i class="fas fa-times chip-remove ms-1" title="Remove selection"></i>
            `;
            this.wrapper.appendChild(this.chip);

            const initialId = this.target.dataset.initialId || (this.hiddenInput ? this.hiddenInput.value : '');
            const initialTitle = this.target.dataset.initialTitle || this.target.dataset.initialLabel || this.input.dataset.initialLabel || '';
            const initialSubtitle = this.target.dataset.initialSubtitle || '';

            if (initialId && initialTitle) {
                const label = initialSubtitle ? `${initialTitle} (${initialSubtitle})` : initialTitle;
                this.setSelectedChip(initialId, label, false);
            }

            this.target.dataset.asyncSearchInitialized = 'true';
            this.input.dataset.asyncSearchInitialized = 'true';
        }

        bindEvents() {
            const triggerInitialFetch = () => {
                if (this.hiddenInput && this.hiddenInput.value && !this.chip.classList.contains('d-none')) {
                    return;
                }
                const query = this.input.value.trim();
                if (query.length >= this.minChars) {
                    this.fetchResults(query);
                }
            };

            this.input.addEventListener('focus', triggerInitialFetch);
            this.input.addEventListener('click', () => {
                if (!this.dropdown.classList.contains('show')) {
                    triggerInitialFetch();
                }
            });

            this.input.addEventListener('input', () => {
                clearTimeout(this.debounceTimer);
                const query = this.input.value.trim();

                if (this.hiddenInput && this.hiddenInput.value) {
                    this.hiddenInput.value = '';
                    this.dispatchEvents('item-cleared', null, '');
                }

                if (this.clearBtn) {
                    this.clearBtn.classList.toggle('d-none', query.length === 0);
                }

                if (query.length < this.minChars) {
                    this.closeDropdown();
                    return;
                }

                this.debounceTimer = setTimeout(() => {
                    this.fetchResults(query);
                }, this.debounceDelay);
            });

            this.input.addEventListener('keydown', (e) => this.handleKeydown(e));

            if (this.clearBtn) {
                this.clearBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.clearSelection();
                });
            }

            this.chip.querySelector('.chip-remove').addEventListener('click', (e) => {
                e.preventDefault();
                this.clearSelection();
            });

            document.addEventListener('click', (e) => {
                if (!this.wrapper.contains(e.target)) {
                    this.closeDropdown();
                }
            });
        }

        setUrl(newUrl) {
            this.url = newUrl;
            this.target.dataset.searchUrl = newUrl;
            this.target.dataset.autocompleteUrl = newUrl;
            if (this.input) {
                this.input.dataset.searchUrl = newUrl;
            }
        }

        setExtraParams(paramsObj) {
            this.extraParams = Object.assign({}, this.extraParams, paramsObj);
        }

        async fetchResults(query) {
            if (!this.url) {
                this.dropdown.innerHTML = `
                    <div class="p-2 text-center text-muted fs-xs">
                        Endpoint URL is not configured.
                    </div>
                `;
                this.openDropdown();
                return;
            }

            if (this.abortController) {
                this.abortController.abort();
            }
            this.abortController = new AbortController();

            this.setLoading(true);
            this.dropdown.innerHTML = `
                <div class="p-3 text-center text-muted fs-xs">
                    <i class="fas fa-circle-notch fa-spin me-1 text-primary"></i> Fetching records...
                </div>
            `;
            this.openDropdown();

            try {
                const separator = this.url.includes('?') ? '&' : '?';
                let endpoint = `${this.url}${separator}q=${encodeURIComponent(query)}`;

                if (this.extraParams && typeof this.extraParams === 'object') {
                    for (const [key, value] of Object.entries(this.extraParams)) {
                        if (value !== undefined && value !== null) {
                            endpoint += `&${encodeURIComponent(key)}=${encodeURIComponent(value)}`;
                        }
                    }
                }

                const response = await fetch(endpoint, {
                    method: 'GET',
                    headers: {
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    signal: this.abortController.signal
                });

                if (!response.ok) {
                    throw new Error(`HTTP Error ${response.status}`);
                }

                const data = await response.json();
                this.currentResults = Array.isArray(data) ? data : (data.results || data.items || []);
                this.renderDropdown(this.currentResults);
            } catch (err) {
                if (err.name === 'AbortError') return;
                this.dropdown.innerHTML = `
                    <div class="p-2 text-center text-danger fs-xs">
                        <i class="fas fa-exclamation-triangle me-1"></i> Failed to retrieve options.
                    </div>
                `;
            } finally {
                this.setLoading(false);
            }
        }

        renderDropdown(items) {
            this.selectedIndex = -1;
            this.dropdown.innerHTML = '';

            if (!items || items.length === 0) {
                this.dropdown.innerHTML = `
                    <div class="p-3 text-center text-muted fs-xs">
                        <i class="fas fa-info-circle me-1"></i> No matching entries found.
                    </div>
                `;
                return;
            }

            items.forEach((item, index) => {
                const itemEl = document.createElement('div');
                itemEl.className = 'async-result-item';
                itemEl.dataset.index = index;

                const title = item.title || item.name || item.text || item.company_name || 'Record';
                const subtitle = item.subtitle || item.phone || item.sku || item.code || '';
                const badge = item.badge || item.status || '';
                const badgeClass = item.badge_class || 'badge-soft-primary';

                itemEl.innerHTML = `
                    <div class="d-flex flex-column text-truncate pe-2">
                        <span class="async-result-title text-truncate fw-semibold">${this.escapeHtml(title)}</span>
                        ${subtitle ? `<span class="async-result-subtitle text-truncate fs-2xs text-muted font-mono">${this.escapeHtml(subtitle)}</span>` : ''}
                    </div>
                    ${badge ? `<span class="badge ${badgeClass} fs-2xs flex-shrink-0">${this.escapeHtml(badge)}</span>` : ''}
                `;

                itemEl.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.selectItem(item);
                });

                this.dropdown.appendChild(itemEl);
            });
        }

        handleKeydown(e) {
            const items = this.dropdown.querySelectorAll('.async-result-item');
            if (!this.dropdown.classList.contains('show') || !items.length) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                this.selectedIndex = (this.selectedIndex + 1) >= items.length ? 0 : this.selectedIndex + 1;
                this.highlightItem(items);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                this.selectedIndex = (this.selectedIndex - 1) < 0 ? items.length - 1 : this.selectedIndex - 1;
                this.highlightItem(items);
            } else if (e.key === 'Enter') {
                if (this.selectedIndex >= 0 && this.selectedIndex < items.length) {
                    e.preventDefault();
                    this.selectItem(this.currentResults[this.selectedIndex]);
                }
            } else if (e.key === 'Escape') {
                this.closeDropdown();
            }
        }

        highlightItem(items) {
            items.forEach((el, index) => {
                if (index === this.selectedIndex) {
                    el.classList.add('active');
                    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                } else {
                    el.classList.remove('active');
                }
            });
        }

        selectItem(item) {
            const id = item.id !== undefined ? item.id : (item.pk !== undefined ? item.pk : '');
            const title = item.title || item.name || item.text || item.company_name || '';
            const subtitle = item.subtitle || item.phone || item.sku || item.code || '';
            const displayLabel = subtitle ? `${title} (${subtitle})` : title;

            this.setSelectedChip(id, displayLabel, true);
            this.closeDropdown();
            this.dispatchEvents('item-selected', item, id);
        }

        setSelectedChip(id, label, triggerChange = true) {
            if (this.hiddenInput) {
                this.hiddenInput.value = id;
                if (triggerChange) {
                    this.hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }

            this.chip.querySelector('.chip-label').textContent = label;
            this.chip.classList.remove('d-none');
            this.input.classList.add('d-none');

            if (this.clearBtn) this.clearBtn.classList.add('d-none');
        }

        clearSelection() {
            if (this.hiddenInput) {
                this.hiddenInput.value = '';
                this.hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
            }

            this.chip.classList.add('d-none');
            this.input.value = '';
            this.input.classList.remove('d-none');
            if (this.clearBtn) this.clearBtn.classList.add('d-none');
            this.closeDropdown();
            this.input.focus();

            this.dispatchEvents('item-cleared', null, '');
        }

        dispatchEvents(eventName, item, id) {
            const payload = {
                bubbles: true,
                cancelable: true,
                detail: Object.assign({}, item || {}, {
                    id: id,
                    item: item,
                    extra_data: (item && item.extra_data) ? item.extra_data : item
                })
            };

            this.target.dispatchEvent(new CustomEvent(eventName, payload));
            this.input.dispatchEvent(new CustomEvent(eventName, payload));
            if (this.hiddenInput) {
                this.hiddenInput.dispatchEvent(new CustomEvent(eventName, payload));
            }
        }

        setLoading(isLoading) {
            if (this.spinner) {
                this.spinner.classList.toggle('d-none', !isLoading);
            }
            if (this.clearBtn && isLoading) {
                this.clearBtn.classList.add('d-none');
            }
        }

        openDropdown() {
            this.dropdown.classList.add('show');
        }

        closeDropdown() {
            this.dropdown.classList.remove('show');
            this.selectedIndex = -1;
        }

        escapeHtml(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }
    }

    class TableRealtimeFilterEngine {
        constructor(config = {}) {
            this.input = typeof config.input === 'string' ? document.querySelector(config.input) : config.input;
            this.clearBtn = typeof config.clearBtn === 'string' ? document.querySelector(config.clearBtn) : config.clearBtn;
            this.tableBody = typeof config.tableBody === 'string' ? document.querySelector(config.tableBody) : config.tableBody;
            this.noResultsRow = typeof config.noResultsRow === 'string' ? document.querySelector(config.noResultsRow) : config.noResultsRow;
            this.queryDisplaySpan = typeof config.queryDisplaySpan === 'string' ? document.querySelector(config.queryDisplaySpan) : config.queryDisplaySpan;
            this.counterBadge = typeof config.counterBadge === 'string' ? document.querySelector(config.counterBadge) : config.counterBadge;
            this.rowSelector = config.rowSelector || '.grn-data-row, .table-search-row';

            if (!this.input || !this.tableBody) {
                return;
            }

            this.indexedRows = [];
            this.totalRowsCount = 0;

            this.buildIndex();
            this.bindEvents();

            if (this.input.value.trim().length > 0) {
                this.filter(this.input.value);
            }
        }

        buildIndex() {
            const rows = Array.from(this.tableBody.querySelectorAll(this.rowSelector));
            this.totalRowsCount = rows.length;

            this.indexedRows = rows.map(row => {
                const grn = (row.dataset.grn || '').toLowerCase();
                const supplier = (row.dataset.supplier || '').toLowerCase();
                const bill = (row.dataset.bill || '').toLowerCase();
                const date = (row.dataset.date || '').toLowerCase();
                const status = (row.dataset.status || '').toLowerCase();
                const amount = (row.dataset.amount || '').toLowerCase();
                const fullText = row.textContent.toLowerCase();

                return {
                    element: row,
                    searchData: `${grn} ${supplier} ${bill} ${date} ${status} ${amount} ${fullText}`.replace(/\s+/g, ' ')
                };
            });
        }

        filter(query) {
            const cleanQuery = (query || '').toLowerCase().trim();
            const tokens = cleanQuery.split(/\s+/).filter(Boolean);

            let visibleCount = 0;

            this.indexedRows.forEach(item => {
                const matches = tokens.length === 0 || tokens.every(token => item.searchData.includes(token));

                if (matches) {
                    item.element.style.display = '';
                    visibleCount++;
                } else {
                    item.element.style.display = 'none';
                }
            });

            if (this.noResultsRow) {
                if (visibleCount === 0 && this.totalRowsCount > 0) {
                    if (this.queryDisplaySpan) {
                        this.queryDisplaySpan.innerText = query;
                    }
                    this.noResultsRow.style.display = '';
                } else {
                    this.noResultsRow.style.display = 'none';
                }
            }

            if (this.counterBadge) {
                this.counterBadge.innerText = `Showing ${visibleCount} of ${this.totalRowsCount} GRNs`;
            }

            if (this.clearBtn) {
                this.clearBtn.style.display = cleanQuery.length > 0 ? 'block' : 'none';
            }
        }

        reset() {
            if (this.input) {
                this.input.value = '';
            }
            this.filter('');
            if (this.input) {
                this.input.focus();
            }
        }

        bindEvents() {
            this.input.addEventListener('input', (e) => {
                this.filter(e.target.value);
            });

            if (this.clearBtn) {
                this.clearBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.reset();
                });
            }

            this.input.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') {
                    e.preventDefault();
                    this.reset();
                }
            });
        }
    }

    function initTableSearch(container = document) {
        if (!container) return;

        const grnSearchInput = container.querySelector('#grnSearchInput');
        const grnTableBody = container.querySelector('#grnTableBody');

        if (grnSearchInput && grnTableBody && !grnSearchInput.dataset.tableSearchInitialized) {
            grnSearchInput.dataset.tableSearchInitialized = 'true';
            new TableRealtimeFilterEngine({
                input: grnSearchInput,
                clearBtn: container.querySelector('#clearGrnSearchBtn'),
                tableBody: grnTableBody,
                noResultsRow: container.querySelector('#noGrnMatchesRow'),
                queryDisplaySpan: container.querySelector('#searchQueryTermText'),
                counterBadge: container.querySelector('#grnCounterBadge'),
                rowSelector: '.grn-data-row'
            });
        }

        const genericSearchInputs = container.querySelectorAll('[data-table-search-target]');
        genericSearchInputs.forEach(input => {
            if (input.dataset.tableSearchInitialized) return;
            input.dataset.tableSearchInitialized = 'true';

            const targetTableBodyId = input.dataset.tableSearchTarget;
            const targetTableBody = document.querySelector(targetTableBodyId);
            if (targetTableBody) {
                new TableRealtimeFilterEngine({
                    input: input,
                    clearBtn: input.dataset.clearBtn ? document.querySelector(input.dataset.clearBtn) : null,
                    tableBody: targetTableBody,
                    noResultsRow: input.dataset.noResultsRow ? document.querySelector(input.dataset.noResultsRow) : null,
                    queryDisplaySpan: input.dataset.queryDisplay ? document.querySelector(input.dataset.queryDisplay) : null,
                    counterBadge: input.dataset.counterBadge ? document.querySelector(input.dataset.counterBadge) : null,
                    rowSelector: input.dataset.rowSelector || 'tr'
                });
            }
        });
    }

    function initContextualSearch(container = document) {
        if (!container) return;

        const selectors = [
            '.async-search-group',
            '.contextual-search-wrapper',
            'input[data-autocomplete-url]',
            'input[data-search-url]',
            '.async-search-input'
        ];

        let elements = [];
        if (container.matches && selectors.some(s => container.matches(s))) {
            elements.push(container);
        }

        if (container.querySelectorAll) {
            const queried = container.querySelectorAll(selectors.join(', '));
            elements = elements.concat(Array.from(queried));
        }

        elements.forEach(el => {
            if (el.closest('template, [id*="template" i], [id*="Template"], [data-template]')) {
                return;
            }

            if (el.tagName === 'INPUT' && el.closest('.contextual-search-wrapper, .async-search-group')) {
                return;
            }

            if (el.dataset.asyncSearchInitialized === 'true' && container === document) {
                return;
            }

            new ContextualSearchDropdown(el);
        });

        initTableSearch(container);
    }

    try {
        Object.defineProperty(window, 'initContextualSearch', {
            value: initContextualSearch,
            writable: false,
            configurable: false
        });
        Object.defineProperty(window, 'TableRealtimeFilterEngine', {
            value: TableRealtimeFilterEngine,
            writable: false,
            configurable: false
        });
    } catch (e) {
        window.initContextualSearch = initContextualSearch;
        window.TableRealtimeFilterEngine = TableRealtimeFilterEngine;
    }

    document.addEventListener('DOMContentLoaded', () => {
        window.initContextualSearch(document);
    });

    document.addEventListener('shown.bs.modal', (e) => {
        if (e.target) {
            window.initContextualSearch(e.target);
        }
    });
})();