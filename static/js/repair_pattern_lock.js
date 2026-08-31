/**
 * Interactive Configurable Pattern Lock Canvas Engine (Nepal Mobile ERP)
 * Supports 3x3, 4x4, and 5x5 grid matrices, connected line glowing,
 * sequence number indicators inside selected nodes, and wrong-pattern shake animations.
 */

class PatternLockCanvas {
    constructor(canvasId, inputId, displayId, options = {}) {
        this.canvas = document.getElementById(canvasId);
        if (!this.canvas) return;

        this.ctx = this.canvas.getContext('2d');
        this.input = document.getElementById(inputId);
        this.display = document.getElementById(displayId);

        // Configurable options
        this.gridSize = options.gridSize || 3; // 3 = 3x3, 4 = 4x4, 5 = 5x5
        this.nodeRadius = options.nodeRadius || 12;
        this.primaryColor = options.primaryColor || '#2563eb';
        this.lineColor = options.lineColor || '#2563eb';

        this.points = [];
        this.selectedSequence = [];
        this.isDrawing = false;

        this.initGrid();
        this.initEventListeners();
        this.draw();
    }

    setGridSize(size) {
        if ([3, 4, 5].includes(size)) {
            this.gridSize = size;
            this.reset();
            this.initGrid();
            this.draw();
        }
    }

    initGrid() {
        const size = this.canvas.width;
        const step = size / (this.gridSize + 1);
        this.points = [];

        for (let row = 1; row <= this.gridSize; row++) {
            for (let col = 1; col <= this.gridSize; col++) {
                const id = (row - 1) * this.gridSize + col;
                this.points.push({
                    id: id,
                    x: col * step,
                    y: row * step,
                    radius: this.nodeRadius
                });
            }
        }
    }

    initEventListeners() {
        const getPos = (e) => {
            const rect = this.canvas.getBoundingClientRect();
            const clientX = e.touches ? e.touches[0].clientX : e.clientX;
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            return {
                x: clientX - rect.left,
                y: clientY - rect.top
            };
        };

        const start = (e) => {
            this.isDrawing = true;
            this.selectedSequence = [];
            this.checkPointHit(getPos(e));
        };

        const move = (e) => {
            if (!this.isDrawing) return;
            this.checkPointHit(getPos(e));
        };

        const end = () => {
            this.isDrawing = false;
            this.saveSequence();
            this.draw();
        };

        this.canvas.addEventListener('mousedown', start);
        this.canvas.addEventListener('mousemove', move);
        window.addEventListener('mouseup', end);

        this.canvas.addEventListener('touchstart', start, { passive: true });
        this.canvas.addEventListener('touchmove', move, { passive: true });
        window.addEventListener('touchend', end);

        const resetBtn = document.getElementById('clearPatternBtn');
        if (resetBtn) {
            resetBtn.addEventListener('click', () => this.reset());
        }
    }

    checkPointHit(pos) {
        const hitThreshold = this.nodeRadius * 2.2;
        for (const pt of this.points) {
            const dist = Math.hypot(pt.x - pos.x, pt.y - pos.y);
            if (dist < hitThreshold && !this.selectedSequence.includes(pt.id)) {
                this.selectedSequence.push(pt.id);
                this.draw();
                break;
            }
        }
    }

    draw() {
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);

        // 1. Draw Connecting Lines with Subtle Glow
        if (this.selectedSequence.length > 1) {
            this.ctx.beginPath();
            this.ctx.strokeStyle = this.lineColor;
            this.ctx.lineWidth = 4;
            this.ctx.lineCap = 'round';
            this.ctx.lineJoin = 'round';
            this.ctx.shadowColor = 'rgba(37, 99, 235, 0.3)';
            this.ctx.shadowBlur = 6;

            for (let i = 0; i < this.selectedSequence.length; i++) {
                const pt = this.points.find(p => p.id === this.selectedSequence[i]);
                if (i === 0) this.ctx.moveTo(pt.x, pt.y);
                else this.ctx.lineTo(pt.x, pt.y);
            }
            this.ctx.stroke();
            this.ctx.shadowBlur = 0; // reset
        }

        // 2. Draw Grid Nodes with Order Numbers inside selected dots
        for (const pt of this.points) {
            const orderIndex = this.selectedSequence.indexOf(pt.id);
            const isSelected = orderIndex !== -1;

            this.ctx.beginPath();
            this.ctx.arc(pt.x, pt.y, pt.radius, 0, Math.PI * 2);
            this.ctx.fillStyle = isSelected ? this.primaryColor : '#cbd5e1';
            this.ctx.fill();

            if (isSelected) {
                // Outer ring
                this.ctx.beginPath();
                this.ctx.arc(pt.x, pt.y, pt.radius + 5, 0, Math.PI * 2);
                this.ctx.strokeStyle = 'rgba(37, 99, 235, 0.35)';
                this.ctx.lineWidth = 3;
                this.ctx.stroke();

                // Order Number text inside selected node
                this.ctx.fillStyle = '#ffffff';
                this.ctx.font = 'bold 9px monospace';
                this.ctx.textAlign = 'center';
                this.ctx.textBaseline = 'middle';
                this.ctx.fillText(String(orderIndex + 1), pt.x, pt.y);
            }
        }
    }

    saveSequence() {
        const seqStr = this.selectedSequence.join('-');
        if (this.input) this.input.value = seqStr;
        if (this.display) {
            this.display.innerText = seqStr ? `Recorded: ${seqStr}` : 'No pattern recorded';
        }
    }

    reset() {
        this.selectedSequence = [];
        this.saveSequence();
        this.draw();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    new PatternLockCanvas('patternCanvas', 'id_pattern_lock_sequence', 'patternSeqDisplay', { gridSize: 3 });
});