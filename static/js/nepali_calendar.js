/**
 * Client-side Bikram Sambat (BS) Calendar Converter
 */
const NepaliCalendarHelper = {
    MONTHS_EN: ["Baishakh", "Jestha", "Ashadh", "Shrawan", "Bhadra", "Ashwin", "Kartik", "Mangsir", "Poush", "Magh", "Falgun", "Chaitra"],
    MONTHS_NP: ["वैशाख", "जेठ", "असार", "साउन", "भदौ", "असोज", "कार्तिक", "मंसिर", "पुस", "माघ", "फागुन", "चैत"],

    async convertAdToBs(adDateStr) {
        try {
            const res = await fetch(`/api/convert-date/?action=ad_to_bs&date=${encodeURIComponent(adDateStr)}`);
            const data = await res.json();
            return data.status === 'success' ? data : null;
        } catch (e) {
            return null;
        }
    }
};