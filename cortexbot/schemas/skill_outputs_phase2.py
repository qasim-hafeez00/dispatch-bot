"""
cortexbot/schemas/skill_outputs_phase2.py

Phase 2 Pydantic Validation Schemas

Adds validation for:
- HOS compliance outputs
- Transit monitoring outputs
- Detention claims
- Invoice outputs
- Payment reconciliation
- Settlement calculations
- Financial skill outputs
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Literal, List, Dict, Any
from datetime import datetime


# ─────────────────────────────────────────────────────────────
# HOS STATUS (Skill 14)
# ─────────────────────────────────────────────────────────────

class HOSAlert(BaseModel):
    level:   Literal["INFO", "WARNING", "CRITICAL", "EMERGENCY"]
    code:    str
    message: str


class HOSStatusOutput(BaseModel):
    """Validated HOS status from ELD."""
    current_status:         str
    driving_today_hrs:      float = Field(ge=0.0, le=11.0)
    on_duty_today_hrs:      float = Field(ge=0.0, le=14.0)
    time_remaining_driving: float = Field(ge=0.0, le=11.0)
    time_remaining_window:  float = Field(ge=0.0, le=14.0)
    weekly_on_duty_hours:   float = Field(ge=0.0, le=70.0)
    break_taken_today:      bool
    eld_provider:           str
    last_updated:           str

    # Derived
    @property
    def hours_available(self) -> float:
        return min(self.time_remaining_driving, self.time_remaining_window)

    @property
    def can_drive(self) -> bool:
        return self.time_remaining_driving > 0.5 and self.time_remaining_window > 0.5


# ─────────────────────────────────────────────────────────────
# TRANSIT MONITORING (Skill 15)
# ─────────────────────────────────────────────────────────────

class GPSPosition(BaseModel):
    latitude:  Optional[float] = None
    longitude: Optional[float] = None
    speed_mph: float = Field(default=0.0, ge=0.0, le=90.0)
    heading:   float = Field(default=0.0, ge=0.0, le=360.0)
    timestamp: str


class ETAInfo(BaseModel):
    eta_utc:         str
    minutes_away:    int = Field(ge=0)
    miles_remaining: int = Field(ge=0)


class TransitMonitorOutput(BaseModel):
    load_id:          str
    gps_status:       Literal["OK", "NO_SIGNAL", "STALE"]
    last_gps_position: Optional[GPSPosition] = None
    last_eta:         Optional[ETAInfo] = None
    delay_detected:   bool = False
    delay_hours:      float = Field(default=0.0, ge=0.0)
    last_monitored_at: str


# ─────────────────────────────────────────────────────────────
# DETENTION (Skill 16)
# ─────────────────────────────────────────────────────────────

class DetentionClaimLine(BaseModel):
    facility_type:  Literal["pickup", "delivery", "extra_stop"]
    arrival_time:   str
    departure_time: Optional[str] = None
    total_hours:    float = Field(ge=0.0, le=72.0)
    free_hours:     float = Field(ge=0.0, le=8.0, default=2.0)
    billable_hours: float = Field(ge=0.0)
    rate_per_hr:    float = Field(ge=0.0, le=500.0)
    amount:         float = Field(ge=0.0)
    documented:     bool = False

    @field_validator("billable_hours")
    @classmethod
    def validate_billable(cls, v, info):
        return max(0.0, v)


class AccessorialSummary(BaseModel):
    """Complete accessorial claim for invoice."""
    pickup_detention:   float = Field(ge=0.0, default=0.0)
    delivery_detention: float = Field(ge=0.0, default=0.0)
    lumper:             float = Field(ge=0.0, default=0.0)
    tonu:               float = Field(ge=0.0, default=0.0)
    layover:            float = Field(ge=0.0, default=0.0)
    extra_stops:        float = Field(ge=0.0, default=0.0)
    driver_assist:      float = Field(ge=0.0, default=0.0)
    total_accessorials: float = Field(ge=0.0)
    line_items:         List[dict] = []

    @model_validator(mode="after")
    def validate_total(self):
        computed = (
            self.pickup_detention + self.delivery_detention +
            self.lumper + self.tonu + self.layover +
            self.extra_stops + self.driver_assist
        )
        if abs(computed - self.total_accessorials) > 0.05:
            self.total_accessorials = round(computed, 2)
        return self


# ─────────────────────────────────────────────────────────────
# INVOICE (Skill 17)
# ─────────────────────────────────────────────────────────────

class InvoiceOutput(BaseModel):
    """Validated invoice generation result."""
    invoice_number: str
    invoice_amount: float = Field(ge=0.0, le=100000.0)
    linehaul_amount: float = Field(ge=0.0)
    accessorial_amount: float = Field(ge=0.0, default=0.0)
    invoice_s3_url: str
    factoring_used: bool = False
    submitted_to:   Optional[str] = None
    invoice_submitted_at: str

    @model_validator(mode="after")
    def validate_invoice_math(self):
        expected = round(self.linehaul_amount + self.accessorial_amount, 2)
        if abs(expected - self.invoice_amount) > 0.05:
            # Recompute from components
            self.invoice_amount = expected
        return self


# ─────────────────────────────────────────────────────────────
# PAYMENT RECONCILIATION (Skill 19)
# ─────────────────────────────────────────────────────────────

class PaymentRecordOutput(BaseModel):
    """Validated payment record."""
    invoice_number:  str
    invoice_amount:  float = Field(ge=0.0)
    payment_status:  Literal["PENDING", "PAID", "SHORT_PAID", "OVERPAID", "OVERDUE", "IN_COLLECTIONS"]
    amount_paid:     Optional[float] = Field(None, ge=0.0)
    payment_variance: Optional[float] = None
    payment_received_date: Optional[str] = None
    payment_due_date: str

    @property
    def is_paid_in_full(self) -> bool:
        return self.payment_status == "PAID"

    @property
    def is_overdue(self) -> bool:
        return self.payment_status in ("OVERDUE", "IN_COLLECTIONS")


# ─────────────────────────────────────────────────────────────
# DISPATCHER FEE (Skill Q)
# ─────────────────────────────────────────────────────────────

class DispatchFeeOutput(BaseModel):
    """Validated dispatch fee calculation."""
    gross_revenue:   float = Field(ge=0.0, le=100000.0)
    fee_pct:         float = Field(ge=0.03, le=0.15)
    dispatch_fee:    float = Field(ge=0.0)
    net_carrier:     float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_fee_math(self):
        expected_fee = round(self.gross_revenue * self.fee_pct, 2)
        if abs(expected_fee - self.dispatch_fee) > 0.05:
            self.dispatch_fee = expected_fee
            self.net_carrier  = round(self.gross_revenue - expected_fee, 2)
        return self


# ─────────────────────────────────────────────────────────────
# DRIVER SETTLEMENT (Skill R)
# ─────────────────────────────────────────────────────────────

class DriverSettlementOutput(BaseModel):
    """Validated driver settlement."""
    gross_revenue:    float = Field(ge=0.0)
    dispatch_fee:     float = Field(ge=0.0)
    fuel_advances:    float = Field(ge=0.0, default=0.0)
    lumper_advances:  float = Field(ge=0.0, default=0.0)
    repair_advances:  float = Field(ge=0.0, default=0.0)
    other_deductions: float = Field(ge=0.0, default=0.0)
    total_deductions: float = Field(ge=0.0)
    net_settlement:   float = Field(ge=0.0)
    payment_reference: Optional[str] = None

    @model_validator(mode="after")
    def validate_settlement_math(self):
        computed_deductions = (
            self.dispatch_fee + self.fuel_advances +
            self.lumper_advances + self.repair_advances + self.other_deductions
        )
        if abs(computed_deductions - self.total_deductions) > 0.05:
            self.total_deductions = round(computed_deductions, 2)

        computed_net = max(0.0, round(self.gross_revenue - self.total_deductions, 2))
        if abs(computed_net - self.net_settlement) > 0.05:
            self.net_settlement = computed_net
        return self


# ─────────────────────────────────────────────────────────────
# DRIVER ADVANCE (Skill S)
# ─────────────────────────────────────────────────────────────

class DriverAdvanceOutput(BaseModel):
    advance_issued:  bool
    advance_type:    Literal["FUEL", "LUMPER", "EMERGENCY", "TOLL"]
    advance_amount:  float = Field(ge=0.0, le=600.0)
    advance_code:    Optional[str] = None
    advance_network: Optional[Literal["EFS", "Comdata", "Stripe"]] = None


# ─────────────────────────────────────────────────────────────
# BACKHAUL PLANNING (Skill 21)
# ─────────────────────────────────────────────────────────────

class BackhaulCandidate(BaseModel):
    dat_load_id:    str
    broker_company: str
    broker_phone:   Optional[str] = None
    origin_city:    str
    origin_state:   str
    dest_city:      str
    dest_state:     str
    posted_rate:    Optional[float] = Field(None, ge=0.5, le=10.0)


class BackhaulPlanOutput(BaseModel):
    backhaul_candidates:      List[BackhaulCandidate] = []
    delivery_market_quality:  Literal["RICH", "AVERAGE", "POOR"] = "AVERAGE"
    driver_available_at:      str
    backhaul_search_complete: bool = True
