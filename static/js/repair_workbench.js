/**
 * Technician Interactive Workbench AJAX Controller
 */
document.addEventListener('DOMContentLoaded', () => {
    // Dynamic recalculation on customer charges
    const customerChargeInput = document.querySelector('[name="customer_charge"]');
    if (customerChargeInput) {
        customerChargeInput.addEventListener('input', (e) => {
            const val = parseFloat(e.target.value) || 0;
            console.log(`[Workbench] Updated part customer charge: Rs. ${val}`);
        });
    }
});