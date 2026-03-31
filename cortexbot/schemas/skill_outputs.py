"""
cortexbot/schemas/skill_outputs.py

Pydantic validation schemas for every Claude/AI output.

WHY THIS IS CRITICAL:
AI models can "hallucinate" — confidently output wrong data.
In a dispatch system, wrong data = wrong addresses, wrong rates, legal liability.

Every time Claude returns JSON, we validate it against these schemas.
If validation fails → retry → escalate, NEVER use invalid data.

Example protection:
- Rate must be $0.50–$10.00/mile (catches hallucinated rates like $25.00/mile)
- Addresses must be > 5 characters (catches empty strings)
- Rate is required when outcome is BOOKED (can't book without a rate)
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Literal, List, Dict, Any


# ============================================================
# VOICE CALL OUTPUT
# Validates what Claude extracts from Bland AI call transcript.
# ============================================================

class VoiceCallOutput(BaseModel):
    """
    Validated output from Bland AI broker call.
    
    All 25 fields extracted from the call transcript are validated here.
    If any required field for a BOOKED outcome is missing,
    Pydantic raises a ValueError immediately.
    """
    
    # ── Call Outcome ────────────────────────────────────────
    outcome: Literal[
        "BOOKED",           # Rate agreed, carrier confirmed, ready to book
        "RATE_TOO_LOW",     # Broker's best rate was below our floor
        "NO_ANSWER",        # Phone rang but no answer
        "VOICEMAIL",        # Went to voicemail (we left a message)
        "LOAD_COVERED",     # Broker said load was already booked
        "CARRIER_REJECTED", # Carrier said NO to this load
        "CALL_FAILED",      # Technical error during call
    ]
    
    # ── Rate (required when BOOKED) ─────────────────────────
    agreed_rate_per_mile: Optional[float] = Field(
        None,
        ge=0.50,   # Minimum $0.50/mile (catches negative or zero)
        le=10.00,  # Maximum $10.00/mile (catches wildly high hallucinations)
        description="Agreed rate in dollars per mile"
    )
    agreed_flat_rate: Optional[float] = Field(
        None,
        ge=50.0,
        le=50000.0,
        description="Agreed flat rate in dollars (alternative to per-mile)"
    )
    
    # ── Accessorials ────────────────────────────────────────
    detention_free_hours: Optional[int] = Field(
        None, ge=0, le=8,
        description="Free hours before detention billing starts (usually 2)"
    )
    detention_rate_per_hour: Optional[float] = Field(
        None, ge=10.0, le=500.0,
        description="Dollars per hour for detention after free period"
    )
    tonu_amount: Optional[float] = Field(
        None, ge=0.0, le=1000.0,
        description="Truck Order Not Used fee if broker cancels after dispatch"
    )
    lumper_payer: Optional[Literal["broker", "carrier"]] = Field(
        None, description="Who pays the lumper (unloading) fee"
    )
    
    # ── Addresses (required when BOOKED) ────────────────────
    pickup_full_address: Optional[str] = Field(
        None, min_length=5,
        description="Complete pickup address including city, state, zip"
    )
    delivery_full_address: Optional[str] = Field(
        None, min_length=5,
        description="Complete delivery address including city, state, zip"
    )
    pickup_datetime: Optional[str] = Field(
        None, description="Pickup date/time in ISO 8601 format"
    )
    delivery_datetime: Optional[str] = Field(
        None, description="Delivery date/time in ISO 8601 format"
    )
    
    # ── Load Specs ───────────────────────────────────────────
    commodity: Optional[str] = Field(None, description="What's being hauled")
    weight_lbs: Optional[int] = Field(
        None, ge=500, le=80000,
        description="Load weight in pounds"
    )
    piece_count: Optional[int] = Field(
        None, ge=1, le=99999,
        description="Number of pallets, cases, drums, etc."
    )
    load_type: Optional[Literal["live", "drop_hook", "preloaded"]] = Field(
        None, description="How the load is picked up"
    )
    driver_assist_required: Optional[bool] = Field(
        None, description="Does the driver need to help load/unload?"
    )
    
    # ── Administrative ───────────────────────────────────────
    tracking_requirement: Optional[str] = Field(
        None, description="macropoint | fourkites | samsara | none"
    )
    payment_terms: Optional[str] = Field(
        None, description="Net 15 | Net 30 | Net 45 | Quick Pay X%"
    )
    quick_pay_option: Optional[str] = Field(
        None, description="Quick pay percentage and terms if available"
    )
    factoring_allowed: Optional[bool] = Field(
        None, description="Does broker allow factoring companies?"
    )
    broker_contact_name: Optional[str] = Field(
        None, description="Name of the broker representative"
    )
    broker_rc_email: Optional[str] = Field(
        None, description="Email to send carrier packet / receive RC"
    )
    load_reference: Optional[str] = Field(
        None, description="Broker's load reference number"
    )
    
    # ── Cross-field Validation ───────────────────────────────
    @model_validator(mode="after")
    def validate_booked_fields(self):
        """When outcome is BOOKED, critical fields must be present."""
        if self.outcome == "BOOKED":
            errors = []
            
            # Rate is absolutely required
            if self.agreed_rate_per_mile is None and self.agreed_flat_rate is None:
                errors.append("Rate (agreed_rate_per_mile or agreed_flat_rate) required when BOOKED")
            
            # Addresses are required
            if not self.pickup_full_address:
                errors.append("pickup_full_address required when BOOKED")
            if not self.delivery_full_address:
                errors.append("delivery_full_address required when BOOKED")
            
            if errors:
                raise ValueError(f"BOOKED outcome missing required fields: {'; '.join(errors)}")
        
        return self
    
    def get_accessorials(self) -> dict:
        """Extract accessorial terms as a dict."""
        return {
            "detention_free_hrs": self.detention_free_hours or 2,
            "detention_rate": self.detention_rate_per_hour,
            "tonu_amount": self.tonu_amount,
            "lumper_payer": self.lumper_payer,
        }
    
    def get_load_details(self) -> dict:
        """Extract all load details as a dict."""
        return {
            "pickup_full_address": self.pickup_full_address,
            "delivery_full_address": self.delivery_full_address,
            "pickup_datetime": self.pickup_datetime,
            "delivery_datetime": self.delivery_datetime,
            "commodity": self.commodity,
            "weight_lbs": self.weight_lbs,
            "piece_count": self.piece_count,
            "load_type": self.load_type,
            "tracking_requirement": self.tracking_requirement,
            "payment_terms": self.payment_terms,
            "broker_contact_name": self.broker_contact_name,
            "broker_rc_email": self.broker_rc_email,
            "load_reference": self.load_reference,
            "driver_assist_required": self.driver_assist_required,
            "lumper_payer": self.lumper_payer,
        }


# ============================================================
# EMAIL CLASSIFICATION OUTPUT
# Validates what GPT-4o-mini says about an incoming email.
# ============================================================

class EmailClassificationOutput(BaseModel):
    """Validated email classification result."""
    
    category: Literal[
        "RC",              # Rate Confirmation document
        "CARRIER_PACKET",  # Broker's carrier setup form
        "PAYMENT",         # Payment/remittance notification
        "DISPUTE",         # Invoice dispute or claim
        "COMPLIANCE",      # Insurance/COI/FMCSA documents
        "LOAD_TENDER",     # Direct shipper load tender (EDI)
        "CLAIM",           # Cargo claim notification
        "OTHER",           # Anything else
    ]
    
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="How confident the classifier is (0=unsure, 1=certain)"
    )
    
    # Extracted identifiers (if detected in subject/body)
    load_ref: Optional[str] = Field(None, description="Load reference number if found")
    broker_name: Optional[str] = Field(None, description="Broker company if identified")
    carrier_id: Optional[str] = Field(None, description="Carrier ID if matched")
    
    route_to_skill: str = Field(description="Which skill should handle this email")
    priority: Literal["P1", "P2", "P3"] = Field(description="Processing priority")
    
    auto_reply_template: Optional[Literal[
        "RC", "CARRIER_PACKET", "PAYMENT", "DISPUTE"
    ]] = Field(None, description="Auto-reply template to use, if any")


# ============================================================
# LOAD SEARCH OUTPUT
# Validates result of DAT/Truckstop load search.
# ============================================================

class LoadCandidate(BaseModel):
    """A single load from the load board."""
    
    dat_load_id: str
    broker_mc: str
    broker_company: str
    broker_phone: Optional[str] = None
    
    # Route
    origin_city: str
    origin_state: str
    destination_city: str
    destination_state: str
    
    # Details
    equipment_type: str
    weight_lbs: Optional[int] = Field(None, ge=0, le=80000)
    commodity: Optional[str] = None
    
    # Rate
    posted_rate_cpm: Optional[float] = Field(None, ge=0.0, le=10.0)
    market_rate_estimate: Optional[float] = Field(None, ge=0.0, le=10.0)
    
    # Scoring
    profitability_score: float = Field(0.0, description="Net CPM score after deductions")
    deadhead_miles: int = Field(0, ge=0, le=500)
    
    # Flags
    quick_pay_available: bool = False
    drop_and_hook: bool = False
    high_dwell_risk: bool = False
    flags: List[str] = Field(default_factory=list)


class LoadSearchOutput(BaseModel):
    """Validated load board search result."""
    
    status: Literal[
        "LOADS_FOUND",    # At least 1 eligible load found
        "NO_LOADS",       # No loads passed eligibility gates
        "POSTING_TRUCK",  # No loads — truck posted on DAT
        "SEARCH_FAILED",  # API error
    ]
    
    eligible_loads: List[LoadCandidate] = Field(default_factory=list)
    rejected_count: int = 0
    search_radius_miles: int = Field(ge=25, le=500)
    carrier_id: str
    
    # Rejection breakdown
    hard_rejected: int = Field(0, description="Failed equipment/weight/hazmat checks")
    timing_rejected: int = Field(0, description="Failed HOS/appointment checks")
    rate_rejected: int = Field(0, description="Rate below carrier floor")
    credit_rejected: int = Field(0, description="Broker credit too low")


# ============================================================
# RATE INTELLIGENCE OUTPUT
# Validates DAT market data and negotiation calculations.
# ============================================================

class RateIntelOutput(BaseModel):
    """Validated market rate intelligence for a lane."""
    
    lane: str = Field(description="Origin City, ST → Dest City, ST")
    equipment: str
    
    # Market data
    market_rate_7day: float = Field(ge=0.50, le=10.0, description="DAT 7-day average CPM")
    market_rate_30day: float = Field(ge=0.50, le=10.0, description="DAT 30-day average CPM")
    load_to_truck_ratio: float = Field(ge=0.0, le=20.0, description="Market tightness indicator")
    
    market_condition: Literal["VERY_TIGHT", "TIGHT", "BALANCED", "LOOSE"]
    trend: Literal["RISING", "STABLE", "FALLING"]
    
    # Negotiation targets
    anchor_rate: float = Field(ge=0.50, le=10.0, description="Opening offer (anchor high)")
    counter_rate: float = Field(ge=0.50, le=10.0, description="Counter when pushed back")
    walk_away_rate: float = Field(ge=0.50, le=10.0, description="Minimum acceptable rate")
    
    # Context for the call
    talking_points: str = Field(description="What the AI dispatcher should say to justify rate")
    
    # Internal history
    internal_best_rate: Optional[float] = Field(None, ge=0.0, le=10.0)
    internal_best_broker: Optional[str] = None
    
    @field_validator("anchor_rate", "counter_rate", "walk_away_rate", mode="after")
    @classmethod
    def validate_rate_order(cls, v, info):
        """Ensure anchor >= counter >= walk_away."""
        # This is simplified — full validation done in model_validator
        return v
    
    @model_validator(mode="after")
    def validate_rate_hierarchy(self):
        """Anchor must be >= counter >= walk_away."""
        if self.anchor_rate < self.counter_rate:
            raise ValueError(f"anchor_rate ({self.anchor_rate}) must be >= counter_rate ({self.counter_rate})")
        if self.counter_rate < self.walk_away_rate:
            raise ValueError(f"counter_rate ({self.counter_rate}) must be >= walk_away_rate ({self.walk_away_rate})")
        return self


# ============================================================
# RC REVIEW OUTPUT
# Validates the RC verification check results.
# ============================================================

class RCDiscrepancy(BaseModel):
    """A single field that doesn't match between RC and negotiated."""
    field: str
    rc_value: Any
    expected_value: Any
    severity: Literal["CRITICAL", "WARNING"]  # CRITICAL = don't sign; WARNING = note but sign


class RCReviewOutput(BaseModel):
    """Validated RC review result."""
    
    discrepancies: List[RCDiscrepancy] = Field(default_factory=list)
    all_critical_match: bool
    rate_verified: bool
    addresses_verified: bool
    accessorials_present: bool
    mc_number_correct: bool
    quality_score: float = Field(ge=0.0, le=1.0)
    
    @property
    def can_sign(self) -> bool:
        """Return True only if no CRITICAL discrepancies found."""
        critical = [d for d in self.discrepancies if d.severity == "CRITICAL"]
        return len(critical) == 0


# ============================================================
# DISPATCH OUTPUT
# Confirms dispatch sheet was sent successfully.
# ============================================================

class DispatchOutput(BaseModel):
    """Validated dispatch result."""
    
    load_id: str
    dispatch_sheet_sent: bool
    whatsapp_delivered: bool
    sms_delivered: bool
    broker_notified: bool
    awaiting_driver_ack: bool
    dispatch_timestamp: str
