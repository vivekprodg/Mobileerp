/**
 * POS Counter Terminal & Billing Engine (Hisaav Minimalist Architecture)
 * File: static/js/pos_engine.js
 *
 * Core Capabilities:
 * 1. Self-Healing Two-Tier Card Rendering:
 *    - Dynamically detects or creates the items container (#itemsContainer) without null-pointer crashes.
 *    - Renders two-tier stacked cards (.item-card) with instant quantity, price, and discount calculations.
 * 2. Complete Historical & Backdated Bill Synchronization:
 *    - Entire #btnChangeBillDate container is clickable with hover feedback.
 *    - Auto-Select Date Event (onChange): Clicking any day on the calendar instantly selects and updates the bill date.
 *    - Zero "Apply" Button Dependency: Directly applies the date on click without requiring an extra button press.
 *    - Explicit Floating Calendar Cleanup: Immediately closes and hides #ndp-nepali-box so it never hangs on screen.
 *    - Synchronized Outside-Click & Cancel: Clicking outside or pressing Escape cleanly closes both popups.
 *    - Validates selected dates against the active open Fiscal Year (e.g. 2083/84).
 *    - Strictly preserves the chosen B.S. date in the checkout payload (bill_date_bs) across Cash,
 *      Credit, Split payments, and parked bills (Hold/Recall).
 * 3. Zero-Query Instant Suggestions & Live Typeahead:
 *    - Barcode / 15-Digit IMEI box opens live suggestions with recent products.
 *    - Patron Details box opens live suggestions with recent customers and debtors.
 *    - Two-tier item row autocomplete queries the live catalog under the active row.
 * 4. Dual-Mode Discounts (% vs Flat Rs.):
 *    - In-row and whole-bill segmented toggles with instant VAT recalculation and savings badges.
 * 5. Split Payments & Customer Udhaari Allocation [F8]:
 *    - Real-time remaining balance calculations across Cash, Wallets, Card, Bank, and Credit.
 * 6. Trade-In [F6], Repair Handover [F7], and Parked Cart [F9/F10] Parity.
 */

