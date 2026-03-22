"""
Unit tests for configuration.
"""
import os
import pytest
from unittest.mock import patch


class TestSettings:
    def test_is_production(self):
        from app.core.config import Settings
        s = Settings(
            APP_ENV="production",
            CORS_ORIGINS=["https://app.bittu.com"],
            SUPABASE_URL="https://x.supabase.co",
            SUPABASE_ANON_KEY="test",
            SUPABASE_SERVICE_ROLE_KEY="test",
            SUPABASE_JWT_SECRET="test",
            DATABASE_URL="postgresql://localhost/test",
            DATABASE_DIRECT_URL="postgresql://localhost/test",
        )
        assert s.is_production is True
        assert s.is_development is False

    def test_is_development(self):
        from app.core.config import Settings
        s = Settings(
            APP_ENV="development",
            SUPABASE_URL="https://x.supabase.co",
            SUPABASE_ANON_KEY="test",
            SUPABASE_SERVICE_ROLE_KEY="test",
            SUPABASE_JWT_SECRET="test",
            DATABASE_URL="postgresql://localhost/test",
            DATABASE_DIRECT_URL="postgresql://localhost/test",
        )
        assert s.is_development is True

    def test_production_rejects_wildcard_cors(self):
        from app.core.config import Settings
        with pytest.raises(Exception):
            Settings(
                APP_ENV="production",
                CORS_ORIGINS=["*"],
                SUPABASE_URL="https://x.supabase.co",
                SUPABASE_ANON_KEY="test",
                SUPABASE_SERVICE_ROLE_KEY="test",
                SUPABASE_JWT_SECRET="test",
                DATABASE_URL="postgresql://localhost/test",
                DATABASE_DIRECT_URL="postgresql://localhost/test",
            )
