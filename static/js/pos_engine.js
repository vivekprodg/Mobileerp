/**
 * POS Counter Terminal & Parked Bill Engine (Smart Multi-Attribute Matching & Zero Storage Architecture)
 *
 * Core Capabilities:
 * 1. Optimized Terminal Boot:
 *    - Replaced the full-inventory query (?q=a) with top 24 frequently sold items.
 *    - On-demand debounced server-side catalog searching & category-scoped queries.
 * 2. Explicit Item Discount Types:
 *    - Three discrete modes per line item: NONE, PERCENTAGE (%), and AMOUNT (Rs.).
 *    - Mode toggles: [None | % | Rs.] segmented control on every cart row.
 *    - Direct Amount Usage: In AMOUNT mode, entered value is deducted directly as cash concession.
 *    - Real-Time Effective Percentage: Displays exact mathematical concession percentage against line gross.
 *    - Multi-Quantity Clarity: When Quantity > 1, indicates total line deduction, per-unit discount, and effective percentage.
 *    - Supervisor Limit Warning: Real-time badge alerting if concession exceeds product max discount or store threshold.
 *    - Non-Discountable Enforcement: Disables discount controls when product.is_discountable is false.
 * 3. Dual-Mode Bill-Level Discounts:
 *    - Interactive toggle [% | Rs.] with proportional allocation across discountable items.
 * 4. Parked Bill State Preservation (F9 / F10):
 *    - Serializes and restores exact line discount types (NONE, PERCENTAGE, AMOUNT), input values, and reasons.
 * 5. Multi-Attribute Smart Category Filtering:
 *    - Dynamic category filtering with smart keyword patterns and database Category IDs.
 * 6. Instant Gun Scanner Routing & Dual-IMEI Auto-Match:
 *    - Automatically pairs IMEI 1 and IMEI 2 from catalog/server without unnecessary modal prompts on exact match.
 * 7. Interactive Dual-IMEI Capture Modal:
 *    - Handset box picker and auto-matched secondary IMEI.
 * 8. Strict Walk-In Credit (Udhaari) Guard:
 *    - Blocks credit sales for anonymous walk-ins, mandating a registered customer profile.
 * 9. Server-Side Catalog Price Integrity (SEC-01) & Salted Manager PIN Overrides (SEC-02).
 * 10. Multi-Mode & Split Checkout (F8) with Trade-In Buy-Back Credit deductions & Transaction Ref Tracking.
 * 11. Emergency Offline Safety Net (IndexedDB `pending_sales` queue) & Web Audio chime synthesis.
 */

// ============================================================================
// 0. GLOBAL SECURITY & DOM UTILITIES
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
// 1. WEB AUDIO FEEDBACK CHIME SYNTHESIZER
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
        } catch (e) {}
    }
}

// ============================================================================
// 2. CUSTOMER & UDHAARI MANAGEMENT MODULE
// ============================================================================
class POSCustomerManager {
    constructor(engine) {
        this.engine = engine;
        this.searchApiUrl = '/customers/api/search/';
        this.customerInput = document.getElementById('posCustomerInput') || document.getElementById('customerSelect');
        this.customerPanInput = document.getElementById('posCustomerPanInput');
        this.selectedCustomer = null;
        this.debounceTimer = null;
        this.searchResults = [];
        this.highlightedIndex = -1;

        this.createDropdownElement();
        this.createSelectedBadgeElement();
        this.initEvents();
    }

    createDropdownElement() {
        if (!this.customerInput || this.customerInput.tagName === 'SELECT') return;
        const parent = this.customerInput.closest('.input-group') || this.customerInput.parentElement;
        if (parent) {
            parent.classList.add('position-relative');
            this.dropdownEl = document.createElement('div');
            this.dropdownEl.id = 'posCustomerSearchDropdown';
            this.dropdownEl.className = 'dropdown-menu shadow-lg border bg-white p-1 w-100 rounded-3';
            this.dropdownEl.style.cssText = 'position: absolute; top: 100%; left: 0; right: 0; z-index: 1050; max-height: 280px; overflow-y: auto; display: none; margin-top: 4px;';
            parent.appendChild(this.dropdownEl);
        }
    }

    createSelectedBadgeElement() {
        if (!this.customerInput || this.customerInput.tagName === 'SELECT') return;
        const parent = this.customerInput.closest('.input-group') || this.customerInput.parentElement;
        if (parent) {
            this.selectedBadgeEl = document.createElement('div');
            this.selectedBadgeEl.id = 'posSelectedCustomerChip';
            this.selectedBadgeEl.className = 'd-none align-items-center gap-2 bg-success bg-opacity-10 border border-success border-opacity-25 px-2 py-1 rounded-pill fs-xs text-dark w-100';
            this.selectedBadgeEl.innerHTML = `
                <i class="fas fa-user-check text-success ms-1"></i>
                <span class="fw-bold text-truncate flex-grow-1" id="selectedCustomerChipName">Customer Name</span>
                <span class="badge bg-danger bg-opacity-10 text-danger fs-2xs d-none font-monospace" id="selectedCustomerUdhaariBadge">Rs. 0.00 Due</span>
                <button type="button" class="btn btn-link text-muted p-0 me-1 border-0" id="clearSelectedCustomerBtn" title="Remove customer from bill" style="line-height: 1;">
                    <i class="fas fa-times-circle text-danger"></i>
                </button>
            `;
            parent.parentElement.insertBefore(this.selectedBadgeEl, parent);
        }
    }

    initEvents() {
        if (this.customerInput && this.customerInput.tagName !== 'SELECT') {
            this.customerInput.addEventListener('input', (e) => {
                const query = e.target.value.trim();
                clearTimeout(this.debounceTimer);

                if (this.selectedCustomer && this.selectedCustomer.name !== query) {
                    this.clearCustomer(false);
                }

                if (query.length >= 2) {
                    this.debounceTimer = setTimeout(() => this.search(query), 220);
                } else {
                    this.hideDropdown();
                }
            });

            this.customerInput.addEventListener('keydown', (e) => {
                if (!this.dropdownEl || this.dropdownEl.style.display === 'none') return;

                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this.navigateDropdown(1);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this.navigateDropdown(-1);
                } else if (e.key === 'Enter') {
                    if (this.highlightedIndex >= 0 && this.searchResults[this.highlightedIndex]) {
                        e.preventDefault();
                        this.selectCustomer(this.searchResults[this.highlightedIndex]);
                    }
                } else if (e.key === 'Escape') {
                    this.hideDropdown();
                }
            });
        } else if (this.customerInput && this.customerInput.tagName === 'SELECT') {
            this.customerInput.addEventListener('change', (e) => {
                const opt = e.target.options[e.target.selectedIndex];
                if (opt && opt.value) {
                    this.selectCustomer({
                        id: opt.value,
                        name: opt.text,
                        phone: opt.dataset.phone || '',
                        pan: opt.dataset.pan || '',
                        customer_type: opt.dataset.type || 'RETAIL',
                        credit_balance: opt.dataset.debt || 0
                    });
                } else {
                    this.clearCustomer(true);
                }
            });
        }

        if (this.customerPanInput) {
            this.customerPanInput.addEventListener('input', (e) => {
                this.engine.cart.customerPan = e.target.value.trim();
            });
        }

        if (this.dropdownEl) {
            this.dropdownEl.addEventListener('click', (e) => {
                const itemEl = e.target.closest('[data-customer-index]');
                if (itemEl) {
                    const idx = parseInt(itemEl.dataset.customerIndex, 10);
                    if (this.searchResults[idx]) {
                        this.selectCustomer(this.searchResults[idx]);
                    }
                }
            });
        }

        document.addEventListener('click', (e) => {
            if (this.dropdownEl && !this.dropdownEl.contains(e.target) && e.target !== this.customerInput) {
                this.hideDropdown();
            }
        });

        const clearBtn = document.getElementById('clearSelectedCustomerBtn');
        if (clearBtn) {
            clearBtn.addEventListener('click', () => {
                this.clearCustomer(true);
            });
        }
    }

    async search(query) {
        try {
            const response = await fetch(`${this.searchApiUrl}?q=${encodeURIComponent(query)}`);
            if (!response.ok) return;
            const data = await response.json();
            this.searchResults = data.results || [];
            this.renderDropdown();
        } catch (err) {
            console.error('[POSCustomerManager] Search error:', err);
        }
    }

    renderDropdown() {
        if (!this.dropdownEl) return;

        if (this.searchResults.length === 0) {
            this.dropdownEl.innerHTML = `
                <div class="px-3 py-2 text-muted fs-xs text-center">
                    <i class="fas fa-user-slash me-1 opacity-50"></i> No registered customers found.
                </div>
            `;
            this.showDropdown();
            return;
        }

        this.highlightedIndex = -1;
        this.dropdownEl.innerHTML = this.searchResults.map((c, idx) => {
            const creditBal = parseFloat(c.credit_balance || 0);
            const hasDebt = creditBal > 0;
            const typeLabel = c.customer_type === 'WHOLESALE' ? 'Wholesale' : (c.customer_type === 'VIP' ? 'VIP' : 'Retail');
            const typeBadgeClass = c.customer_type === 'WHOLESALE' ? 'bg-info text-dark' : (c.customer_type === 'VIP' ? 'bg-warning text-dark' : 'bg-light text-muted border');

            return `
                <div class="dropdown-item p-2 rounded-2 pointer d-flex justify-content-between align-items-center border-bottom border-light customer-search-item" 
                     data-customer-index="${idx}" role="button" tabindex="0">
                    <div class="d-flex flex-column">
                        <div class="fw-bold text-dark fs-xs d-flex align-items-center gap-1">
                            <span>${escapeHtml(c.name)}</span>
                            <span class="badge ${typeBadgeClass} fs-2xs px-1">${typeLabel}</span>
                        </div>
                        <div class="text-muted fs-2xs font-monospace mt-1">
                            <i class="fas fa-phone fs-2xs me-1 text-success"></i>${escapeHtml(c.phone)}
                            ${c.pan ? ` &bull; PAN: ${escapeHtml(c.pan)}` : ''}
                        </div>
                    </div>
                    <div class="text-end ps-2 flex-shrink-0">
                        ${hasDebt ? `
                            <span class="badge bg-danger bg-opacity-10 text-danger fw-bold fs-2xs px-2 py-1">
                                Udhaari: Rs. ${creditBal.toFixed(2)}
                            </span>
                        ` : `
                            <span class="badge bg-success bg-opacity-10 text-success fs-2xs">Clear</span>
                        `}
                    </div>
                </div>
            `;
        }).join('');

        this.showDropdown();
    }

    navigateDropdown(direction) {
        const items = this.dropdownEl.querySelectorAll('.customer-search-item');
        if (!items.length) return;

        if (this.highlightedIndex >= 0 && items[this.highlightedIndex]) {
            items[this.highlightedIndex].classList.remove('active', 'bg-light');
        }

        this.highlightedIndex += direction;
        if (this.highlightedIndex < 0) this.highlightedIndex = items.length - 1;
        if (this.highlightedIndex >= items.length) this.highlightedIndex = 0;

        const target = items[this.highlightedIndex];
        if (target) {
            target.classList.add('active', 'bg-light');
            target.scrollIntoView({ block: 'nearest' });
        }
    }

    showDropdown() {
        if (this.dropdownEl) this.dropdownEl.style.display = 'block';
    }

    hideDropdown() {
        if (this.dropdownEl) this.dropdownEl.style.display = 'none';
        this.highlightedIndex = -1;
    }

    selectCustomer(customer) {
        this.selectedCustomer = customer;
        this.hideDropdown();

        this.engine.cart.setCustomer(customer);

        if (this.customerInput) {
            if (this.customerInput.tagName === 'SELECT') {
                this.customerInput.value = customer.id;
            } else {
                this.customerInput.value = customer.name;
            }
        }

        if (this.customerPanInput && customer.pan) {
            this.customerPanInput.value = customer.pan;
            this.engine.cart.customerPan = customer.pan;
        }

        const parentGroup = this.customerInput ? this.customerInput.closest('.input-group') : null;
        if (parentGroup && this.selectedBadgeEl) {
            parentGroup.classList.add('d-none');
            this.selectedBadgeEl.classList.remove('d-none');
            this.selectedBadgeEl.classList.add('d-flex');

            const nameEl = document.getElementById('selectedCustomerChipName');
            if (nameEl) {
                nameEl.innerText = `${customer.name} (${customer.phone})`;
            }

            const udhaariBadge = document.getElementById('selectedCustomerUdhaariBadge');
            const debt = parseFloat(customer.credit_balance || 0);
            if (udhaariBadge) {
                if (debt > 0) {
                    udhaariBadge.innerText = `Udhaari: Rs. ${debt.toFixed(2)}`;
                    udhaariBadge.classList.remove('d-none');
                } else {
                    udhaariBadge.classList.add('d-none');
                }
            }
        }

        this.engine.showNotification(`Customer "${customer.name}" linked to POS bill.`, 'success');
    }

    clearCustomer(clearInput = true) {
        this.selectedCustomer = null;
        this.engine.cart.clearCustomer();

        if (clearInput && this.customerInput) {
            this.customerInput.value = '';
        }

        if (clearInput && this.customerPanInput) {
            this.customerPanInput.value = '';
        }

        const parentGroup = this.customerInput ? this.customerInput.closest('.input-group') : null;
        if (parentGroup && this.selectedBadgeEl) {
            parentGroup.classList.remove('d-none');
            this.selectedBadgeEl.classList.remove('d-flex');
            this.selectedBadgeEl.classList.add('d-none');
        }

        if (clearInput && this.customerInput && this.customerInput.tagName !== 'SELECT') {
            this.customerInput.focus();
        }
    }
}

