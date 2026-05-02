"""
cortexbot/mocks/ocr_mock.py

Returns a hardcoded set of RC fields that matches the 25-field schema
expected by s12_rc_review.py. Covers the DAT-TEST-001 fixture load.
"""
import logging

logger = logging.getLogger("mock.ocr")

_MOCK_RC_FIELDS = {
    "carrier_mc_number":          "MC-654321",
    "broker_mc_number":           "MC-123456",
    "broker_company_name":        "Test Freight LLC",
    "load_reference":             "DAT-TEST-001",
    "pickup_full_address":        "100 Industrial Blvd, Dallas, TX 75201",
    "pickup_date":                "2026-05-01",
    "pickup_appointment_time":    "08:00",
    "delivery_full_address":      "500 Warehouse Dr, Atlanta, GA 30301",
    "delivery_date":              "2026-05-02",
    "delivery_appointment_time":  "17:00",
    "commodity":                  "Auto Parts",
    "weight_lbs":                 38000,
    "piece_count":                None,
    "equipment_type":             "53' Dry Van",
    "rate_per_mile":              2.80,
    "flat_rate":                  None,
    "fuel_surcharge_included":    True,
    "detention_free_hours":       2,
    "detention_rate_per_hour":    50,
    "tonu_amount":                150,
    "layover_rate":               None,
    "lumper_payer":               None,
    "tracking_method":            "MacroPoint",
    "payment_terms_days":         30,
    "quick_pay_pct":              2.0,
    "factoring_allowed":          True,
    "invoice_email":              "ar@testfreight.com",
}


async def mock_extract_rc_fields(s3_url: str) -> dict:
    logger.info("[MOCK OCR] returning hardcoded RC fields for %s", s3_url)
    return {
        "fields":         _MOCK_RC_FIELDS.copy(),
        "quality_issues": [],
        "quality_score":  1.0,
    }
