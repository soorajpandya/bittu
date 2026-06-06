"""ElevenLabs TTS voice notification endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.elevenlabs_service import (
    ElevenLabsService,
    VOICE_FILES_DIR,
)

router = APIRouter(prefix="/voice", tags=["Restaurant Settings"])
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


# ── Public, token-gated audio playback ────────────────────────────────────
# The ``token`` is the unguessable Razorpay payment id (``pay_xxx``). The
# MP3 is pre-generated server-side during the captured-payment webhook so
# the FE can play it directly via ``<audio src="...">`` without sending an
# Authorization header (which avoids browser autoplay/CORS friction).
@router.get(
    "/payment-audio/{token}.mp3",
    response_class=FileResponse,
    include_in_schema=False,
)
async def payment_audio(token: str):
    safe = "".join(c for c in (token or "") if c.isalnum() or c in ("-", "_"))[:64]
    if not safe:
        raise HTTPException(status_code=404, detail="not_found")
    path = VOICE_FILES_DIR / f"{safe}.mp3"
    if not path.exists():
        raise HTTPException(status_code=404, detail="not_found")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
