"""
ElevenLabs Text-to-Speech — Service.

Purpose: Voice payment confirmation notifications in Hindi, English, Gujarati.
Uses eleven_multilingual_v2 model.
Returns raw audio/mpeg bytes.
"""
import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"


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
        messages = {
            "hi": f"पेमेंट सफल। {amount} रुपये प्राप्त हुए।",
            "en": f"Payment successful. {amount} rupees received.",
            "gu": f"ચુકવણી સફળ. {amount} રૂપિયા પ્રાપ્ત થયા.",
        }
        text = messages.get(language, messages["en"])
        return await self.text_to_speech(text)
