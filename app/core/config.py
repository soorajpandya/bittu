from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # ── App ──
    APP_NAME: str = "BITTU"
    APP_ENV: str = "development"  # production | staging | development
    ENVIRONMENT: str = "development"  # alias used in main.py
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[str] = ["*"]
    ALLOWED_HOSTS: list[str] = ["*"]
    SECRET_KEY: str = ""  # Required in production for signing
    # Public, internet-reachable origin of THIS backend. Used to build
    # absolute URLs for assets the frontend loads directly (e.g. the
    # ElevenLabs payment-confirmation MP3 played via <audio src=...>).
    # Must be the API host, not the frontend host. Leave blank to emit
    # relative URLs (only works when FE and API share an origin).
    PUBLIC_API_BASE_URL: str = "https://api.bittupos.com"

    @field_validator("CORS_ORIGINS")
    @classmethod
    def validate_cors_origins(cls, v: list[str], info) -> list[str]:
        env = info.data.get("APP_ENV", "development")
        if env == "production" and "*" in v:
            raise ValueError("Wildcard CORS origin ('*') is not allowed in production")
        return v

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    # ── Supabase ──
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: str
    SUPABASE_SERVICE_ROLE_KEY: str
    SUPABASE_JWT_SECRET: str

    # ── Database ──
    DATABASE_URL: str  # pooled connection via PgBouncer
    DATABASE_DIRECT_URL: str  # direct for migrations
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 300
    DB_STATEMENT_TIMEOUT: int = 30000  # 30s in ms

    # ── Redis ──
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_MAX_CONNECTIONS: int = 50

    # ── Razorpay ──
    # No hardcoded defaults — credentials MUST come from environment (.env in
    # local, systemd EnvironmentFile in prod). Hardcoding live keys here would
    # leak them via the repo and prevent rotation.
    RAZORPAY_KEY_ID: str
    RAZORPAY_KEY_SECRET: str
    RAZORPAY_WEBHOOK_SECRET: str

    # ── Onboarding SaaS subscriptions (Razorpay Subscriptions) ──
    # Razorpay Plan ids backing each recurring "Software" plan. These are
    # created once in the Razorpay dashboard (Subscriptions → Plans). The
    # backend creates a per-merchant subscription against the matching plan
    # id when the merchant picks starter/business. growth/enterprise have a
    # ₹0 subscription (integrated-payments revenue model) so no plan id.
    # Defaults point at the existing ₹5,000/yr plan; override per-tier via env.
    RZP_PLAN_ID_STARTER: str = "plan_T2h5aH7NNRKuLt"
    RZP_PLAN_ID_BUSINESS: str = "plan_T2h5aH7NNRKuLt"
    # Number of billing cycles Razorpay should schedule for a new subscription.
    RZP_SUBSCRIPTION_TOTAL_COUNT: int = 100
    # One-time device fee (paise) for business/enterprise. Informational here;
    # collected as a separate one-time order, NOT part of the subscription gate.
    RZP_DEVICE_FEE_PAISE: int = 3000000  # ₹30,000
    # GST rate (percent) added on top of GST-exclusive onboarding prices
    # (software subscription + one-time device fee). 18% standard SaaS/goods.
    ONBOARDING_GST_PERCENT: float = 18.0

    # ── PhonePe ──
    PHONEPE_CLIENT_ID: str = ""
    PHONEPE_CLIENT_SECRET: str = ""
    PHONEPE_CLIENT_VERSION: str = "1"   
    PHONEPE_MODE: str = "sandbox"  # sandbox | live

    # ── PayU ──
    PAYU_MERCHANT_KEY: str = ""
    PAYU_MERCHANT_SALT: str = ""
    PAYU_MODE: str = "test"  # test | live

    # ── Paytm ──
    PAYTM_MID: str = ""
    PAYTM_MERCHANT_KEY: str = ""
    PAYTM_WEBSITE: str = "WEBSTAGING"
    PAYTM_MODE: str = "test"  # test | live

    # ── Cashfree PG ──
    CASHFREE_APP_ID: str = ""
    CASHFREE_SECRET_KEY: str = ""
    CASHFREE_MODE: str = "sandbox"  # sandbox | live

    # ── Cashfree 1-Click Onboarding ──
    CF_ONECLICK_CLIENT_ID: str = ""
    CF_ONECLICK_CLIENT_SECRET: str = ""

    # ── Zivonpay ──
    ZIVONPAY_MERCHANT_ID: str = ""
    ZIVONPAY_API_KEY: str = ""
    ZIVONPAY_API_SECRET: str = ""
    ZIVONPAY_MODE: str = "sandbox"  # sandbox | live

    # ── ElevenLabs TTS ──
    ELEVENLABS_API_KEY: str = ""
    ELEVENLABS_VOICE_ID: str = "xoV6iGVuOGYHLWjXhVC7"

    # ── OpenAI ──
    OPENAI_API_KEY: str = ""

    # ── Bittu AI (business assistant) ──
    # Reuses OPENAI_API_KEY. Cheap text model by default; tool-calling capable.
    BITTU_AI_MODEL: str = "gpt-4o-mini"
    BITTU_AI_ENABLED: bool = True

    # ── Google Cloud Vision ──
    GOOGLE_VISION_API_KEY: str = ""

    # ── Google Business Profile ──
    GOOGLE_BUSINESS_CLIENT_ID: str = ""
    GOOGLE_BUSINESS_CLIENT_SECRET: str = ""
    GOOGLE_BUSINESS_REDIRECT_URI: str = "https://www.bittupos.com/google/callback"

    # ── Attestr (KYC / FSSAI / GSTIN verification) ──
    # Pre-base64-encoded "client_id:client_secret" string from the Attestr
    # dashboard. Sent verbatim as `Authorization: Basic <token>`.
    ATTESTR_AUTH_TOKEN: str = ""
    ATTESTR_BASE_URL: str = "https://api.attestr.com"
    ATTESTR_TIMEOUT_SECONDS: float = 20.0

    # ── Upstash Redis (REST) ──
    UPSTASH_REDIS_REST_URL: str = ""
    UPSTASH_REDIS_REST_TOKEN: str = ""

    # ── Rate Limiting ──
    RATE_LIMIT_PER_MINUTE: int = 1200  # Backend fallback; nginx enforces 300r/m at edge
    RATE_LIMIT_AUTH_PER_MINUTE: int = 60  # Stricter for auth endpoints

    # ── Request signing (HMAC) ──
    # off     — middleware disabled (legacy / smoke)
    # monitor — verify + log failures, never reject (safe rollout default)
    # enforce — reject unsigned / tampered / replayed requests with 401
    REQUEST_SIGNING_MODE: str = "monitor"
    # TTL for per-(user,device) HMAC keys cached in Redis. Rotated on every
    # refresh; expiry is a defence-in-depth fallback only.
    SESSION_SIGNING_KEY_TTL_SECONDS: int = 30 * 24 * 3600

    # ── Deployment ──
    WORKERS: int = 4
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30  # seconds
    TRUSTED_PROXIES: list[str] = ["127.0.0.1"]
    RATE_LIMIT_BURST: int = 30

    # ── WebSocket ──
    WS_HEARTBEAT_INTERVAL: int = 30
    WS_MAX_CONNECTIONS_PER_RESTAURANT: int = 50

    # ── Business Rules ──
    ORDER_TIMEOUT_MINUTES: int = 30
    SESSION_TIMEOUT_MINUTES: int = 60
    PAYMENT_VERIFICATION_TIMEOUT: int = 60
    MAX_RETRY_PAYMENT_WEBHOOK: int = 3

@lru_cache()
def get_settings() -> Settings:
    return Settings()
