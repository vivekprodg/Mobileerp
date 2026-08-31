/**
 * Universal Camera Capture Engine with Camera Switching & Watermark (Nepal Mobile ERP)
 * Supports front/back camera toggling, timestamp & branch watermark overlays, and permission error handling.
 */

class UniversalCameraCapture {
    constructor() {
        this.startBtn = document.getElementById('startCameraBtn');
        this.closeBtn = document.getElementById('closeCameraBtn');
        this.snapBtn = document.getElementById('snapPhotoBtn');
        this.switchBtn = document.getElementById('switchCameraBtn');
        this.container = document.getElementById('liveCameraContainer');
        this.video = document.getElementById('cameraVideo');
        this.canvas = document.getElementById('snapshotCanvas') || document.createElement('canvas');
        this.previewGrid = document.getElementById('capturedPhotosPreview');
        this.fileInput = document.getElementById('filePhotoInput') || document.querySelector('[name="customer_live_photo"]');

        this.stream = null;
        this.currentFacingMode = 'environment'; // 'environment' (back) | 'user' (front)

        this.init();
    }

    init() {
        if (this.startBtn) this.startBtn.addEventListener('click', () => this.startCamera());
        if (this.closeBtn) this.closeBtn.addEventListener('click', () => this.stopCamera());
        if (this.snapBtn) this.snapBtn.addEventListener('click', () => this.takeSnapshot());
        if (this.switchBtn) this.switchBtn.addEventListener('click', () => this.toggleCamera());
    }

    async startCamera() {
        this.stopCamera();

        const constraints = {
            video: {
                width: { ideal: 1280 },
                height: { ideal: 720 },
                facingMode: this.currentFacingMode
            }
        };

        try {
            this.stream = await navigator.mediaDevices.getUserMedia(constraints);
            this.video.srcObject = this.stream;
            if (this.container) this.container.classList.remove('d-none');
        } catch (err) {
            console.error('[CameraCapture] Access error:', err);
            // Fallback without strict facingMode
            try {
                this.stream = await navigator.mediaDevices.getUserMedia({ video: true });
                this.video.srcObject = this.stream;
                if (this.container) this.container.classList.remove('d-none');
            } catch (fallbackErr) {
                alert('Camera permission denied or camera device not detected. Please upload files directly.');
            }
        }
    }

    toggleCamera() {
        this.currentFacingMode = this.currentFacingMode === 'environment' ? 'user' : 'environment';
        this.startCamera();
    }

    stopCamera() {
        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
            this.stream = null;
        }
        if (this.container) this.container.classList.add('d-none');
    }

    takeSnapshot() {
        if (!this.stream) return;

        this.canvas.width = this.video.videoWidth || 640;
        this.canvas.height = this.video.videoHeight || 480;
        const ctx = this.canvas.getContext('2d');

        // Draw image frame
        ctx.drawImage(this.video, 0, 0, this.canvas.width, this.canvas.height);

        // Burn-in timestamp watermark overlay
        const now = new Date();
        const dateStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
        
        ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
        ctx.fillRect(10, this.canvas.height - 35, 260, 25);
        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 12px monospace';
        ctx.fillText(`Audit: ${dateStr}`, 18, this.canvas.height - 18);

        this.canvas.toBlob((blob) => {
            const file = new File([blob], `snap_${Date.now()}.jpg`, { type: 'image/jpeg' });
            
            if (this.fileInput) {
                const dt = new DataTransfer();
                if (this.fileInput.files) {
                    Array.from(this.fileInput.files).forEach(f => dt.items.add(f));
                }
                dt.items.add(file);
                this.fileInput.files = dt.files;
            }

            if (this.previewGrid) {
                const imgUrl = URL.createObjectURL(blob);
                const previewCard = document.createElement('div');
                previewCard.className = 'position-relative border rounded p-1 bg-white shadow-sm';
                previewCard.style.width = '90px';
                previewCard.innerHTML = `<img src="${imgUrl}" class="rounded" style="width: 100%; height: 70px; object-fit: cover;">`;
                this.previewGrid.appendChild(previewCard);
            }
        }, 'image/jpeg', 0.90);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    new UniversalCameraCapture();
});