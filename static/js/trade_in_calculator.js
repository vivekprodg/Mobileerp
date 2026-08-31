/**
 * Interactive 10-Point Technical Diagnosis & Trade-In Valuation Calculator (Nepal Mobile ERP)
 * Features 300ms calculation debouncing, live loading spinners, and rate-limited NTA MDMS checks.
 */

class TradeInCalculator {
    constructor() {
        this.baseMarketInput = document.querySelector('[name="market_base_value"]');
        this.imeiInput = document.querySelector('[name="imei_1"]');
        this.checkSelects = document.querySelectorAll('.tradein-check-select');
        this.mdmsStatusSelect = document.querySelector('[name="mdms_status"]');

        this.finalOfferText = document.getElementById('calcFinalOffer');
        this.totalDeductionsText = document.getElementById('calcTotalDeductions');
        this.marginDeductionText = document.getElementById('calcMarginDeduction');
        this.baseValueText = document.getElementById('calcBaseValue');
        this.conditionGradeText = document.getElementById('calcConditionGrade');
        this.deductionListContainer = document.getElementById('calcDeductionList');
        this.mdmsVerifyBtn = document.getElementById('verifyMdmsBtn');
        this.mdmsResultBanner = document.getElementById('mdmsResultBanner');

        this.debounceTimer = null;
        this.isCalculating = false;

        this.initEventListeners();
        this.debouncedRecalculate();
    }

    initEventListeners() {
        if (this.baseMarketInput) {
            this.baseMarketInput.addEventListener('input', () => this.debouncedRecalculate());
        }

        this.checkSelects.forEach(select => {
            select.addEventListener('change', () => this.debouncedRecalculate());
        });

        if (this.mdmsVerifyBtn && this.imeiInput) {
            this.mdmsVerifyBtn.addEventListener('click', () => this.verifyMDMS(this.imeiInput.value.trim()));
        }
    }

    debouncedRecalculate(delay = 300) {
        clearTimeout(this.debounceTimer);
        this.debounceTimer = setTimeout(() => {
            this.recalculate();
        }, delay);
    }

    setLoadingState(isLoading) {
        this.isCalculating = isLoading;
        if (this.finalOfferText) {
            if (isLoading) {
                this.finalOfferText.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
            }
        }
    }

    async verifyMDMS(imei) {
        if (!imei || imei.length < 14) {
            alert('Please enter a valid 15-digit IMEI number.');
            return;
        }

        if (this.mdmsVerifyBtn) {
            this.mdmsVerifyBtn.disabled = true;
            this.mdmsVerifyBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-1"></i> Verifying...';
        }

        try {
            const resp = await fetch(`/sales/api/mdms/check/?imei=${encodeURIComponent(imei)}`);
            
            if (resp.status === 429) {
                if (this.mdmsResultBanner) {
                    this.mdmsResultBanner.classList.remove('d-none');
                    this.mdmsResultBanner.className = 'alert alert-warning p-2 fs-xs d-flex align-items-center gap-2 mb-3';
                    this.mdmsResultBanner.innerHTML = '<i class="fas fa-hourglass-half fs-6"></i> <span>MDMS Rate limit reached. Please wait 1 minute before re-checking.</span>';
                }
                return;
            }

            const data = await resp.json();

            if (this.mdmsResultBanner) {
                this.mdmsResultBanner.classList.remove('d-none');
                if (data.is_registered) {
                    this.mdmsResultBanner.className = 'alert alert-success p-2 fs-xs d-flex align-items-center gap-2 mb-3';
                    this.mdmsResultBanner.innerHTML = `<i class="fas fa-check-circle fs-6"></i> <span><strong>NTA MDMS Verified:</strong> ${data.message} (${data.model || ''})</span>`;
                    if (this.mdmsStatusSelect) this.mdmsStatusSelect.value = 'REGISTERED_OFFICIAL';
                } else {
                    this.mdmsResultBanner.className = 'alert alert-danger p-2 fs-xs d-flex align-items-center gap-2 mb-3';
                    this.mdmsResultBanner.innerHTML = `<i class="fas fa-triangle-exclamation fs-6"></i> <span><strong>Warning:</strong> ${data.message}</span>`;
                    if (this.mdmsStatusSelect) this.mdmsStatusSelect.value = 'GRAY_UNREGISTERED';
                }
            }
        } catch (err) {
            if (this.mdmsResultBanner) {
                this.mdmsResultBanner.classList.remove('d-none');
                this.mdmsResultBanner.className = 'alert alert-secondary p-2 fs-xs d-flex align-items-center gap-2 mb-3';
                this.mdmsResultBanner.innerHTML = '<i class="fas fa-circle-question fs-6"></i> <span>NTA Gateway temporarily unreachable. Proceeding with pending offline flag.</span>';
            }
        } finally {
            if (this.mdmsVerifyBtn) {
                this.mdmsVerifyBtn.disabled = false;
                this.mdmsVerifyBtn.innerHTML = '<i class="fas fa-shield-halved me-1"></i> Verify NTA MDMS';
            }
        }
    }

