/**
 * Universal Camera & Image Barcode Decoder Engine (Enhanced)
 * Supports:
 * 1. Live Camera Stream with Canvas Contrast & Binarization Preprocessing
 * 2. Direct Image File Decode (Select/Drop mobile box photos directly from PC)
 * 3. ZXing High-Sensitivity Multi-Format Reader with TRY_HARDER enabled
 * 4. Fallback Multi-Pass Grayscale Thresholding
 */

class CameraBarcodeScanner {
    constructor(options = {}) {
        this.videoElement = null;
        this.canvasElement = document.createElement('canvas');
        this.canvasCtx = this.canvasElement.getContext('2d', { willReadFrequently: true });
        this.stream = null;
        this.animFrameId = null;

        this.scanIntervalMs = options.scanInterval || 60;
        this.cooldownMs = options.cooldown || 1200;
        this.lastScanTime = 0;
        this.isScanning = false;
        this.facingMode = options.facingMode || 'environment';

        this.zxingReader = null;
        this.barcodeDetector = null;
        this.hasTorch = false;
        this.isTorchOn = false;

        this.onScanSuccess = null;
        this.onScanError = null;

        this.initDecoders();
    }

    async initDecoders() {
        // 1. Check Native BarcodeDetector
        if ('BarcodeDetector' in window) {
            try {
                const formats = ['code_128', 'ean_13', 'code_39', 'upc_a', 'qr_code', 'data_matrix'];
                this.barcodeDetector = new BarcodeDetector({ formats: formats });
            } catch (e) {}
        }

        // 2. Load ZXing with TRY_HARDER configuration
        this.loadZXing();
    }

    loadZXing() {
        if (window.ZXing) {
            this.setupZXingReader();
            return;
        }

        const script = document.createElement('script');
        script.src = 'https://cdn.jsdelivr.net/npm/@zxing/library@0.20.0/umd/index.min.js';
        script.async = true;
        script.onload = () => {
            this.setupZXingReader();
        };
        document.head.appendChild(script);
    }

    setupZXingReader() {
        if (window.ZXing && window.ZXing.BrowserMultiFormatReader) {
            const hints = new Map();
            const formats = [
                window.ZXing.BarcodeFormat.CODE_128,
                window.ZXing.BarcodeFormat.EAN_13,
                window.ZXing.BarcodeFormat.CODE_39,
                window.ZXing.BarcodeFormat.UPC_A,
                window.ZXing.BarcodeFormat.QR_CODE,
                window.ZXing.BarcodeFormat.DATA_MATRIX
            ];
            hints.set(window.ZXing.DecodeHintType.POSSIBLE_FORMATS, formats);
            hints.set(window.ZXing.DecodeHintType.TRY_HARDER, true);

            this.zxingReader = new window.ZXing.BrowserMultiFormatReader(hints);
        }
    }

    /**
     * Start live video feed with enhanced image contrast filters
     */
    async start(videoEl, successCallback, errorCallback = null) {
        this.stop();
        this.videoElement = videoEl;
        this.onScanSuccess = successCallback;
        this.onScanError = errorCallback;

        if (!this.videoElement) return;

        const constraints = {
            audio: false,
            video: {
                facingMode: { ideal: this.facingMode },
                width: { ideal: 1280, min: 640 },
                height: { ideal: 720, min: 480 }
            }
        };

        try {
            this.stream = await navigator.mediaDevices.getUserMedia(constraints);
            this.videoElement.srcObject = this.stream;
            this.videoElement.setAttribute('playsinline', 'true');
            await this.videoElement.play();

            const track = this.stream.getVideoTracks()[0];
            if (track && track.getCapabilities) {
                this.hasTorch = Boolean(track.getCapabilities().torch);
            }

            this.isScanning = true;
            this.lastScanTime = 0;
            this.scanLoop();
        } catch (err) {
            // Fallback for basic webcams
            try {
                this.stream = await navigator.mediaDevices.getUserMedia({ video: true });
                this.videoElement.srcObject = this.stream;
                await this.videoElement.play();
                this.isScanning = true;
                this.scanLoop();
            } catch (e) {
                if (this.onScanError) this.onScanError(new Error('Camera access denied or unavailable.'));
            }
        }
    }

    /**
     * Continuous Frame Detection with Real-time Contrast Preprocessing
     */
    async scanLoop() {
        if (!this.isScanning || !this.videoElement) return;

        const now = Date.now();

        if (this.videoElement.readyState === this.videoElement.HAVE_ENOUGH_DATA) {
            if (now - this.lastScanTime > this.cooldownMs) {
                const code = await this.detectCodeFromVideo();
                if (code) {
                    this.lastScanTime = now;
                    this.playBeep();
                    if (typeof this.onScanSuccess === 'function') {
                        this.onScanSuccess(code);
                    }
                }
            }
        }

        if (this.isScanning) {
            setTimeout(() => {
                this.animFrameId = requestAnimationFrame(() => this.scanLoop());
            }, this.scanIntervalMs);
        }
    }