// ============================================================================
// 3. CART & PRICING ENGINE MODULE (EXPLICIT NONE, %, AND AMOUNT (Rs.) MODES)
// ============================================================================
class POSCart {
    constructor(engine) {
        this.engine = engine;
        this.items = [];
        this.customer = null;
        this.customerPhone = '';
        this.customerPan = '';
        this.customerType = 'RETAIL';

        // Dual-Mode Bill Discount State
        this.billDiscountType = 'PERCENTAGE'; // 'PERCENTAGE' or 'AMOUNT'
        this.billDiscountValue = 0;
        this.billDiscountPercent = 0;
        this.billDiscountReason = '';
        this.activeCartIdempotencyKey = generateUUID();

        this.cartTableBody = document.getElementById('posCartTableBody') || document.getElementById('cartItemsContainer');
        this.subtotalText = document.getElementById('posSubtotalText') || document.getElementById('billSubtotalText');
        this.vatText = document.getElementById('posVatText');
        this.vatRow = document.getElementById('posVatRow');
        this.grandTotalText = document.getElementById('posGrandTotalText') || document.getElementById('billGrandTotalText');

        this.initBillDiscountControls();
        this.bindEvents();
    }

    initBillDiscountControls() {
        const discInput = document.getElementById('posBillDiscountInput') || document.getElementById('billDiscountInput');
        if (!discInput) return;

        const parent = discInput.parentElement;
        if (parent && !document.getElementById('posBillDiscountTypeToggleBtn')) {
            const labelSpan = parent.querySelector('span');
            if (labelSpan) {
                labelSpan.innerHTML = `<span>Bill Discount:</span>`;
            }

            // Input group with compact toggle button [% | Rs.]
            const group = document.createElement('div');
            group.className = 'input-group input-group-sm';
            group.style.width = '135px';

            const toggleBtn = document.createElement('button');
            toggleBtn.type = 'button';
            toggleBtn.id = 'posBillDiscountTypeToggleBtn';
            toggleBtn.className = 'btn btn-outline-secondary px-2 py-0 fw-bold fs-2xs font-mono';
            toggleBtn.innerText = this.billDiscountType === 'AMOUNT' ? 'Rs.' : '%';
            toggleBtn.title = 'Toggle between Percentage (%) and Cash Amount (Rs.)';

            discInput.parentNode.insertBefore(group, discInput);
            group.appendChild(toggleBtn);
            group.appendChild(discInput);

            discInput.style.width = '';
            discInput.classList.add('flex-grow-1', 'text-end', 'font-mono');
            discInput.placeholder = this.billDiscountType === 'AMOUNT' ? 'Rs.' : '%';

            toggleBtn.addEventListener('click', () => {
                this.toggleBillDiscountType();
            });
        }
    }

    toggleBillDiscountType() {
        this.billDiscountType = this.billDiscountType === 'AMOUNT' ? 'PERCENTAGE' : 'AMOUNT';
        const toggleBtn = document.getElementById('posBillDiscountTypeToggleBtn');
        if (toggleBtn) {
            toggleBtn.innerText = this.billDiscountType === 'AMOUNT' ? 'Rs.' : '%';
            toggleBtn.classList.toggle('btn-primary', this.billDiscountType === 'AMOUNT');
            toggleBtn.classList.toggle('btn-outline-secondary', this.billDiscountType !== 'AMOUNT');
        }

        const discInput = document.getElementById('posBillDiscountInput') || document.getElementById('billDiscountInput');
        if (discInput) {
            if (this.billDiscountType === 'PERCENTAGE') {
                discInput.max = '100';
                discInput.step = '0.5';
                discInput.placeholder = '%';
                if (parseFloat(discInput.value) > 100) discInput.value = '100';
            } else {
                discInput.removeAttribute('max');
                discInput.step = '1';
                discInput.placeholder = 'Rs.';
            }
            this.billDiscountValue = Math.max(0, parseFloat(discInput.value) || 0);
        }

        this.render();
    }

    bindEvents() {
        if (this.cartTableBody) {
            // Change event handler for quantities, prices, and discounts
            this.cartTableBody.addEventListener('change', (e) => {
                const qtyInput = e.target.closest('.cart-qty-input');
                if (qtyInput) {
                    const idx = parseInt(qtyInput.dataset.cartIndex, 10);
                    this.updateQuantity(idx, qtyInput.value);
                    return;
                }

                const priceInput = e.target.closest('.cart-price-input');
                if (priceInput) {
                    const idx = parseInt(priceInput.dataset.cartIndex, 10);
                    this.updatePrice(idx, priceInput.value);
                    return;
                }

                const discInput = e.target.closest('.cart-disc-input');
                if (discInput) {
                    const idx = parseInt(discInput.dataset.cartIndex, 10);
                    this.updateItemDiscount(idx, discInput.value, true);
                    return;
                }
            });

            // Real-time live input listener for discount amounts and rates
            this.cartTableBody.addEventListener('input', (e) => {
                const discInput = e.target.closest('.cart-disc-input');
                if (discInput) {
                    const idx = parseInt(discInput.dataset.cartIndex, 10);
                    this.updateItemDiscount(idx, discInput.value, false);
                    return;
                }
            });

            // Click event handler for remove and discrete 3-way discount mode toggles
            this.cartTableBody.addEventListener('click', (e) => {
                const removeBtn = e.target.closest('.cart-remove-btn');
                if (removeBtn) {
                    const idx = parseInt(removeBtn.dataset.cartIndex, 10);
                    this.removeItem(idx);
                    return;
                }

                const modeBtn = e.target.closest('.cart-disc-mode-btn');
                if (modeBtn) {
                    const idx = parseInt(modeBtn.dataset.cartIndex, 10);
                    const mode = modeBtn.dataset.mode; // 'NONE', 'PERCENTAGE', 'AMOUNT'
                    this.setItemDiscountMode(idx, mode);
                    return;
                }
            });
        }
    }

    renewIdempotencyKey() {
        this.activeCartIdempotencyKey = generateUUID();
    }

    setCustomer(customer) {
        this.customer = customer;
        this.customerPhone = customer.phone || '';
        this.customerPan = customer.pan || this.customerPan || '';
        this.customerType = customer.customer_type || 'RETAIL';
        this.render();
    }

    clearCustomer() {
        this.customer = null;
        this.customerPhone = '';
        this.customerPan = '';
        this.customerType = 'RETAIL';
        this.render();
    }

    addItem(product) {
        if (!product) return;

        const isImei = Boolean(product.requires_imei || product.match_type === 'IMEI' || this.engine.isPhoneProduct(product));
        const incomingImei1 = (product.imei_1 || product.imei1 || product.imei_number || '').trim();
        const incomingImei2 = (product.imei_2 || product.imei2 || product.secondary_imei || '').trim();

        // 1. Dual-IMEI Collision Check across all items
        if (isImei && (incomingImei1 || incomingImei2)) {
            const duplicateItem = this.items.find(existingItem => {
                const existingImei1 = (existingItem.imei_number || existingItem.imei_1 || existingItem.imei1 || '').trim();
                const existingImei2 = (existingItem.secondary_imei || existingItem.imei_2 || existingItem.imei2 || '').trim();

                const imei1Collision = incomingImei1 && (
                    (existingImei1 && existingImei1 === incomingImei1) ||
                    (existingImei2 && existingImei2 === incomingImei1)
                );

                const imei2Collision = incomingImei2 && (
                    (existingImei1 && existingImei1 === incomingImei2) ||
                    (existingImei2 && existingImei2 === incomingImei2)
                );

                return imei1Collision || imei2Collision;
            });

            if (duplicateItem) {
                const matchedImei = incomingImei1 || incomingImei2;
                this.engine.showNotification(`Handset with IMEI "${matchedImei}" is already added to the active cart.`, 'warning');
                POSAudioSynthesizer.play('error');
                return;
            }
        }

        // 2. Price Resolution
        let rawPrice = 0;
        if (product.unit_price !== undefined && product.unit_price !== null && !isNaN(parseFloat(product.unit_price))) {
            rawPrice = parseFloat(product.unit_price);
        } else if (product.price !== undefined && product.price !== null && !isNaN(parseFloat(product.price))) {
            rawPrice = parseFloat(product.price);
        } else if (product.selling_price !== undefined && product.selling_price !== null && !isNaN(parseFloat(product.selling_price))) {
            rawPrice = parseFloat(product.selling_price);
        }

        const extractedPrice = Math.max(0, rawPrice);
        const extractedCatalogPrice = (product.official_unit_price !== undefined && product.official_unit_price !== null && !isNaN(parseFloat(product.official_unit_price)))
            ? parseFloat(product.official_unit_price)
            : ((product.catalog_price !== undefined && product.catalog_price !== null && !isNaN(parseFloat(product.catalog_price)))
                ? parseFloat(product.catalog_price)
                : extractedPrice);

        const extractedCostPrice = (product.cost_price !== undefined && product.cost_price !== null && !isNaN(parseFloat(product.cost_price)))
            ? parseFloat(product.cost_price)
            : 0;

        const extractedQuantity = (product.quantity !== undefined && product.quantity !== null && parseFloat(product.quantity) > 0)
            ? parseFloat(product.quantity)
            : 1;

        // 3. Discount Initialization (Explicitly NONE, PERCENTAGE, or AMOUNT)
        let discountType = 'NONE';
        const rawDiscType = String(product.discount_type || '').toUpperCase().trim();
        if (rawDiscType === 'AMOUNT' || rawDiscType === 'FIXED') {
            discountType = 'AMOUNT';
        } else if (rawDiscType === 'PERCENTAGE') {
            discountType = 'PERCENTAGE';
        }

        let discountValue = 0;
        if (product.discount_value !== undefined && product.discount_value !== null && !isNaN(parseFloat(product.discount_value))) {
            discountValue = Math.max(0, parseFloat(product.discount_value));
        } else if (product.discount_percent !== undefined && product.discount_percent !== null && !isNaN(parseFloat(product.discount_percent))) {
            discountValue = Math.max(0, parseFloat(product.discount_percent));
        }

        if (discountValue > 0 && discountType === 'NONE') {
            discountType = 'PERCENTAGE';
        }

        const isDiscountable = product.is_discountable !== undefined ? Boolean(product.is_discountable) : true;
        const maxDiscountPercent = product.max_discount_percent !== undefined ? parseFloat(product.max_discount_percent) : 10;
        const conversionId = product.package_conversion_id || product.conversion_id || product.unit_conversion_id || null;

        // Non-discountable items are strictly initialized to NONE with 0 discount
        if (!isDiscountable) {
            discountType = 'NONE';
            discountValue = 0;
        }

        // 4. Group non-serialized accessories if already present
        const nonImeiMatch = !isImei
            ? this.items.find(i => (i.product_id === (product.product_id || product.id)) && ((i.conversion_id || null) === conversionId))
            : null;

        if (nonImeiMatch) {
            nonImeiMatch.quantity += extractedQuantity;
        } else {
            const taxMode = this.engine.isVatMode ? (product.tax_pricing_type || 'INCLUSIVE') : 'EXEMPT';
            const vatRate = this.engine.isVatMode ? (parseFloat(product.vat_rate) || this.engine.defaultVatRate || 0) : 0;

            let warrantyTerms = product.warranty_terms || `${product.warranty_months || 12}M Warranty`;
            if (product.component_warranty_rules && product.component_warranty_rules.length > 0) {
                warrantyTerms = product.component_warranty_rules.map(r => `${r.component_name}: ${r.warranty_months}M`).join(' | ');
            }

            this.items.push({
                product_id: product.product_id || product.id,
                category_id: product.category_id || null,
                category_name: product.category_name || '',
                item_instance_id: product.item_instance_id || null,
                name: product.name,
                unit_price: extractedPrice,
                price: extractedPrice,
                official_unit_price: extractedCatalogPrice,
                catalog_price: extractedCatalogPrice,
                cost_price: extractedCostPrice,
                quantity: extractedQuantity,
                unit_code: product.unit_code || 'Pcs',
                discount_type: discountType,
                discount_value: discountValue,
                discount_percent: discountType === 'PERCENTAGE' ? discountValue : 0,
                is_discountable: isDiscountable,
                max_discount_percent: maxDiscountPercent,
                tax_pricing_type: taxMode,
                vat_rate: vatRate,
                requires_imei: isImei,
                imei_1: incomingImei1,
                imei1: incomingImei1,
                imei_number: incomingImei1,
                imei_2: incomingImei2,
                imei2: incomingImei2,
                secondary_imei: incomingImei2,
                mdms_status: product.mdms_status || product.default_mdms_status || 'REGISTERED_OFFICIAL',
                warranty_months: product.warranty_months || 12,
                warranty_terms: warrantyTerms,
                conversion_id: conversionId
            });
        }

        this.render();
    }

    removeItem(index) {
        if (index >= 0 && index < this.items.length) {
            this.items.splice(index, 1);
            this.render();
        }
    }

    updateQuantity(index, quantity) {
        const val = parseFloat(quantity);
        if (this.items[index]) {
            this.items[index].quantity = val > 0 ? val : 1;
            if (this.items[index].discount_type === 'AMOUNT') {
                const lineGross = this.items[index].quantity * (this.items[index].unit_price || 0);
                if (this.items[index].discount_value > lineGross) {
                    this.items[index].discount_value = lineGross;
                }
            }
            this.render();
        }
    }