    async recalculate() {
        const baseVal = parseFloat(this.baseMarketInput?.value) || 0;
        
        if (this.baseValueText) {
            this.baseValueText.innerText = `Rs. ${baseVal.toFixed(2)}`;
        }

        if (baseVal <= 0) {
            if (this.finalOfferText) this.finalOfferText.innerText = 'Rs. 0.00';
            if (this.totalDeductionsText) this.totalDeductionsText.innerText = '-Rs. 0.00';
            if (this.marginDeductionText) this.marginDeductionText.innerText = '-Rs. 0.00';
            return;
        }

        this.setLoadingState(true);

        const checklistData = {};
        this.checkSelects.forEach(sel => {
            checklistData[sel.name] = sel.value;
        });

        const csrfTokenEl = document.querySelector('[name=csrfmiddlewaretoken]');
        const csrfToken = csrfTokenEl ? csrfTokenEl.value : '';

        try {
            const resp = await fetch('/sales/api/trade-in/calculate/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    base_market_value: baseVal,
                    ...checklistData
                })
            });

            if (resp.ok) {
                const data = await resp.json();
                if (data.status === 'success') {
                    if (this.finalOfferText) this.finalOfferText.innerText = `Rs. ${parseFloat(data.final_offer).toFixed(2)}`;
                    if (this.totalDeductionsText) this.totalDeductionsText.innerText = `-Rs. ${parseFloat(data.total_deductions).toFixed(2)}`;
                    if (this.marginDeductionText) this.marginDeductionText.innerText = `-Rs. ${parseFloat(data.margin_deduction || 0).toFixed(2)}`;
                    if (this.conditionGradeText) this.conditionGradeText.innerText = data.condition_grade.replace(/_/g, ' ');

                    if (this.deductionListContainer && data.deduction_breakdown) {
                        if (data.deduction_breakdown.length === 0) {
                            this.deductionListContainer.innerHTML = '<li class="text-success fs-xs"><i class="fas fa-check-circle me-1"></i> No defects flagged (Pristine Grade A).</li>';
                        } else {
                            this.deductionListContainer.innerHTML = data.deduction_breakdown.map(d => `
                                <li class="d-flex justify-content-between text-danger fs-2xs py-1 border-bottom">
                                    <span>${d.category.replace(/_/g, ' ').toUpperCase()}:</span>
                                    <strong>-${d.penalty_percent} (Rs. ${parseFloat(d.deduction_amount).toFixed(2)})</strong>
                                </li>
                            `).join('');
                        }
                    }
                }
            }
        } catch (err) {
            console.error('[TradeInCalculator] API error:', err);
        } finally {
            this.setLoadingState(false);
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    new TradeInCalculator();
});