// ============================================================================
// 0. GLOBAL SECURITY, FORMATTING & UTILITIES
// ============================================================================
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function escapeRegex(text) {
    if (!text) return '';
    return String(text).replace(/[-[\]{}()*+?.,\\^$|#\s]/g, '\\$&');
}

function highlightMatch(fullText, query) {
    if (!query || !fullText) return escapeHtml(fullText);
    const escaped = escapeHtml(fullText);
    const regex = new RegExp(`(${escapeRegex(query)})`, 'gi');
    return escaped.replace(regex, '<span class="text-primary fw-extrabold text-decoration-underline">$1</span>');
}

function formatCurrencyNPR(num) {
    const val = Number(num) || 0;
    return 'Rs. ' + val.toLocaleString('en-IN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

function getCsrfToken() {
    const tokenEl = document.querySelector('[name=csrfmiddlewaretoken]');
    if (tokenEl && tokenEl.value) return tokenEl.value;

    const cookieMatch = document.cookie.match(/csrftoken=([^;]+)/);
    return cookieMatch ? decodeURIComponent(cookieMatch[1]) : '';
}

function generateUUID() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
        const r = Math.random() * 16 | 0;
        const v = c === 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

// ============================================================================
// 1. WEB AUDIO FEEDBACK SYNTHESIZER
// ============================================================================
class POSAudioSynthesizer {
    static play(type = 'success') {
        try {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;
            const ctx = new AudioContext();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();

            osc.type = 'sine';
            if (type === 'success') {
                osc.frequency.setValueAtTime(1800, ctx.currentTime);
            } else if (type === 'second') {
                osc.frequency.setValueAtTime(2400, ctx.currentTime);
            } else {
                osc.frequency.setValueAtTime(420, ctx.currentTime);
            }

            gain.gain.setValueAtTime(0.12, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.09);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.09);
        } catch (e) {
            // Audio policy handled gracefully
        }
    }
}

// ============================================================================
// 2. PATRON / CUSTOMER MANAGEMENT (AUTOCOMPLETE & LIVE DROPDOWN)
// ============================================================================
class POSCustomerManager {
    constructor(engine) {
        this.engine = engine;
        this.searchApiUrl = '/customers/api/search/';

        this.customerInput = document.getElementById('posCustomerInput') || document.getElementById('cust-name');
        this.customerIdInput = document.getElementById('posCustomerId');
        this.customerPanInput = document.getElementById('posCustomerPanInput') || document.getElementById('cust-pan');
        this.customerMobileInput = document.getElementById('posCustomerMobileInput') || document.getElementById('cust-mobile');
        this.dropdownEl = document.getElementById('customerSearchDropdown');

        this.clearNameBtn = document.getElementById('clearCustomerNameBtn');
        this.clearPanBtn = document.getElementById('clearCustomerPanBtn');
        this.clearMobileBtn = document.getElementById('clearCustomerMobileBtn');
        this.quickWalkInBtn = document.getElementById('quickWalkInCustomerBtn');

        this.selectedCustomer = null;
        this.debounceTimer = null;
        this.currentResults = [];
        this.activeIndex = -1;

        this.initEvents();
    }

    initEvents() {
        if (this.clearNameBtn && this.customerInput) {
            this.clearNameBtn.addEventListener('click', () => {
                this.customerInput.value = '';
                if (this.customerIdInput) this.customerIdInput.value = '';
                this.customerInput.focus();
                this.clearCustomer(false);
            });
        }

        if (this.clearPanBtn && this.customerPanInput) {
            this.clearPanBtn.addEventListener('click', () => {
                this.customerPanInput.value = '';
                this.engine.cart.customerPan = '';
            });
        }

        if (this.clearMobileBtn && this.customerMobileInput) {
            this.clearMobileBtn.addEventListener('click', () => {
                this.customerMobileInput.value = '';
                this.engine.cart.customerPhone = '';
            });
        }

        if (this.quickWalkInBtn) {
            this.quickWalkInBtn.addEventListener('click', () => {
                this.setWalkInCustomer();
            });
        }

        if (this.customerPanInput) {
            this.customerPanInput.addEventListener('input', (e) => {
                this.engine.cart.customerPan = e.target.value.trim();
            });
        }

        if (this.customerMobileInput) {
            this.customerMobileInput.addEventListener('input', (e) => {
                this.engine.cart.customerPhone = e.target.value.trim();
            });
        }

        if (this.customerInput) {
            this.customerInput.addEventListener('focus', () => {
                const val = this.customerInput.value.trim();
                this.fetchAndRenderCustomers(val);
            });

            this.customerInput.addEventListener('click', () => {
                if (this.dropdownEl && this.dropdownEl.classList.contains('d-none')) {
                    const val = this.customerInput.value.trim();
                    this.fetchAndRenderCustomers(val);
                }
            });

            this.customerInput.addEventListener('input', (e) => {
                const query = e.target.value.trim();
                if (this.customerIdInput) this.customerIdInput.value = '';

                clearTimeout(this.debounceTimer);
                this.debounceTimer = setTimeout(() => {
                    this.fetchAndRenderCustomers(query);
                }, 150);
            });

            this.customerInput.addEventListener('keydown', (e) => {
                if (!this.dropdownEl || this.dropdownEl.classList.contains('d-none')) return;

                const items = this.dropdownEl.querySelectorAll('.pos-suggestion-item');
                if (!items.length) return;

                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this.activeIndex = (this.activeIndex + 1) % items.length;
                    this.updateActiveItem(items);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this.activeIndex = (this.activeIndex - 1 + items.length) % items.length;
                    this.updateActiveItem(items);
                } else if (e.key === 'Enter') {
                    if (this.activeIndex >= 0 && this.currentResults[this.activeIndex]) {
                        e.preventDefault();
                        this.selectCustomer(this.currentResults[this.activeIndex]);
                    }
                } else if (e.key === 'Escape') {
                    this.closeDropdown();
                }
            });
        }

        document.addEventListener('click', (e) => {
            if (this.dropdownEl && !this.dropdownEl.contains(e.target) && e.target !== this.customerInput) {
                this.closeDropdown();
            }
        });
    }

    async fetchAndRenderCustomers(query = '') {
        if (!this.dropdownEl) return;

        try {
            this.dropdownEl.innerHTML = '<div class="p-3 text-center text-muted fs-xs"><i class="fas fa-spinner fa-spin me-1 text-primary"></i> Loading customers...</div>';
            this.dropdownEl.classList.remove('d-none');

            const url = query ? `${this.searchApiUrl}?q=${encodeURIComponent(query)}&limit=15` : `${this.searchApiUrl}?limit=15`;
            const resp = await fetch(url);
            if (!resp.ok) throw new Error('Search failed');

            const data = await resp.json();
            this.currentResults = data.results || (Array.isArray(data) ? data : []);
            this.activeIndex = -1;

            if (this.currentResults.length === 0) {
                this.dropdownEl.innerHTML = `
                    <div class="p-3 text-center text-muted fs-xs">
                        <i class="fas fa-user-slash text-secondary opacity-50 d-block mb-1"></i>
                        No customer found matching "<strong>${escapeHtml(query)}</strong>"
                    </div>`;
                return;
            }

            let html = '';
            this.currentResults.forEach((c, idx) => {
                const extra = c.extra_data || {};
                const name = c.name || extra.name || c.title || 'Customer';
                const phone = c.phone || extra.phone_number || extra.phone || '';
                const pan = c.pan || extra.pan_number || extra.pan || '';
                const dueBalance = parseFloat(c.credit_balance || extra.credit_balance || 0);

                let badgeHtml = '';
                if (dueBalance > 0) {
                    badgeHtml = `<span class="badge bg-danger bg-opacity-10 text-danger border border-danger border-opacity-25 pos-suggestion-badge">Due: Rs. ${dueBalance.toLocaleString('en-IN', {minimumFractionDigits: 2})}</span>`;
                } else if (pan) {
                    badgeHtml = `<span class="badge bg-primary bg-opacity-10 text-primary border border-primary border-opacity-25 pos-suggestion-badge">PAN: ${escapeHtml(pan)}</span>`;
                } else {
                    badgeHtml = `<span class="badge bg-success bg-opacity-10 text-success border border-success border-opacity-25 pos-suggestion-badge">Clear</span>`;
                }

                html += `
                    <div class="pos-suggestion-item" data-index="${idx}">
                        <div>
                            <div class="pos-suggestion-title">${highlightMatch(name, query)}</div>
                            <div class="pos-suggestion-subtitle">
                                <span class="font-mono">${phone ? highlightMatch(phone, query) : 'No Phone'}</span>
                                ${pan ? ` &bull; PAN: ${highlightMatch(pan, query)}` : ''}
                            </div>
                        </div>
                        <div class="text-end ps-2">
                            ${badgeHtml}
                        </div>
                    </div>
                `;
            });

            this.dropdownEl.innerHTML = html;

            this.dropdownEl.querySelectorAll('.pos-suggestion-item').forEach(itemEl => {
                itemEl.addEventListener('click', () => {
                    const idx = parseInt(itemEl.dataset.index, 10);
                    if (this.currentResults[idx]) {
                        this.selectCustomer(this.currentResults[idx]);
                    }
                });
            });

        } catch (err) {
            console.warn('[POSCustomerManager] Search error:', err);
            this.dropdownEl.innerHTML = '<div class="p-2 text-center text-danger fs-xs">Search unavailable</div>';
        }
    }

    updateActiveItem(items) {
        items.forEach((item, idx) => {
            if (idx === this.activeIndex) {
                item.classList.add('active');
                item.scrollIntoView({ block: 'nearest' });
            } else {
                item.classList.remove('active');
            }
        });
    }

    closeDropdown() {
        if (this.dropdownEl) {
            this.dropdownEl.classList.add('d-none');
            this.activeIndex = -1;
        }
    }

    setWalkInCustomer() {
        if (this.customerInput) this.customerInput.value = 'Walk-in Customer';
        if (this.customerPanInput) this.customerPanInput.value = '-';
        if (this.customerMobileInput) this.customerMobileInput.value = '-';
        if (this.customerIdInput) this.customerIdInput.value = '';

        this.selectCustomer({
            id: null,
            name: 'Walk-in Customer',
            phone: '-',
            pan: '-',
            customer_type: 'RETAIL',
            credit_balance: 0
        });
    }

    selectCustomer(rawItem) {
        const extra = rawItem.extra_data || {};
        const customer = {
            id: rawItem.id || extra.id || null,
            name: rawItem.name || extra.name || rawItem.title || 'Walk-in Customer',
            phone: rawItem.phone || extra.phone_number || extra.phone || '',
            pan: rawItem.pan || extra.pan_number || extra.pan || '',
            customer_type: rawItem.customer_type || extra.customer_type || 'RETAIL',
            credit_balance: parseFloat(rawItem.credit_balance !== undefined ? rawItem.credit_balance : (extra.credit_balance || 0))
        };

        this.selectedCustomer = customer;
        this.engine.cart.setCustomer(customer);

        if (this.customerInput) this.customerInput.value = customer.name;
        if (this.customerIdInput) this.customerIdInput.value = customer.id || '';
        if (this.customerPanInput) {
            this.customerPanInput.value = customer.pan || '-';
            this.engine.cart.customerPan = customer.pan || '';
        }
        if (this.customerMobileInput) {
            this.customerMobileInput.value = customer.phone || '-';
            this.engine.cart.customerPhone = customer.phone || '';
        }

        this.closeDropdown();
        POSAudioSynthesizer.play('success');
        this.engine.showNotification(`Customer "${customer.name}" linked to bill.`, 'success');
    }

    clearCustomer(clearInputs = true) {
        this.selectedCustomer = null;
        this.engine.cart.clearCustomer();

        if (clearInputs) {
            if (this.customerInput) this.customerInput.value = '';
            if (this.customerIdInput) this.customerIdInput.value = '';
            if (this.customerPanInput) this.customerPanInput.value = '';
            if (this.customerMobileInput) this.customerMobileInput.value = '';
            if (this.customerInput) this.customerInput.focus();
        }
        this.closeDropdown();
    }
}

// ============================================================================
// 3. TWO-TIER STACKED CARD CART & CALCULATION ENGINE (SELF-HEALING)
// ============================================================================
class POSCart {
    constructor(engine) {
        this.engine = engine;
        this.items = [];

        this.customer = null;
        this.customerPhone = '';
        this.customerPan = '';
        this.customerType = 'RETAIL';

        this.activeCartIdempotencyKey = generateUUID();

        // Whole-Bill Discount State
        this.billDiscountType = 'PERCENTAGE';
        this.billDiscountValue = 0;
        this.billDiscountAmount = 0;
        this.billDiscountPercent = 0;

        // Container Reference (Resolved Dynamically)
        this.container = null;
        this.addRowBtn = document.getElementById('btnAddCartRow') || document.querySelector('.btn-add-table-row');

        // Summary Fields
        this.subtotalText = document.getElementById('posSubtotalText');
        this.discountText = document.getElementById('posDiscountTotalText');
        this.nonTaxableText = document.getElementById('posNonTaxableText');
        this.taxableText = document.getElementById('posTaxableText');
        this.vatText = document.getElementById('posVatText');
        this.vatRow = document.getElementById('posVatRow');

        // Interactive Bill Discount Controls
        this.btnDiscountPercent = document.getElementById('btnDiscountModePercent');
        this.btnDiscountFixed = document.getElementById('btnDiscountModeFixed');
        this.billDiscountInput = document.getElementById('posBillDiscountInput');
        this.billDiscountSymbol = document.getElementById('billDiscountSymbolDisplay');
        this.billDiscountSavingsBadge = document.getElementById('billDiscountSavingsBadge');

        // Trade-In Display & Input
        this.tradeInInput = document.getElementById('trade-in-input');
        this.tradeInText = document.getElementById('posTradeInText');
        this.tradeInSummaryRow = document.getElementById('posTradeInSummaryRow');
        this.removeTradeInBtn = document.getElementById('posRemoveTradeInBtn');

        // Grand Total Display
        this.grandTotalText = document.getElementById('posGrandTotalText');

        // Narration & Terms Inputs
        this.narrationInput = document.getElementById('posBillNarration');
        this.termsInput = document.getElementById('posBillTerms');

        this.initBillDiscountControls();
        this.initEvents();
    }

    /**
     * Self-healing container resolver. Discovers or creates the items area without crashes.
     */
    getContainer() {
        if (!this.container || !document.body.contains(this.container)) {
            this.container = document.getElementById('itemsContainer')
                || document.getElementById('posCartTableBody')
                || document.getElementById('posItemsContainer')
                || document.getElementById('cartItemsContainer')
                || document.getElementById('cartTableBody')
                || document.querySelector('.pos-items-card-container #itemsContainer')
                || document.querySelector('#itemsContainer');

            if (!this.container) {
                const parent = document.querySelector('.pos-items-card-container');
                const addBtn = document.getElementById('btnAddCartRow') || document.querySelector('.btn-add-table-row');

                if (parent) {
                    let div = parent.querySelector('#itemsContainer');
                    if (!div) {
                        div = document.createElement('div');
                        div.id = 'itemsContainer';
                        const addStrip = parent.querySelector('.table-add-strip');
                        if (addStrip) {
                            parent.insertBefore(div, addStrip);
                        } else if (addBtn) {
                            parent.insertBefore(div, addBtn);
                        } else {
                            parent.appendChild(div);
                        }
                    }
                    this.container = div;
                } else if (addBtn && addBtn.parentElement) {
                    let div = document.createElement('div');
                    div.id = 'itemsContainer';
                    addBtn.parentElement.insertBefore(div, addBtn);
                    this.container = div;
                }
            }
        }
        return this.container;
    }

    initBillDiscountControls() {
        if (this.btnDiscountPercent) {
            this.btnDiscountPercent.addEventListener('click', (e) => {
                e.preventDefault();
                this.setBillDiscountMode('PERCENTAGE');
            });
        }

        if (this.btnDiscountFixed) {
            this.btnDiscountFixed.addEventListener('click', (e) => {
                e.preventDefault();
                this.setBillDiscountMode('AMOUNT');
            });
        }

        if (this.billDiscountInput) {
            this.billDiscountInput.addEventListener('input', (e) => {
                let val = parseFloat(e.target.value);
                if (isNaN(val) || val < 0) val = 0;

                if (this.billDiscountType === 'PERCENTAGE' && val > 100) {
                    val = 100;
                    e.target.value = '100';
                }

                this.billDiscountValue = val;
                this.recalculateTotals();
            });
        }

        this.syncBillDiscountUI();
    }

    setBillDiscountMode(mode) {
        const normalized = (mode === 'AMOUNT' || mode === 'FIXED') ? 'AMOUNT' : 'PERCENTAGE';
        if (this.billDiscountType === normalized) {
            this.syncBillDiscountUI();
            return;
        }

        this.billDiscountType = normalized;

        if (this.billDiscountType === 'PERCENTAGE' && this.billDiscountValue > 100) {
            this.billDiscountValue = 0;
            if (this.billDiscountInput) this.billDiscountInput.value = '0';
        }

        this.syncBillDiscountUI();
        this.recalculateTotals();
    }

    syncBillDiscountUI() {
        const isFixed = (this.billDiscountType === 'AMOUNT');

        if (this.btnDiscountPercent && this.btnDiscountFixed) {
            if (isFixed) {
                this.btnDiscountPercent.classList.remove('active', 'btn-dark');
                this.btnDiscountPercent.classList.add('btn-outline-secondary');
                this.btnDiscountFixed.classList.remove('btn-outline-secondary');
                this.btnDiscountFixed.classList.add('active', 'btn-dark');
            } else {
                this.btnDiscountFixed.classList.remove('active', 'btn-dark');
                this.btnDiscountFixed.classList.add('btn-outline-secondary');
                this.btnDiscountPercent.classList.remove('btn-outline-secondary');
                this.btnDiscountPercent.classList.add('active', 'btn-dark');
            }
        }

        if (this.billDiscountSymbol) {
            this.billDiscountSymbol.innerText = isFixed ? 'Rs.' : '%';
        }

        if (this.billDiscountInput) {
            this.billDiscountInput.placeholder = isFixed ? '0.00' : '0';
            if (isFixed) {
                this.billDiscountInput.removeAttribute('max');
                this.billDiscountInput.step = '1';
            } else {
                this.billDiscountInput.max = '100';
                this.billDiscountInput.step = '0.5';
            }
        }
    }

    initEvents() {
        if (this.addRowBtn) {
            this.addRowBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.addEmptyRow();
            });
        }

        const container = this.getContainer();
        if (container) {
            container.addEventListener('input', (e) => {
                const target = e.target;
                const card = target.closest('.item-card');
                if (!card) return;

                const idx = parseInt(card.dataset.index, 10);
                if (isNaN(idx) || !this.items[idx]) return;

                if (target.classList.contains('cart-qty-input') || target.classList.contains('calc-qty')) {
                    this.items[idx].quantity = Math.max(1, parseFloat(target.value) || 1);
                    this.updateRowCalculations(idx);
                } else if (target.classList.contains('cart-price-input') || target.classList.contains('calc-rate')) {
                    this.items[idx].unit_price = Math.max(0, parseFloat(target.value) || 0);
                    this.updateRowCalculations(idx);
                } else if (target.classList.contains('cart-disc-input') || target.classList.contains('calc-discount')) {
                    let dVal = Math.max(0, parseFloat(target.value) || 0);
                    if (this.items[idx].discount_type === 'PERCENTAGE' && dVal > 100) {
                        dVal = 100;
                        target.value = '100';
                    }
                    this.items[idx].discount_value = dVal;
                    this.updateRowCalculations(idx);
                } else if (target.classList.contains('product-name-input')) {
                    this.items[idx].name = target.value;
                }
            });

            container.addEventListener('change', (e) => {
                const target = e.target;
                const card = target.closest('.item-card');
                if (!card) return;

                const idx = parseInt(card.dataset.index, 10);
                if (isNaN(idx) || !this.items[idx]) return;

                if (target.classList.contains('cart-tax-select') || target.classList.contains('calc-tax')) {
                    this.items[idx].tax_type = target.value;
                    this.recalculateTotals();
                }
            });

            container.addEventListener('click', (e) => {
                const modeBtn = e.target.closest('.item-disc-mode-btn');
                if (modeBtn) {
                    e.preventDefault();
                    const card = modeBtn.closest('.item-card');
                    if (!card) return;

                    const idx = parseInt(card.dataset.index, 10);
                    if (isNaN(idx) || !this.items[idx]) return;

                    const newMode = modeBtn.dataset.mode;
                    this.items[idx].discount_type = newMode;

                    if (newMode === 'PERCENTAGE' && this.items[idx].discount_value > 100) {
                        this.items[idx].discount_value = 100;
                    }

                    const toggleGroup = card.querySelector('.item-disc-toggle-group');
                    if (toggleGroup) {
                        toggleGroup.querySelectorAll('.item-disc-mode-btn').forEach(btn => {
                            if (btn.dataset.mode === newMode) {
                                btn.classList.add('active', 'btn-dark');
                                btn.classList.remove('btn-outline-secondary');
                            } else {
                                btn.classList.remove('active', 'btn-dark');
                                btn.classList.add('btn-outline-secondary');
                            }
                        });
                    }

                    const symbolSpan = card.querySelector('.item-disc-symbol');
                    if (symbolSpan) {
                        symbolSpan.innerText = (newMode === 'AMOUNT') ? 'Rs.' : '%';
                    }

                    const discInput = card.querySelector('.cart-disc-input');
                    if (discInput) {
                        discInput.value = this.items[idx].discount_value || 0;
                        if (newMode === 'PERCENTAGE') {
                            discInput.max = '100';
                        } else {
                            const qty = Math.max(0, parseFloat(this.items[idx].quantity) || 0);
                            const rate = Math.max(0, parseFloat(this.items[idx].unit_price) || 0);
                            discInput.max = (qty * rate).toString();
                        }
                    }

                    this.updateRowCalculations(idx);
                    return;
                }

                const deleteBtn = e.target.closest('.cart-remove-btn, .btn-remove');
                if (deleteBtn) {
                    const card = deleteBtn.closest('.item-card');
                    if (card) {
                        const idx = parseInt(card.dataset.index, 10);
                        if (!isNaN(idx)) {
                            this.removeItem(idx);
                        }
                    }
                }
            });

            this.initTableRowAutocomplete();
        }

        if (this.tradeInInput) {
            this.tradeInInput.addEventListener('input', () => {
                this.recalculateTotals();
            });
        }

        if (this.removeTradeInBtn) {
            this.removeTradeInBtn.addEventListener('click', () => {
                if (this.tradeInInput) this.tradeInInput.value = '0.00';
                this.recalculateTotals();
            });
        }
    }

    initTableRowAutocomplete() {
        let activeRowDropdown = null;
        let rowDebounceTimer = null;
        let activeRowSuggestionIdx = -1;
        let currentRowResults = [];

        const closeAllRowDropdowns = () => {
            document.querySelectorAll('.row-product-search-dropdown').forEach(d => {
                d.classList.add('d-none');
                d.innerHTML = '';
            });
            activeRowDropdown = null;
            activeRowSuggestionIdx = -1;
            currentRowResults = [];
        };

        const container = this.getContainer();
        if (!container) return;

        container.addEventListener('focusin', (e) => {
            const input = e.target.closest('.product-name-input');
            if (!input) return;

            const card = input.closest('.item-card');
            const wrapper = input.closest('.product-input-wrapper') || input.parentElement;
            const dropdown = wrapper ? wrapper.querySelector('.row-product-search-dropdown') : null;
            if (!dropdown) return;

            closeAllRowDropdowns();
            activeRowDropdown = dropdown;
            fetchRowProducts(input.value.trim(), dropdown, parseInt(card.dataset.index, 10));
        });

        container.addEventListener('input', (e) => {
            const input = e.target.closest('.product-name-input');
            if (!input) return;

            const card = input.closest('.item-card');
            const wrapper = input.closest('.product-input-wrapper') || input.parentElement;
            const dropdown = wrapper ? wrapper.querySelector('.row-product-search-dropdown') : null;
            if (!dropdown) return;

            activeRowDropdown = dropdown;
            clearTimeout(rowDebounceTimer);
            rowDebounceTimer = setTimeout(() => {
                fetchRowProducts(input.value.trim(), dropdown, parseInt(card.dataset.index, 10));
            }, 150);
        });

        container.addEventListener('keydown', (e) => {
            const input = e.target.closest('.product-name-input');
            if (!input || !activeRowDropdown || activeRowDropdown.classList.contains('d-none')) return;

            const card = input.closest('.item-card');
            const items = activeRowDropdown.querySelectorAll('.pos-suggestion-item');
            if (!items.length) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeRowSuggestionIdx = (activeRowSuggestionIdx + 1) % items.length;
                updateActiveRowItem(items);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeRowSuggestionIdx = (activeRowSuggestionIdx - 1 + items.length) % items.length;
                updateActiveRowItem(items);
            } else if (e.key === 'Enter') {
                if (activeRowSuggestionIdx >= 0 && currentRowResults[activeRowSuggestionIdx]) {
                    e.preventDefault();
                    selectRowProduct(currentRowResults[activeRowSuggestionIdx], parseInt(card.dataset.index, 10));
                }
            } else if (e.key === 'Escape') {
                closeAllRowDropdowns();
            }
        });

        const updateActiveRowItem = (items) => {
            items.forEach((item, idx) => {
                if (idx === activeRowSuggestionIdx) {
                    item.classList.add('active');
                    item.scrollIntoView({ block: 'nearest' });
                } else {
                    item.classList.remove('active');
                }
            });
        };

        const fetchRowProducts = async (query, dropdown, rowIdx) => {
            try {
                dropdown.innerHTML = '<div class="p-2 text-center text-muted fs-xs"><i class="fas fa-spinner fa-spin me-1 text-primary"></i> Querying catalog...</div>';
                dropdown.classList.remove('d-none');

                const url = query ? `/products/api/search/?mode=simple&q=${encodeURIComponent(query)}&limit=15` : `/products/api/search/?mode=simple&limit=15`;
                const res = await fetch(url);
                if (!res.ok) throw new Error('Search failed');

                const data = await res.json();
                currentRowResults = data.results || (Array.isArray(data) ? data : []);
                activeRowSuggestionIdx = -1;

                if (!currentRowResults.length) {
                    dropdown.innerHTML = `<div class="p-2 text-center text-muted fs-xs">No matching products found.</div>`;
                    return;
                }

                let html = '';
                currentRowResults.forEach((p, idx) => {
                    const price = parseFloat(p.price || p.selling_price || 0);
                    const stock = parseFloat(p.available_stock || 0);
                    const isImei = Boolean(p.requires_imei || p.requires_imei_tracking);

                    html += `
                        <div class="pos-suggestion-item py-1.5 px-2" data-index="${idx}">
                            <div class="text-truncate pe-2">
                                <div class="pos-suggestion-title fs-xs">${highlightMatch(p.name, query)}</div>
                                <div class="pos-suggestion-subtitle fs-2xs">
                                    SKU: ${escapeHtml(p.sku || 'N/A')} &bull; Stock: <strong>${stock}</strong>
                                    ${isImei ? '<span class="badge bg-primary bg-opacity-10 text-primary ms-1 font-mono">IMEI</span>' : ''}
                                </div>
                            </div>
                            <div class="text-end font-mono fw-bold fs-xs text-primary">
                                Rs. ${price.toLocaleString('en-IN', {minimumFractionDigits: 2})}
                            </div>
                        </div>
                    `;
                });

                dropdown.innerHTML = html;

                dropdown.querySelectorAll('.pos-suggestion-item').forEach(itemEl => {
                    itemEl.addEventListener('click', () => {
                        const idx = parseInt(itemEl.dataset.index, 10);
                        if (currentRowResults[idx]) {
                            selectRowProduct(currentRowResults[idx], rowIdx);
                        }
                    });
                });

            } catch (err) {
                dropdown.innerHTML = `<div class="p-2 text-center text-danger fs-xs">Failed to load products</div>`;
            }
        };

        const selectRowProduct = (item, rowIdx) => {
            if (!this.items[rowIdx]) return;

            const price = parseFloat(item.price || item.selling_price || 0);
            const isVatApplicable = item.is_vat_applicable !== false && item.tax_pricing_type !== 'EXEMPT';

            this.items[rowIdx].product_id = item.id || item.product_id;
            this.items[rowIdx].name = item.name || item.title;
            this.items[rowIdx].unit_price = price;
            this.items[rowIdx].tax_type = isVatApplicable ? '13%' : 'No';
            this.items[rowIdx].requires_imei = Boolean(item.requires_imei || item.requires_imei_tracking);
            this.items[rowIdx].imei_1 = item.imei_1 || '';

            closeAllRowDropdowns();
            POSAudioSynthesizer.play('success');
            this.render();

            const c = this.getContainer();
            if (c) {
                const cards = c.querySelectorAll('.item-card');
                if (cards[rowIdx]) {
                    const qtyInput = cards[rowIdx].querySelector('.cart-qty-input') || cards[rowIdx].querySelector('.calc-qty');
                    if (qtyInput) qtyInput.focus();
                }
            }
        };

        document.addEventListener('click', (e) => {
            if (activeRowDropdown && !activeRowDropdown.contains(e.target) && !e.target.classList.contains('product-name-input')) {
                closeAllRowDropdowns();
            }
        });
    }

    renewIdempotencyKey() {
        this.activeCartIdempotencyKey = generateUUID();
    }

    setCustomer(customer) {
        this.customer = customer;
        this.customerPhone = customer.phone || '';
        this.customerPan = customer.pan || this.customerPan || '';
        this.customerType = customer.customer_type || 'RETAIL';
    }

    clearCustomer() {
        this.customer = null;
        this.customerPhone = '';
        this.customerPan = '';
        this.customerType = 'RETAIL';
    }

    addEmptyRow() {
        this.items.push({
            id: Date.now() + Math.random(),
            product_id: null,
            item_instance_id: null,
            name: '',
            quantity: 1,
            unit_price: 0,
            discount_type: 'PERCENTAGE',
            discount_value: 0,
            discount_percent: 0,
            tax_type: '13%',
            requires_imei: false,
            imei_1: '',
            imei_2: '',
            secondary_imei: '',
            unit_code: 'Pcs',
            is_discountable: true,
            is_repair_service: false,
            repair_ticket_id: null
        });

        this.render();

        const container = this.getContainer();
        if (container) {
            const cards = container.querySelectorAll('.item-card');
            const lastCard = cards[cards.length - 1];
            if (lastCard) {
                const nameInp = lastCard.querySelector('.product-name-input');
                if (nameInp) nameInp.focus();
            }
        }
    }

    addItem(product) {
        if (!product) return;

        const enforceImei = this.engine ? this.engine.enforceImei : true;
        const isRepair = Boolean(product.is_repair_service || product.repair_ticket_id);
        const isPhone = !isRepair && Boolean(product.requires_imei || product.match_type === 'IMEI');

        const incomingImei1 = (product.imei_1 || product.imei1 || product.imei_number || product.imei || '').trim();
        const incomingImei2 = (product.imei_2 || product.imei2 || product.secondary_imei || '').trim();
        const isSerializedHandset = !isRepair && (enforceImei ? isPhone : Boolean(incomingImei1));

        if (incomingImei1 || incomingImei2) {
            const duplicateItem = this.items.find(existingItem => {
                const existingImei1 = (existingItem.imei_1 || existingItem.imei_number || '').trim();
                const existingImei2 = (existingItem.imei_2 || existingItem.secondary_imei || '').trim();

                const imei1Collision = incomingImei1 && (existingImei1 === incomingImei1 || existingImei2 === incomingImei1);
                const imei2Collision = incomingImei2 && (existingImei1 === incomingImei2 || existingImei2 === incomingImei2);

                return imei1Collision || imei2Collision;
            });

            if (duplicateItem) {
                const matchedImei = incomingImei1 || incomingImei2;
                this.engine.showNotification(`Handset with IMEI "${matchedImei}" is already in the bill.`, 'warning');
                POSAudioSynthesizer.play('error');
                return;
            }
        }

        let rawPrice = 0;
        if (product.unit_price !== undefined && product.unit_price !== null && !isNaN(parseFloat(product.unit_price))) {
            rawPrice = parseFloat(product.unit_price);
        } else if (product.price !== undefined && product.price !== null && !isNaN(parseFloat(product.price))) {
            rawPrice = parseFloat(product.price);
        } else if (product.selling_price !== undefined && product.selling_price !== null && !isNaN(parseFloat(product.selling_price))) {
            rawPrice = parseFloat(product.selling_price);
        }

        const extractedPrice = Math.max(0, rawPrice);
        const extractedQuantity = (product.quantity !== undefined && product.quantity !== null && parseFloat(product.quantity) > 0)
            ? parseFloat(product.quantity)
            : 1;

        let taxType = '13%';
        if (product.is_vat_applicable === false || product.tax_pricing_type === 'EXEMPT') {
            taxType = 'No';
        }

        let discountType = 'PERCENTAGE';
        if (product.discount_type) {
            const rawType = String(product.discount_type).toUpperCase().trim();
            if (rawType === 'AMOUNT' || rawType === 'FIXED') {
                discountType = 'AMOUNT';
            } else if (rawType === 'PERCENTAGE') {
                discountType = 'PERCENTAGE';
            }
        }
        let discountValue = parseFloat(product.discount_value) || 0;

        const existingNonImei = (!isSerializedHandset && !isRepair)
            ? this.items.find(i => (i.product_id === (product.product_id || product.id)) && !i.imei_1)
            : null;

        if (existingNonImei) {
            existingNonImei.quantity += extractedQuantity;
            this.engine.showNotification(`Incremented ${existingNonImei.name} (Qty: ${existingNonImei.quantity}).`, 'info');
        } else {
            const newItemObj = {
                id: Date.now() + Math.random(),
                product_id: product.product_id || product.id,
                item_instance_id: product.item_instance_id || null,
                name: product.name,
                quantity: extractedQuantity,
                unit_price: extractedPrice,
                discount_type: discountType,
                discount_value: discountValue,
                discount_percent: discountType === 'PERCENTAGE' ? discountValue : 0,
                tax_type: taxType,
                requires_imei: isSerializedHandset,
                imei_1: incomingImei1,
                imei_2: incomingImei2,
                secondary_imei: incomingImei2,
                unit_code: product.unit_code || 'Pcs',
                is_discountable: product.is_discountable !== false,
                is_repair_service: isRepair,
                repair_ticket_id: product.repair_ticket_id || null
            };

            if (this.items.length === 1 && !this.items[0].name && this.items[0].unit_price === 0) {
                this.items[0] = newItemObj;
            } else {
                this.items.push(newItemObj);
            }

            this.engine.showNotification(`Added ${product.name} to bill.`, 'success');
        }

        POSAudioSynthesizer.play('success');
        this.render();
    }

    removeItem(index) {
        if (index >= 0 && index < this.items.length) {
            const removed = this.items.splice(index, 1)[0];
            if (removed && removed.is_repair_service) {
                this.engine.repairTicketId = null;
                const repairCard = document.getElementById('linkedRepairCard');
                if (repairCard) repairCard.classList.add('d-none');
            }
            this.render();
        }
    }

    clear(resetDate = true) {
        this.items = [];
        this.billDiscountType = 'PERCENTAGE';
        this.billDiscountValue = 0;
        this.billDiscountAmount = 0;
        this.billDiscountPercent = 0;
        if (this.billDiscountInput) this.billDiscountInput.value = '0';
        this.syncBillDiscountUI();

        this.renewIdempotencyKey();
        if (this.tradeInInput) this.tradeInInput.value = '0.00';
        if (this.narrationInput) this.narrationInput.value = '';
        if (this.termsInput) this.termsInput.value = '';

        if (resetDate && this.engine && typeof this.engine.resetBillDateToToday === 'function') {
            this.engine.resetBillDateToToday();
        }

        this.render();
    }

    updateRowCalculations(idx) {
        const item = this.items[idx];
        if (!item) return;

        const qty = Math.max(0, parseFloat(item.quantity) || 0);
        const rate = Math.max(0, parseFloat(item.unit_price) || 0);
        const gross = qty * rate;

        let lineDisc = 0;
        let effectivePct = 0;

        if (item.is_discountable !== false) {
            const discVal = Math.max(0, parseFloat(item.discount_value) || 0);
            const discType = item.discount_type || 'PERCENTAGE';

            if (discType === 'PERCENTAGE') {
                effectivePct = Math.min(100, discVal);
                lineDisc = gross * (effectivePct / 100);
            } else {
                lineDisc = Math.min(gross, discVal);
                effectivePct = gross > 0 ? (lineDisc / gross) * 100 : 0;
            }
            item.discount_percent = effectivePct;
        } else {
            item.discount_percent = 0;
            item.discount_value = 0;
        }

        const netLine = Math.max(0, gross - lineDisc);

        const container = this.getContainer();
        if (container) {
            const cards = container.querySelectorAll('.item-card');
            const card = cards[idx];
            if (card) {
                const grossEl = card.querySelector('.row-gross-amount') || card.querySelector('.calc-amount');
                const netEl = card.querySelector('.row-net-amount') || card.querySelector('.calc-net');
                const badgeEl = card.querySelector('.line-disc-savings-badge');

                if (grossEl) grossEl.value = gross.toFixed(2);
                if (netEl) netEl.value = netLine.toFixed(2);

                if (badgeEl) {
                    if (lineDisc > 0) {
                        badgeEl.style.display = 'inline-block';
                        badgeEl.innerText = `-Rs. ${lineDisc.toFixed(2)}`;
                    } else {
                        badgeEl.style.display = 'none';
                    }
                }
            }
        }

        this.recalculateTotals();
    }

    recalculateTotals() {
        let subTotal = 0;
        let itemDiscountTotal = 0;

        for (const item of this.items) {
            const qty = Math.max(0, parseFloat(item.quantity) || 0);
            const rate = Math.max(0, parseFloat(item.unit_price) || 0);
            const gross = qty * rate;

            let lineDisc = 0;
            if (item.is_discountable !== false) {
                const discVal = Math.max(0, parseFloat(item.discount_value) || 0);
                const discType = item.discount_type || 'PERCENTAGE';
                if (discType === 'PERCENTAGE') {
                    lineDisc = gross * (Math.min(100, discVal) / 100);
                } else {
                    lineDisc = Math.min(gross, discVal);
                }
            }

            subTotal += gross;
            itemDiscountTotal += lineDisc;
        }

        const netAfterItemDisc = Math.max(0, subTotal - itemDiscountTotal);

        let billDiscountAmt = 0;
        let effectiveBillPct = 0;
        const rawBillInput = Math.max(0, parseFloat(this.billDiscountValue) || 0);

        if (netAfterItemDisc > 0 && rawBillInput > 0) {
            if (this.billDiscountType === 'PERCENTAGE') {
                effectiveBillPct = Math.min(100, rawBillInput);
                billDiscountAmt = netAfterItemDisc * (effectiveBillPct / 100);
            } else {
                billDiscountAmt = Math.min(rawBillInput, netAfterItemDisc);
                effectiveBillPct = (billDiscountAmt / netAfterItemDisc) * 100;
            }
        }

        this.billDiscountAmount = billDiscountAmt;
        this.billDiscountPercent = effectiveBillPct;

        let taxableAmount = 0;
        let nonTaxableAmount = 0;

        for (const item of this.items) {
            const qty = Math.max(0, parseFloat(item.quantity) || 0);
            const rate = Math.max(0, parseFloat(item.unit_price) || 0);
            const gross = qty * rate;

            let lineDisc = 0;
            if (item.is_discountable !== false) {
                const discVal = Math.max(0, parseFloat(item.discount_value) || 0);
                const discType = item.discount_type || 'PERCENTAGE';
                if (discType === 'PERCENTAGE') {
                    lineDisc = gross * (Math.min(100, discVal) / 100);
                } else {
                    lineDisc = Math.min(gross, discVal);
                }
            }

            const lineNet = Math.max(0, gross - lineDisc);
            const lineBillDisc = netAfterItemDisc > 0 ? (billDiscountAmt * (lineNet / netAfterItemDisc)) : 0;
            const finalLinePayable = Math.max(0, lineNet - lineBillDisc);

            if (item.tax_type === '13%') {
                taxableAmount += finalLinePayable;
            } else {
                nonTaxableAmount += finalLinePayable;
            }
        }

        const vatAmount = taxableAmount * 0.13;

        let tradeInCredit = 0;
        if (this.tradeInInput) {
            tradeInCredit = Math.max(0, parseFloat(this.tradeInInput.value) || 0);
        } else if (this.engine.tradeInManager) {
            tradeInCredit = this.engine.tradeInManager.getCreditAmount();
        }

        const payableTotal = Math.max(0, (taxableAmount + vatAmount + nonTaxableAmount) - tradeInCredit);
        const grandTotal = Math.round(payableTotal);
        const combinedDiscount = itemDiscountTotal + billDiscountAmt;

        if (this.subtotalText) {
            this.subtotalText.innerText = subTotal.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }
        if (this.discountText) {
            this.discountText.innerText = combinedDiscount.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }
        if (this.billDiscountSavingsBadge) {
            if (billDiscountAmt <= 0) {
                this.billDiscountSavingsBadge.innerText = '-Rs. 0.00';
            } else if (this.billDiscountType === 'PERCENTAGE') {
                this.billDiscountSavingsBadge.innerText = `-Rs. ${billDiscountAmt.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
            } else {
                this.billDiscountSavingsBadge.innerText = `-Rs. ${billDiscountAmt.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} (Eff. ${effectiveBillPct.toFixed(1)}%)`;
            }
        }
        if (this.nonTaxableText) {
            this.nonTaxableText.innerText = nonTaxableAmount.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }
        if (this.taxableText) {
            this.taxableText.innerText = taxableAmount.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }
        if (this.vatText) {
            this.vatText.innerText = formatCurrencyNPR(vatAmount);
        }

        if (this.tradeInText) {
            this.tradeInText.innerText = `-Rs. ${tradeInCredit.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
        }
        if (this.tradeInSummaryRow) {
            this.tradeInSummaryRow.style.display = tradeInCredit > 0 ? 'flex' : 'none';
        }

        if (this.grandTotalText) {
            this.grandTotalText.innerText = formatCurrencyNPR(grandTotal);
        }

        return {
            subTotal,
            itemDiscountTotal,
            billDiscountAmt,
            billDiscountPercent: effectiveBillPct,
            totalDiscount: combinedDiscount,
            taxableAmount,
            nonTaxableAmount,
            vatAmount,
            tradeInCredit,
            grandTotal
        };
    }

    render() {
        const container = this.getContainer();
        if (!container) {
            console.error('[POSCart] Target items container (#itemsContainer) not found in DOM.');
            return;
        }

        container.innerHTML = '';

        if (this.items.length === 0) {
            container.innerHTML = `
                <div class="text-center py-5 text-muted bg-white rounded-3 border">
                    <i class="fas fa-barcode fs-2 d-block mb-2 opacity-25"></i>
                    <span>Scan an IMEI, box barcode, or click <strong>+ Add</strong> to start billing.</span>
                </div>
            `;
            this.recalculateTotals();
            return;
        }

        this.items.forEach((item, idx) => {
            const qty = Math.max(1, parseFloat(item.quantity) || 1);
            const rate = Math.max(0, parseFloat(item.unit_price) || 0);
            const gross = qty * rate;

            let lineDisc = 0;
            if (item.is_discountable !== false) {
                const discVal = Math.max(0, parseFloat(item.discount_value) || 0);
                if (item.discount_type === 'PERCENTAGE') {
                    lineDisc = gross * (Math.min(100, discVal) / 100);
                } else {
                    lineDisc = Math.min(gross, discVal);
                }
            }

            const netLine = Math.max(0, gross - lineDisc);
            const primaryImei = item.imei_1 || item.imei_number || '';
            const card = document.createElement('div');
            card.className = 'item-card';
            card.id = `item-row-${idx + 1}`;
            card.dataset.index = idx;

            card.innerHTML = `
                <!-- Tier 1: Identification & Deletion -->
                <div class="tier-top">
                    <span class="item-sn">${idx + 1}</span>
                    <div class="product-input-wrapper position-relative">
                        <label class="field-label">PRODUCT NAME / IMEI</label>
                        <input type="hidden" class="row-product-id" value="${item.product_id || ''}">
                        <input type="text" 
                               class="input-text product-name-input" 
                               value="${escapeHtml(item.name)}" 
                               placeholder="Type product name or scan IMEI..." 
                               autocomplete="off">
                        <div class="row-product-search-dropdown pos-live-dropdown d-none"></div>
                        ${primaryImei ? `<div class="text-muted font-mono row-imei-display mt-1" style="font-size: 11px;">IMEI: <strong>${escapeHtml(primaryImei)}</strong></div>` : ''}
                    </div>
                    <button type="button" class="btn-remove cart-remove-btn" title="Remove line">&times;</button>
                </div>

                <!-- Tier 2: 6 Calculated Inputs Grid with In-Row Discount Toggle -->
                <div class="tier-bottom">
                    <div class="calc-field">
                        <label class="field-label">QUANTITY</label>
                        <input type="number" 
                               class="calc-qty cart-qty-input font-mono text-center" 
                               value="${qty}" 
                               min="1">
                    </div>
                    <div class="calc-field">
                        <label class="field-label">RATE (Rs.)</label>
                        <input type="number" 
                               step="0.01" 
                               class="calc-rate cart-price-input font-mono text-end" 
                               value="${rate.toFixed(2)}">
                    </div>
                    <div class="calc-field">
                        <label class="field-label">AMOUNT</label>
                        <input type="text" 
                               class="calc-amount readonly row-gross-amount font-mono text-end" 
                               value="${gross.toFixed(2)}" 
                               readonly>
                    </div>

                    <!-- Line Discount with Interactive % and Rs. Toggle Buttons -->
                    <div class="calc-field calc-field-discount">
                        <div class="d-flex justify-content-between align-items-center mb-1">
                            <label class="field-label mb-0">DISCOUNT</label>
                            <span class="badge bg-danger bg-opacity-10 text-danger border border-danger border-opacity-25 font-mono line-disc-savings-badge" id="lineDiscountBadge-${idx + 1}" style="font-size: 9px; padding: 1px 4px; ${lineDisc > 0 ? '' : 'display: none;'}">
                                -Rs. ${lineDisc.toFixed(2)}
                            </span>
                        </div>
                        <div class="d-flex align-items-center gap-1">
                            <input type="hidden" class="cart-item-disc-type" value="${item.discount_type || 'PERCENTAGE'}">
                            <div class="btn-group btn-group-sm item-disc-toggle-group" role="group">
                                <button type="button" 
                                        class="btn btn-sm ${item.discount_type === 'PERCENTAGE' ? 'btn-dark active' : 'btn-outline-secondary'} font-mono py-0 px-2 item-disc-mode-btn mode-percent" 
                                        data-index="${idx}" 
                                        data-mode="PERCENTAGE" 
                                        title="Percentage (%)">%</button>
                                <button type="button" 
                                        class="btn btn-sm ${item.discount_type === 'AMOUNT' ? 'btn-dark active' : 'btn-outline-secondary'} font-mono py-0 px-2 item-disc-mode-btn mode-fixed" 
                                        data-index="${idx}" 
                                        data-mode="AMOUNT" 
                                        title="Flat Cash (Rs.)">Rs.</button>
                            </div>
                            <div class="input-group input-group-sm flex-grow-1">
                                <span class="input-group-text bg-light text-muted font-mono fs-2xs py-0 px-1.5 item-disc-symbol">
                                    ${item.discount_type === 'AMOUNT' ? 'Rs.' : '%'}
                                </span>
                                <input type="number" 
                                       step="any" 
                                       class="form-control form-control-sm text-end font-mono fs-xs fw-bold calc-discount cart-disc-input" 
                                       data-index="${idx}"
                                       placeholder="0" 
                                       value="${item.discount_value || 0}" 
                                       min="0"
                                       ${item.discount_type === 'PERCENTAGE' ? 'max="100"' : `max="${gross}"`}
                                       autocomplete="off">
                            </div>
                        </div>
                    </div>

                    <div class="calc-field">
                        <label class="field-label">TAX</label>
                        <select class="calc-tax cart-tax-select">
                            <option value="13%" ${item.tax_type === '13%' ? 'selected' : ''}>13%</option>
                            <option value="No" ${item.tax_type === 'No' ? 'selected' : ''}>No</option>
                        </select>
                    </div>
                    <div class="calc-field">
                        <label class="field-label">NET AMOUNT</label>
                        <input type="text" 
                               class="calc-net readonly net-amount row-net-amount font-mono" 
                               value="${netLine.toFixed(2)}" 
                               readonly>
                    </div>
                </div>
            `;

            container.appendChild(card);
        });

        this.recalculateTotals();
    }
}

// ============================================================================
// 4. TRADE-IN / BUY-BACK VOUCHER ATTACHMENT MANAGER
// ============================================================================
class POSTradeInManager {
    constructor(engine) {
        this.engine = engine;
        this.voucherId = null;
        this.voucherNumber = '';
        this.creditAmount = 0;
        this.tradeInDetails = null;

        this.row = document.getElementById('posTradeInRow');
        this.text = document.getElementById('posTradeInText');
        this.tag = document.getElementById('posTradeInTag');
    }

    getCreditAmount() {
        return this.creditAmount;
    }

    setDirectExchange(modelName, value) {
        this.voucherId = null;
        this.voucherNumber = 'EXCHANGE-DIRECT';
        this.creditAmount = Math.max(0, parseFloat(value) || 0);
        this.tradeInDetails = { model: modelName, value: this.creditAmount };
        const tradeInInp = document.getElementById('trade-in-input');
        if (tradeInInp) tradeInInp.value = this.creditAmount.toFixed(2);
        this.engine.cart.recalculateTotals();
    }

    async attachByVoucherNumber(voucherNum) {
        if (!voucherNum || voucherNum.trim() === '') return;
        try {
            const resp = await fetch(`/sales/api/trade-in/lookup/?voucher=${encodeURIComponent(voucherNum.trim())}`);
            if (!resp.ok) {
                const errData = await resp.json();
                this.engine.showNotification(errData.error || 'Trade-in voucher not found.', 'danger');
                return;
            }
            const data = await resp.json();
            const voucher = data.voucher || (Array.isArray(data.results) ? data.results[0] : data);

            this.voucherId = voucher.id;
            this.voucherNumber = voucher.voucher_number;
            this.creditAmount = parseFloat(voucher.final_trade_in_value || voucher.amount || 0);
            this.tradeInDetails = { model: `${voucher.brand_name || ''} ${voucher.model_name || ''}`.trim(), value: this.creditAmount };

            const tradeInInp = document.getElementById('trade-in-input');
            if (tradeInInp) tradeInInp.value = this.creditAmount.toFixed(2);

            this.engine.showNotification(`Trade-In voucher ${this.voucherNumber} attached (Credit: Rs. ${this.creditAmount.toFixed(2)}).`, 'success');
            this.engine.cart.recalculateTotals();
        } catch (err) {
            console.error('[POSTradeInManager] Error attaching voucher:', err);
            this.engine.showNotification('Failed to look up trade-in voucher.', 'danger');
        }
    }

    remove() {
        this.voucherId = null;
        this.voucherNumber = '';
        this.creditAmount = 0;
        this.tradeInDetails = null;
        const tradeInInp = document.getElementById('trade-in-input');
        if (tradeInInp) tradeInInp.value = '0.00';
        this.engine.cart.recalculateTotals();
    }
}

// ============================================================================
// 5. PARKED BILL / HOLD CART MANAGER MODULE (F9 / F10)
// ============================================================================
class POSHoldCartManager {
    constructor(engine) {
        this.engine = engine;
        this.apiUrl = '/pos/api/hold-carts/';
        this.holdBtn = document.getElementById('posHoldCartBtn');
        this.modalEl = document.getElementById('holdBillsModal');
        this.tableBody = document.getElementById('heldCartsTableBody');

        this.initEvents();
    }

    initEvents() {
        if (this.holdBtn) {
            this.holdBtn.addEventListener('click', () => this.holdCurrentCart());
        }

        if (this.tableBody) {
            this.tableBody.addEventListener('click', (e) => {
                const recallBtn = e.target.closest('.recall-held-cart-btn');
                if (recallBtn) {
                    const ref = recallBtn.dataset.holdRef;
                    this.recallCart(ref);
                    return;
                }

                const deleteBtn = e.target.closest('.delete-held-cart-btn');
                if (deleteBtn) {
                    const ref = deleteBtn.dataset.holdRef;
                    this.deleteHeldCart(ref);
                }
            });
        }
    }

    async holdCurrentCart() {
        if (this.engine.cart.items.length === 0) {
            this.engine.showNotification('Cannot hold an empty bill.', 'warning');
            return;
        }

        const totals = this.engine.cart.recalculateTotals();
        const customerName = document.getElementById('posCustomerInput')?.value.trim() || 'Walk-in Customer';
        const customerPhone = document.getElementById('posCustomerMobileInput')?.value.trim() || '';
        const customerPan = document.getElementById('posCustomerPanInput')?.value.trim() || '';
        const notes = prompt('Enter a short note for this held bill (optional):') || 'Held at counter';

        const serializedItems = this.engine.cart.items.map(item => ({
            product_id: item.product_id,
            name: item.name,
            quantity: item.quantity,
            unit_price: item.unit_price,
            discount_type: item.discount_type || 'PERCENTAGE',
            discount_value: item.discount_value || 0,
            discount_input_value: item.discount_value || 0,
            discount_percent: item.discount_percent || 0,
            tax_type: item.tax_type || '13%',
            requires_imei: Boolean(item.requires_imei),
            imei_1: item.imei_1 || '',
            imei_2: item.imei_2 || '',
            is_repair_service: Boolean(item.is_repair_service),
            repair_ticket_id: item.repair_ticket_id || null
        }));

        const tradeInDeduction = parseFloat(document.getElementById('trade-in-input')?.value) || 0;
        const effectiveBillDateBs = this.engine.getEffectiveBillDateBs();

        const payload = {
            cart: serializedItems,
            subtotal: totals.subTotal,
            customer_name: customerName,
            customer_phone: customerPhone,
            customer_pan: customerPan,
            bill_date_bs: effectiveBillDateBs || '',
            bill_discount_type: this.engine.cart.billDiscountType,
            bill_discount_input_value: this.engine.cart.billDiscountValue,
            bill_discount_value: this.engine.cart.billDiscountValue,
            bill_discount_amount: totals.billDiscountAmt,
            bill_discount_percent: totals.billDiscountPercent,
            discount_percent: totals.billDiscountPercent,
            trade_in_voucher_id: this.engine.tradeInManager ? this.engine.tradeInManager.voucherId : null,
            trade_in_credit_amount: tradeInDeduction,
            notes: notes
        };

        try {
            const resp = await fetch(this.apiUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify(payload)
            });

            const data = await resp.json();
            if (resp.ok && data.status === 'success') {
                this.engine.showNotification(`Bill parked successfully (${data.hold_reference}).`, 'success');
                this.engine.cart.clear();
                this.engine.customerManager.clearCustomer(true);
            } else {
                this.engine.showNotification(data.error || 'Failed to hold bill.', 'danger');
            }
        } catch (err) {
            console.error('[POSHoldCartManager] Hold error:', err);
            this.engine.showNotification('Network error while holding bill.', 'danger');
        }
    }

    async openRecallModal() {
        const modalDom = document.getElementById('holdBillsModal') || this.modalEl;
        if (!modalDom) return;
        const modal = bootstrap.Modal.getOrCreateInstance(modalDom);
        modal.show();
        await this.loadHeldCarts();
    }

    async loadHeldCarts() {
        if (!this.tableBody) return;
        this.tableBody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center py-4 text-muted">
                    <span class="spinner-border spinner-border-sm me-2 text-primary"></span> Loading parked bills...
                </td>
            </tr>
        `;

        try {
            const resp = await fetch(this.apiUrl);
            if (!resp.ok) throw new Error('Failed to fetch held bills');
            const data = await resp.json();
            const carts = data.held_carts || [];

            if (carts.length === 0) {
                this.tableBody.innerHTML = `
                    <tr>
                        <td colspan="6" class="text-center py-4 text-muted">
                            <i class="fas fa-pause-circle fs-3 d-block mb-2 opacity-25"></i>
                            No parked bills currently on hold.
                        </td>
                    </tr>
                `;
                return;
            }

            this.tableBody.innerHTML = carts.map(c => {
                const itemCount = (c.cart_payload && c.cart_payload.items) ? c.cart_payload.items.length : 0;
                const createdTime = c.created_at ? new Date(c.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '-';

                return `
                    <tr>
                        <td class="ps-4 font-mono fw-bold text-primary">${escapeHtml(c.hold_reference)}</td>
                        <td>
                            <strong>${escapeHtml(c.customer_name || 'Walk-in')}</strong>
                            ${c.customer_phone ? `<small class="text-muted d-block font-mono">${escapeHtml(c.customer_phone)}</small>` : ''}
                        </td>
                        <td><span class="badge bg-light text-dark border">${itemCount} Items</span></td>
                        <td class="font-mono fw-bold">Rs. ${parseFloat(c.subtotal || 0).toFixed(2)}</td>
                        <td class="text-muted fs-xs">${createdTime}</td>
                        <td class="text-end pe-4">
                            <button type="button" class="btn btn-primary btn-sm rounded-pill px-3 recall-held-cart-btn me-1" data-hold-ref="${escapeHtml(c.hold_reference)}">
                                <i class="fas fa-play me-1"></i> Recall
                            </button>
                            <button type="button" class="btn btn-outline-danger btn-sm rounded-circle p-1 delete-held-cart-btn" data-hold-ref="${escapeHtml(c.hold_reference)}" title="Discard">
                                <i class="fas fa-trash-can"></i>
                            </button>
                        </td>
                    </tr>
                `;
            }).join('');
        } catch (err) {
            this.tableBody.innerHTML = `
                <tr>
                    <td colspan="6" class="text-center py-4 text-danger">
                        <i class="fas fa-exclamation-triangle me-1"></i> Error loading held bills.
                    </td>
                </tr>
            `;
        }
    }

    async recallCart(reference) {
        try {
            const resp = await fetch(`${this.apiUrl}${encodeURIComponent(reference)}/`);
            if (!resp.ok) {
                this.engine.showNotification('Could not retrieve parked bill.', 'danger');
                return;
            }
            const data = await resp.json();
            const items = (data.cart_payload && data.cart_payload.items) ? data.cart_payload.items : [];

            if (items.length > 0) {
                this.engine.cart.clear(false);

                items.forEach(item => {
                    this.engine.cart.addItem({
                        ...item,
                        unit_price: item.unit_price || item.price || 0,
                        discount_value: item.discount_value || item.discount_amount || 0,
                        tax_type: item.tax_type || (item.tax_pricing_type === 'EXEMPT' ? 'No' : '13%')
                    });
                });

                const recalledBsDate = (data.cart_payload && data.cart_payload.bill_date_bs) || data.bill_date_bs || '';
                if (recalledBsDate) {
                    this.engine.setCustomBillDateBs(recalledBsDate);
                } else {
                    this.engine.resetBillDateToToday();
                }

                const billDiscType = (data.cart_payload && data.cart_payload.bill_discount_type) || data.bill_discount_type || 'PERCENTAGE';
                const billDiscVal = (data.cart_payload && data.cart_payload.bill_discount_value !== undefined)
                    ? data.cart_payload.bill_discount_value
                    : (data.bill_discount_value !== undefined ? data.bill_discount_value : (data.discount_percent || 0));

                this.engine.cart.billDiscountType = (billDiscType === 'AMOUNT' || billDiscType === 'FIXED') ? 'AMOUNT' : 'PERCENTAGE';
                this.engine.cart.billDiscountValue = parseFloat(billDiscVal) || 0;
                if (this.engine.cart.billDiscountInput) {
                    this.engine.cart.billDiscountInput.value = this.engine.cart.billDiscountValue;
                }
                this.engine.cart.syncBillDiscountUI();

                if (data.customer_name && data.customer_name !== 'Walk-in' && data.customer_name !== 'Walk-in Customer') {
                    const custInput = document.getElementById('posCustomerInput');
                    if (custInput) custInput.value = data.customer_name;
                    if (data.customer_phone) {
                        const mobInput = document.getElementById('posCustomerMobileInput');
                        if (mobInput) mobInput.value = data.customer_phone;
                    }
                    if (data.customer_pan) {
                        const panInput = document.getElementById('posCustomerPanInput');
                        if (panInput) panInput.value = data.customer_pan;
                    }
                }

                await fetch(`${this.apiUrl}${encodeURIComponent(reference)}/`, {
                    method: 'DELETE',
                    headers: { 'X-CSRFToken': getCsrfToken() }
                });

                const modalDom = document.getElementById('holdBillsModal') || this.modalEl;
                if (modalDom) {
                    const modal = bootstrap.Modal.getInstance(modalDom);
                    if (modal) modal.hide();
                }

                this.engine.cart.render();
                this.engine.showNotification(`Restored held bill ${reference}.`, 'success');
            }
        } catch (err) {
            console.error('[POSHoldCartManager] Recall error:', err);
            this.engine.showNotification('Error restoring bill.', 'danger');
        }
    }

    async deleteHeldCart(reference) {
        if (!confirm(`Permanently delete held bill ${reference}?`)) return;
        try {
            const resp = await fetch(`${this.apiUrl}${encodeURIComponent(reference)}/`, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': getCsrfToken() }
            });
            if (resp.ok) {
                this.engine.showNotification(`Deleted held bill ${reference}.`, 'info');
                await this.loadHeldCarts();
            }
        } catch (err) {
            this.engine.showNotification('Error deleting bill.', 'danger');
        }
    }
}

// ============================================================================
// 6. CHECKOUT, OVERRIDE PIN & SPLIT PAYMENT SUBMISSION
// ============================================================================
class POSCheckout {
    constructor(engine) {
        this.engine = engine;
        this.checkoutApiUrl = '/sales/api/checkout/';
        this.isProcessing = false;
        this.managerPin = null;
    }

    setButtonsDisabled(disabled) {
        this.isProcessing = disabled;
        const cashBtn = document.getElementById('posDirectCashCheckoutBtn');
        const creditBtn = document.getElementById('posCreditPayBtn');
        const splitBtn = document.getElementById('posSplitPaymentBtn');
        const confirmSplitBtn = document.getElementById('confirmSplitPaymentBtn');

        if (cashBtn) {
            cashBtn.disabled = disabled;
            if (disabled) cashBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i> PROCESSING...';
            else cashBtn.innerHTML = '💵 CASH PAYMENT (नगद भुक्तानी) [F4]';
        }
        if (creditBtn) creditBtn.disabled = disabled;
        if (splitBtn) splitBtn.disabled = disabled;
        if (confirmSplitBtn) {
            confirmSplitBtn.disabled = disabled;
            if (disabled) confirmSplitBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i> Finalizing...';
            else confirmSplitBtn.innerHTML = '<i class="fas fa-check-circle me-1"></i> Finalize Split Payment';
        }
    }

    async processDirectCash() {
        if (this.isProcessing) return;

        if (this.engine.cart.items.length === 0) {
            this.engine.showNotification('Bill is empty. Scan barcodes or add items before checkout.', 'warning');
            return;
        }

        const totals = this.engine.cart.recalculateTotals();
        const payload = this.buildPayload([{ mode: 'CASH', amount: totals.grandTotal, transaction_ref: '' }]);

        this.setButtonsDisabled(true);
        await this.executeSubmit(payload, 'CASH (नगद)');
    }

    async processCreditCheckout() {
        if (this.isProcessing) return;

        if (this.engine.cart.items.length === 0) {
            this.engine.showNotification('Bill is empty.', 'warning');
            return;
        }

        const totals = this.engine.cart.recalculateTotals();
        const custName = document.getElementById('posCustomerInput')?.value.trim() || '';
        const custId = document.getElementById('posCustomerId')?.value || (this.engine.cart.customer ? this.engine.cart.customer.id : null);

        if (!custId && (!custName || custName.toLowerCase() === 'walk-in customer' || custName.toLowerCase() === 'walk-in' || custName === '-')) {
            this.engine.showNotification('Credit sales (Udhaari) strictly require a registered customer profile.', 'danger');
            POSAudioSynthesizer.play('error');
            document.getElementById('posCustomerInput')?.focus();
            return;
        }

        const payload = this.buildPayload([{ mode: 'CREDIT', amount: totals.grandTotal, transaction_ref: 'UDHAARI' }]);
        this.setButtonsDisabled(true);
        await this.executeSubmit(payload, 'CREDIT (उधारो)');
    }

    async processSplitCheckout() {
        if (this.isProcessing) return;

        if (!this.engine.cart.items || this.engine.cart.items.length === 0) {
            this.engine.showNotification('Bill is empty. Add items before checking out.', 'warning');
            return;
        }

        const totals = this.engine.cart.recalculateTotals();
        const grandTotal = totals.grandTotal;

        const cash = parseFloat(document.getElementById('splitCashAmt')?.value) || 0;
        const credit = parseFloat(document.getElementById('splitCreditAmt')?.value) || 0;
        const fonepay = parseFloat(document.getElementById('splitFonepayAmt')?.value) || 0;
        const fonepayRef = (document.getElementById('splitFonepayRef')?.value || '').trim();
        const esewa = parseFloat(document.getElementById('splitEsewaAmt')?.value) || 0;
        const esewaRef = (document.getElementById('splitEsewaRef')?.value || '').trim();
        const khalti = parseFloat(document.getElementById('splitKhaltiAmt')?.value) || 0;
        const khaltiRef = (document.getElementById('splitKhaltiRef')?.value || '').trim();
        const card = parseFloat(document.getElementById('splitCardAmt')?.value) || 0;
        const cardRef = (document.getElementById('splitCardRef')?.value || '').trim();
        const bank = parseFloat(document.getElementById('splitBankAmt')?.value) || 0;
        const bankRef = (document.getElementById('splitBankRef')?.value || '').trim();

        const payments = [];
        if (cash > 0) payments.push({ mode: 'CASH', amount: cash, transaction_ref: '' });
        if (fonepay > 0) payments.push({ mode: 'FONEPAY', amount: fonepay, transaction_ref: fonepayRef });
        if (esewa > 0) payments.push({ mode: 'ESEWA', amount: esewa, transaction_ref: esewaRef });
        if (khalti > 0) payments.push({ mode: 'KHALTI', amount: khalti, transaction_ref: khaltiRef });
        if (card > 0) payments.push({ mode: 'CARD', amount: card, transaction_ref: cardRef });
        if (bank > 0) payments.push({ mode: 'BANK_TRANSFER', amount: bank, transaction_ref: bankRef });
        if (credit > 0) payments.push({ mode: 'CREDIT', amount: credit, transaction_ref: 'UDHAARI' });

        const totalTendered = cash + fonepay + esewa + khalti + card + bank + credit;

        if (Math.abs(totalTendered - grandTotal) > 0.05) {
            const diff = grandTotal - totalTendered;
            this.engine.showNotification(
                `Payment distribution mismatch: Total entered (Rs. ${totalTendered.toLocaleString('en-IN', {minimumFractionDigits: 2})}) ` +
                `must equal Grand Total (Rs. ${grandTotal.toLocaleString('en-IN', {minimumFractionDigits: 2})}). ` +
                `Remaining Balance: Rs. ${diff.toFixed(2)}`,
                'danger'
            );
            POSAudioSynthesizer.play('error');
            return;
        }

        const isCreditSale = credit > 0 || totalTendered < grandTotal;
        const custName = document.getElementById('posCustomerInput')?.value.trim() || '';
        const custId = document.getElementById('posCustomerId')?.value || (this.engine.cart.customer ? this.engine.cart.customer.id : null);
        const isAnonymous = !custName || custName.toLowerCase() === 'walk-in customer' || custName.toLowerCase() === 'walk-in' || custName === '-';

        if (isCreditSale && isAnonymous && !custId) {
            this.engine.showNotification(
                'Customer Udhaari (Credit) strictly requires selecting or registering a customer profile.',
                'danger'
            );
            POSAudioSynthesizer.play('error');
            const custInput = document.getElementById('posCustomerInput');
            if (custInput) custInput.focus();
            return;
        }

        const payload = this.buildPayload(payments);
        this.setButtonsDisabled(true);

        const modalEl = document.getElementById('splitPaymentModal');
        if (modalEl && typeof bootstrap !== 'undefined') {
            const modalInstance = bootstrap.Modal.getInstance(modalEl);
            if (modalInstance) modalInstance.hide();
        }

        await this.executeSubmit(payload, 'SPLIT PAYMENT');
    }

    buildPayload(payments) {
        const custName = document.getElementById('posCustomerInput')?.value.trim() || 'Walk-in Customer';
        const custPhone = document.getElementById('posCustomerMobileInput')?.value.trim() || '';
        const custPan = document.getElementById('posCustomerPanInput')?.value.trim() || '';
        const custId = document.getElementById('posCustomerId')?.value || null;
        const narration = document.getElementById('posBillNarration')?.value.trim() || '';
        const terms = document.getElementById('posBillTerms')?.value.trim() || '';

        const effectiveBsDate = this.engine.getEffectiveBillDateBs();

        const packagedCartItems = this.engine.cart.items.map(item => {
            const qty = Math.max(1, parseFloat(item.quantity) || 1);
            const price = Math.max(0, parseFloat(item.unit_price) || 0);
            const discVal = Math.max(0, parseFloat(item.discount_value) || 0);
            const discType = item.discount_type || 'PERCENTAGE';
            const gross = price * qty;

            let effectivePct = 0;
            if (discType === 'PERCENTAGE') {
                effectivePct = Math.min(100, discVal);
            } else if (gross > 0) {
                effectivePct = (Math.min(discVal, gross) / gross) * 100;
            }

            return {
                product_id: item.product_id,
                item_instance_id: item.item_instance_id || null,
                name: item.name,
                unit_price: price,
                price: price,
                quantity: qty,
                discount_type: discType,
                discount_value: discVal,
                discount_input_value: discVal,
                discount_percent: effectivePct,
                tax_pricing_type: item.tax_type === '13%' ? 'EXCLUSIVE' : 'EXEMPT',
                vat_rate: item.tax_type === '13%' ? 13 : 0,
                requires_imei: Boolean(item.requires_imei),
                imei_1: item.imei_1 || '',
                imei_number: item.imei_1 || '',
                imei_2: item.imei_2 || '',
                secondary_imei: item.imei_2 || '',
                repair_ticket_id: item.repair_ticket_id || null,
                is_repair_service: Boolean(item.is_repair_service)
            };
        });

        const totals = this.engine.cart.recalculateTotals();
        const tradeInDeduction = parseFloat(document.getElementById('trade-in-input')?.value) || 0;

        return {
            idempotency_key: this.engine.cart.activeCartIdempotencyKey,
            cart: packagedCartItems,
            payments: payments,
            customer_id: custId || (this.engine.cart.customer ? this.engine.cart.customer.id : null),
            customer_name: custName,
            customer_phone: custPhone,
            customer_pan: custPan,
            bill_date_bs: effectiveBsDate || '',
            bill_discount_type: this.engine.cart.billDiscountType,
            bill_discount_input_value: this.engine.cart.billDiscountValue,
            bill_discount_value: this.engine.cart.billDiscountValue,
            bill_discount_amount: totals.billDiscountAmt,
            bill_discount_percent: totals.billDiscountPercent,
            discount_percent: totals.billDiscountPercent,
            trade_in_voucher_id: this.engine.tradeInManager ? this.engine.tradeInManager.voucherId : null,
            trade_in_credit_amount: tradeInDeduction,
            manager_pin: this.managerPin || '',
            notes: narration ? (terms ? `${narration} | Terms: ${terms}` : narration) : terms
        };
    }

    async executeSubmit(payload, payModeDisplay = 'CASH') {
        const csrfToken = getCsrfToken();

        try {
            const response = await fetch(this.checkoutApiUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                    'X-Idempotency-Key': payload.idempotency_key
                },
                body: JSON.stringify(payload)
            });

            const data = await response.json();

            if (response.status === 403 && (data.message || '').toLowerCase().includes('manager pin')) {
                this.setButtonsDisabled(false);
                const pin = prompt('Supervisor authorization PIN required for this discount or credit override:');
                if (pin) {
                    this.managerPin = pin;
                    payload.manager_pin = pin;
                    this.setButtonsDisabled(true);
                    return await this.executeSubmit(payload, payModeDisplay);
                } else {
                    this.engine.showNotification('Checkout cancelled: Supervisor PIN authorization was not provided.', 'warning');
                    this.managerPin = null;
                    return;
                }
            }

            if (response.ok && data.status === 'success') {
                this.managerPin = null;
                const totals = this.engine.cart.recalculateTotals();

                this.engine.showThermalReceipt(data.estimate_number, payload.customer_name, payModeDisplay, totals);
                this.engine.showNotification(`Bill ${data.estimate_number} finalized successfully!`, 'success');
                POSAudioSynthesizer.play('success');

                this.engine.cart.clear(true);
                this.engine.customerManager.clearCustomer(true);
                this.engine.resetBillDateToToday();
            } else {
                this.engine.showNotification(`Checkout failed: ${data.message || 'Validation error'}`, 'danger');
                POSAudioSynthesizer.play('error');
            }
        } catch (err) {
            console.error('[POSCheckout] Submission error:', err);
            this.engine.showNotification('Network communication error during checkout.', 'danger');
        } finally {
            this.setButtonsDisabled(false);
        }
    }
}

// ============================================================================
// 7. MASTER POS ENGINE ORCHESTRATOR
// ============================================================================
class SmartPOSEngine {
    constructor() {
        const taxConfigEl = document.getElementById('posTaxConfigMeta');
        this.enforceImei = taxConfigEl ? (taxConfigEl.dataset.enforceImei !== 'false') : true;

        this.todayBs = (taxConfigEl?.dataset.todayBs || '').trim();
        this.activeFiscalYear = (taxConfigEl?.dataset.activeFiscalYear || '2083/84').trim();
        this.canBackdate = taxConfigEl ? (taxConfigEl.dataset.canBackdate === 'true') : true;
        this.isPrivileged = taxConfigEl ? (taxConfigEl.dataset.isPrivileged === 'true') : false;

        this.customBillDateBs = null;
        this.splitGrandTotal = 0;

        this.initDomElements();

        // Instantiate Modules
        this.customerManager = new POSCustomerManager(this);
        this.tradeInManager = new POSTradeInManager(this);
        this.cart = new POSCart(this);
        this.holdCartManager = new POSHoldCartManager(this);
        this.checkout = new POSCheckout(this);

        this.bindEvents();
        this.initDateControls();
        this.initHotkeys();
        this.initBarcodeSearchTypeahead();
        this.initSplitModalDynamicCalculations();

        // Initial empty card render
        this.cart.render();
    }

    initDomElements() {
        this.scannerInput = document.getElementById('posProductSearchInput');
        this.barcodeDropdown = document.getElementById('barcodeSearchDropdown');
        this.cashBtn = document.getElementById('posDirectCashCheckoutBtn');
        this.creditBtn = document.getElementById('posCreditPayBtn');
        this.splitBtn = document.getElementById('posSplitPaymentBtn');
        this.splitModalEl = document.getElementById('splitPaymentModal');

        this.btnChangeBillDate = document.getElementById('btnChangeBillDate');
        this.btnResetTodayDate = document.getElementById('btnResetTodayDate');
        this.btnApplyCustomDate = document.getElementById('btnApplyCustomDate');
        this.btnCancelCustomDate = document.getElementById('btnCancelCustomDate');
        this.customDatePickerWrapper = document.getElementById('customDatePickerWrapper');
        this.posBillDateBsInput = document.getElementById('posBillDateBsInput');
        this.displayBillDateBs = document.getElementById('displayBillDateBs');
        this.dateValidationErrorAlert = document.getElementById('dateValidationErrorAlert');
    }

    getEffectiveBillDateBs() {
        if (this.customBillDateBs) {
            return this.customBillDateBs;
        }

        if (this.posBillDateBsInput && this.posBillDateBsInput.value) {
            const rawVal = this.posBillDateBsInput.value.trim().replace(/\//g, '-').replace(/\./g, '-');
            if (rawVal && /^\d{4}-\d{2}-\d{2}$/.test(rawVal)) {
                return rawVal;
            }
        }

        return this.todayBs || '';
    }

    /**
     * Explicitly close and hide the floating calendar popup (#ndp-nepali-box)
     * so it never lingers on screen as an orphan.
     */
    closeFloatingCalendarBox() {
        const ndpBox = document.getElementById('ndp-nepali-box') || document.querySelector('.ndp-nepali-box');
        if (ndpBox) {
            ndpBox.style.display = 'none';
        }
        if (this.posBillDateBsInput) {
            this.posBillDateBsInput.blur();
        }
    }

    setCustomBillDateBs(dateStr) {
        if (!dateStr) return;
        const normalized = dateStr.trim().replace(/\//g, '-').replace(/\./g, '-');
        this.customBillDateBs = normalized;

        if (this.posBillDateBsInput) {
            this.posBillDateBsInput.value = normalized;
        }
        if (this.displayBillDateBs) {
            this.displayBillDateBs.innerText = `${normalized} BS`;
        }
        if (this.btnResetTodayDate) {
            this.btnResetTodayDate.classList.remove('d-none');
        }
        if (this.customDatePickerWrapper) {
            this.customDatePickerWrapper.classList.add('d-none');
        }
        this.hideDateError();
        this.closeFloatingCalendarBox();
    }

    resetBillDateToToday() {
        this.customBillDateBs = null;

        const defaultToday = this.todayBs || (document.getElementById('posTaxConfigMeta')?.dataset.todayBs || '').trim();
        if (this.posBillDateBsInput) {
            this.posBillDateBsInput.value = defaultToday;
        }
        if (this.displayBillDateBs) {
            this.displayBillDateBs.innerText = defaultToday ? `${defaultToday} BS` : 'Today';
        }
        if (this.btnResetTodayDate) {
            this.btnResetTodayDate.classList.add('d-none');
        }
        if (this.customDatePickerWrapper) {
            this.customDatePickerWrapper.classList.add('d-none');
        }
        this.hideDateError();
        this.closeFloatingCalendarBox();
    }

    showDateError(msg) {
        if (this.dateValidationErrorAlert) {
            this.dateValidationErrorAlert.innerText = msg;
            this.dateValidationErrorAlert.classList.remove('d-none');
        } else {
            alert(msg);
        }
    }

    hideDateError() {
        if (this.dateValidationErrorAlert) {
            this.dateValidationErrorAlert.innerText = '';
            this.dateValidationErrorAlert.classList.add('d-none');
        }
    }

    deriveBsFiscalYear(bsYear, bsMonth) {
        if (bsMonth >= 4) {
            const nextShort = String(bsYear + 1).slice(-2);
            return `${bsYear}/${nextShort}`;
        } else {
            const prevYear = bsYear - 1;
            const currShort = String(bsYear).slice(-2);
            return `${prevYear}/${currShort}`;
        }
    }

    async promptForSupervisorPin(reason = 'Supervisor PIN required to backdate bill.') {
        if (this.checkout && typeof this.checkout.promptForManagerPin === 'function') {
            return await this.checkout.promptForManagerPin(reason);
        }
        const pin = prompt(`${reason}\nEnter Supervisor PIN:`);
        return pin ? pin.trim() : null;
    }

    async validateAndApplyCustomDate(rawVal) {
        this.hideDateError();
        if (!rawVal) {
            this.showDateError('Please enter or select a valid Bikram Sambat date.');
            return false;
        }

        const cleanVal = rawVal.trim().replace(/\//g, '-').replace(/\./g, '-');
        const parts = cleanVal.split('-').map(Number);
        if (parts.length !== 3 || isNaN(parts[0]) || isNaN(parts[1]) || isNaN(parts[2])) {
            this.showDateError('Invalid date format. Expected YYYY-MM-DD (e.g. 2083-06-12).');
            return false;
        }

        const [year, month, day] = parts;
        if (year < 2000 || year > 2095 || month < 1 || month > 12 || day < 1 || day > 32) {
            this.showDateError('B.S. date components are outside the supported Bikram Sambat calendar range.');
            return false;
        }

        const formattedDate = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
        const derivedFy = this.deriveBsFiscalYear(year, month);

        if (this.activeFiscalYear && derivedFy !== this.activeFiscalYear) {
            this.showDateError(`Selected date (${formattedDate}) belongs to Fiscal Year ${derivedFy}. Only transactions within active FY (${this.activeFiscalYear}) are permitted.`);
            return false;
        }

        // Supervisor authorization check for cashiers in live mode
        if (!this.isPrivileged && this.enforceImei && this.todayBs && formattedDate !== this.todayBs) {
            const pin = await this.promptForSupervisorPin('Supervisor authorization PIN is required to change or backdate the bill date in live counter mode.');
            if (!pin) {
                this.showDateError('Date adjustment cancelled: Supervisor authorization was not provided.');
                return false;
            }
            if (this.checkout) {
                this.checkout.managerPin = pin;
            }
        }

        this.setCustomBillDateBs(formattedDate);
        this.showNotification(`Bill date updated to ${formattedDate} BS (FY ${derivedFy}).`, 'info');
        return true;
    }

    /**
     * Binds the visual Nepali Datepicker with an immediate auto-select onChange handler.
     */
    bindNepaliDatePicker() {
        if (!this.posBillDateBsInput) return;

        if (typeof this.posBillDateBsInput.nepaliDatePicker === 'function') {
            try {
                this.posBillDateBsInput.nepaliDatePicker({
                    ndpYear: true,
                    ndpMonth: true,
                    ndpYearCount: 25,
                    readOnlyInput: false,
                    dateFormat: 'YYYY-MM-DD',
                    language: 'english',
                    onChange: (e) => {
                        const pickedDate = (e && e.bs) ? e.bs : (this.posBillDateBsInput ? this.posBillDateBsInput.value : '');
                        if (pickedDate) {
                            this.validateAndApplyCustomDate(pickedDate);
                        }
                    }
                });
                this.posBillDateBsInput.dataset.ndpInitialized = 'true';
            } catch (err) {
                console.warn('[SmartPOSEngine] Datepicker binding warning:', err);
            }
        }
    }

    toggleDatepickerPopover() {
        if (!this.customDatePickerWrapper) return;
        const isHidden = this.customDatePickerWrapper.classList.contains('d-none');

        if (isHidden) {
            this.customDatePickerWrapper.classList.remove('d-none');
            this.hideDateError();

            if (this.posBillDateBsInput) {
                this.bindNepaliDatePicker();
                this.posBillDateBsInput.focus();
                this.posBillDateBsInput.select();
            }
        } else {
            this.customDatePickerWrapper.classList.add('d-none');
            this.hideDateError();
            this.closeFloatingCalendarBox();
        }
    }

    initDateControls() {
        if (!this.todayBs && this.posBillDateBsInput) {
            this.todayBs = this.posBillDateBsInput.value.trim();
        }

        // Initialize datepicker library binding with auto-select
        this.bindNepaliDatePicker();

        // 1. Entire .bs-date-badge container opens datepicker
        if (this.btnChangeBillDate && this.customDatePickerWrapper) {
            this.btnChangeBillDate.addEventListener('click', (e) => {
                if (e.target.closest('#btnResetTodayDate')) {
                    return;
                }
                e.preventDefault();
                e.stopPropagation();
                this.toggleDatepickerPopover();
            });

            this.btnChangeBillDate.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    this.toggleDatepickerPopover();
                }
            });
        }

        // 2. Prevent clicks inside popover from bubbling up to document and closing it
        if (this.customDatePickerWrapper) {
            this.customDatePickerWrapper.addEventListener('click', (e) => {
                e.stopPropagation();
            });
        }

        // 3. Document click-outside listener to dismiss both white container AND floating calendar
        document.addEventListener('click', (e) => {
            const ndpBox = document.getElementById('ndp-nepali-box') || document.querySelector('.ndp-nepali-box');
            if (this.customDatePickerWrapper && !this.customDatePickerWrapper.classList.contains('d-none')) {
                const clickedInsideWrapper = this.customDatePickerWrapper.contains(e.target);
                const clickedOnTrigger = this.btnChangeBillDate && this.btnChangeBillDate.contains(e.target);
                const clickedInsideNdpBox = ndpBox && ndpBox.contains(e.target);

                if (!clickedInsideWrapper && !clickedOnTrigger && !clickedInsideNdpBox) {
                    this.customDatePickerWrapper.classList.add('d-none');
                    this.hideDateError();
                    this.closeFloatingCalendarBox();
                }
            }
        });

        // 4. Auto-select upon calendar dateSelect, change, or Enter key
        if (this.posBillDateBsInput) {
            this.posBillDateBsInput.addEventListener('dateSelect', (e) => {
                const selectedVal = e.detail?.value || this.posBillDateBsInput.value;
                if (selectedVal) {
                    this.validateAndApplyCustomDate(selectedVal);
                }
            });

            this.posBillDateBsInput.addEventListener('change', (e) => {
                const selectedVal = e.target.value;
                if (selectedVal && /^\d{4}[-/. ]\d{1,2}[-/. ]\d{1,2}$/.test(selectedVal.trim())) {
                    this.validateAndApplyCustomDate(selectedVal);
                }
            });

            this.posBillDateBsInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    this.validateAndApplyCustomDate(this.posBillDateBsInput.value);
                } else if (e.key === 'Escape') {
                    if (this.customDatePickerWrapper) {
                        this.customDatePickerWrapper.classList.add('d-none');
                        this.hideDateError();
                        this.closeFloatingCalendarBox();
                    }
                }
            });
        }

        // 5. Apply button (preserved for backward compatibility if present in DOM)
        if (this.btnApplyCustomDate && this.posBillDateBsInput) {
            this.btnApplyCustomDate.addEventListener('click', (e) => {
                e.preventDefault();
                this.validateAndApplyCustomDate(this.posBillDateBsInput.value);
            });
        }

        // 6. Cancel button closes both white box and floating calendar
        if (this.btnCancelCustomDate && this.customDatePickerWrapper) {
            this.btnCancelCustomDate.addEventListener('click', (e) => {
                e.preventDefault();
                this.customDatePickerWrapper.classList.add('d-none');
                this.hideDateError();
                this.closeFloatingCalendarBox();
            });
        }

        // 7. Reset button
        if (this.btnResetTodayDate) {
            this.btnResetTodayDate.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this.resetBillDateToToday();
                this.showNotification('Bill date reset to today.', 'info');
            });
        }
    }

    bindEvents() {
        if (this.cashBtn) {
            this.cashBtn.addEventListener('click', () => this.checkout.processDirectCash());
        }
        if (this.creditBtn) {
            this.creditBtn.addEventListener('click', () => this.checkout.processCreditCheckout());
        }
        if (this.splitBtn) {
            this.splitBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.openSplitPaymentModal();
            });
        }
    }

    initHotkeys() {
        window.addEventListener('keydown', (e) => {
            if (e.key === 'F2') {
                e.preventDefault();
                if (this.scannerInput) {
                    this.scannerInput.focus();
                    this.scannerInput.select();
                }
            } else if (e.key === 'F4') {
                e.preventDefault();
                this.checkout.processDirectCash();
            } else if (e.key === 'F6') {
                e.preventDefault();
                const excModal = document.getElementById('exchangeModal');
                if (excModal && typeof bootstrap !== 'undefined') {
                    bootstrap.Modal.getOrCreateInstance(excModal).show();
                }
            } else if (e.key === 'F7') {
                e.preventDefault();
                const repModal = document.getElementById('repairTicketSearchModal');
                if (repModal && typeof bootstrap !== 'undefined') {
                    bootstrap.Modal.getOrCreateInstance(repModal).show();
                }
            } else if (e.key === 'F8') {
                e.preventDefault();
                this.openSplitPaymentModal();
            } else if (e.key === 'F9') {
                e.preventDefault();
                this.holdCartManager.holdCurrentCart();
            } else if (e.key === 'F10') {
                e.preventDefault();
                this.holdCartManager.openRecallModal();
            }
        });
    }

    openSplitPaymentModal() {
        const modalEl = document.getElementById('splitPaymentModal');
        if (!modalEl || typeof bootstrap === 'undefined') return;

        const totals = this.cart.recalculateTotals();

        if (!this.cart.items || this.cart.items.length === 0 || totals.grandTotal <= 0) {
            this.showNotification('Cannot open Split Payment: Cart is empty or bill total is zero.', 'warning');
            POSAudioSynthesizer.play('error');
            return;
        }

        this.splitGrandTotal = totals.grandTotal;

        const payableEl = document.getElementById('splitModalPayableTotal');
        if (payableEl) {
            payableEl.innerText = formatCurrencyNPR(totals.grandTotal);
        }

        const cashInput = document.getElementById('splitCashAmt');
        const creditInput = document.getElementById('splitCreditAmt');
        const fonepayInput = document.getElementById('splitFonepayAmt');
        const fonepayRef = document.getElementById('splitFonepayRef');
        const esewaInput = document.getElementById('splitEsewaAmt');
        const esewaRef = document.getElementById('splitEsewaRef');
        const khaltiInput = document.getElementById('splitKhaltiAmt');
        const khaltiRef = document.getElementById('splitKhaltiRef');
        const cardInput = document.getElementById('splitCardAmt');
        const cardRef = document.getElementById('splitCardRef');
        const bankInput = document.getElementById('splitBankAmt');
        const bankRef = document.getElementById('splitBankRef');

        const inputsToClear = [
            cashInput, creditInput, fonepayInput, esewaInput,
            khaltiInput, cardInput, bankInput
        ];
        inputsToClear.forEach(inp => {
            if (inp) inp.value = '';
        });

        const refsToClear = [fonepayRef, esewaRef, khaltiRef, cardRef, bankRef];
        refsToClear.forEach(ref => {
            if (ref) ref.value = '';
        });

        const remBalEl = document.getElementById('splitRemainingBalance');
        if (remBalEl) {
            remBalEl.innerText = formatCurrencyNPR(totals.grandTotal);
            remBalEl.className = 'fs-4 fw-bold text-danger font-mono';
        }

        const modalInstance = bootstrap.Modal.getOrCreateInstance(modalEl);
        modalInstance.show();

        setTimeout(() => {
            if (cashInput) cashInput.focus();
        }, 300);
    }

    initSplitModalDynamicCalculations() {
        const modalEl = document.getElementById('splitPaymentModal');
        if (!modalEl) return;

        const inputs = modalEl.querySelectorAll('.split-pay-input');
        const cashInput = document.getElementById('splitCashAmt');
        const creditInput = document.getElementById('splitCreditAmt');
        const fonepayInput = document.getElementById('splitFonepayAmt');
        const esewaInput = document.getElementById('splitEsewaAmt');
        const khaltiInput = document.getElementById('splitKhaltiAmt');
        const cardInput = document.getElementById('splitCardAmt');
        const bankInput = document.getElementById('splitBankAmt');
        const remBalEl = document.getElementById('splitRemainingBalance');

        const recalculateSplitRemaining = () => {
            const grandTotal = this.splitGrandTotal || this.cart.recalculateTotals().grandTotal;

            const cash = parseFloat(cashInput?.value) || 0;
            const fonepay = parseFloat(fonepayInput?.value) || 0;
            const esewa = parseFloat(esewaInput?.value) || 0;
            const khalti = parseFloat(khaltiInput?.value) || 0;
            const card = parseFloat(cardInput?.value) || 0;
            const bank = parseFloat(bankInput?.value) || 0;
            const credit = parseFloat(creditInput?.value) || 0;

            const totalImmediatePaid = cash + fonepay + esewa + khalti + card + bank;
            const totalTendered = totalImmediatePaid + credit;
            const netRemainingBalance = grandTotal - totalTendered;

            if (remBalEl) {
                if (Math.abs(netRemainingBalance) < 0.01) {
                    remBalEl.innerText = 'Rs. 0.00 (Balanced)';
                    remBalEl.className = 'fs-4 fw-bold text-success font-mono';
                } else if (netRemainingBalance > 0) {
                    remBalEl.innerText = formatCurrencyNPR(netRemainingBalance);
                    remBalEl.className = 'fs-4 fw-bold text-danger font-mono';
                } else {
                    remBalEl.innerText = `Overpaid: ${formatCurrencyNPR(Math.abs(netRemainingBalance))}`;
                    remBalEl.className = 'fs-4 fw-bold text-warning font-mono';
                }
            }
        };

        inputs.forEach(inp => {
            inp.addEventListener('input', recalculateSplitRemaining);
        });

        if (creditInput) {
            const allocateRemainingToUdhaari = () => {
                const currentCreditVal = parseFloat(creditInput.value) || 0;
                if (currentCreditVal === 0) {
                    const grandTotal = this.splitGrandTotal || this.cart.recalculateTotals().grandTotal;

                    const cash = parseFloat(cashInput?.value) || 0;
                    const fonepay = parseFloat(fonepayInput?.value) || 0;
                    const esewa = parseFloat(esewaInput?.value) || 0;
                    const khalti = parseFloat(khaltiInput?.value) || 0;
                    const card = parseFloat(cardInput?.value) || 0;
                    const bank = parseFloat(bankInput?.value) || 0;

                    const totalImmediatePaid = cash + fonepay + esewa + khalti + card + bank;
                    const unpaidBeforeCredit = Math.max(0, grandTotal - totalImmediatePaid);

                    if (unpaidBeforeCredit > 0) {
                        creditInput.value = unpaidBeforeCredit.toFixed(2);
                        recalculateSplitRemaining();
                    }
                }
            };

            creditInput.addEventListener('focus', allocateRemainingToUdhaari);
            creditInput.addEventListener('click', allocateRemainingToUdhaari);
        }

        const confirmSplitBtn = document.getElementById('confirmSplitPaymentBtn');
        if (confirmSplitBtn) {
            confirmSplitBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.checkout.processSplitCheckout();
            });
        }
    }

    initBarcodeSearchTypeahead() {
        if (!this.scannerInput || !this.barcodeDropdown) return;

        let debounceTimer = null;
        let activeIndex = -1;
        let currentResults = [];

        const closeDropdown = () => {
            this.barcodeDropdown.classList.add('d-none');
            this.barcodeDropdown.innerHTML = '';
            activeIndex = -1;
            currentResults = [];
        };

        const fetchAndRenderProducts = async (query = '') => {
            try {
                this.barcodeDropdown.innerHTML = '<div class="p-3 text-center text-muted fs-xs"><i class="fas fa-spinner fa-spin me-1 text-primary"></i> Querying stock...</div>';
                this.barcodeDropdown.classList.remove('d-none');

                const url = query ? `/products/api/search/?mode=pos&q=${encodeURIComponent(query)}&limit=15` : `/products/api/search/?mode=pos&limit=15`;
                const resp = await fetch(url);
                if (!resp.ok) throw new Error('Search failed');

                const data = await resp.json();
                currentResults = data.results || (Array.isArray(data) ? data : []);
                activeIndex = -1;

                if (!currentResults.length) {
                    this.barcodeDropdown.innerHTML = `
                        <div class="p-3 text-center text-muted fs-xs">
                            <i class="fas fa-box-open opacity-50 d-block mb-1"></i>
                            No products found matching "<strong>${escapeHtml(query)}</strong>"
                        </div>`;
                    return;
                }

                let html = '';
                currentResults.forEach((p, idx) => {
                    const price = parseFloat(p.price || p.selling_price || 0);
                    const stock = parseFloat(p.available_stock || 0);
                    const isImei = Boolean(p.requires_imei || p.requires_imei_tracking || p.match_type === 'IMEI');
                    const subtitle = p.specs || p.model_name || `SKU: ${p.sku || 'N/A'}`;

                    html += `
                        <div class="pos-suggestion-item" data-index="${idx}">
                            <div>
                                <div class="pos-suggestion-title">
                                    ${highlightMatch(p.name, query)}
                                    ${isImei ? '<span class="badge bg-primary bg-opacity-10 text-primary ms-1 font-mono fs-2xs">IMEI</span>' : ''}
                                </div>
                                <div class="pos-suggestion-subtitle">
                                    ${escapeHtml(subtitle)} &bull; Stock: <strong class="${stock > 0 ? 'text-success' : 'text-danger'}">${stock}</strong>
                                    ${p.imei_1 ? ` &bull; IMEI: <span class="font-mono text-dark fw-bold">${highlightMatch(p.imei_1, query)}</span>` : ''}
                                </div>
                            </div>
                            <div class="text-end ps-3">
                                <div class="fw-bold font-mono text-primary fs-xs">Rs. ${price.toLocaleString('en-IN', {minimumFractionDigits: 2})}</div>
                                <div class="fs-3xs text-muted font-mono mt-0.5">${p.unit_code || 'Pcs'}</div>
                            </div>
                        </div>
                    `;
                });

                this.barcodeDropdown.innerHTML = html;

                this.barcodeDropdown.querySelectorAll('.pos-suggestion-item').forEach(itemEl => {
                    itemEl.addEventListener('click', () => {
                        const idx = parseInt(itemEl.dataset.index, 10);
                        if (currentResults[idx]) {
                            this.selectProductItem(currentResults[idx]);
                        }
                    });
                });

            } catch (err) {
                console.warn('[SmartPOSEngine] Product typeahead error:', err);
                this.barcodeDropdown.innerHTML = '<div class="p-2 text-center text-danger fs-xs">Catalog lookup unavailable</div>';
            }
        };

        const updateActiveItem = (items) => {
            items.forEach((item, idx) => {
                if (idx === activeIndex) {
                    item.classList.add('active');
                    item.scrollIntoView({ block: 'nearest' });
                } else {
                    item.classList.remove('active');
                }
            });
        };

        this.scannerInput.addEventListener('focus', () => {
            const val = this.scannerInput.value.trim();
            fetchAndRenderProducts(val);
        });

        this.scannerInput.addEventListener('click', () => {
            if (this.barcodeDropdown.classList.contains('d-none')) {
                const val = this.scannerInput.value.trim();
                fetchAndRenderProducts(val);
            }
        });

        this.scannerInput.addEventListener('input', (e) => {
            const query = e.target.value.trim();

            if (query.length >= 14 && /^\d+$/.test(query)) {
                clearTimeout(debounceTimer);
                closeDropdown();
                return;
            }

            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                fetchAndRenderProducts(query);
            }, 150);
        });

        this.scannerInput.addEventListener('keydown', async (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const code = this.scannerInput.value.trim();

                if (activeIndex >= 0 && currentResults[activeIndex]) {
                    this.selectProductItem(currentResults[activeIndex]);
                    return;
                }

                if (code) {
                    clearTimeout(debounceTimer);
                    closeDropdown();
                    await this.handleDirectImeiOrBarcodeScan(code);
                    this.scannerInput.value = '';
                }
                return;
            }

            if (this.barcodeDropdown.classList.contains('d-none')) return;

            const items = this.barcodeDropdown.querySelectorAll('.pos-suggestion-item');
            if (!items.length) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeIndex = (activeIndex + 1) % items.length;
                updateActiveItem(items);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeIndex = (activeIndex - 1 + items.length) % items.length;
                updateActiveItem(items);
            } else if (e.key === 'Escape') {
                closeDropdown();
            }
        });

        document.addEventListener('click', (e) => {
            if (!this.barcodeDropdown.contains(e.target) && e.target !== this.scannerInput) {
                closeDropdown();
            }
        });
    }

    selectProductItem(item) {
        if (!item) return;
        this.cart.addItem(item);
        if (this.scannerInput) this.scannerInput.value = '';
        if (this.barcodeDropdown) {
            this.barcodeDropdown.classList.add('d-none');
            this.barcodeDropdown.innerHTML = '';
        }
        POSAudioSynthesizer.play('success');
    }

    async handleDirectImeiOrBarcodeScan(code) {
        try {
            const resp = await fetch(`/products/api/search/?q=${encodeURIComponent(code)}&customer_type=RETAIL`);
            if (!resp.ok) throw new Error('Search failed');
            const data = await resp.json();
            const results = data.results || [];

            if (results.length > 0) {
                const item = results[0];
                this.cart.addItem(item);
                POSAudioSynthesizer.play('success');
            } else {
                this.cart.addItem({
                    product_id: null,
                    name: `Scanned Device (${code})`,
                    unit_price: 0,
                    imei_1: code.length >= 14 ? code : '',
                    requires_imei: code.length >= 14,
                    tax_type: '13%'
                });
                this.showNotification(`Item '${code}' added to bill. Please verify name and rate.`, 'info');
                POSAudioSynthesizer.play('second');
            }
        } catch (err) {
            console.error('[SmartPOSEngine] Scanner lookup error:', err);
            this.showNotification('Error querying barcode catalog.', 'danger');
        }
    }

    showThermalReceipt(slipNo, custName, payMode, totals) {
        const slipEl = document.getElementById('recSlipNo');
        const custEl = document.getElementById('recCustomer');
        const subtotalEl = document.getElementById('recSubtotal');
        const grandEl = document.getElementById('recGrandTotal');
        const payModeEl = document.getElementById('recPayMode');
        const dateEl = document.getElementById('recBillDate');

        const effectiveBsDate = this.getEffectiveBillDateBs();
        if (dateEl) {
            dateEl.innerText = effectiveBsDate ? `${effectiveBsDate} BS` : '';
        }

        if (slipEl) slipEl.innerText = slipNo;
        if (custEl) custEl.innerText = custName;
        if (subtotalEl) subtotalEl.innerText = formatCurrencyNPR(totals.subTotal);
        if (grandEl) grandEl.innerText = formatCurrencyNPR(totals.grandTotal);
        if (payModeEl) payModeEl.innerText = payMode;

        const modalEl = document.getElementById('receiptModal');
        if (modalEl && typeof bootstrap !== 'undefined') {
            bootstrap.Modal.getOrCreateInstance(modalEl).show();
        }
    }

    showNotification(message, type = 'info') {
        const container = document.getElementById('posNotificationArea') || document.body;
        const toast = document.createElement('div');
        toast.className = `alert alert-${type} alert-dismissible fade show position-fixed top-0 end-0 m-3 shadow-lg rounded-4 p-3 fs-xs fw-bold`;
        toast.style.zIndex = '9999';
        toast.style.maxWidth = '360px';
        toast.innerHTML = `
            <div>${escapeHtml(message)}</div>
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        `;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }
}

// Global initialization
window.POSEngine = SmartPOSEngine;

function initializePOS() {
    if (!window.smartPos) {
        window.smartPos = new SmartPOSEngine();
        window.posEngine = window.smartPos;
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializePOS);
} else {
    initializePOS();
}