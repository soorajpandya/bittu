"""
Razorpay QR Codes API service (Phase 2 fills mapping + status APIs).

Phase 1 surface: REST wrappers around `/v1/payments/qr_codes`.
"""
from __future__ import annotations

import base64
import io
from decimal import Decimal
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import qrcode
from PIL import Image

from app.services.razorpay.client import get_razorpay_client


async def create_qr(
    *,
    name: str,
    amount_paise: Optional[int],
    description: str = "",
    fixed_amount: bool = True,
    usage: str = "single_use",
    qr_type: str = "upi_qr",
    close_by: Optional[int] = None,
    customer_id: Optional[str] = None,
    notes: Optional[Mapping[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> dict:
    body: dict[str, Any] = {
        "type": qr_type,
        "name": name,
        "usage": usage,
        "fixed_amount": fixed_amount,
        "description": description,
    }
    if fixed_amount and amount_paise is not None:
        body["payment_amount"] = int(amount_paise)
    if close_by:
        body["close_by"] = int(close_by)
    if customer_id:
        body["customer_id"] = customer_id
    if notes:
        body["notes"] = dict(notes)

    client = await get_razorpay_client()
    return await client.post(
        "/v1/payments/qr_codes",
        operation="qr.create",
        json_body=body,
        idempotency_key=idempotency_key,
        merchant_id=merchant_id,
    )


async def fetch_qr(qr_id: str, *, merchant_id: Optional[str] = None) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/payments/qr_codes/{qr_id}",
        operation="qr.fetch",
        merchant_id=merchant_id,
    )


async def list_qr(
    *,
    count: int = 25,
    skip: int = 0,
    merchant_id: Optional[str] = None,
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        "/v1/payments/qr_codes",
        operation="qr.list",
        params={"count": count, "skip": skip},
        merchant_id=merchant_id,
    )


async def fetch_qr_payments(
    qr_id: str, *, merchant_id: Optional[str] = None
) -> dict:
    client = await get_razorpay_client()
    return await client.get(
        f"/v1/payments/qr_codes/{qr_id}/payments",
        operation="qr.payments",
        merchant_id=merchant_id,
    )


async def close_qr(qr_id: str, *, merchant_id: Optional[str] = None) -> dict:
    client = await get_razorpay_client()
    return await client.post(
        f"/v1/payments/qr_codes/{qr_id}/close",
        operation="qr.close",
        json_body={},
        merchant_id=merchant_id,
    )


def build_upi_intent_for_pos(
    *,
    base_upi_intent: str,
    fixed_amount: bool,
    payment_amount_paise: Optional[int],
    payer_name: Optional[str] = "Bittu POS",
) -> str:
    """Normalize a UPI intent for POS rendering.

    - Keeps original routing/tracking params like `pa`, `tr`, `tn`.
    - If `fixed_amount=True`, sets `am` from paise.
    - If `fixed_amount=False`, removes `am` so customer enters amount.
    """
    if not base_upi_intent:
        raise ValueError("base_upi_intent is required")

    parts = urlsplit(base_upi_intent)
    if parts.scheme.lower() != "upi" or parts.netloc.lower() != "pay":
        raise ValueError("base_upi_intent must start with upi://pay")

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("cu", "INR")
    if payer_name:
        query["pn"] = payer_name

    if fixed_amount:
        if payment_amount_paise is None:
            raise ValueError("payment_amount_paise is required when fixed_amount=True")
        amount_rupees = (Decimal(int(payment_amount_paise)) / Decimal("100")).normalize()
        query["am"] = format(amount_rupees, "f")
    else:
        query.pop("am", None)

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _decode_qr_payload_from_image_bytes(image_bytes: bytes) -> Optional[str]:
    """Best-effort QR decode from image bytes.

    Tries ZXing first (more robust on stylized QR images), then OpenCV.
    Kept optional so deployments without these dependencies keep running.
    """
    try:
        import zxingcpp  # type: ignore

        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        results = zxingcpp.read_barcodes(pil_img)
        if results:
            txt = (results[0].text or "").strip()
            if txt:
                return txt
    except Exception:
        pass

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return None

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    detector = cv2.QRCodeDetector()
    data, _points, _straight = detector.detectAndDecode(img)
    if not data:
        return None
    return data.strip()


async def extract_upi_intent_from_image_url(
    *,
    image_url: str,
    fixed_amount: Optional[bool] = None,
    payment_amount_paise: Optional[int] = None,
    qr_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    payer_name: Optional[str] = "Bittu POS",
) -> dict[str, Any]:
    """Extract + normalize UPI intent from a hosted QR image URL.

    Primary path decodes the downloaded QR image.
    Fallback path (when decode is unavailable/failed) uses Razorpay QR fetch
    if `qr_id` is provided and `image_content` exists.
    """
    if not image_url:
        raise ValueError("image_url is required")

    raw_intent: Optional[str] = None
    decode_source = "image_decode"

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(image_url)
        resp.raise_for_status()
        raw_intent = _decode_qr_payload_from_image_bytes(resp.content)

    fetched_qr: Optional[dict[str, Any]] = None
    if not raw_intent and qr_id:
        fetched_qr = await fetch_qr(qr_id, merchant_id=merchant_id)
        raw_intent = fetched_qr.get("image_content")
        decode_source = "razorpay_fetch"

    if not raw_intent:
        raise ValueError(
            "Could not decode UPI intent from image_url. "
            "Install OpenCV for image decoding or pass qr_id for fetch fallback."
        )

    if fixed_amount is None:
        if fetched_qr is None and qr_id:
            fetched_qr = await fetch_qr(qr_id, merchant_id=merchant_id)
        if fetched_qr is not None:
            fixed_amount = bool(fetched_qr.get("fixed_amount"))
            if payment_amount_paise is None and fetched_qr.get("payment_amount") is not None:
                payment_amount_paise = int(fetched_qr.get("payment_amount"))
        else:
            fixed_amount = False

    if bool(fixed_amount) and payment_amount_paise is None:
        # Preserve decoded payload when amount metadata is unavailable.
        final_intent = raw_intent
    else:
        final_intent = build_upi_intent_for_pos(
            base_upi_intent=raw_intent,
            fixed_amount=bool(fixed_amount),
            payment_amount_paise=payment_amount_paise,
            payer_name=payer_name,
        )

    return {
        "source": decode_source,
        "raw_upi_intent": raw_intent,
        "upi_intent": final_intent,
        "fixed_amount": bool(fixed_amount),
        "payment_amount_paise": payment_amount_paise,
        "qr_id": qr_id,
        "merchant_id": merchant_id,
    }


async def resolve_upi_intent_for_qr(
    *,
    upi_intent: Optional[str],
    image_url: Optional[str],
    qr_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    fixed_amount: Optional[bool] = None,
    payment_amount_paise: Optional[int] = None,
    payer_name: Optional[str] = "Bittu POS",
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a usable UPI intent from existing fields or image decode."""
    if upi_intent and upi_intent.startswith("upi://pay"):
        return upi_intent, "existing"

    if not image_url:
        return None, None

    try:
        extracted = await extract_upi_intent_from_image_url(
            image_url=image_url,
            fixed_amount=fixed_amount,
            payment_amount_paise=payment_amount_paise,
            qr_id=qr_id,
            merchant_id=merchant_id,
            payer_name=payer_name,
        )
    except Exception:
        return None, None

    final_intent = extracted.get("upi_intent")
    if not final_intent or not str(final_intent).startswith("upi://pay"):
        return None, None
    return str(final_intent), str(extracted.get("source") or "image_decode")


def generate_qr_image_bytes_from_upi_intent(
    upi_intent: str,
    *,
    box_size: int = 12,
    border: int = 2,
) -> bytes:
    """Generate PNG QR bytes from a UPI intent string."""
    if not upi_intent or not upi_intent.startswith("upi://pay"):
        raise ValueError("upi_intent must start with upi://pay")

    return generate_qr_image_bytes_from_payload(
        upi_intent,
        box_size=box_size,
        border=border,
    )


def generate_qr_image_bytes_from_payload(
    payload: str,
    *,
    box_size: int = 12,
    border: int = 2,
) -> bytes:
    """Generate PNG QR bytes from any payload string."""
    if not payload:
        raise ValueError("payload is required")

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    if not isinstance(img, Image.Image):
        img = img.get_image()

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate_qr_data_url_from_upi_intent(upi_intent: str) -> str:
    """Generate a browser-friendly data URL for a UPI QR image."""
    png = generate_qr_image_bytes_from_upi_intent(upi_intent)
    b64 = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{b64}"


def generate_qr_data_url_from_payload(payload: str) -> str:
    """Generate a browser-friendly data URL for any QR payload."""
    png = generate_qr_image_bytes_from_payload(payload)
    b64 = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{b64}"


def prefer_bittu_qr_image_url(
    *,
    upi_intent: Optional[str],
    razorpay_image_url: Optional[str],
) -> Optional[str]:
    """Return a Bittu-generated QR data URL when possible.

    Falls back to Razorpay's hosted QR URL if the UPI intent is unavailable
    or invalid.
    """
    if upi_intent and upi_intent.startswith("upi://pay"):
        try:
            return generate_qr_data_url_from_upi_intent(upi_intent)
        except Exception:
            return razorpay_image_url
    return razorpay_image_url
