"""ElevenLabs TTS voice notification endpoints."""
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.elevenlabs_service import ElevenLabsService

router = APIRouter(prefix="/voice", tags=["Voice / TTS"])
_svc = ElevenLabsService()


class TTSIn(BaseModel):
    text: str
    voice_id: str | None = None
    stability: float = 0.5
    similarity_boost: float = 0.75


class PaymentVoiceIn(BaseModel):
    amount: float
    language: str = "en"  # en | hi | gu


@router.post("/tts", response_class=Response)
async def text_to_speech(
    body: TTSIn,
    user: UserContext = Depends(require_permission("voice.use")),
):
    audio = await _svc.text_to_speech(
        text=body.text,
        voice_id=body.voice_id,
        stability=body.stability,
        similarity_boost=body.similarity_boost,
    )
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/payment-notification", response_class=Response)
async def payment_voice_notification(
    body: PaymentVoiceIn,
    user: UserContext = Depends(require_permission("voice.use")),
):
    audio = await _svc.payment_voice_notification(
        amount=body.amount,
        language=body.language,
    )
    return Response(content=audio, media_type="audio/mpeg")
