"""
ElevenLabs Text-to-Speech — Service.

Purpose: Voice payment confirmation notifications in Hindi, English, Gujarati.
Uses eleven_multilingual_v2 model.
Returns raw audio/mpeg bytes.
"""
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"

# Webhook-pre-generated payment confirmation MP3s land here and are served
# publicly (token-gated by the unguessable razorpay_payment_id) via the
# voice.payment_audio route. The dir is created lazily on first write.
VOICE_FILES_DIR = Path(__file__).resolve().parents[2] / "files" / "voice"
VOICE_AUDIO_PATH_TEMPLATE = "/api/v1/voice/payment-audio/{token}.mp3"
# Backwards-compatible alias (some callers/tests import this name).
VOICE_AUDIO_URL_TEMPLATE = VOICE_AUDIO_PATH_TEMPLATE


def _public_audio_url(token: str) -> str:
    """Build the URL the frontend loads for a payment voice clip.

    Returns an ABSOLUTE url when ``PUBLIC_API_BASE_URL`` is configured so the
    FE can play it via ``new Audio(voice_url)`` even when it is served from a
    different origin (e.g. localhost:5173 or order.bittupos.com). Falls back
    to a relative path only when no base URL is set.
    """
    rel = VOICE_AUDIO_PATH_TEMPLATE.format(token=token)
    base = (getattr(_cfg(), "PUBLIC_API_BASE_URL", "") or "").rstrip("/")
    return f"{base}{rel}" if base else rel


def _cfg():
    return get_settings()


class ElevenLabsService:

    async def text_to_speech(
        self,
        text: str,
        voice_id: str | None = None,
        stability: float = 0.5,
        similarity_boost: float = 0.75,
    ) -> bytes:
        """
        Convert text to speech using ElevenLabs API.
        Returns audio/mpeg binary data.
        Supports Hindi, English, Gujarati via eleven_multilingual_v2.
        """
        s = _cfg()
        vid = voice_id or s.ELEVENLABS_VOICE_ID

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{ELEVENLABS_TTS_URL}/{vid}",
                json={
                    "text": text,
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {
                        "stability": stability,
                        "similarity_boost": similarity_boost,
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "xi-api-key": s.ELEVENLABS_API_KEY,
                },
            )
            resp.raise_for_status()

        logger.info("elevenlabs_tts_generated", chars=len(text))
        return resp.content

    async def payment_voice_notification(
        self,
        amount: float,
        language: str = "en",
    ) -> bytes:
        """Generate a payment confirmation voice notification."""
        # Render whole-rupee amounts as integers so the TTS engine doesn't
        # say "five point oh rupees" for ₹5.00.
        amt_f = float(amount)
        amt_str = str(int(amt_f)) if amt_f.is_integer() else f"{amt_f:.2f}"
        messages = {
            "hi": f"{amt_str} रुपये बिट्टू पर प्राप्त हुए। धन्यवाद।",
            "en": f"{amt_str} Rupees Received on Bittu. Thank You.",
            "gu": f"{amt_str} રૂપિયા બિટ્ટુ પર પ્રાપ્ત થયા. આભાર.",
        }
        text = messages.get(language, messages["en"])
        return await self.text_to_speech(text)

    async def ensure_payment_voice_file(
        self,
        *,
        token: str,
        amount: float,
        language: str = "en",
    ) -> str:
        """Generate (if missing) the payment confirmation MP3 for ``token``
        and return its public URL.

        ``token`` should be the ``razorpay_payment_id`` — unguessable and
        therefore safe to use as a capability key for the public audio
        endpoint. Returns an empty string on any failure (missing API key,
        TTS error, write error) so callers can ship a falsy ``voice_url``
        in the realtime event without breaking the webhook.
        """
        s = _cfg()
        if not s.ELEVENLABS_API_KEY:
            return ""
        safe = "".join(c for c in (token or "") if c.isalnum() or c in ("-", "_"))[:64]
        if not safe:
            return ""
        try:
            VOICE_FILES_DIR.mkdir(parents=True, exist_ok=True)
            target = VOICE_FILES_DIR / f"{safe}.mp3"
            if not target.exists():
                audio = await self.payment_voice_notification(
                    amount=amount, language=language,
                )
                tmp = target.with_suffix(".mp3.tmp")
                tmp.write_bytes(audio)
                tmp.replace(target)
            return _public_audio_url(safe)
        except Exception:  # noqa: BLE001
            logger.exception(
                "elevenlabs_payment_voice_file_failed",
                token=safe, amount=amount, language=language,
            )
            return ""