    /**
     * Decode from video frame
     */
    async detectCodeFromVideo() {
        if (!this.videoElement) return null;

        const w = this.videoElement.videoWidth;
        const h = this.videoElement.videoHeight;
        if (w === 0 || h === 0) return null;

        this.canvasElement.width = w;
        this.canvasElement.height = h;

        // 1. Apply image filter to sharpen barcode lines
        this.canvasCtx.filter = 'contrast(1.4) grayscale(1)';
        this.canvasCtx.drawImage(this.videoElement, 0, 0, w, h);

        // 2. Try Native BarcodeDetector
        if (this.barcodeDetector) {
            try {
                const barcodes = await this.barcodeDetector.detect(this.canvasElement);
                if (barcodes && barcodes.length > 0 && barcodes[0].rawValue) {
                    return barcodes[0].rawValue.trim();
                }
            } catch (e) {}
        }

        // 3. Try ZXing MultiFormat Reader
        if (this.zxingReader) {
            try {
                const result = this.zxingReader.decodeFromCanvas(this.canvasElement);
                if (result && result.getText()) {
                    return result.getText().trim();
                }
            } catch (e) {}
        }

        return null;
    }

    /**
     * DIRECT IMAGE FILE DECODER (Decodes saved mobile box pictures directly)
     */
    async decodeFromImageFile(file) {
        if (!file) return null;

        return new Promise((resolve, reject) => {
            const img = new Image();
            const reader = new FileReader();

            reader.onload = (e) => {
                img.onload = async () => {
                    this.canvasElement.width = img.naturalWidth || img.width;
                    this.canvasElement.height = img.naturalHeight || img.height;
                    
                    // Draw original
                    this.canvasCtx.filter = 'none';
                    this.canvasCtx.drawImage(img, 0, 0);

                    let detectedCode = null;

                    // Pass 1: Try Native Detector
                    if (this.barcodeDetector) {
                        try {
                            const barcodes = await this.barcodeDetector.detect(this.canvasElement);
                            if (barcodes && barcodes.length > 0 && barcodes[0].rawValue) {
                                detectedCode = barcodes[0].rawValue.trim();
                            }
                        } catch (err) {}
                    }

                    // Pass 2: Try ZXing with original image
                    if (!detectedCode && this.zxingReader) {
                        try {
                            const result = this.zxingReader.decodeFromCanvas(this.canvasElement);
                            if (result && result.getText()) {
                                detectedCode = result.getText().trim();
                            }
                        } catch (err) {}
                    }

                    // Pass 3: Enhance Contrast & Grayscale and Retry
                    if (!detectedCode && this.zxingReader) {
                        try {
                            this.canvasCtx.filter = 'contrast(1.6) grayscale(1)';
                            this.canvasCtx.drawImage(img, 0, 0);
                            const result = this.zxingReader.decodeFromCanvas(this.canvasElement);
                            if (result && result.getText()) {
                                detectedCode = result.getText().trim();
                            }
                        } catch (err) {}
                    }

                    if (detectedCode) {
                        this.playBeep();
                        if (typeof this.onScanSuccess === 'function') {
                            this.onScanSuccess(detectedCode);
                        }
                        resolve(detectedCode);
                    } else {
                        reject(new Error('No readable barcode found in this image. Please ensure the barcode lines are clear and well-lit.'));
                    }
                };
                img.src = e.target.result;
            };

            reader.readAsDataURL(file);
        });
    }

    playBeep() {
        try {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;
            const ctx = new AudioContext();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(1850, ctx.currentTime);
            gain.gain.setValueAtTime(0.2, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.09);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.09);
        } catch (e) {}
    }

    async toggleTorch() {
        if (!this.stream || !this.hasTorch) return false;
        const track = this.stream.getVideoTracks()[0];
        if (!track) return false;
        try {
            this.isTorchOn = !this.isTorchOn;
            await track.applyConstraints({ advanced: [{ torch: this.isTorchOn }] });
            return this.isTorchOn;
        } catch (e) {
            return false;
        }
    }

    async switchCamera() {
        this.facingMode = this.facingMode === 'environment' ? 'user' : 'environment';
        if (this.isScanning && this.videoElement) {
            await this.start(this.videoElement, this.onScanSuccess, this.onScanError);
        }
    }

    stop() {
        this.isScanning = false;
        if (this.animFrameId) {
            cancelAnimationFrame(this.animFrameId);
            this.animFrameId = null;
        }
        if (this.stream) {
            this.stream.getTracks().forEach(t => t.stop());
            this.stream = null;
        }
        if (this.videoElement) {
            this.videoElement.srcObject = null;
        }
        this.isTorchOn = false;
    }
}

window.CameraBarcodeScanner = CameraBarcodeScanner;