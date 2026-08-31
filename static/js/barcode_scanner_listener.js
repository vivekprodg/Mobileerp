/**
 * Hardware USB & Bluetooth Barcode Scanner Listener (Nepal Mobile Shop ERP)
 * Captures high-speed scanner keystroke sequences, isolates hardware input
 * from manual human typing, supports dynamic threshold configuration, and
 * routes barcode inputs cleanly without unintentional form submissions.
 */

class BarcodeScannerListener {
    constructor(callback, options = {}) {
        this.callback = callback;
        this.buffer = '';
        this.lastKeystrokeTime = Date.now();
        
        // Configurable options
        this.scannerSpeedThreshold = options.threshold || 75; // ms between consecutive keystrokes
        this.minBarcodeLength = options.minLength || 3;
        this.enabled = options.enabled !== undefined ? options.enabled : true;
        this.allowInInputs = options.allowInInputs || false;
        this.dedicatedInputSelector = options.dedicatedInputSelector || '#posProductSearchInput';

        this.init();
    }

    enable() {
        this.enabled = true;
    }

    disable() {
        this.enabled = false;
        this.buffer = '';
    }

    setThreshold(ms) {
        if (typeof ms === 'number' && ms > 10) {
            this.scannerSpeedThreshold = ms;
        }
    }

    isFormInputElement(element) {
        if (!element) return false;
        
        // Dedicated scan inputs (like POS fast search or GRN IMEI boxes) always handle scan routing directly
        if (this.dedicatedInputSelector && element.matches(this.dedicatedInputSelector)) {
            return false;
        }

        const tagName = element.tagName ? element.tagName.toUpperCase() : '';
        const isEditable = element.isContentEditable;
        const isInput = tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT';
        
        return isInput || isEditable;
    }

    init() {
        document.addEventListener('keydown', (e) => {
            if (!this.enabled) return;

            const activeEl = document.activeElement;
            const inFormField = this.isFormInputElement(activeEl);

            const currentTime = Date.now();
            const timeDiff = currentTime - this.lastKeystrokeTime;
            this.lastKeystrokeTime = currentTime;

            // Reset buffer if keystroke delay indicates manual human typing
            if (timeDiff > this.scannerSpeedThreshold) {
                this.buffer = '';
            }

            if (e.key === 'Enter') {
                if (this.buffer.length >= this.minBarcodeLength) {
                    // Only intercept if we're not inside a generic text field or if in dedicated scan box
                    if (!inFormField || (this.dedicatedInputSelector && activeEl && activeEl.matches(this.dedicatedInputSelector))) {
                        e.preventDefault();
                        e.stopPropagation();

                        const scannedData = this.buffer.trim();
                        this.buffer = '';

                        if (typeof this.callback === 'function') {
                            this.callback(scannedData);
                        }

                        document.dispatchEvent(new CustomEvent('barcode-scanned', {
                            detail: { code: scannedData }
                        }));
                    } else {
                        this.buffer = '';
                    }
                }
            } else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
                this.buffer += e.key;
            }
        }, true);
    }
}