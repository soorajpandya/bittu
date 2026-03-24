"""AI Menu Scanner endpoints — GPT-4o Vision + Google OCR."""
import base64

from fastapi import APIRouter, Depends, UploadFile, File
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.menu_scanner_service import MenuScannerService

router = APIRouter(prefix="/menu-scan", tags=["AI Menu Scanner"])
_svc = MenuScannerService()


class ScanBase64In(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"


@router.post("/ai-menu-scan")
async def ai_menu_scan(
    body: ScanBase64In,
    user: UserContext = Depends(require_permission("menu.manage")),
):
    """AI menu scan — primary endpoint used by frontend."""
    return await _svc.scan_menu_image(body.image_base64, body.mime_type)


@router.post("/base64")
async def scan_menu_base64(
    body: ScanBase64In,
    user: UserContext = Depends(require_permission("menu.manage")),
):
    """Scan a menu image provided as base64 string."""
    return await _svc.scan_menu_image(body.image_base64, body.mime_type)


@router.post("/upload")
async def scan_menu_upload(
    file: UploadFile = File(...),
    user: UserContext = Depends(require_permission("menu.manage")),
):
    """Scan a menu image uploaded as multipart file."""
    content = await file.read()
    image_b64 = base64.b64encode(content).decode()
    mime = file.content_type or "image/jpeg"
    return await _svc.scan_menu_image(image_b64, mime)
