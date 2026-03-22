"""PayU payment endpoints."""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.core.auth import UserContext, require_permission
from app.services.payu_service import PayUService

router = APIRouter(prefix="/payu", tags=["PayU"])
_svc = PayUService()


class CreateOrderIn(BaseModel):
    txnid: str
    amount: str
    productinfo: str
    firstname: str
    email: str
    surl: str
    furl: str


class VerifyResponseIn(BaseModel):
    status: str
    txnid: str
    amount: str
    productinfo: str
    firstname: str
    email: str
    response_hash: str
    udf1: str = ""
    udf2: str = ""
    udf3: str = ""
    udf4: str = ""
    udf5: str = ""


@router.post("/create-order")
async def create_payu_order(
    body: CreateOrderIn,
    user: UserContext = Depends(require_permission("payments.create")),
):
    result = _svc.generate_hash(
        txnid=body.txnid,
        amount=body.amount,
        productinfo=body.productinfo,
        firstname=body.firstname,
        email=body.email,
    )
    result["surl"] = body.surl
    result["furl"] = body.furl
    return result


@router.post("/verify-response")
async def verify_payu_response(body: VerifyResponseIn):
    is_valid = _svc.verify_response_hash(
        status=body.status,
        email=body.email,
        firstname=body.firstname,
        productinfo=body.productinfo,
        amount=body.amount,
        txnid=body.txnid,
        response_hash=body.response_hash,
        udf1=body.udf1,
        udf2=body.udf2,
        udf3=body.udf3,
        udf4=body.udf4,
        udf5=body.udf5,
    )
    return {"valid": is_valid, "txnid": body.txnid, "status": body.status}


@router.post("/callback")
async def payu_callback(request: Request):
    """PayU redirect callback — receives form-urlencoded POST, redirects to SPA."""
    form = await request.form()
    params = "&".join(f"{k}={v}" for k, v in form.items())
    # 302 redirect to frontend SPA with PayU data as query params
    frontend_url = form.get("udf1", "/payment-status")
    return Response(
        status_code=302,
        headers={"Location": f"{frontend_url}?{params}"},
    )
