"""
cortexbot/config.py — Phase 2 Complete

All configuration loaded from environment variables (.env.local).
Phase 2 adds: Stripe, EFS/Comdata, QuickBooks, Highway.com, Samsara, Motive.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── App ─────────────────────────────────────────────────
    app_name: str = "CortexBot"
    app_version: str = "2.0.0"
    environment: str = "development"
    base_url: str = "http://localhost:8000"

    # ── Database ─────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://cortex:cortex@postgres:5432/cortexbot"
    database_url_sync: str = "postgresql://cortex:cortex@postgres:5432/cortexbot"

    # ── Redis ─────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"

    # ── Security ─────────────────────────────────────────────
    jwt_secret: str = "change-this-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24

    # ── AI Models ─────────────────────────────────────────────
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # ── Bland AI (Phone Calls) ────────────────────────────────
    bland_ai_api_key: str = ""
    bland_ai_caller_id: str = ""
    bland_ai_base_url: str = "https://api.bland.ai/v1"

    # ── Twilio (WhatsApp + SMS) ───────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = ""
    twilio_sms_number: str = ""

    # ── SendGrid (Email) ──────────────────────────────────────
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = "dispatch@cortexbot.com"
    sendgrid_from_name: str = "CortexBot Dispatch"
    sendgrid_inbound_domain: str = ""

    # ── DAT API (Load Board + Rate Data) ─────────────────────
    dat_client_id: str = ""
    dat_client_secret: str = ""
    dat_base_url: str = "https://identity.dat.com/access/v1"
    dat_loads_url: str = "https://freight.api.dat.com/loads/v2"
    dat_rates_url: str = "https://rates.api.dat.com/rate-view/v1"

    # ── FMCSA ─────────────────────────────────────────────────
    fmcsa_api_key: str = ""
    fmcsa_base_url: str = "https://mobile.fmcsa.dot.gov/qc/services/carriers"

    # ── Highway.com (Fraud / Cargo Theft) ────────────────────
    highway_api_key: str = ""
    highway_api_base_url: str = "https://api.usehighway.com/v1"

    # ── AWS ────────────────────────────────────────────────────
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    aws_s3_bucket: str = "cortexbot-docs"

    # ── DocuSign ───────────────────────────────────────────────
    docusign_integration_key: str = ""
    docusign_secret_key: str = ""
    docusign_account_id: str = ""
    docusign_base_url: str = "https://demo.docusign.net/restapi"

    # ── Google Maps ───────────────────────────────────────────
    google_maps_api_key: str = ""

    # ── ELD Providers ─────────────────────────────────────────
    samsara_api_key: str = ""
    samsara_base_url: str = "https://api.samsara.com/v1"
    motive_api_key: str = ""
    motive_base_url: str = "https://api.keeptruckin.com/v1"

    # ── Stripe (Driver Settlement Payments) ───────────────────
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""

    # ── Fuel Card APIs ────────────────────────────────────────
    efs_api_key: str = ""
    efs_api_base_url: str = "https://api.efspay.com/v1"
    comdata_api_key: str = ""
    comdata_api_base_url: str = "https://api.comdata.com/v1"

    # ── QuickBooks Online ─────────────────────────────────────
    quickbooks_client_id: str = ""
    quickbooks_client_secret: str = ""
    quickbooks_company_id: str = ""
    quickbooks_base_url: str = "https://quickbooks.api.intuit.com/v3"

    # ── NOAA/Weather ─────────────────────────────────────────
    noaa_api_base_url: str = "https://api.weather.gov"

    # ── Escalation ────────────────────────────────────────────
    oncall_phone: str = ""
    oncall_email: str = ""

    # ── ngrok (Dev webhooks) ──────────────────────────────────
    ngrok_token: str = ""

    # ── Rate Limits & Cache TTLs ──────────────────────────────
    dat_rate_cache_ttl_seconds: int = 900
    fmcsa_cache_ttl_seconds: int = 86400
    highway_cache_ttl_seconds: int = 3600
    whatsapp_context_ttl_seconds: int = 86400
    eld_gps_cache_ttl_seconds: int = 60
    eld_hos_cache_ttl_seconds: int = 300

    # ── Dispatch Timings ──────────────────────────────────────
    carrier_confirmation_timeout_secs: int = 90
    load_search_radius_miles: int = 100
    load_search_max_radius_miles: int = 200
    detention_alert_advance_minutes: int = 15
    gps_check_interval_minutes: int = 15
    weather_check_interval_minutes: int = 30

    # ── Financial Defaults ────────────────────────────────────
    default_dispatch_fee_pct: float = 0.060   # 6%
    default_detention_free_hours: int = 2
    max_fuel_advance: float = 400.0
    max_lumper_advance: float = 300.0
    max_emergency_advance: float = 500.0
    max_cash_advance: float = 200.0

    class Config:
        env_file = ".env.local"
        case_sensitive = False
        extra = "ignore"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    # ── Computed Webhook URLs ─────────────────────────────────
    @property
    def bland_ai_webhook_url(self) -> str:
        return f"{self.base_url}/webhooks/bland/call-complete"

    @property
    def twilio_webhook_url(self) -> str:
        return f"{self.base_url}/webhooks/twilio/whatsapp"

    @property
    def sendgrid_webhook_url(self) -> str:
        return f"{self.base_url}/webhooks/sendgrid/inbound"

    @property
    def dat_rate_injection_url(self) -> str:
        return f"{self.base_url}/internal/rate-data"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
