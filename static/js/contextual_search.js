/**
 * Universal Contextual Asynchronous Searchable Dropdown Engine
 * Enterprise ERP - Mobile Shop & Optical Store Management System
 * File: static/js/contextual_search.js
 *
 * Core Capabilities:
 * 1. Dual Element Initialization:
 *    - Direct Inputs: Decorated <input data-autocomplete-url="..."> or .async-search-input.
 *    - Container Wrappers: Containers such as <div class="async-search-group"> or
 *      <div class="contextual-search-wrapper"> that contain only a hidden input. Automatically
 *      generates the visible text search input, loading indicators, clear buttons, and dropdown boxes.
 * 2. Zero-Query Instant Population:
 *    - Focusing or clicking an empty input triggers a fetch with q="" to display the top 15-20
 *      active, recent, or default records immediately without requiring typing.
 * 3. Dynamic Parameter & Chaining Support:
 *    - Supports dynamic query parameters (e.g. data-extra-params, category_id filtering, supplier scoping).
 *    - Exposes setUrl(newUrl) and setExtraParams(paramsObj) to dynamically re-chain dependent dropdowns.
 * 4. Form Value Synchronization & Chips:
 *    - Synchronizes the selected ID with the hidden input for clean Django form submissions.
 *    - Renders an interactive chip badge with a remove cross button.
 *    - Dispatches native 'change' and custom 'item-selected' / 'item-cleared' events containing item.extra_data.
 * 5. Robust Keyboard Navigation:
 *    - ArrowDown, ArrowUp, Enter, and Escape support with auto-scrolling into view.
 * 6. Global Scope Protection:
 *    - Freezes window.initContextualSearch via Object.defineProperty to prevent legacy inline scripts
 *      from accidentally overriding or corrupting the global engine.
 */

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
            this.url = el.dataset.searchUrl || el.dataset.autocompleteUrl || '';
            this.minChars = parseInt(el.dataset.minChars || '0', 10);
            this.debounceDelay = parseInt(el.dataset.debounce || '200', 10);
            this.placeholder = el.dataset.placeholder || el.getAttribute('placeholder') || 'Type or click to search...';

            if (el.dataset.extraParams) {
                try {
                    this.extraParams = JSON.parse(el.dataset.extraParams);
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

            // Inline action controls (Spinner & Clear)
            let controlsWrap = this.wrapper.querySelector('.async-controls-wrap');
            if (!controlsWrap) {
                controlsWrap = document.createElement('div');
                controlsWrap.className = 'async-controls-wrap position-absolute end-0 top-50 translate-middle-y me-2 d-flex align-items-center gap-1';
                controlsWrap.style.zIndex = '5';
                this.wrapper.style.position = 'relative';
                this.wrapper.appendChild(controlsWrap);
            }

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

            // Dropdown results list container
            this.dropdown = document.createElement('div');
            this.dropdown.className = 'async-search-dropdown shadow-lg';
            this.wrapper.appendChild(this.dropdown);

            // Selected chip component
            this.chip = document.createElement('div');
            this.chip.className = 'async-selected-chip d-none';
            this.chip.innerHTML = `
                <span class="chip-label text-truncate" style="max-width: 280px;"></span>
                <i class="fas fa-times chip-remove ms-1" title="Remove selection"></i>
            `;
            this.wrapper.appendChild(this.chip);

            // Pre-fill initial selection if provided
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
            // Zero-Query population on Focus and Click
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

            // Debounced typing handler
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

            // Keyboard navigation
            this.input.addEventListener('keydown', (e) => this.handleKeydown(e));

            // Inline clear button
            if (this.clearBtn) {
                this.clearBtn.addEventListener('click', (e) => {
                    e.preventDefault();
                    this.clearSelection();
                });
            }

            // Chip remove button
            this.chip.querySelector('.chip-remove').addEventListener('click', (e) => {
                e.preventDefault();
                this.clearSelection();
            });

            // Close dropdown when clicking outside
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

    /**
     * Unified Global Contextual Search Initializer
     * Accepts Document, Form, Modal, Table Row, or Container DIV
     */
    function initContextualSearch(container = document) {
        if (!container) return;

        const selectors = [
            '.async-search-group:not([data-async-search-initialized="true"])',
            '.contextual-search-wrapper:not([data-async-search-initialized="true"])',
            'input[data-autocomplete-url]:not([data-async-search-initialized="true"])',
            'input[data-search-url]:not([data-async-search-initialized="true"])',
            '.async-search-input:not([data-async-search-initialized="true"])'
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
            if (el.dataset.asyncSearchInitialized === 'true') return;
            new ContextualSearchDropdown(el);
        });
    }

    // Protect Global Scope against accidental inline script overwrites
    try {
        Object.defineProperty(window, 'initContextualSearch', {
            value: initContextualSearch,
            writable: false,
            configurable: false
        });
    } catch (e) {
        window.initContextualSearch = initContextualSearch;
    }

    // Auto-mount on DOM Ready
    document.addEventListener('DOMContentLoaded', () => {
        window.initContextualSearch(document);
    });

    // Re-mount on dynamic Bootstrap Modal display
    document.addEventListener('shown.bs.modal', (e) => {
        if (e.target) {
            window.initContextualSearch(e.target);
        }
    });
})();