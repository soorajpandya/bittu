from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

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
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

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

    # ── Cashfree Verification / KYC ──
    CF_VERIFY_CLIENT_ID: str = ""
    CF_VERIFY_CLIENT_SECRET: str = ""
    CF_VERIFY_PUBLIC_KEY: str = ""  # RSA public key PEM for x-cf-signature
    CF_VERIFY_MODE: str = "sandbox"  # sandbox | production

    # ── Cashfree 1-Click Onboarding (separate credentials) ──
    CF_ONECLICK_CLIENT_ID: str = ""
    CF_ONECLICK_CLIENT_SECRET: str = ""

    # ── Cashfree DigiLocker ──
    CF_DIGILOCKER_CLIENT_ID: str = ""
    CF_DIGILOCKER_CLIENT_SECRET: str = ""
    CF_DIGILOCKER_MODE: str = "sandbox"  # sandbox | live

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

    # ── Google Cloud Vision ──
    GOOGLE_VISION_API_KEY: str = ""

    # ── Google Business Profile ──
    GOOGLE_BUSINESS_CLIENT_ID: str = ""
    GOOGLE_BUSINESS_CLIENT_SECRET: str = ""
    GOOGLE_BUSINESS_REDIRECT_URI: str = "https://www.merabittu.com/google/callback"

    # ── Upstash Redis (REST) ──
    UPSTASH_REDIS_REST_URL: str = ""
    UPSTASH_REDIS_REST_TOKEN: str = ""

    # ── Rate Limiting ──
    RATE_LIMIT_PER_MINUTE: int = 1200  # Backend fallback; nginx enforces 300r/m at edge
    RATE_LIMIT_AUTH_PER_MINUTE: int = 60  # Stricter for auth endpoints

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