    updatePrice(index, price) {
        const val = parseFloat(price);
        if (this.items[index] && !isNaN(val) && val >= 0) {
            this.items[index].unit_price = val;
            this.items[index].price = val;
            if (this.items[index].discount_type === 'AMOUNT') {
                const lineGross = this.items[index].quantity * val;
                if (this.items[index].discount_value > lineGross) {
                    this.items[index].discount_value = lineGross;
                }
            }
            this.render();
        }
    }

    setItemDiscountMode(index, mode) {
        if (!this.items[index]) return;
        if (this.items[index].is_discountable === false) {
            this.engine.showNotification('This item is designated as non-discountable.', 'warning');
            return;
        }

        const validModes = ['NONE', 'PERCENTAGE', 'AMOUNT'];
        const normalizedMode = validModes.includes(mode) ? mode : 'NONE';
        const price = (this.items[index].unit_price !== undefined) ? this.items[index].unit_price : (this.items[index].price || 0);
        const lineGross = this.items[index].quantity * price;

        this.items[index].discount_type = normalizedMode;

        if (normalizedMode === 'NONE') {
            this.items[index].discount_value = 0;
            this.items[index].discount_percent = 0;
        } else if (normalizedMode === 'PERCENTAGE') {
            const currentVal = parseFloat(this.items[index].discount_value) || 0;
            this.items[index].discount_value = Math.min(100, Math.max(0, currentVal));
            this.items[index].discount_percent = this.items[index].discount_value;
        } else if (normalizedMode === 'AMOUNT') {
            const currentVal = parseFloat(this.items[index].discount_value) || 0;
            this.items[index].discount_value = Math.min(lineGross, Math.max(0, currentVal));
            this.items[index].discount_percent = lineGross > 0 ? ((this.items[index].discount_value / lineGross) * 100) : 0;
        }

        this.render();
    }

    updateItemDiscount(index, discountVal, shouldFullRender = true) {
        let num = parseFloat(discountVal);
        if (isNaN(num) || num < 0) num = 0;

        if (this.items[index]) {
            if (this.items[index].is_discountable === false) {
                this.items[index].discount_type = 'NONE';
                this.items[index].discount_value = 0;
                this.items[index].discount_percent = 0;
                if (shouldFullRender) this.render();
                return;
            }

            const type = this.items[index].discount_type || 'NONE';
            const price = (this.items[index].unit_price !== undefined) ? this.items[index].unit_price : (this.items[index].price || 0);
            const lineGross = this.items[index].quantity * price;

            if (type === 'NONE') {
                this.items[index].discount_value = 0;
                this.items[index].discount_percent = 0;
            } else if (type === 'PERCENTAGE') {
                num = Math.min(100, Math.max(0, num));
                this.items[index].discount_value = num;
                this.items[index].discount_percent = num;
            } else if (type === 'AMOUNT') {
                num = Math.min(lineGross, Math.max(0, num));
                this.items[index].discount_value = num;
                this.items[index].discount_percent = lineGross > 0 ? ((num / lineGross) * 100) : 0;
            }

            if (shouldFullRender) {
                this.render();
            } else {
                this.updateLiveRowCalculations(index);
                this.refreshSummaryDisplays();
            }
        }
    }

