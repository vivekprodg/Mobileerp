/**
 * Thermal Printer Interface (Raw ESC/POS & Print Popups)
 */
class ThermalPrinterHelper {
    static printSlipFromUrl(url) {
        const printWindow = window.open(url, '_blank', 'width=320,height=600');
        if (printWindow) {
            printWindow.focus();
        }
    }

    static playBeepSound() {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(1800, ctx.currentTime);
            gain.gain.setValueAtTime(0.1, ctx.currentTime);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.08);
        } catch (e) {}
    }
}