    updateLiveRowCalculations(index) {
        const item = this.items[index];
        if (!item) return;

        const price = (item.unit_price !== undefined) ? item.unit_price : (item.price || 0);
        const lineGross = item.quantity * price;
        const type = item.discount_type || 'NONE';
        const val = parseFloat(item.discount_value) || 0;

        let lineDisc = 0;
        let effectivePct = 0;

        if (item.is_discountable !== false && type !== 'NONE') {
            if (type === 'AMOUNT') {
                lineDisc = Math.min(val, lineGross);
                effectivePct = lineGross > 0 ? ((lineDisc / lineGross) * 100) : 0;
            } else {
                effectivePct = Math.min(100, Math.max(0, val));
                lineDisc = lineGross * (effectivePct / 100);
            }
        }

        const lineTotal = Math.max(0, lineGross - lineDisc);
        const allowedThreshold = item.max_discount_percent !== undefined ? parseFloat(item.max_discount_percent) : 10;
        const supervisorLimit = this.engine.supervisorThreshold || 10;
        const effectiveLimit = Math.min(allowedThreshold, supervisorLimit);
        const exceedsLimit = effectivePct > effectiveLimit;

        const totalEl = document.getElementById(`cartLineTotal_${index}`);
        if (totalEl) totalEl.innerText = `Rs. ${lineTotal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

        const detailEl = document.getElementById(`cartDiscDetail_${index}`);
        if (detailEl) {
            if (lineDisc > 0) {
                const perUnitDisc = item.quantity > 0 ? (lineDisc / item.quantity) : 0;
                let text = '';
                if (item.quantity > 1) {
                    text = `<span class="text-danger font-mono fs-2xs">-Rs. ${lineDisc.toFixed(2)} line discount (Rs. ${perUnitDisc.toFixed(2)}/unit) &bull; Effective: <strong>${effectivePct.toFixed(2)}%</strong></span>`;
                } else {
                    text = `<span class="text-danger font-mono fs-2xs">-Rs. ${lineDisc.toFixed(2)} &bull; Effective: <strong>${effectivePct.toFixed(2)}%</strong></span>`;
                }

                if (exceedsLimit) {
                    text += ` <span class="badge bg-warning text-dark border border-warning fs-2xs px-1 py-0 ms-1" title="Exceeds allowed ${effectiveLimit}% limit - Manager PIN required at checkout"><i class="fas fa-key me-1"></i>PIN Required (&gt;${effectiveLimit}%)</span>`;
                }
                detailEl.innerHTML = text;
            } else {
                detailEl.innerHTML = '';
            }
        }
    }

    clear() {
        this.items = [];
        this.billDiscountType = 'PERCENTAGE';
        this.billDiscountValue = 0;
        this.billDiscountPercent = 0;
        this.billDiscountReason = '';

        const discInput = document.getElementById('posBillDiscountInput') || document.getElementById('billDiscountInput');
        if (discInput) discInput.value = '0';
        const toggleBtn = document.getElementById('posBillDiscountTypeToggleBtn');
        if (toggleBtn) toggleBtn.innerText = '%';

        this.renewIdempotencyKey();
        this.render();
    }

    calculateTotals() {
        let subtotal = 0;
        let itemDiscountTotal = 0;

        for (const item of this.items) {
            const price = (item.unit_price !== undefined) ? item.unit_price : (item.price || 0);
            const lineGross = item.quantity * price;

            let lineDisc = 0;
            if (item.is_discountable !== false) {
                const discType = item.discount_type || 'NONE';
                const discVal = Math.max(0, parseFloat(item.discount_value !== undefined ? item.discount_value : (item.discount_percent || 0)) || 0);

                if (discType === 'AMOUNT' || discType === 'FIXED') {
                    lineDisc = Math.min(discVal, lineGross);
                } else if (discType === 'PERCENTAGE') {
                    const pct = Math.min(100, discVal);
                    lineDisc = lineGross * (pct / 100);
                }
            }

            subtotal += lineGross;
            itemDiscountTotal += lineDisc;
        }

        const netAfterItemDisc = Math.max(0, subtotal - itemDiscountTotal);

        let billDiscountAmt = 0;
        const billDiscType = this.billDiscountType || 'PERCENTAGE';
        const billDiscVal = Math.max(0, parseFloat(this.billDiscountValue) || 0);

        if (billDiscType === 'AMOUNT' || billDiscType === 'FIXED') {
            billDiscountAmt = Math.min(billDiscVal, netAfterItemDisc);
        } else {
            const billPct = Math.min(100, billDiscVal);
            billDiscountAmt = netAfterItemDisc * (billPct / 100);
        }

        this.billDiscountPercent = netAfterItemDisc > 0 ? ((billDiscountAmt / netAfterItemDisc) * 100) : 0;

        let taxableTotal = 0;
        let nonTaxableTotal = 0;
        let totalVat = 0;
        let exclusiveVatToAdd = 0;

        for (const item of this.items) {
            const price = (item.unit_price !== undefined) ? item.unit_price : (item.price || 0);
            const lineGross = item.quantity * price;

            let lineDisc = 0;
            if (item.is_discountable !== false) {
                const discType = item.discount_type || 'NONE';
                const discVal = Math.max(0, parseFloat(item.discount_value !== undefined ? item.discount_value : (item.discount_percent || 0)) || 0);
                if (discType === 'AMOUNT' || discType === 'FIXED') {
                    lineDisc = Math.min(discVal, lineGross);
                } else if (discType === 'PERCENTAGE') {
                    lineDisc = lineGross * (Math.min(100, discVal) / 100);
                }
            }

            const lineNet = Math.max(0, lineGross - lineDisc);

            let lineBillDisc = 0;
            if (netAfterItemDisc > 0 && billDiscountAmt > 0) {
                lineBillDisc = billDiscountAmt * (lineNet / netAfterItemDisc);
            }

            const netLinePayable = Math.max(0, lineNet - lineBillDisc);
            const taxMode = item.tax_pricing_type || 'EXEMPT';
            const rate = this.engine.isVatMode ? (parseFloat(item.vat_rate) || 0) : 0;

            if (this.engine.isVatMode && rate > 0) {
                if (taxMode === 'EXCLUSIVE') {
                    const baseTaxable = netLinePayable;
                    const vatAmt = baseTaxable * (rate / 100);
                    taxableTotal += baseTaxable;
                    totalVat += vatAmt;
                    exclusiveVatToAdd += vatAmt;
                } else if (taxMode === 'INCLUSIVE') {
                    const divisor = 1 + (rate / 100);
                    const baseTaxable = netLinePayable / divisor;
                    const vatAmt = netLinePayable - baseTaxable;
                    taxableTotal += baseTaxable;
                    totalVat += vatAmt;
                } else {
                    nonTaxableTotal += netLinePayable;
                }
            } else {
                nonTaxableTotal += netLinePayable;
            }
        }

        const tradeInCredit = this.engine.tradeInManager.getCreditAmount();
        const grossPayable = (subtotal - itemDiscountTotal - billDiscountAmt) + exclusiveVatToAdd;
        const netAfterTradeIn = Math.max(0, grossPayable - tradeInCredit);
        const grandTotal = Math.round(netAfterTradeIn);

        return {
            subtotal,
            itemDiscountTotal,
            billDiscountAmt,
            totalDiscount: itemDiscountTotal + billDiscountAmt,
            tradeInCredit,
            taxableTotal,
            nonTaxableTotal,
            totalVat,
            exclusiveVatToAdd,
            grandTotal
        };
    }

    refreshSummaryDisplays() {
        const totals = this.calculateTotals();
        if (this.subtotalText) this.subtotalText.innerText = `Rs. ${totals.subtotal.toFixed(2)}`;
        if (this.grandTotalText) this.grandTotalText.innerText = `Rs. ${totals.grandTotal.toFixed(2)}`;

        if (this.engine.isVatMode && totals.totalVat > 0) {
            if (this.vatRow) this.vatRow.style.display = 'flex';
            if (this.vatText) this.vatText.innerText = `Rs. ${totals.totalVat.toFixed(2)}`;
        } else if (this.vatRow) {
            this.vatRow.style.display = 'none';
        }

        this.engine.tradeInManager.updateUi(totals.tradeInCredit);
    }

    render() {
        if (!this.cartTableBody) return;
        const isSimpleDivContainer = this.cartTableBody.tagName === 'DIV';

        if (this.items.length === 0) {
            if (isSimpleDivContainer) {
                this.cartTableBody.innerHTML = `
                    <div class="text-center py-5 text-muted">
                        <i class="fas fa-shopping-cart fs-1 opacity-25 d-block mb-2"></i>
                        <span class="fs-xs">Cart is empty. Scan an IMEI or select a product to begin.</span>
                    </div>
                `;
            } else {
                this.cartTableBody.innerHTML = `
                    <tr>
                        <td colspan="5" class="text-center py-5 text-muted">
                            <i class="fas fa-shopping-cart fs-1 opacity-25 d-block mb-2"></i>
                            Cart is empty. Scan barcodes or select products to begin.
                        </td>
                    </tr>
                `;
            }
        } else {
            const supervisorLimit = this.engine.supervisorThreshold || 10;

            if (isSimpleDivContainer) {
                this.cartTableBody.innerHTML = this.items.map((item, idx) => {
                    const price = (item.unit_price !== undefined) ? item.unit_price : (item.price || 0);
                    const lineGross = item.quantity * price;
                    const type = item.discount_type || 'NONE';
                    const val = parseFloat(item.discount_value) || 0;

                    let lineDisc = 0;
                    let effectivePct = 0;

                    if (item.is_discountable !== false && type !== 'NONE') {
                        if (type === 'AMOUNT') {
                            lineDisc = Math.min(val, lineGross);
                            effectivePct = lineGross > 0 ? ((lineDisc / lineGross) * 100) : 0;
                        } else {
                            effectivePct = Math.min(100, Math.max(0, val));
                            lineDisc = lineGross * (effectivePct / 100);
                        }
                    }

                    const lineTotal = Math.max(0, lineGross - lineDisc);
                    const primaryImei = item.imei_number || item.imei_1 || item.imei1 || '';
                    const secondaryImei = item.secondary_imei || item.imei_2 || item.imei2 || '';
                    const isDiscountDisabled = item.is_discountable === false;

                    const allowedLimit = Math.min(
                        item.max_discount_percent !== undefined ? parseFloat(item.max_discount_percent) : 10,
                        supervisorLimit
                    );
                    const exceedsLimit = effectivePct > allowedLimit;
                    const perUnitDisc = item.quantity > 0 ? (lineDisc / item.quantity) : 0;

                    return `
                        <div class="cart-row p-2 border-bottom">
                            <div class="d-flex align-items-center justify-content-between mb-1">
                                <div style="width: 50%;">
                                    <strong class="text-dark fs-xs d-block text-truncate" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong>
                                    ${primaryImei ? `
                                        <div class="d-flex flex-wrap gap-1 mt-1">
                                            <span class="badge bg-primary bg-opacity-10 text-primary font-mono" style="font-size: 8.5px;">
                                                SIM 1: ${escapeHtml(primaryImei)}
                                            </span>
                                            ${secondaryImei ? `
                                                <span class="badge bg-info bg-opacity-10 text-info font-mono" style="font-size: 8.5px;">
                                                    SIM 2: ${escapeHtml(secondaryImei)}
                                                </span>
                                            ` : ''}
                                        </div>
                                    ` : ''}
                                </div>
                                <div style="width: 15%;" class="text-center font-mono fs-xs">
                                    <input type="number" class="form-control form-control-sm text-center p-0 font-mono cart-qty-input fs-xs" 
                                           data-cart-index="${idx}" value="${item.quantity}" min="1" ${item.requires_imei ? 'readonly' : ''}>
                                </div>
                                <div style="width: 25%;" class="text-end font-mono fs-xs fw-bold text-dark" id="cartLineTotal_${idx}">
                                    Rs. ${lineTotal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                                </div>
                                <div style="width: 10%;" class="text-end">
                                    <button type="button" class="btn btn-link text-danger p-0 border-0 cart-remove-btn" data-cart-index="${idx}">
                                        <i class="fas fa-times-circle"></i>
                                    </button>
                                </div>
                            </div>
                            
                            <div class="d-flex align-items-center justify-content-between pt-1 mt-1 border-top border-light flex-wrap gap-1">
                                <span class="text-muted fs-2xs fw-semibold">Discount:</span>
                                <div class="d-flex align-items-center gap-1">
                                    ${isDiscountDisabled ? `
                                        <span class="badge bg-secondary bg-opacity-10 text-secondary border fs-2xs" title="Non-Discountable Item">
                                            <i class="fas fa-ban me-1"></i>No Disc
                                        </span>
                                    ` : `
                                        <div class="btn-group btn-group-sm" role="group" style="height: 22px;">
                                            <button type="button" class="btn btn-xs py-0 px-1 font-mono fs-2xs cart-disc-mode-btn ${type === 'NONE' ? 'btn-dark active' : 'btn-outline-secondary'}" data-cart-index="${idx}" data-mode="NONE" title="No Discount">None</button>
                                            <button type="button" class="btn btn-xs py-0 px-1 font-mono fs-2xs cart-disc-mode-btn ${type === 'PERCENTAGE' ? 'btn-primary active' : 'btn-outline-secondary'}" data-cart-index="${idx}" data-mode="PERCENTAGE" title="Percentage (%)">%</button>
                                            <button type="button" class="btn btn-xs py-0 px-1 font-mono fs-2xs cart-disc-mode-btn ${type === 'AMOUNT' ? 'btn-primary active' : 'btn-outline-secondary'}" data-cart-index="${idx}" data-mode="AMOUNT" title="Fixed Amount (Rs.)">Rs.</button>
                                        </div>

                                        ${type !== 'NONE' ? `
                                            <div class="input-group input-group-sm" style="width: 95px; height: 22px;">
                                                <span class="input-group-text p-0 px-1 fs-2xs font-mono bg-light text-muted">${type === 'AMOUNT' ? 'Rs.' : '%'}</span>
                                                <input type="number" 
                                                       class="form-control form-control-sm text-end p-0 px-1 cart-disc-input font-mono fs-2xs" 
                                                       data-cart-index="${idx}"
                                                       value="${val}"
                                                       min="0"
                                                       ${type === 'PERCENTAGE' ? 'max="100" step="0.5"' : `max="${lineGross}" step="1"`}>
                                            </div>
                                        ` : ''}
                                    `}
                                </div>
                            </div>

                            <div class="d-flex justify-content-end align-items-center mt-1" id="cartDiscDetail_${idx}">
                                ${lineDisc > 0 ? `
                                    <span class="text-danger font-mono fs-2xs">
                                        -Rs. ${lineDisc.toFixed(2)} ${item.quantity > 1 ? `(Rs. ${perUnitDisc.toFixed(2)}/unit)` : ''} &bull; Effective: <strong>${effectivePct.toFixed(2)}%</strong>
                                    </span>
                                    ${exceedsLimit ? `
                                        <span class="badge bg-warning text-dark border border-warning fs-2xs px-1 py-0 ms-1" title="Exceeds allowed ${effectiveLimit}% limit - Manager PIN required at checkout">
                                            <i class="fas fa-key me-1"></i>PIN Req
                                        </span>
                                    ` : ''}
                                ` : ''}
                            </div>
                        </div>
                    `;
                }).join('');
            } else {
                this.cartTableBody.innerHTML = this.items.map((item, idx) => {
                    const price = (item.unit_price !== undefined) ? item.unit_price : (item.price || 0);
                    const lineGross = item.quantity * price;
                    const type = item.discount_type || 'NONE';
                    const val = parseFloat(item.discount_value) || 0;

                    let lineDisc = 0;
                    let effectivePct = 0;

                    if (item.is_discountable !== false && type !== 'NONE') {
                        if (type === 'AMOUNT') {
                            lineDisc = Math.min(val, lineGross);
                            effectivePct = lineGross > 0 ? ((lineDisc / lineGross) * 100) : 0;
                        } else {
                            effectivePct = Math.min(100, Math.max(0, val));
                            lineDisc = lineGross * (effectivePct / 100);
                        }
                    }

                    const lineTotal = Math.max(0, lineGross - lineDisc);
                    const primaryImei = item.imei_number || item.imei_1 || item.imei1 || '';
                    const secondaryImei = item.secondary_imei || item.imei_2 || item.imei2 || '';
                    const isDiscountDisabled = item.is_discountable === false;

                    const allowedLimit = Math.min(
                        item.max_discount_percent !== undefined ? parseFloat(item.max_discount_percent) : 10,
                        supervisorLimit
                    );
                    const exceedsLimit = effectivePct > allowedLimit;
                    const perUnitDisc = item.quantity > 0 ? (lineDisc / item.quantity) : 0;

                    return `
                        <tr class="scan-flash">
                            <td>
                                <strong class="text-dark fs-xs">${escapeHtml(item.name)}</strong>
                                <div class="d-flex flex-wrap align-items-center gap-1 mt-1">
                                    ${primaryImei ? `<span class="badge bg-primary bg-opacity-10 text-primary font-monospace fs-2xs"><i class="fas fa-barcode me-1"></i>SIM 1: ${escapeHtml(primaryImei)}</span>` : ''}
                                    ${secondaryImei ? `<span class="badge bg-info bg-opacity-10 text-info font-monospace fs-2xs"><i class="fas fa-barcode me-1"></i>SIM 2: ${escapeHtml(secondaryImei)}</span>` : ''}
                                </div>
                                
                                <div class="d-flex align-items-center gap-2 mt-2 flex-wrap">
                                    <span class="text-muted fs-2xs fw-semibold">Disc:</span>
                                    ${isDiscountDisabled ? `
                                        <span class="badge bg-secondary bg-opacity-10 text-secondary border fs-2xs" title="Non-Discountable Item">
                                            <i class="fas fa-ban me-1"></i>No Disc
                                        </span>
                                    ` : `
                                        <div class="btn-group btn-group-sm" role="group" style="height: 22px;">
                                            <button type="button" class="btn btn-xs py-0 px-2 font-mono fs-2xs cart-disc-mode-btn ${type === 'NONE' ? 'btn-dark active' : 'btn-outline-secondary'}" data-cart-index="${idx}" data-mode="NONE" title="No Discount">None</button>
                                            <button type="button" class="btn btn-xs py-0 px-2 font-mono fs-2xs cart-disc-mode-btn ${type === 'PERCENTAGE' ? 'btn-primary active' : 'btn-outline-secondary'}" data-cart-index="${idx}" data-mode="PERCENTAGE" title="Percentage (%)">%</button>
                                            <button type="button" class="btn btn-xs py-0 px-2 font-mono fs-2xs cart-disc-mode-btn ${type === 'AMOUNT' ? 'btn-primary active' : 'btn-outline-secondary'}" data-cart-index="${idx}" data-mode="AMOUNT" title="Fixed Amount (Rs.)">Rs.</button>
                                        </div>

                                        ${type !== 'NONE' ? `
                                            <div class="input-group input-group-sm" style="width: 105px; height: 22px;">
                                                <span class="input-group-text p-0 px-1 fs-2xs font-mono bg-light text-muted">${type === 'AMOUNT' ? 'Rs.' : '%'}</span>
                                                <input type="number" 
                                                       class="form-control form-control-sm text-end p-0 px-1 cart-disc-input font-mono fs-2xs" 
                                                       data-cart-index="${idx}"
                                                       value="${val}"
                                                       min="0"
                                                       ${type === 'PERCENTAGE' ? 'max="100" step="0.5"' : `max="${lineGross}" step="1"`}>
                                            </div>
                                        ` : ''}
                                    `}
                                </div>

                                <div class="mt-1" id="cartDiscDetail_${idx}">
                                    ${lineDisc > 0 ? `
                                        <span class="text-danger font-mono fs-2xs">
                                            -Rs. ${lineDisc.toFixed(2)} ${item.quantity > 1 ? `(Rs. ${perUnitDisc.toFixed(2)}/unit)` : ''} &bull; Effective: <strong>${effectivePct.toFixed(2)}%</strong>
                                        </span>
                                        ${exceedsLimit ? `
                                            <span class="badge bg-warning text-dark border border-warning fs-2xs px-1 py-0 ms-1" title="Exceeds allowed ${effectiveLimit}% limit - Manager PIN required at checkout">
                                                <i class="fas fa-key me-1"></i>PIN Req (&gt;${effectiveLimit}%)
                                            </span>
                                        ` : ''}
                                    ` : ''}
                                </div>
                            </td>
                            <td class="text-center" style="width: 75px;">
                                <input type="number" class="form-control form-control-sm text-center p-1 cart-qty-input font-monospace"
                                       data-cart-index="${idx}"
                                       value="${item.quantity}" min="1" ${item.requires_imei ? 'readonly' : ''}>
                            </td>
                            <td class="text-end" style="width: 105px;">
                                <input type="number" step="0.01" class="form-control form-control-sm text-end p-1 cart-price-input font-monospace"
                                       data-cart-index="${idx}"
                                       value="${price.toFixed(2)}">
                            </td>
                            <td class="text-end fw-bold font-monospace fs-xs" id="cartLineTotal_${idx}">
                                Rs. ${lineTotal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                            </td>
                            <td class="text-center" style="width: 30px;">
                                <button type="button" class="btn btn-link text-danger p-0 border-0 cart-remove-btn" data-cart-index="${idx}">
                                    <i class="fas fa-trash-can"></i>
                                </button>
                            </td>
                        </tr>
                    `;
                }).join('');
            }
        }

        this.refreshSummaryDisplays();
    }
}

// ============================================================================
// 4. TRADE-IN & EXCHANGE MANAGER MODULE
// ============================================================================
class POSTradeInManager {
    constructor(engine) {
        this.engine = engine;
        this.voucherId = null;
        this.voucherNumber = '';
        this.creditAmount = 0;
        this.tradeInDetails = null;

        this.row = document.getElementById('posTradeInRow') || document.getElementById('exchangeDiscountRow');
        this.text = document.getElementById('posTradeInText') || document.getElementById('billExchangeText');
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
        this.engine.cart.render();
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
            this.voucherId = data.id;
            this.voucherNumber = data.voucher_number;
            this.creditAmount = parseFloat(data.final_trade_in_value || 0);
            this.tradeInDetails = { model: `${data.brand_name} ${data.model_name}`, value: this.creditAmount };

            this.engine.showNotification(`Trade-In voucher ${this.voucherNumber} attached (Credit: Rs. ${this.creditAmount.toFixed(2)}).`, 'success');
            this.engine.cart.render();
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
        this.engine.cart.render();
    }

    updateUi(credit) {
        if (!this.row) return;
        if (credit > 0) {
            this.row.style.display = 'flex';
            if (this.text) this.text.innerText = `-Rs. ${credit.toFixed(2)}`;
            if (this.tag) this.tag.innerText = this.voucherNumber ? `(${this.voucherNumber})` : '';
        } else {
            this.row.style.display = 'none';
        }
    }
}

// ============================================================================
// 5. PARKED BILL / HOLD CART MANAGER MODULE (F9 / F10)
// ============================================================================
class POSHoldCartManager {
    constructor(engine) {
        this.engine = engine;
        this.apiUrl = '/pos-session/api/hold-carts/';
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
            this.engine.showNotification('Cannot hold an empty cart.', 'warning');
            return;
        }

        const totals = this.engine.cart.calculateTotals();
        const customerName = this.engine.customerManager.selectedCustomer
            ? this.engine.customerManager.selectedCustomer.name
            : (document.getElementById('posCustomerInput')?.value.trim() || 'Walk-in Customer');

        const customerPhone = this.engine.cart.customerPhone || '';
        const notes = prompt('Enter a short note for this held cart (optional):') || 'Held at counter';

        const serializedItems = this.engine.cart.items.map(item => ({
            ...item,
            unit_price: (item.unit_price !== undefined) ? item.unit_price : (item.price || 0),
            price: (item.unit_price !== undefined) ? item.unit_price : (item.price || 0),
            official_unit_price: item.official_unit_price || item.catalog_price || item.unit_price,
            discount_type: item.discount_type || 'NONE',
            discount_value: item.discount_value !== undefined ? parseFloat(item.discount_value) : (item.discount_percent || 0),
            discount_percent: item.discount_percent || 0,
            is_discountable: item.is_discountable !== false
        }));

        const payload = {
            cart: serializedItems,
            subtotal: totals.subtotal,
            customer_name: customerName,
            customer_phone: customerPhone,
            bill_discount_type: this.engine.cart.billDiscountType || 'PERCENTAGE',
            bill_discount_value: this.engine.cart.billDiscountValue || 0,
            bill_discount_percent: this.engine.cart.billDiscountPercent || 0,
            discount_reason: this.engine.cart.billDiscountReason || '',
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
                this.engine.showNotification(`Cart parked successfully (${data.hold_reference}).`, 'success');
                this.engine.cart.clear();
                this.engine.customerManager.clearCustomer(true);
                this.engine.tradeInManager.remove();
            } else {
                this.engine.showNotification(data.error || 'Failed to hold cart.', 'danger');
            }
        } catch (err) {
            console.error('[POSHoldCartManager] Hold error:', err);
            this.engine.showNotification('Network error while holding cart.', 'danger');
        }
    }

    async openRecallModal() {
        if (!this.modalEl) return;
        const modal = bootstrap.Modal.getOrCreateInstance(this.modalEl);
        modal.show();
        await this.loadHeldCarts();
    }

    async loadHeldCarts() {
        if (!this.tableBody) return;
        this.tableBody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center py-4 text-muted">
                    <span class="spinner-border spinner-border-sm me-2 text-primary"></span> Loading parked carts...
                </td>
            </tr>
        `;

        try {
            const resp = await fetch(this.apiUrl);
            if (!resp.ok) throw new Error('Failed to fetch held carts');
            const data = await resp.json();
            const carts = data.held_carts || [];

            if (carts.length === 0) {
                this.tableBody.innerHTML = `
                    <tr>
                        <td colspan="6" class="text-center py-4 text-muted">
                            <i class="fas fa-pause-circle fs-3 d-block mb-2 opacity-25"></i>
                            No parked carts currently on hold.
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
                        <td class="ps-4 font-monospace fw-bold text-primary">${escapeHtml(c.hold_reference)}</td>
                        <td>
                            <strong>${escapeHtml(c.customer_name || 'Walk-in')}</strong>
                            ${c.customer_phone ? `<small class="text-muted d-block font-monospace">${escapeHtml(c.customer_phone)}</small>` : ''}
                        </td>
                        <td><span class="badge bg-light text-dark border">${itemCount} Items</span></td>
                        <td class="font-monospace fw-bold">Rs. ${parseFloat(c.subtotal || 0).toFixed(2)}</td>
                        <td class="text-muted fs-xs">${createdTime}</td>
                        <td class="text-end pe-4">
                            <button type="button" class="btn btn-primary btn-sm rounded-pill px-3 recall-held-cart-btn me-1" data-hold-ref="${escapeHtml(c.hold_reference)}">
                                <i class="fas fa-play me-1"></i> Recall
                            </button>
                            <button type="button" class="btn btn-outline-danger btn-sm rounded-circle p-1 delete-held-cart-btn" data-hold-ref="${escapeHtml(c.hold_reference)}" title="Discard Cart">
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
                        <i class="fas fa-exclamation-triangle me-1"></i> Error loading held carts.
                    </td>
                </tr>
            `;
        }
    }

    async recallCart(reference) {
        try {
            const resp = await fetch(`${this.apiUrl}${encodeURIComponent(reference)}/`);
            if (!resp.ok) {
                this.engine.showNotification('Could not retrieve parked cart.', 'danger');
                return;
            }
            const data = await resp.json();
            const items = (data.cart_payload && data.cart_payload.items) ? data.cart_payload.items : [];

            if (items.length > 0) {
                this.engine.cart.clear();

                items.forEach(item => {
                    let dType = 'NONE';
                    if (item.discount_type === 'AMOUNT' || item.discount_type === 'FIXED') {
                        dType = 'AMOUNT';
                    } else if (item.discount_type === 'PERCENTAGE') {
                        dType = 'PERCENTAGE';
                    }

                    this.engine.cart.addItem({
                        ...item,
                        discount_type: dType,
                        discount_value: item.discount_value !== undefined ? item.discount_value : (item.discount_percent || 0),
                        is_discountable: item.is_discountable !== false
                    });
                });

                let billDiscType = (data.cart_payload && data.cart_payload.bill_discount_type) ? data.cart_payload.bill_discount_type : 'PERCENTAGE';
                if (billDiscType === 'FIXED') billDiscType = 'AMOUNT';

                const billDiscVal = (data.cart_payload && data.cart_payload.bill_discount_value !== undefined) ? data.cart_payload.bill_discount_value : (data.discount_percent || 0);

                this.engine.cart.billDiscountType = billDiscType;
                this.engine.cart.billDiscountValue = parseFloat(billDiscVal) || 0;
                this.engine.cart.billDiscountReason = (data.cart_payload && data.cart_payload.discount_reason) ? data.cart_payload.discount_reason : '';

                const discInput = document.getElementById('posBillDiscountInput') || document.getElementById('billDiscountInput');
                if (discInput) discInput.value = this.engine.cart.billDiscountValue;
                const toggleBtn = document.getElementById('posBillDiscountTypeToggleBtn');
                if (toggleBtn) {
                    toggleBtn.innerText = this.engine.cart.billDiscountType === 'AMOUNT' ? 'Rs.' : '%';
                    toggleBtn.classList.toggle('btn-primary', this.engine.cart.billDiscountType === 'AMOUNT');
                    toggleBtn.classList.toggle('btn-outline-secondary', this.engine.cart.billDiscountType !== 'AMOUNT');
                }

                if (data.customer_name && data.customer_name !== 'Walk-in' && data.customer_name !== 'Walk-in Customer') {
                    const custInput = document.getElementById('posCustomerInput');
                    if (custInput) custInput.value = data.customer_name;
                    if (data.customer_phone) this.engine.cart.customerPhone = data.customer_phone;
                }

                await fetch(`${this.apiUrl}${encodeURIComponent(reference)}/`, {
                    method: 'DELETE',
                    headers: { 'X-CSRFToken': getCsrfToken() }
                });

                const modal = bootstrap.Modal.getInstance(this.modalEl);
                if (modal) modal.hide();

                this.engine.cart.render();
                this.engine.showNotification(`Restored held cart ${reference} with accurate pricing and discounts.`, 'success');
            }
        } catch (err) {
            console.error('[POSHoldCartManager] Recall error:', err);
            this.engine.showNotification('Error restoring cart.', 'danger');
        }
    }

    async deleteHeldCart(reference) {
        if (!confirm(`Permanently delete held cart ${reference}?`)) return;
        try {
            const resp = await fetch(`${this.apiUrl}${encodeURIComponent(reference)}/`, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': getCsrfToken() }
            });
            if (resp.ok) {
                this.engine.showNotification(`Deleted held cart ${reference}.`, 'info');
                await this.loadHeldCarts();
            }
        } catch (err) {
            this.engine.showNotification('Error deleting cart.', 'danger');
        }
    }
}

// ============================================================================
// 6. CHECKOUT, OVERRIDE PIN & OFFLINE ENGINE
// ============================================================================
class POSCheckout {
    constructor(engine) {
        this.engine = engine;
        this.checkoutApiUrl = '/sales/api/checkout/';
        this.isProcessing = false;
        this.managerPin = null;

        this.ensurePinModalExists();
    }

    ensurePinModalExists() {
        if (document.getElementById('posManagerPinModal')) return;

        const modalDiv = document.createElement('div');
        modalDiv.id = 'posManagerPinModal';
        modalDiv.className = 'modal fade';
        modalDiv.tabIndex = -1;
        modalDiv.setAttribute('data-bs-backdrop', 'static');
        modalDiv.innerHTML = `
            <div class="modal-dialog modal-dialog-centered modal-sm">
                <div class="modal-content border-0 shadow-lg rounded-4 overflow-hidden">
                    <div class="modal-header bg-dark text-white border-0 py-2 px-3">
                        <div class="d-flex align-items-center gap-2">
                            <span class="badge bg-warning text-dark p-2 rounded-circle"><i class="fas fa-key"></i></span>
                            <h6 class="modal-title fs-sm fw-bold text-white mb-0" id="posManagerPinModalLabel">Supervisor Override PIN</h6>
                        </div>
                        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" id="closePinModalBtn" aria-label="Close"></button>
                    </div>
                    <div class="modal-body p-4 text-center">
                        <p class="fs-xs text-muted mb-3" id="posPinModalReasonText">
                            This transaction requires Manager authorization for price override or discount.
                        </p>
                        <div class="mb-3">
                            <input type="password" id="posManagerPinInput" 
                                   class="form-control form-control-lg text-center font-monospace fw-bold fs-4 letter-spacing-lg" 
                                   placeholder="••••" maxlength="6" autocomplete="new-password">
                        </div>
                        <div id="posPinErrorAlert" class="alert alert-danger p-2 fs-2xs d-none mb-0"></div>
                    </div>
                    <div class="modal-footer bg-light border-0 py-2 d-flex justify-content-between">
                        <button type="button" class="btn btn-outline-secondary btn-sm px-3 rounded-pill" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-primary btn-sm px-4 rounded-pill fw-bold" id="submitManagerPinBtn">
                            Authorize Sale
                        </button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modalDiv);

        document.getElementById('submitManagerPinBtn').addEventListener('click', () => this.handlePinSubmission());
        document.getElementById('posManagerPinInput').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.handlePinSubmission();
            }
        });
    }

    promptForManagerPin(reason = '') {
        return new Promise((resolve) => {
            const modalEl = document.getElementById('posManagerPinModal');
            const pinInput = document.getElementById('posManagerPinInput');
            const reasonText = document.getElementById('posPinModalReasonText');
            const errAlert = document.getElementById('posPinErrorAlert');

            if (errAlert) errAlert.classList.add('d-none');
            if (pinInput) pinInput.value = '';
            if (reasonText && reason) reasonText.innerText = reason;

            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();

            setTimeout(() => pinInput && pinInput.focus(), 300);

            this._pinResolveCallback = resolve;

            const onHidden = () => {
                modalEl.removeEventListener('hidden.bs.modal', onHidden);
                if (this._pinResolveCallback) {
                    this._pinResolveCallback(null);
                    this._pinResolveCallback = null;
                }
            };
            modalEl.addEventListener('hidden.bs.modal', onHidden);
        });
    }

    handlePinSubmission() {
        const pinInput = document.getElementById('posManagerPinInput');
        const pin = pinInput ? pinInput.value.trim() : '';

        if (!pin || pin.length < 4) {
            const errAlert = document.getElementById('posPinErrorAlert');
            if (errAlert) {
                errAlert.innerText = 'Please enter a 4-6 digit numeric PIN.';
                errAlert.classList.remove('d-none');
            }
            return;
        }

        const modalEl = document.getElementById('posManagerPinModal');
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();

        if (this._pinResolveCallback) {
            this._pinResolveCallback(pin);
            this._pinResolveCallback = null;
        }
    }

    setButtonsDisabled(disabled) {
        this.isProcessing = disabled;
        const cashBtn = document.getElementById('posDirectCashCheckoutBtn') || document.getElementById('quickCashPayBtn');
        const splitConfirmBtn = document.getElementById('confirmSplitPaymentBtn');
        const digitalPayBtn = document.getElementById('digitalPayBtn');

        if (cashBtn) {
            cashBtn.disabled = disabled;
            if (disabled) cashBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i> PROCESSING...';
            else cashBtn.innerHTML = '<i class="fas fa-money-bill-wave me-2"></i> CASH CHECKOUT [F4]';
        }
        if (splitConfirmBtn) {
            splitConfirmBtn.disabled = disabled;
        }
        if (digitalPayBtn) {
            digitalPayBtn.disabled = disabled;
        }
    }

    async processDirectCash() {
        if (this.isProcessing) return;

        if (this.engine.cart.items.length === 0) {
            this.engine.showNotification('Cart is empty. Add items before checking out.', 'warning');
            return;
        }

        const totals = this.engine.cart.calculateTotals();
        const payload = this.buildPayload([{ mode: 'CASH', amount: totals.grandTotal, transaction_ref: '' }]);

        this.setButtonsDisabled(true);
        await this.executeSubmit(payload, 'CASH (नगद)');
    }

    async processSplitCheckout() {
        if (this.isProcessing) return;

        if (this.engine.cart.items.length === 0) {
            this.engine.showNotification('Cart is empty.', 'warning');
            return;
        }

        const totals = this.engine.cart.calculateTotals();
        const cash = parseFloat(document.getElementById('splitCashAmt')?.value) || 0;

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

        const credit = parseFloat(document.getElementById('splitCreditAmt')?.value) || 0;
        const creditRef = (document.getElementById('splitCreditRef')?.value || '').trim();

        const payments = [];
        if (cash > 0) payments.push({ mode: 'CASH', amount: cash, transaction_ref: '' });
        if (fonepay > 0) payments.push({ mode: 'FONEPAY', amount: fonepay, transaction_ref: fonepayRef });
        if (esewa > 0) payments.push({ mode: 'ESEWA', amount: esewa, transaction_ref: esewaRef });
        if (khalti > 0) payments.push({ mode: 'KHALTI', amount: khalti, transaction_ref: khaltiRef });
        if (card > 0) payments.push({ mode: 'CARD', amount: card, transaction_ref: cardRef });
        if (bank > 0) payments.push({ mode: 'BANK', amount: bank, transaction_ref: bankRef });
        if (credit > 0) payments.push({ mode: 'CREDIT', amount: credit, transaction_ref: creditRef });

        const totalEntered = cash + fonepay + esewa + khalti + card + bank + credit;
        if (Math.abs(totalEntered - totals.grandTotal) > 0.5) {
            this.engine.showNotification(`Entered payments total (Rs. ${totalEntered.toFixed(2)}) must equal Grand Total (Rs. ${totals.grandTotal.toFixed(2)}).`, 'danger');
            return;
        }

        const isUnpaidOrCredit = credit > 0 || (totalEntered < totals.grandTotal);
        if (isUnpaidOrCredit && !this.engine.cart.customer) {
            this.engine.showNotification('Credit sales (Udhaari) require selecting or registering a customer profile.', 'danger');
            POSAudioSynthesizer.play('error');
            const custInput = document.getElementById('posCustomerInput');
            if (custInput) custInput.focus();
            return;
        }

        const payload = this.buildPayload(payments);
        this.setButtonsDisabled(true);

        const modalEl = document.getElementById('splitPaymentModal');
        if (modalEl) {
            const modalInstance = bootstrap.Modal.getInstance(modalEl);
            if (modalInstance) modalInstance.hide();
        }

        await this.executeSubmit(payload, 'SPLIT PAYMENT');
    }

    buildPayload(payments) {
        const customerInput = document.getElementById('posCustomerInput') || document.getElementById('customerSelect');
        let custName = 'Walk-in Customer';

        if (this.engine.customerManager.selectedCustomer) {
            custName = this.engine.customerManager.selectedCustomer.name;
        } else if (customerInput) {
            if (customerInput.tagName === 'SELECT' && customerInput.selectedIndex >= 0) {
                const optText = customerInput.options[customerInput.selectedIndex].text;
                custName = optText.includes('Walk-in') ? 'Walk-in Customer' : optText;
            } else {
                custName = customerInput.value.trim() || 'Walk-in Customer';
            }
        }

        const custPan = this.engine.cart.customerPan || 
                        document.getElementById('posCustomerPanInput')?.value.trim() || 
                        (this.engine.customerManager.selectedCustomer ? this.engine.customerManager.selectedCustomer.pan : '') || '';

        const packagedCartItems = this.engine.cart.items.map(item => {
            const price = (item.unit_price !== undefined) ? item.unit_price : (item.price || 0);
            const discType = item.discount_type || 'NONE';
            const discVal = item.discount_value !== undefined ? parseFloat(item.discount_value) : 0;
            const lineGross = item.quantity * price;

            let effectivePct = 0;
            if (discType === 'PERCENTAGE') {
                effectivePct = discVal;
            } else if (discType === 'AMOUNT' && lineGross > 0) {
                effectivePct = (Math.min(discVal, lineGross) / lineGross) * 100;
            }

            return {
                product_id: item.product_id,
                item_instance_id: item.item_instance_id || null,
                unit_price: price,
                price: price,
                official_unit_price: item.official_unit_price || item.catalog_price || price,
                catalog_price: item.catalog_price || item.official_unit_price || price,
                quantity: item.quantity,
                discount_type: discType,
                discount_value: discVal,
                discount_input_value: discVal,
                discount_percent: effectivePct,
                is_discountable: item.is_discountable !== false,
                tax_pricing_type: item.tax_pricing_type || 'EXEMPT',
                vat_rate: item.vat_rate || 0,
                requires_imei: Boolean(item.requires_imei),
                imei_1: item.imei_number || item.imei_1 || item.imei1 || '',
                imei_number: item.imei_number || item.imei_1 || item.imei1 || '',
                imei_2: item.secondary_imei || item.imei_2 || item.imei2 || '',
                secondary_imei: item.secondary_imei || item.imei_2 || item.imei2 || '',
                unit_conversion_id: item.conversion_id || null,
                conversion_id: item.conversion_id || null
            };
        });

        const totals = this.engine.cart.calculateTotals();
        let billDiscType = this.engine.cart.billDiscountType || 'PERCENTAGE';
        if (billDiscType === 'FIXED') billDiscType = 'AMOUNT';

        const billDiscVal = this.engine.cart.billDiscountValue !== undefined ? parseFloat(this.engine.cart.billDiscountValue) : 0;
        const netAfterItem = Math.max(0, totals.subtotal - totals.itemDiscountTotal);

        let effectiveBillPct = 0;
        if (billDiscType === 'PERCENTAGE') {
            effectiveBillPct = billDiscVal;
        } else if (netAfterItem > 0) {
            effectiveBillPct = (Math.min(billDiscVal, netAfterItem) / netAfterItem) * 100;
        }

        const discountReason = document.getElementById('posDiscountReasonInput')?.value.trim() ||
                               this.engine.cart.billDiscountReason ||
                               '';

        const standardizedPayments = (payments || []).map(p => ({
            mode: p.mode,
            amount: parseFloat(p.amount) || 0,
            transaction_ref: (p.transaction_ref || p.reference || '').trim()
        }));

        return {
            idempotency_key: this.engine.cart.activeCartIdempotencyKey,
            cart: packagedCartItems,
            payments: standardizedPayments,
            customer_id: this.engine.cart.customer ? this.engine.cart.customer.id : null,
            customer_name: custName,
            customer_phone: this.engine.cart.customerPhone || '',
            customer_pan: custPan,
            bill_discount_type: billDiscType,
            bill_discount_value: billDiscVal,
            bill_discount_input_value: billDiscVal,
            bill_discount_percent: effectiveBillPct,
            discount_percent: effectiveBillPct,
            discount_reason: discountReason,
            trade_in_voucher_id: this.engine.tradeInManager.voucherId,
            manager_pin: this.managerPin || '',
            repair_ticket_id: this.engine.repairTicketId || null,
            notes: document.getElementById('posBillNotesInput')?.value.trim() || 
                   (this.engine.tradeInManager.tradeInDetails ? `Exchange: ${this.engine.tradeInManager.tradeInDetails.model}` : '')
        };
    }

    async executeSubmit(payload, payModeDisplay = 'CASH') {
        const csrfToken = getCsrfToken();

        if (!navigator.onLine) {
            await this.handleOfflineSave(payload, payModeDisplay);
            return;
        }

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
                const pin = await this.promptForManagerPin(data.message);
                if (pin) {
                    this.managerPin = pin;
                    payload.manager_pin = pin;
                    this.setButtonsDisabled(true);
                    return await this.executeSubmit(payload, payModeDisplay);
                } else {
                    this.engine.showNotification('Sale cancelled: Supervisor PIN authorization was not provided.', 'warning');
                    this.managerPin = null;
                    return;
                }
            }

            if (response.ok && data.status === 'success') {
                this.managerPin = null;
                const totals = this.engine.cart.calculateTotals();

                this.engine.showThermalReceipt(data.estimate_number, payload.customer_name, payModeDisplay, totals);
                this.engine.showNotification(`Bill ${data.estimate_number} finalized successfully!`, 'success');
                POSAudioSynthesizer.play('success');

                this.engine.cart.clear();
                this.engine.tradeInManager.remove();
                this.engine.customerManager.clearCustomer(true);
                this.engine.repairTicketId = null;

                const repairBanner = document.getElementById('linkedRepairBanner');
                if (repairBanner) repairBanner.remove();
            } else {
                this.engine.showNotification(`Checkout failed: ${data.message || 'Validation error'}`, 'danger');
                POSAudioSynthesizer.play('error');
            }
        } catch (err) {
            console.warn('[POSCheckout] Network error. Invoking emergency offline queue...', err);
            await this.handleOfflineSave(payload, payModeDisplay);
        } finally {
            this.setButtonsDisabled(false);
        }
    }

    async handleOfflineSave(payload, payModeDisplay) {
        try {
            if (typeof IDBStorageService !== 'undefined') {
                const tempId = `OFFLINE-${payload.idempotency_key ? payload.idempotency_key.substring(0, 8).toUpperCase() : Date.now()}`;
                const offlineOrder = {
                    ...payload,
                    client_temp_id: tempId,
                    created_at: new Date().toISOString(),
                    is_offline: true,
                    grand_total: this.engine.cart.calculateTotals().grandTotal
                };

                await IDBStorageService.set('pending_sales', offlineOrder);

                const totals = this.engine.cart.calculateTotals();
                this.engine.showThermalReceipt(tempId, payload.customer_name, `${payModeDisplay} (OFFLINE)`, totals);

                this.engine.showNotification(
                    `[OFFLINE MODE] Internet unavailable. Bill ${tempId} saved locally and queued for automatic sync!`,
                    'warning'
                );
                POSAudioSynthesizer.play('success');

                this.engine.cart.clear();
                this.engine.tradeInManager.remove();
                this.engine.customerManager.clearCustomer(true);
                this.engine.repairTicketId = null;
            } else {
                this.engine.showNotification('Network communication failure. Checkout not committed.', 'danger');
            }
        } catch (dbErr) {
            console.error('[POSCheckout] Failed to save offline order to IndexedDB:', dbErr);
            this.engine.showNotification('Critical Error: Failed to commit order to offline database.', 'danger');
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
        this.shopTaxMode = taxConfigEl ? taxConfigEl.dataset.taxMode : 'PAN';
        this.isVatMode = this.shopTaxMode === 'VAT';
        this.defaultVatRate = taxConfigEl ? parseFloat(taxConfigEl.dataset.defaultVatRate || 0) : 0;
        this.supervisorThreshold = taxConfigEl ? parseFloat(taxConfigEl.dataset.managerApprovalDiscount || 10) : 10;

        this.currentCategory = 'ALL';
        this.currentCategoryId = null;
        this.initialProducts = [];
        this.allLoadedProducts = [];
        this.pendingPhoneProduct = null;
        this.repairTicketId = null;
        this.searchDebounceTimer = null;

        this.initDomElements();

        this.customerManager = new POSCustomerManager(this);
        this.tradeInManager = new POSTradeInManager(this);
        this.cart = new POSCart(this);
        this.holdCartManager = new POSHoldCartManager(this);
        this.checkout = new POSCheckout(this);

        this.bindEvents();
        this.initHotkeys();
        this.detectLinkedRepairTicket();
        this.loadInitialCatalog();

        // Hardware scanner listener bridge
        if (typeof BarcodeScannerListener !== 'undefined') {
            this.barcodeListener = new BarcodeScannerListener(
                (code) => this.handleDirectImeiOrBarcodeScan(code),
                {
                    threshold: 60,
                    dedicatedInputSelector: '#posProductSearchInput, #searchInput',
                    enabled: true
                }
            );
        }
    }

    initDomElements() {
        this.searchInput = document.getElementById('posProductSearchInput') || document.getElementById('searchInput');
        this.clearSearchBtn = document.getElementById('clearSearchBtn');
        this.categoryPills = document.querySelectorAll('.cat-pill');
        this.productGrid = document.getElementById('productGridContainer') || document.getElementById('posSearchResultsContainer');
        this.cartContainer = document.getElementById('cartItemsContainer') || document.getElementById('posCartTableBody');

        this.subtotalText = document.getElementById('posSubtotalText') || document.getElementById('billSubtotalText');
        this.grandTotalText = document.getElementById('posGrandTotalText') || document.getElementById('billGrandTotalText');
        this.exchangeRow = document.getElementById('posTradeInRow') || document.getElementById('exchangeDiscountRow');
        this.exchangeText = document.getElementById('posTradeInText') || document.getElementById('billExchangeText');
        this.discountInput = document.getElementById('posBillDiscountInput') || document.getElementById('billDiscountInput');

        this.quickCashBtn = document.getElementById('posDirectCashCheckoutBtn') || document.getElementById('quickCashPayBtn');
        this.digitalPayBtn = document.getElementById('posSplitPaymentBtn') || document.getElementById('digitalPayBtn');
        this.clearCartBtn = document.getElementById('clearCartBtn');

        // Modal Elements
        this.imeiModalEl = document.getElementById('imeiCaptureModal');
        this.imeiModal = this.imeiModalEl ? bootstrap.Modal.getOrCreateInstance(this.imeiModalEl) : null;
        this.modalInStockSelect = document.getElementById('modalInStockSelect');
        this.modalInputImei1 = document.getElementById('modalInputImei1');
        this.modalInputImei2 = document.getElementById('modalInputImei2');
        this.confirmImeiBtn = document.getElementById('confirmImeiModalBtn');

        this.exchangeModalEl = document.getElementById('exchangeModal');
        this.applyExchangeBtn = document.getElementById('applyExchangeBtn');

        this.receiptModalEl = document.getElementById('receiptModal');
        this.receiptModal = this.receiptModalEl ? bootstrap.Modal.getOrCreateInstance(this.receiptModalEl) : null;
    }

    bindEvents() {
        // Fast Search Input & Barcode Gun Listener
        if (this.searchInput) {
            this.searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    clearTimeout(this.searchDebounceTimer);
                    this.handleDirectImeiOrBarcodeScan(this.searchInput.value.trim());
                }
            });

            this.searchInput.addEventListener('input', (e) => {
                const val = e.target.value;
                const cleanVal = val.trim();
                if (cleanVal.length >= 14 && /^\d+$/.test(cleanVal)) {
                    clearTimeout(this.searchDebounceTimer);
                    this.handleDirectImeiOrBarcodeScan(cleanVal);
                } else {
                    this.handleSearchInput(val);
                }
            });
        }

        if (this.clearSearchBtn) {
            this.clearSearchBtn.addEventListener('click', () => {
                clearTimeout(this.searchDebounceTimer);
                if (this.searchInput) this.searchInput.value = '';
                this.allLoadedProducts = [...(this.initialProducts || [])];
                this.filterCatalogBySearch('');
                if (this.searchInput) this.searchInput.focus();
            });
        }

        // Category Filter Buttons (Supports Smart Keys and Category IDs)
        this.categoryPills.forEach(pill => {
            pill.addEventListener('click', () => {
                this.categoryPills.forEach(p => p.classList.remove('active'));
                pill.classList.add('active');

                if (pill.dataset.categoryId) {
                    this.currentCategoryId = parseInt(pill.dataset.categoryId, 10);
                    this.currentCategory = pill.dataset.categoryKey || '';
                } else {
                    this.currentCategoryId = null;
                    this.currentCategory = pill.dataset.categoryKey || 'ALL';
                }

                const currentQuery = this.searchInput ? this.searchInput.value.trim() : '';
                if (currentQuery) {
                    this.handleSearchInput(currentQuery);
                } else {
                    this.filterCatalogByCategory();
                }
            });
        });

        // Modal IMEI Pairing Listeners
        if (this.modalInStockSelect) {
            this.modalInStockSelect.addEventListener('change', () => this.handleInStockSelectChange());
        }

        if (this.modalInputImei1) {
            this.modalInputImei1.addEventListener('input', (e) => this.autoMatchImei2(e.target.value));
        }

        if (this.confirmImeiBtn) {
            this.confirmImeiBtn.addEventListener('click', () => this.confirmImeiAddition());
        }

        // Trade-In Exchange Listener
        if (this.applyExchangeBtn) {
            this.applyExchangeBtn.addEventListener('click', () => this.applyExchangeDiscount());
        }

        // Direct Attach Trade-In Button (Voucher Lookup)
        const attachTradeInBtn = document.getElementById('attachTradeInPromptBtn');
        if (attachTradeInBtn) {
            attachTradeInBtn.addEventListener('click', () => {
                const voucherCode = prompt('Enter Trade-In Buy-Back Voucher Code (e.g. EXC-BR-XXXXXX):');
                if (voucherCode) {
                    this.tradeInManager.attachByVoucherNumber(voucherCode);
                }
            });
        }

        // Bill Discount Input Listener
        if (this.discountInput) {
            ['input', 'change'].forEach(evt => {
                this.discountInput.addEventListener(evt, (e) => {
                    let val = parseFloat(e.target.value) || 0;
                    if (this.cart.billDiscountType === 'PERCENTAGE') {
                        val = Math.max(0, Math.min(100, val));
                    } else {
                        val = Math.max(0, val);
                    }
                    this.cart.billDiscountValue = val;
                    this.cart.render();
                });
            });
        }

        if (this.clearCartBtn) {
            this.clearCartBtn.addEventListener('click', () => this.cart.clear());
        }

        if (this.quickCashBtn) {
            this.quickCashBtn.addEventListener('click', () => this.checkout.processDirectCash());
        }

        if (this.digitalPayBtn) {
            this.digitalPayBtn.addEventListener('click', () => {
                if (document.getElementById('splitPaymentModal')) {
                    this.openSplitPaymentModal();
                } else {
                    this.checkout.processDirectCash();
                }
            });
        }

        const splitConfirmBtn = document.getElementById('confirmSplitPaymentBtn');
        if (splitConfirmBtn) {
            splitConfirmBtn.addEventListener('click', () => this.checkout.processSplitCheckout());
        }
    }

    initHotkeys() {
        document.addEventListener('keydown', (e) => {
            if (e.key === 'F2') {
                e.preventDefault();
                if (this.searchInput) this.searchInput.focus();
            } else if (e.key === 'F4') {
                e.preventDefault();
                this.checkout.processDirectCash();
            } else if (e.key === 'F8') {
                e.preventDefault();
                this.openSplitPaymentModal();
            } else if (e.key === 'F9') {
                e.preventDefault();
                this.holdCartManager.holdCurrentCart();
            } else if (e.key === 'F10') {
                e.preventDefault();
                this.holdCartManager.openRecallModal();
            } else if (e.key === 'Escape') {
                if (this.imeiModalEl && this.imeiModalEl.classList.contains('show')) {
                    this.imeiModal.hide();
                }
            }
        });
    }

    detectLinkedRepairTicket() {
        const urlParams = new URLSearchParams(window.location.search);
        const repairId = urlParams.get('repair_ticket');
        if (repairId) {
            this.repairTicketId = parseInt(repairId, 10);
            const container = document.querySelector('.pos-workspace-container') || document.querySelector('.container-fluid');
            if (container) {
                const banner = document.createElement('div');
                banner.id = 'linkedRepairBanner';
                banner.className = 'alert alert-info py-1 px-3 mb-2 rounded-3 d-flex justify-content-between align-items-center fs-xs';
                banner.innerHTML = `
                    <span><i class="fas fa-wrench me-1"></i> Linked to Repair Ticket <strong>#${this.repairTicketId}</strong> (Delivery on checkout)</span>
                    <button type="button" class="btn-close btn-sm" onclick="document.getElementById('linkedRepairBanner').remove(); window.smartPos.repairTicketId=null;"></button>
                `;
                container.prepend(banner);
            }
        }
    }

    /**
     * Optimized Initial Catalog Boot:
     * Restricts initial payload to the top 24 frequently sold products.
     * Prevents browser freezing and excessive memory allocation on POS terminal load.
     */
    async loadInitialCatalog() {
        try {
            const customerType = this.cart.customerType || 'RETAIL';
            const resp = await fetch(`/products/api/search/?top_selling=true&limit=24&customer_type=${customerType}`);
            if (!resp.ok) throw new Error('Could not load catalog');
            const data = await resp.json();
            this.initialProducts = data.results || [];
            this.allLoadedProducts = [...this.initialProducts];
            this.renderCatalog(this.allLoadedProducts);
        } catch (err) {
            console.warn('[POS] Falling back to direct query', err);
            if (this.productGrid) {
                this.productGrid.innerHTML = `
                    <div class="col-12 text-center py-5 text-muted">
                        <i class="fas fa-barcode fs-2 mb-2 d-block opacity-25"></i>
                        Scan an IMEI or barcode to add items directly to the bill.
                    </div>
                `;
            }
        }
    }

    handleSearchInput(val) {
        clearTimeout(this.searchDebounceTimer);
        const cleanVal = (val || '').trim();

        if (!cleanVal) {
            this.allLoadedProducts = [...(this.initialProducts || [])];
            this.filterCatalogBySearch('');
            return;
        }

        // Instant snappy filter on in-memory items
        this.filterCatalogBySearch(cleanVal.toLowerCase());

        // Debounced on-demand server search for full database inventory reach
        this.searchDebounceTimer = setTimeout(() => {
            this.performOnlineCatalogSearch(cleanVal);
        }, 220);
    }

    async performOnlineCatalogSearch(query) {
        if (!query || query.length < 2) return;
        try {
            const customerType = this.cart.customerType || 'RETAIL';
            let url = `/products/api/search/?q=${encodeURIComponent(query)}&customer_type=${customerType}`;
            if (this.currentCategoryId) {
                url += `&category_id=${this.currentCategoryId}`;
            }

            const resp = await fetch(url);
            if (!resp.ok) return;
            const data = await resp.json();
            const results = data.results || [];

            if (this.searchInput && this.searchInput.value.trim().toLowerCase() === query.toLowerCase()) {
                this.allLoadedProducts = results;
                this.filterCatalogBySearch(query.toLowerCase());
            }
        } catch (err) {
            console.error('[POS] Online catalog search error:', err);
        }
    }

    async filterCatalogByCategory() {
        if (this.currentCategory === 'ALL' && !this.currentCategoryId) {
            this.allLoadedProducts = [...(this.initialProducts || [])];
            this.renderCatalog(this.allLoadedProducts);
            return;
        }

        const localMatches = this.getMatchingCatalogProducts('', this.initialProducts || []);
        if (localMatches.length > 0) {
            this.allLoadedProducts = [...(this.initialProducts || [])];
            this.renderCatalog(localMatches);
            return;
        }

        try {
            const customerType = this.cart.customerType || 'RETAIL';
            let url = `/products/api/search/?limit=24&customer_type=${customerType}`;
            if (this.currentCategoryId) {
                url += `&category_id=${this.currentCategoryId}`;
            } else if (this.currentCategory) {
                url += `&category=${encodeURIComponent(this.currentCategory)}`;
            }

            const resp = await fetch(url);
            if (!resp.ok) return;
            const data = await resp.json();
            const results = data.results || [];
            this.allLoadedProducts = results;
            this.renderCatalog(results);
        } catch (err) {
            console.error('[POS] Category on-demand fetch error:', err);
            this.renderCatalog([]);
        }
    }

    isPhoneProduct(p) {
        if (!p) return false;
        if (p.category_key === 'PHONES') return true;
        if (p.requires_imei === true || p.match_type === 'IMEI') return true;
        if ((p.ram && String(p.ram).trim() !== '') || (p.storage && String(p.storage).trim() !== '')) return true;

        const text = `${p.name || ''} ${p.model_name || ''} ${p.specs || ''} ${p.category_name || ''} ${p.brand_name || ''}`.toLowerCase();
        const phoneKeywords = [
            'iphone', 'galaxy', 'redmi', 'poco', 'realme', 'oneplus', 'pixel',
            'oppo', 'vivo', 'infinix', 'tecno', 'honor', 'motorola', 'nokia',
            'xperia', 'smartphone', 'mobile', 'phone', 'ह्यान्डसेट', 'स्मार्टफोन'
        ];
        if (phoneKeywords.some(kw => text.includes(kw))) return true;

        const phoneRegex = /\b(1[1-6]|se)\s+(128|256|512|64|32|1tb)\b|\b(1[1-6])\s+(pro|plus|max|promax|ultra)\b|\b\d{1,2}\s+\d{2,4}\s+(black|blue|pink|white|green|gold|silver|titanium|yellow|purple)\b/i;
        return phoneRegex.test(text);
    }

    async handleDirectImeiOrBarcodeScan(scannedCode) {
        if (!scannedCode) return;
        const cleanCode = scannedCode.trim();

        // 1. Check in-memory catalog
        const memoryPool = [...this.allLoadedProducts, ...(this.initialProducts || [])];
        for (const prod of memoryPool) {
            if (prod.in_stock_units && prod.in_stock_units.length > 0) {
                const matchedUnit = prod.in_stock_units.find(
                    u => (u.imei_1 && u.imei_1 === cleanCode) || 
                         (u.imei_2 && u.imei_2 === cleanCode) || 
                         (u.serial_number && u.serial_number === cleanCode)
                );
                if (matchedUnit) {
                    this.cart.addItem({
                        ...prod,
                        item_instance_id: matchedUnit.item_instance_id || null,
                        imei_1: matchedUnit.imei_1,
                        imei1: matchedUnit.imei_1,
                        imei_2: matchedUnit.imei_2 || '',
                        imei2: matchedUnit.imei_2 || '',
                        requires_imei: true
                    });
                    if (this.searchInput) {
                        this.searchInput.value = '';
                        this.filterCatalogBySearch('');
                    }
                    POSAudioSynthesizer.play('success');
                    return;
                }
            }
        }

        // 2. Query server live
        try {
            const customerType = this.cart.customerType || 'RETAIL';
            const resp = await fetch(`/products/api/search/?q=${encodeURIComponent(cleanCode)}&customer_type=${customerType}`);
            if (!resp.ok) return;
            const data = await resp.json();
            const results = data.results || [];

            if (results.length > 0) {
                const item = results[0];
                if (item.match_type === 'IMEI') {
                    this.cart.addItem(item);
                    if (this.searchInput) {
                        this.searchInput.value = '';
                        this.filterCatalogBySearch('');
                    }
                    POSAudioSynthesizer.play('success');
                } else if (!item.requires_imei && !this.isPhoneProduct(item)) {
                    this.cart.addItem(item);
                    if (this.searchInput) {
                        this.searchInput.value = '';
                        this.filterCatalogBySearch('');
                    }
                    POSAudioSynthesizer.play('success');
                } else {
                    this.handleProductCardClick(item);
                }
            } else {
                this.showNotification(`No item or IMEI record matched barcode "${cleanCode}".`, 'warning');
                POSAudioSynthesizer.play('error');
            }
        } catch (err) {
            console.error('[POS] Scan lookup error', err);
            this.showNotification('Catalog lookup communication error.', 'danger');
        }
    }

    getMatchingCatalogProducts(query = '', sourceList = null) {
        const cleanQuery = query.toLowerCase().trim();
        const listToFilter = sourceList || this.allLoadedProducts;

        return listToFilter.filter(p => {
            let matchesCat = false;

            if (this.currentCategoryId) {
                matchesCat = (p.category_id && parseInt(p.category_id, 10) === this.currentCategoryId);
            } else if (this.currentCategory === 'ALL') {
                matchesCat = true;
            } else if (this.currentCategory === 'PHONES') {
                matchesCat = this.isPhoneProduct(p);
            } else if (this.currentCategory === 'CHARGERS') {
                const text = `${p.name || ''} ${p.category_name || ''} ${p.specs || ''}`.toLowerCase();
                matchesCat = (p.category_key === 'CHARGERS') || ['charger', 'cable', 'adapter', 'power', 'chrg', 'चार्जर', 'केबल', 'type-c', 'lightning'].some(k => text.includes(k));
            } else if (this.currentCategory === 'GLASS_COVER') {
                const text = `${p.name || ''} ${p.category_name || ''} ${p.specs || ''}`.toLowerCase();
                matchesCat = (p.category_key === 'GLASS_COVER') || ['glass', 'cover', 'case', 'tempered', 'protector', 'कभर', 'ग्लास', 'गिलास', 'screen guard', 'skin'].some(k => text.includes(k));
            } else if (this.currentCategory === 'AUDIO') {
                const text = `${p.name || ''} ${p.category_name || ''} ${p.specs || ''}`.toLowerCase();
                matchesCat = (p.category_key === 'AUDIO') || ['audio', 'earbud', 'headphone', 'airpod', 'neckband', 'speaker', 'earphone', 'tws', 'handsfree', 'एयरबड्स', 'हेडफोन'].some(k => text.includes(k));
            } else if (this.currentCategory === 'PARTS') {
                const text = `${p.name || ''} ${p.category_name || ''} ${p.specs || ''}`.toLowerCase();
                matchesCat = (p.category_key === 'PARTS') || p.is_spare_part === true || ['part', 'spare', 'battery', 'display', 'screen replacement', 'touch panel', 'मर्मत', 'पार्ट्स'].some(k => text.includes(k));
            } else {
                matchesCat = (p.category_key === this.currentCategory || (p.category_name && p.category_name.toUpperCase() === this.currentCategory));
            }

            const matchesQuery = cleanQuery === '' || 
                (p.name && p.name.toLowerCase().includes(cleanQuery)) || 
                (p.brand_name && p.brand_name.toLowerCase().includes(cleanQuery)) || 
                (p.model_name && p.model_name.toLowerCase().includes(cleanQuery)) ||
                (p.specs && p.specs.toLowerCase().includes(cleanQuery)) ||
                (p.sku && p.sku.toLowerCase().includes(cleanQuery)) ||
                (p.barcode && p.barcode.toLowerCase().includes(cleanQuery)) ||
                (p.imei_1 && p.imei_1.toLowerCase().includes(cleanQuery)) ||
                (p.imei_2 && p.imei_2.toLowerCase().includes(cleanQuery));

            return matchesCat && matchesQuery;
        });
    }

    filterCatalogBySearch(query = '') {
        const filtered = this.getMatchingCatalogProducts(query);
        this.renderCatalog(filtered);
    }

    renderCatalog(products) {
        if (!this.productGrid) return;
        this.productGrid.innerHTML = '';

        if (products.length === 0) {
            this.productGrid.innerHTML = `
                <div class="col-12 text-center py-5 text-muted">
                    <i class="fas fa-search fs-2 opacity-25 d-block mb-2"></i>
                    No products matched your search.
                </div>
            `;
            return;
        }

        products.forEach(prod => {
            const isPhone = this.isPhoneProduct(prod);
            let avatarClass = prod.avatar_class || (isPhone ? 'avatar-phone' : 'avatar-part');
            let iconClass = prod.icon_class || (isPhone ? 'fas fa-mobile-screen' : 'fas fa-box');

            if (isPhone && (!prod.avatar_class || prod.avatar_class === 'avatar-phone')) {
                const brand = (prod.brand_name || '').toLowerCase();
                const name = (prod.name || '').toLowerCase();
                if (brand.includes('apple') || name.includes('iphone')) {
                    iconClass = 'fab fa-apple';
                } else if (brand.includes('samsung') || name.includes('galaxy')) {
                    iconClass = 'fas fa-mobile-screen';
                } else {
                    iconClass = 'fas fa-mobile-screen-button';
                }
                avatarClass = 'avatar-phone';
            }

            const card = document.createElement('div');
            card.className = 'product-card';
            card.onclick = () => this.handleProductCardClick(prod);

            const displaySpecs = prod.specs || (prod.ram && prod.storage ? `${prod.ram}/${prod.storage}` : '');

            card.innerHTML = `
                <div>
                    <div class="d-flex align-items-center gap-2 mb-2">
                        <div class="cat-icon-avatar ${avatarClass}">
                            <i class="${iconClass}"></i>
                        </div>
                        <div class="text-truncate">
                            <span class="badge bg-light text-dark border fs-2xs">${escapeHtml(prod.brand_name || 'Generic')}</span>
                            ${isPhone ? '<span class="badge bg-primary bg-opacity-10 text-primary fs-2xs ms-1">IMEI</span>' : ''}
                        </div>
                    </div>
                    <div class="fw-bold text-dark fs-sm text-truncate" title="${escapeHtml(prod.name)}">${escapeHtml(prod.name)}</div>
                    <small class="text-muted fs-2xs d-block text-truncate mt-1">${escapeHtml(displaySpecs)}</small>
                </div>
                <div class="d-flex justify-content-between align-items-center mt-3 pt-2 border-top">
                    <span class="fw-bold font-mono text-primary fs-xs">Rs. ${parseFloat(prod.price || prod.selling_price || 0).toLocaleString()}</span>
                    <span class="badge bg-light text-muted border fs-2xs">${parseInt(prod.available_stock || 0)} in stock</span>
                </div>
            `;
            this.productGrid.appendChild(card);
        });
    }

    handleProductCardClick(product) {
        if (this.isPhoneProduct(product)) {
            this.pendingPhoneProduct = product;
            const titleEl = document.getElementById('imeiModalPhoneName');
            if (titleEl) {
                titleEl.innerText = `${product.name} (${product.specs || ''})`;
            }

            if (this.modalInStockSelect) {
                this.modalInStockSelect.innerHTML = '<option value="">-- Choose from In-Stock Phone Boxes --</option>';
                if (product.in_stock_units && product.in_stock_units.length > 0) {
                    product.in_stock_units.forEach((unit, idx) => {
                        this.modalInStockSelect.innerHTML += `
                            <option value="${escapeHtml(unit.imei_1)}">Box #${idx + 1}: IMEI 1: ${escapeHtml(unit.imei_1)} | IMEI 2: ${escapeHtml(unit.imei_2 || 'N/A')}</option>
                        `;
                    });
                }
            }

            if (this.modalInputImei1) this.modalInputImei1.value = '';
            if (this.modalInputImei2) this.modalInputImei2.value = '';

            if (this.imeiModal) {
                this.imeiModal.show();
                setTimeout(() => this.modalInputImei1 && this.modalInputImei1.focus(), 300);
            }
        } else {
            this.cart.addItem(product);
            POSAudioSynthesizer.play('success');
        }
    }

    handleInStockSelectChange() {
        if (!this.modalInStockSelect) return;
        const selectedImei1 = this.modalInStockSelect.value;
        if (!selectedImei1 || !this.pendingPhoneProduct) return;

        const matched = this.pendingPhoneProduct.in_stock_units.find(u => u.imei_1 === selectedImei1);
        if (matched) {
            if (this.modalInputImei1) this.modalInputImei1.value = matched.imei_1;
            if (this.modalInputImei2) this.modalInputImei2.value = matched.imei_2 || '';
        }
    }

    autoMatchImei2(imei1Val) {
        if (!this.pendingPhoneProduct || !this.pendingPhoneProduct.in_stock_units) return;
        const matched = this.pendingPhoneProduct.in_stock_units.find(u => u.imei_1 === imei1Val.trim());
        if (matched && this.modalInputImei2) {
            this.modalInputImei2.value = matched.imei_2 || '';
        } else if (this.modalInputImei2) {
            this.modalInputImei2.value = '';
        }
    }

    confirmImeiAddition() {
        const imei1 = this.modalInputImei1 ? this.modalInputImei1.value.trim() : '';
        const imei2 = this.modalInputImei2 ? this.modalInputImei2.value.trim() : '';

        if (!imei1) {
            alert('Please enter or select Primary IMEI 1.');
            return;
        }

        const matchedUnit = this.pendingPhoneProduct?.in_stock_units?.find(u => u.imei_1 === imei1);

        this.cart.addItem({
            ...this.pendingPhoneProduct,
            item_instance_id: matchedUnit ? matchedUnit.item_instance_id : null,
            imei_1: imei1,
            imei1: imei1,
            imei_number: imei1,
            imei_2: imei2,
            imei2: imei2,
            secondary_imei: imei2,
            requires_imei: true
        });

        if (this.imeiModal) this.imeiModal.hide();
        this.pendingPhoneProduct = null;
        POSAudioSynthesizer.play('success');
    }

    applyExchangeDiscount() {
        const modelInput = document.getElementById('exchangePhoneModel');
        const valueInput = document.getElementById('exchangePhoneValue');

        const model = modelInput ? modelInput.value.trim() : '';
        const value = parseFloat(valueInput ? valueInput.value : 0) || 0;

        if (!model || value <= 0) {
            alert('Please enter the old phone model and agreed trade-in value.');
            return;
        }

        this.tradeInManager.setDirectExchange(model, value);

        if (this.exchangeModalEl) {
            const modal = bootstrap.Modal.getInstance(this.exchangeModalEl);
            if (modal) modal.hide();
        }

        this.showNotification(`Exchange discount of Rs. ${value.toFixed(2)} applied for ${model}.`, 'success');
    }

    openSplitPaymentModal() {
        const modalEl = document.getElementById('splitPaymentModal');
        if (modalEl) {
            const totals = this.cart.calculateTotals();
            const splitPayable = document.getElementById('splitModalPayableTotal');
            if (splitPayable) splitPayable.innerText = `Rs. ${totals.grandTotal.toFixed(2)}`;

            const splitCash = document.getElementById('splitCashAmt');
            if (splitCash) splitCash.value = totals.grandTotal.toFixed(2);

            const splitFonepay = document.getElementById('splitFonepayAmt');
            if (splitFonepay) splitFonepay.value = '';
            const splitEsewa = document.getElementById('splitEsewaAmt');
            if (splitEsewa) splitEsewa.value = '';
            const splitKhalti = document.getElementById('splitKhaltiAmt');
            if (splitKhalti) splitKhalti.value = '';
            const splitCard = document.getElementById('splitCardAmt');
            if (splitCard) splitCard.value = '';
            const splitBank = document.getElementById('splitBankAmt');
            if (splitBank) splitBank.value = '';
            const splitCredit = document.getElementById('splitCreditAmt');
            if (splitCredit) splitCredit.value = '';

            const refIds = [
                'splitFonepayRef',
                'splitEsewaRef',
                'splitKhaltiRef',
                'splitCardRef',
                'splitBankRef',
                'splitCreditRef'
            ];
            refIds.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });

            const remBal = document.getElementById('splitRemainingBalance');
            if (remBal) remBal.innerText = 'Rs. 0.00';

            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();

            const inputs = modalEl.querySelectorAll('.split-pay-input');
            const updateRemaining = () => {
                let totalEntered = 0;
                inputs.forEach(inp => totalEntered += (parseFloat(inp.value) || 0));
                const diff = totals.grandTotal - totalEntered;
                if (remBal) {
                    remBal.innerText = `Rs. ${diff.toFixed(2)}`;
                    remBal.className = Math.abs(diff) < 0.01 ? 'text-success font-monospace' : 'text-danger font-monospace';
                }
            };
            inputs.forEach(inp => inp.addEventListener('input', updateRemaining));
        }
    }

    showThermalReceipt(slipNo, custName, payMode, totals) {
        const slipEl = document.getElementById('recSlipNo');
        const custEl = document.getElementById('recCustomer');
        const subtotalEl = document.getElementById('recSubtotal');
        const grandEl = document.getElementById('recGrandTotal');
        const payModeEl = document.getElementById('recPayMode');

        if (slipEl) slipEl.innerText = slipNo;
        if (custEl) custEl.innerText = custName;
        if (subtotalEl) subtotalEl.innerText = `Rs. ${totals.subtotal.toFixed(2)}`;
        if (grandEl) grandEl.innerText = `Rs. ${totals.grandTotal.toFixed(2)}`;
        if (payModeEl) payModeEl.innerText = payMode;

        const exRow = document.getElementById('recExchangeRow');
        const exText = document.getElementById('recExchange');
        if (totals.tradeInCredit > 0 && exRow) {
            exRow.style.display = 'flex';
            if (exText) exText.innerText = `-Rs. ${totals.tradeInCredit.toFixed(2)}`;
        } else if (exRow) {
            exRow.style.display = 'none';
        }

        const discRow = document.getElementById('recDiscountRow');
        const discText = document.getElementById('recDiscount');
        if (totals.totalDiscount > 0 && discRow) {
            discRow.style.display = 'flex';
            if (discText) discText.innerText = `-Rs. ${totals.totalDiscount.toFixed(2)}`;
        } else if (discRow) {
            discRow.style.display = 'none';
        }

        const itemsContainer = document.getElementById('recItemsContainer');
        if (itemsContainer) {
            itemsContainer.innerHTML = '';
            this.cart.items.forEach(i => {
                const itemDiv = document.createElement('div');
                itemDiv.className = 'mb-1';
                const primaryImei = i.imei_number || i.imei_1 || i.imei1 || '';
                const secondaryImei = i.secondary_imei || i.imei_2 || i.imei2 || '';
                const price = (i.unit_price !== undefined) ? i.unit_price : (i.price || 0);

                itemDiv.innerHTML = `
                    <div class="d-flex justify-content-between">
                        <strong>${escapeHtml(i.name)}</strong>
                        <span>${i.quantity} x Rs. ${price.toLocaleString()}</span>
                    </div>
                    ${primaryImei ? `<div style="font-size: 8.5px;">&bull; [IMEI 1: ${escapeHtml(primaryImei)}]</div>` : ''}
                    ${secondaryImei ? `<div style="font-size: 8.5px;">&bull; [IMEI 2: ${escapeHtml(secondaryImei)}]</div>` : ''}
                `;
                itemsContainer.appendChild(itemDiv);
            });
        }

        if (this.receiptModal) {
            this.receiptModal.show();
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

// Aliases for full backward compatibility across templates, hardware scripts & partials
window.POSEngine = SmartPOSEngine;

// Initialize on DOM Ready
document.addEventListener('DOMContentLoaded', () => {
    window.smartPos = new SmartPOSEngine();
    window.posEngine = window.smartPos;
});