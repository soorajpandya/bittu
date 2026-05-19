"""
Waitlist API endpoints.

POST   /waitlist                — add customer to waitlist
GET    /waitlist                — get current queue
GET    /waitlist/stats          — today's stats
GET    /waitlist/history        — historical entries
POST   /waitlist/notify-next    — notify best-fit customer
POST   /waitlist/{id}/seat      — seat a customer
POST   /waitlist/{id}/skip      — skip a customer
PATCH  /waitlist/{id}/cancel    — cancel an entry
PUT    /waitlist/reorder        — admin reorder queue
GET    /waitlist/settings       — get waitlist settings
PUT    /waitlist/settings       — update waitlist settings
GET    /waitlist/display/{rid}  — public display screen data
GET    /waitlist/status/{id}    — public entry status (QR customer)
"""
from pathlib import Path
from typing import Optional
from uuid import UUID
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route, invalidate_prefix
from app.core.database import get_connection
from app.services.waitlist_service import WaitlistService

router = APIRouter(prefix="/waitlist", tags=["Dine-In"])
_svc = WaitlistService()
_CACHE_PREFIX = "waitlist"

# Brand assets shipped in backend/files/ — served via /waitlist/assets/{name}
_ASSETS_DIR = Path(__file__).resolve().parents[3] / "files"
_ASSET_MAP = {
    "logo.png":            ("bittu-logo.png",       "image/png"),
    "ding.mp3":            ("ding.mp3",             "audio/mpeg"),
    "gilroy-light.otf":    ("Gilroy-Light.otf",     "font/otf"),
    "gilroy-extrabold.otf":("Gilroy-ExtraBold.otf", "font/otf"),
}


# ── Request / Response models ─────────────────────────────────

class AddEntryRequest(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=100)
    party_size: int = Field(..., ge=1, le=50)
    phone: Optional[str] = Field(None, max_length=20)
    source: str = Field("staff", pattern=r"^(staff|qr)$")
    notes: Optional[str] = Field(None, max_length=500)


class ReorderRequest(BaseModel):
    ordered_ids: list[UUID] = Field(..., min_length=1)


class SettingsUpdate(BaseModel):
    notify_expiry_minutes: Optional[int] = Field(None, ge=1, le=60)
    avg_turnover_minutes: Optional[int] = Field(None, ge=5, le=180)
    sms_enabled: Optional[bool] = None
    whatsapp_enabled: Optional[bool] = None
    display_screen_enabled: Optional[bool] = None
    qr_entry_enabled: Optional[bool] = None
    auto_notify: Optional[bool] = None
    best_fit_enabled: Optional[bool] = None
    display_message: Optional[str] = Field(None, max_length=200)


class NotifyNextRequest(BaseModel):
    table_id: Optional[UUID] = None


class PublicAddEntryRequest(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=100)
    party_size: int = Field(..., ge=1, le=50)
    phone: str = Field(..., min_length=6, max_length=20)
    notes: Optional[str] = Field(None, max_length=500)


# ── Authenticated endpoints ──────────────────────────────────

@router.post("")
async def add_to_waitlist(
    body: AddEntryRequest,
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Add a customer to the waitlist."""
    result = await _svc.add_entry(
        user,
        customer_name=body.customer_name,
        party_size=body.party_size,
        phone=body.phone,
        source=body.source,
        notes=body.notes,
    )
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.get("")
@cached_route(prefix=_CACHE_PREFIX, ttl=10)
async def get_queue(
    status: Optional[str] = Query(None, pattern=r"^(waiting|notified|seated|skipped|cancelled)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Get current waitlist queue."""
    return await _svc.get_queue(user, status=status, limit=limit, offset=offset)


@router.get("/stats")
@cached_route(prefix=_CACHE_PREFIX, ttl=30)
async def get_stats(
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Today's waitlist statistics."""
    return await _svc.get_stats(user)


@router.get("/history")
@cached_route(prefix=_CACHE_PREFIX, ttl=60)
async def get_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Get waitlist history."""
    return await _svc.get_history(user, limit=limit, offset=offset,
                                   date_from=date_from, date_to=date_to)


@router.post("/notify-next")
async def notify_next(
    body: NotifyNextRequest = NotifyNextRequest(),
    user: UserContext = Depends(require_permission("waitlist.manage")),
):
    """Find best-fit customer for available table and notify them.
    Falls back to notifying the next waiter without a table assignment if
    no tables are currently free (or none are configured)."""
    result = await _svc.notify_next(user, table_id=body.table_id)
    if not result:
        raise HTTPException(404, "No customers waiting on the queue")
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.post("/expire-check")
async def expire_check(
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Check and expire overdue notified entries."""
    expired = await _svc.expire_overdue(user)
    if expired:
        await invalidate_prefix(_CACHE_PREFIX, user)
    return {"expired": expired}


class SeatRequest(BaseModel):
    table_id: Optional[UUID] = None
    guest_count: Optional[int] = Field(None, ge=1, le=50)


@router.post("/{entry_id}/seat")
async def seat_customer(
    entry_id: UUID,
    body: Optional[SeatRequest] = None,
    user: UserContext = Depends(require_permission("waitlist.manage")),
):
    """Seat a waitlisted customer. Optionally pass table_id to assign a table
    on the fly (when caller skipped notify-next). Also opens a dine-in session
    so the table appears as occupied on the Table Orders screen."""
    table_id = str(body.table_id) if body and body.table_id else None
    guest_count = body.guest_count if body and body.guest_count else None
    result = await _svc.seat_customer(user, entry_id, table_id=table_id, guest_count=guest_count)
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.post("/{entry_id}/skip")
async def skip_customer(
    entry_id: UUID,
    reason: str = Query("no_show"),
    user: UserContext = Depends(require_permission("waitlist.manage")),
):
    """Skip a waitlisted customer (no-show or manual)."""
    result = await _svc.skip_customer(user, entry_id, reason=reason)
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.patch("/{entry_id}/cancel")
async def cancel_entry(
    entry_id: UUID,
    user: UserContext = Depends(require_permission("waitlist.read")),
):
    """Cancel a waitlist entry."""
    result = await _svc.cancel_entry(user, entry_id)
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.put("/reorder")
async def reorder_queue(
    body: ReorderRequest,
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Admin reorder the waitlist queue."""
    result = await _svc.reorder(user, body.ordered_ids)
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


@router.get("/settings")
@cached_route(prefix=_CACHE_PREFIX, ttl=300)
async def get_settings(
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Get waitlist settings."""
    return await _svc.get_settings(user)


@router.put("/settings")
async def update_settings(
    body: SettingsUpdate,
    user: UserContext = Depends(require_permission("waitlist.admin")),
):
    """Update waitlist settings."""
    result = await _svc.update_settings(user, body.model_dump(exclude_none=True))
    await invalidate_prefix(_CACHE_PREFIX, user)
    return result


# ── Public endpoints (no auth) ───────────────────────────────

@router.post("/public/{restaurant_id}")
async def add_to_waitlist_public(restaurant_id: UUID, body: PublicAddEntryRequest):
    """
    Public QR self-add. No auth — rate-limited per client IP by middleware.
    Honors per-restaurant `qr_entry_enabled` setting.
    """
    result = await _svc.add_entry_public(
        restaurant_id=restaurant_id,
        customer_name=body.customer_name,
        party_size=body.party_size,
        phone=body.phone,
        notes=body.notes,
    )
    # Staff queue cache is tenant-scoped and has a 10s TTL; no cross-tenant
    # invalidation needed here — staff sees the new entry within one cycle.
    return result


@router.get("/display/{restaurant_id}")
async def display_screen(restaurant_id: UUID):
    """Public display screen data — shows 'now serving' and queue."""
    return await _svc.get_display_data(restaurant_id)


@router.get("/status/{entry_id}")
async def entry_status(entry_id: UUID):
    """Public entry status — for QR customer to check their position."""
    result = await _svc.get_entry_status(entry_id)
    if not result:
        raise HTTPException(404, "Entry not found")
    return result


# ── QR code + customer-facing landing page (public, no auth) ─

def _public_base(request: Request) -> str:
    """Public scheme://host derived from forwarded headers (nginx-aware)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@router.get("/qr/{restaurant_id}.png")
async def qr_png(restaurant_id: UUID, request: Request):
    """
    Returns a printable PNG QR code that points at the customer self-add page
    for this restaurant. Print this and stick it on the host stand.
    """
    try:
        import qrcode
    except ImportError:  # pragma: no cover
        raise HTTPException(500, "qrcode library not installed on server")

    target = f"{_public_base(request)}/q/{restaurant_id}"
    img = qrcode.make(target, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/qr/{restaurant_id}", response_class=HTMLResponse)
async def qr_landing_page(restaurant_id: UUID):
    """
    Public landing page customers reach by scanning the printed QR code.
    Self-contained HTML — posts to /api/v1/waitlist/public/{rid} and then
    polls /api/v1/waitlist/status/{entry_id} for live position updates.
    """
    restaurant_name = "the restaurant"
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM restaurants WHERE id = $1", restaurant_id,
        )
        if not row:
            raise HTTPException(404, "Restaurant not found")
        restaurant_name = row["name"] or restaurant_name

    rid = str(restaurant_id)
    safe_name = (
        restaurant_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    html = _QR_LANDING_HTML.replace("__RID__", rid).replace("__NAME__", safe_name)
    return HTMLResponse(content=html)


# ── Web Push (VAPID) — customer browser notifications ────────

class PushKeysModel(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=10)
    keys: PushKeysModel


@router.get("/push/vapid-key")
async def push_vapid_key():
    """Public: returns the server's VAPID public key (URL-safe base64)."""
    from app.services.push_service import get_vapid_keys
    keys = await get_vapid_keys()
    return {"publicKey": keys["public_key"]}


@router.post("/push/subscribe/{entry_id}")
async def push_subscribe(entry_id: UUID, body: PushSubscribeRequest, request: Request):
    """Public: register a Web Push subscription for a waitlist entry."""
    async with get_connection() as conn:
        row = await conn.fetchrow(
            "SELECT restaurant_id FROM waitlist_entries WHERE id = $1", entry_id,
        )
    if not row:
        raise HTTPException(404, "Entry not found")
    from app.services.push_service import save_subscription
    await save_subscription(
        entry_id=str(entry_id),
        restaurant_id=str(row["restaurant_id"]),
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True}


@router.get("/sw.js")
async def push_service_worker():
    """Public: service worker that displays push notifications on the QR page."""
    return Response(content=_PUSH_SW_JS, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600",
                             "Service-Worker-Allowed": "/"})


@router.get("/assets/{name}")
async def waitlist_asset(name: str):
    """Public: serve Bittu brand assets (logo, font, ding sound) for the QR page."""
    entry = _ASSET_MAP.get(name.lower())
    if not entry:
        raise HTTPException(404, "Asset not found")
    filename, media_type = entry
    path = _ASSETS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Asset missing on server")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=604800, immutable",
                 "Access-Control-Allow-Origin": "*"},
    )


_PUSH_SW_JS = """// Bittu waitlist push service worker
self.addEventListener('install', function(e){ self.skipWaiting(); });
self.addEventListener('activate', function(e){ e.waitUntil(self.clients.claim()); });
self.addEventListener('push', function(event){
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch(e) { data = { body: event.data && event.data.text() }; }
  var title = data.title || 'Bittu Waitlist';
  var opts = {
    body: data.body || '',
    tag: data.tag || 'bittu-waitlist',
    renotify: true,
    requireInteraction: true,
    icon: '/api/v1/waitlist/assets/logo.png',
    badge: '/api/v1/waitlist/assets/logo.png',
    data: data
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});
self.addEventListener('notificationclick', function(event){
  event.notification.close();
  event.waitUntil(clients.matchAll({type:'window', includeUncontrolled:true}).then(function(list){
    for (var i=0;i<list.length;i++){ var c=list[i]; if(c.url.indexOf('/api/v1/waitlist/qr/')>=0){ return c.focus(); } }
    if (clients.openWindow) return clients.openWindow('/');
  }));
});
"""


_QR_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#ED1C24" />
  <title>Join the Waitlist — __NAME__</title>
  <link rel="icon" type="image/png" href="/api/v1/waitlist/assets/logo.png" />
  <link rel="apple-touch-icon" href="/api/v1/waitlist/assets/logo.png" />
  <link rel="preload" as="font" type="font/otf" href="/api/v1/waitlist/assets/gilroy-light.otf" crossorigin />
  <link rel="preload" as="font" type="font/otf" href="/api/v1/waitlist/assets/gilroy-extrabold.otf" crossorigin />
  <link rel="preload" as="audio" href="/api/v1/waitlist/assets/ding.mp3" />
  <style>
    @font-face{font-family:'Gilroy';src:url('/api/v1/waitlist/assets/gilroy-light.otf') format('opentype');
               font-weight:300 500;font-style:normal;font-display:swap}
    @font-face{font-family:'Gilroy';src:url('/api/v1/waitlist/assets/gilroy-extrabold.otf') format('opentype');
               font-weight:700 900;font-style:normal;font-display:swap}
    :root{
      --bittu-red:#ED1C24; --bittu-red-2:#FF5A4E; --bittu-ink:#2A2D34; --bittu-ink-2:#5A6072;
      --bittu-mist:#F5F6F8; --bittu-line:#E6E8EE; --bittu-white:#FFFFFF;
      --bittu-shadow:0 30px 60px -20px rgba(35,40,55,.18),0 8px 18px -8px rgba(35,40,55,.08);
      --bittu-radius:20px;
    }
    *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
    html,body{margin:0;padding:0;font-family:'Gilroy','Inter',-apple-system,BlinkMacSystemFont,
              "Segoe UI",Roboto,sans-serif;color:var(--bittu-ink);
              background:
                radial-gradient(1200px 600px at 100% -10%,rgba(237,28,36,.10),transparent 60%),
                radial-gradient(900px 500px at -10% 110%,rgba(255,90,78,.08),transparent 55%),
                linear-gradient(180deg,#FAFAFC 0%,#F2F3F7 100%);
              min-height:100vh;-webkit-font-smoothing:antialiased;
              text-rendering:optimizeLegibility}
    .shell{min-height:100vh;display:flex;flex-direction:column}
    .topbar{display:flex;align-items:center;justify-content:space-between;
            padding:18px 22px;max-width:480px;margin:0 auto;width:100%}
    .topbar img{height:30px;display:block}
    .topbar .secure{font-size:11px;letter-spacing:.12em;text-transform:uppercase;
                    color:var(--bittu-ink-2);font-weight:600;display:flex;align-items:center;gap:6px}
    .topbar .secure::before{content:'';width:8px;height:8px;border-radius:50%;
                            background:#22C55E;box-shadow:0 0 0 4px rgba(34,197,94,.18)}
    .main{flex:1;display:flex;align-items:center;justify-content:center;
          padding:8px 18px 36px}
    .card{background:var(--bittu-white);border-radius:var(--bittu-radius);
          box-shadow:var(--bittu-shadow);padding:30px 26px;max-width:440px;width:100%;
          position:relative;overflow:hidden;border:1px solid rgba(230,232,238,.6)}
    .card::before{content:'';position:absolute;top:0;left:0;right:0;height:4px;
                  background:linear-gradient(90deg,var(--bittu-red),var(--bittu-red-2));}
    .eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;
             color:var(--bittu-red);font-weight:800;margin:6px 0 8px}
    h1{margin:0 0 6px;font-size:28px;font-weight:800;letter-spacing:-.02em;line-height:1.15}
    .sub{color:var(--bittu-ink-2);margin:0 0 26px;font-size:15px;font-weight:500}
    .sub b{color:var(--bittu-ink);font-weight:700}
    label{display:block;font-size:12px;margin:18px 0 8px;color:var(--bittu-ink);
          font-weight:700;letter-spacing:.04em;text-transform:uppercase}
    .field{position:relative}
    input{width:100%;padding:15px 16px;border:1.5px solid var(--bittu-line);
          border-radius:14px;font-size:16px;font-weight:500;background:var(--bittu-mist);
          color:var(--bittu-ink);font-family:inherit;transition:all .18s ease}
    input::placeholder{color:#A5ABBA;font-weight:400}
    input:focus{outline:none;border-color:var(--bittu-red);background:#fff;
                box-shadow:0 0 0 4px rgba(237,28,36,.10)}
    .party{display:flex;align-items:center;justify-content:space-between;gap:14px;
           background:var(--bittu-mist);border:1.5px solid var(--bittu-line);
           border-radius:14px;padding:10px 14px}
    .party button{width:44px;height:44px;border-radius:50%;border:0;background:#fff;
                  font-size:22px;font-weight:700;color:var(--bittu-red);cursor:pointer;
                  box-shadow:0 2px 6px rgba(35,40,55,.08);transition:transform .12s ease,opacity .12s}
    .party button:active{transform:scale(.92)}
    .party button:disabled{opacity:.35;cursor:not-allowed}
    .party .ps{display:flex;flex-direction:column;align-items:center;line-height:1}
    .party .ps b{font-size:28px;font-weight:800;color:var(--bittu-ink);letter-spacing:-.02em}
    .party .ps span{font-size:10px;letter-spacing:.14em;text-transform:uppercase;
                    color:var(--bittu-ink-2);margin-top:4px;font-weight:600}
    .submit{margin-top:28px;width:100%;padding:16px;color:#fff;border:0;
            border-radius:14px;font-size:16px;font-weight:800;letter-spacing:.02em;
            cursor:pointer;font-family:inherit;
            background:linear-gradient(135deg,var(--bittu-red) 0%,var(--bittu-red-2) 100%);
            box-shadow:0 10px 24px -8px rgba(237,28,36,.55);transition:transform .12s,box-shadow .2s}
    .submit:active{transform:translateY(1px)}
    .submit:disabled{opacity:.7;cursor:wait}
    .error{color:#B91C1C;background:#FEF2F2;border:1px solid #FECACA;
           padding:12px 14px;border-radius:12px;font-size:14px;margin-top:14px;font-weight:600}
    /* status view */
    .status{text-align:center}
    .status .greet{font-size:14px;color:var(--bittu-ink-2);font-weight:600;margin:0}
    .status h1{margin:6px 0 4px}
    .pill{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;
          border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.12em;
          text-transform:uppercase;margin:8px 0 24px}
    .pill::before{content:'';width:7px;height:7px;border-radius:50%}
    .pill.waiting{background:#FFF4E5;color:#B45309}
    .pill.waiting::before{background:#F59E0B;animation:pulse 2s infinite}
    .pill.notified{background:#DCFCE7;color:#15803D}
    .pill.notified::before{background:#22C55E}
    .pill.seated{background:#E0E7FF;color:#3730A3}
    .pill.seated::before{background:#6366F1}
    .pill.skipped,.pill.cancelled{background:#FEE2E2;color:#991B1B}
    .pill.skipped::before,.pill.cancelled::before{background:#EF4444}
    @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(245,158,11,.6)}50%{box-shadow:0 0 0 6px rgba(245,158,11,0)}}
    .pos-wrap{background:linear-gradient(135deg,#FFF5F4 0%,#FFEEEC 100%);
              border-radius:18px;padding:26px 18px;margin:8px 0 14px;
              border:1px solid rgba(237,28,36,.12)}
    .pos-label{font-size:11px;letter-spacing:.18em;text-transform:uppercase;
               color:var(--bittu-ink-2);font-weight:700;margin-bottom:4px}
    .pos{font-size:84px;font-weight:900;line-height:1;letter-spacing:-.04em;
         background:linear-gradient(135deg,var(--bittu-red),var(--bittu-red-2));
         -webkit-background-clip:text;background-clip:text;color:transparent;margin:2px 0 6px}
    .eta{color:var(--bittu-ink-2);font-size:14px;font-weight:600;margin-top:4px}
    .eta b{color:var(--bittu-ink);font-weight:800}
    .meta-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:16px}
    .meta-grid .cell{background:var(--bittu-mist);border-radius:12px;padding:12px 14px;text-align:left}
    .meta-grid .cell .k{font-size:10px;letter-spacing:.14em;text-transform:uppercase;
                        color:var(--bittu-ink-2);font-weight:700;margin-bottom:4px}
    .meta-grid .cell .v{font-size:16px;font-weight:800;color:var(--bittu-ink);letter-spacing:-.01em}
    .ready{margin-top:22px;padding:20px 18px;border-radius:16px;
           background:linear-gradient(135deg,#16A34A 0%,#22C55E 100%);
           color:#fff;font-weight:700;text-align:center;font-size:15px;
           box-shadow:0 16px 30px -10px rgba(34,197,94,.5);line-height:1.4;
           animation:rdy .4s ease-out}
    .ready.danger{background:linear-gradient(135deg,#DC2626,#EF4444);
                  box-shadow:0 16px 30px -10px rgba(239,68,68,.4)}
    .ready.info{background:linear-gradient(135deg,#4F46E5,#6366F1);
                box-shadow:0 16px 30px -10px rgba(99,102,241,.4)}
    .ready .big{font-size:22px;font-weight:900;letter-spacing:-.01em;display:block;margin-bottom:4px}
    @keyframes rdy{from{transform:scale(.95);opacity:0}to{transform:scale(1);opacity:1}}
    .hint{color:#9AA1B0;font-size:12px;text-align:center;margin-top:22px;font-weight:500;
          display:flex;align-items:center;justify-content:center;gap:6px}
    .hint .dot{width:6px;height:6px;border-radius:50%;background:#22C55E;
               box-shadow:0 0 0 4px rgba(34,197,94,.2);animation:pulse2 1.6s infinite}
    @keyframes pulse2{0%,100%{opacity:.7}50%{opacity:1}}
    .footer{padding:18px 22px 26px;text-align:center;color:#A5ABBA;
            font-size:11px;letter-spacing:.08em;font-weight:600}
    .footer b{color:var(--bittu-ink-2);font-weight:800}
    /* table-ready celebration overlay */
    .celebrate{position:fixed;inset:0;background:rgba(20,83,45,.92);
               backdrop-filter:blur(8px);display:none;align-items:center;justify-content:center;
               z-index:50;padding:28px;animation:fade .25s ease-out}
    .celebrate.on{display:flex}
    .celebrate .pop{background:#fff;border-radius:24px;padding:36px 28px;max-width:380px;
                    width:100%;text-align:center;box-shadow:0 40px 80px rgba(0,0,0,.4);
                    animation:pop .45s cubic-bezier(.34,1.56,.64,1)}
    .celebrate .icon{width:80px;height:80px;border-radius:50%;margin:0 auto 18px;
                     background:linear-gradient(135deg,#22C55E,#16A34A);
                     display:flex;align-items:center;justify-content:center;
                     color:#fff;font-size:42px;font-weight:900;
                     box-shadow:0 10px 30px rgba(34,197,94,.5)}
    .celebrate h2{font-size:26px;font-weight:900;margin:0 0 6px;letter-spacing:-.02em}
    .celebrate p{margin:0 0 22px;color:var(--bittu-ink-2);font-size:15px;font-weight:500;line-height:1.5}
    .celebrate p b{color:var(--bittu-ink);font-weight:800}
    .celebrate .ok{padding:14px 32px;border-radius:12px;background:var(--bittu-ink);
                   color:#fff;border:0;font-size:15px;font-weight:800;cursor:pointer;
                   font-family:inherit;letter-spacing:.02em}
    @keyframes fade{from{opacity:0}to{opacity:1}}
    @keyframes pop{0%{transform:scale(.6);opacity:0}100%{transform:scale(1);opacity:1}}
  </style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <img src="/api/v1/waitlist/assets/logo.png" alt="Bittu" />
    <div class="secure">Live · Secure</div>
  </header>
  <main class="main">
    <div class="card">
      <div id="form-view">
        <div class="eyebrow">Reserve your spot</div>
        <h1>Join the Waitlist</h1>
        <p class="sub">at <b>__NAME__</b></p>
        <form id="f" autocomplete="on">
          <label for="n">Your name</label>
          <div class="field"><input id="n" required maxlength="100" autocomplete="name" placeholder="Full name" /></div>
          <label for="p">Phone number</label>
          <div class="field"><input id="p" required minlength="6" maxlength="20" inputmode="tel" autocomplete="tel" placeholder="+91 98765 43210" /></div>
          <label>Party size</label>
          <div class="party">
            <button type="button" id="minus" aria-label="decrease">−</button>
            <div class="ps"><b id="ps">2</b><span>Guests</span></div>
            <button type="button" id="plus" aria-label="increase">+</button>
          </div>
          <button class="submit" id="go" type="submit">Join Queue</button>
          <div id="err" class="error" style="display:none"></div>
        </form>
      </div>
      <div id="status-view" class="status" style="display:none">
        <p class="greet" id="hi">Welcome!</p>
        <h1>You're in the queue</h1>
        <div><span class="pill waiting" id="st">Waiting</span></div>
        <div class="pos-wrap">
          <div class="pos-label">Your position</div>
          <div class="pos" id="pos">—</div>
          <div class="eta" id="eta">Calculating wait time…</div>
        </div>
        <div class="meta-grid">
          <div class="cell"><div class="k">Party</div><div class="v" id="meta-party">—</div></div>
          <div class="cell"><div class="k">At</div><div class="v" id="meta-name">__NAME__</div></div>
        </div>
        <div class="ready" id="ready" style="display:none"></div>
        <p class="hint"><span class="dot"></span> Live updates · keep this page open</p>
      </div>
    </div>
  </main>
  <footer class="footer">Powered by <b>BITTU</b> · Smart Restaurant OS</footer>
</div>

<!-- Table-ready celebration overlay -->
<div class="celebrate" id="celebrate">
  <div class="pop">
    <div class="icon">✓</div>
    <h2 id="cele-title">Your table is ready!</h2>
    <p id="cele-body">Please head to the host stand.</p>
    <button class="ok" id="cele-ok">Got it</button>
  </div>
</div>

<audio id="ding" src="/api/v1/waitlist/assets/ding.mp3" preload="auto"></audio>

<script>
(function(){
  var RID = "__RID__";
  var RNAME = "__NAME__";
  var party = 2;
  var lastStatus = null;
  var audioUnlocked = false;
  var psEl = document.getElementById('ps');
  var dingEl = document.getElementById('ding');

  // Unlock audio + vibration on first user gesture (browsers require it)
  function unlockAudio(){
    if(audioUnlocked) return;
    audioUnlocked = true;
    try { dingEl.volume = 0; dingEl.play().then(function(){ dingEl.pause(); dingEl.currentTime=0; dingEl.volume=1; }).catch(function(){}); } catch(e){}
  }
  document.addEventListener('touchstart', unlockAudio, {once:true, passive:true});
  document.addEventListener('click',      unlockAudio, {once:true});

  function playDing(){
    try { dingEl.currentTime = 0; var p = dingEl.play(); if(p && p.catch) p.catch(function(){}); } catch(e){}
    try { if(navigator.vibrate) navigator.vibrate([220,90,220,90,260]); } catch(e){}
  }

  document.getElementById('minus').onclick = function(){ if(party>1){party--;psEl.textContent=party;} };
  document.getElementById('plus').onclick  = function(){ if(party<50){party++;psEl.textContent=party;} };
  var errEl = document.getElementById('err');
  function showErr(m){ errEl.textContent = m; errEl.style.display='block'; }

  document.getElementById('f').addEventListener('submit', async function(e){
    e.preventDefault();
    errEl.style.display='none';
    var name = document.getElementById('n').value.trim();
    var phone = document.getElementById('p').value.trim();
    if(!name || phone.length<6) return;
    var btn = document.getElementById('go');
    btn.disabled = true; btn.textContent = 'Joining…';
    unlockAudio();
    try {
      var r = await fetch('/api/v1/waitlist/public/'+RID, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({customer_name:name, phone:phone, party_size:party})
      });
      var data = await r.json();
      if(!r.ok){
        showErr((data && (data.error && data.error.message || data.detail)) || 'Could not join queue');
        btn.disabled=false; btn.textContent='Join Queue';
        return;
      }
      data.customer_name = data.customer_name || name;
      data.party_size = data.party_size || party;
      localStorage.setItem('wl_'+RID, data.id);
      showStatus(data);
      startPolling(data.id);
      enablePush(data.id);
    } catch(e){
      showErr('Network error. Please try again.');
      btn.disabled=false; btn.textContent='Join Queue';
    }
  });

  function showStatus(e){
    document.getElementById('form-view').style.display='none';
    document.getElementById('status-view').style.display='block';
    var nm = e.customer_name || 'there';
    document.getElementById('hi').textContent = 'Hi '+nm+',';
    document.getElementById('pos').textContent = (e.position!=null) ? ('#'+e.position) : '—';
    document.getElementById('meta-party').textContent = (e.party_size||party)+' guest'+((e.party_size||party)===1?'':'s');
    document.getElementById('meta-name').textContent = RNAME;
    var st = document.getElementById('st');
    var statusLabel = (e.status||'waiting').charAt(0).toUpperCase()+(e.status||'waiting').slice(1);
    st.textContent = statusLabel; st.className = 'pill '+(e.status||'waiting');
    var eta = document.getElementById('eta');
    if(e.status === 'waiting'){
      eta.innerHTML = e.estimated_wait_minutes ? ('Estimated wait <b>~'+e.estimated_wait_minutes+' min</b>') : 'Calculating wait time…';
    } else { eta.textContent = ''; }
    var ready = document.getElementById('ready');
    ready.className = 'ready';
    if(e.status === 'notified'){
      ready.style.display='block';
      var tbl = e.table_number ? (' — Table '+e.table_number) : '';
      ready.innerHTML = '<span class="big">Your table is ready'+tbl+'</span>Please head to the host stand.';
      if(lastStatus !== 'notified') triggerReady(e);
    } else if(e.status === 'seated'){
      ready.style.display='block'; ready.className='ready info';
      ready.innerHTML = '<span class="big">Enjoy your meal!</span>Thank you for choosing us.';
    } else if(e.status === 'skipped' || e.status === 'cancelled'){
      ready.style.display='block'; ready.className='ready danger';
      ready.innerHTML = '<span class="big">Entry '+e.status+'</span>Please ask the host for help.';
    } else {
      ready.style.display='none';
    }
    lastStatus = e.status;
  }

  function triggerReady(e){
    playDing();
    var c = document.getElementById('celebrate');
    var tbl = e.table_number ? ('Table '+e.table_number+' is yours.') : 'Please head to the host stand.';
    document.getElementById('cele-title').textContent = 'Your table is ready!';
    document.getElementById('cele-body').innerHTML = tbl+' <b>'+RNAME+'</b> is waiting for you.';
    c.classList.add('on');
    document.getElementById('cele-ok').onclick = function(){ c.classList.remove('on'); };
    // Replay sound twice more for emphasis
    setTimeout(playDing, 1400);
    setTimeout(playDing, 2800);
  }

  function startPolling(id){
    setInterval(async function(){
      try {
        var r = await fetch('/api/v1/waitlist/status/'+id);
        if(r.ok){ showStatus(await r.json()); }
      } catch(e){}
    }, 10000);
  }

  // Resume if customer already joined on this device
  var existing = localStorage.getItem('wl_'+RID);
  if(existing){
    fetch('/api/v1/waitlist/status/'+existing).then(function(r){
      if(r.ok) return r.json();
      localStorage.removeItem('wl_'+RID); return null;
    }).then(function(d){
      if(d && (d.status==='waiting'||d.status==='notified')){
        // Don't auto-fire ding on page reload if already notified
        lastStatus = d.status;
        showStatus(d);
        startPolling(existing);
        enablePush(existing);
      } else if(d){ localStorage.removeItem('wl_'+RID); }
    }).catch(function(){});
  }

  // ---- Web Push subscription (browser background notifications) ----
  function urlB64ToUint8Array(b64){
    var pad = '='.repeat((4 - b64.length % 4) % 4);
    var s = (b64 + pad).replace(/-/g,'+').replace(/_/g,'/');
    var raw = atob(s); var out = new Uint8Array(raw.length);
    for(var i=0;i<raw.length;i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  async function enablePush(entryId){
    try {
      if(!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return;
      var perm = Notification.permission;
      if(perm === 'denied') return;
      if(perm === 'default'){
        perm = await Notification.requestPermission();
        if(perm !== 'granted') return;
      }
      var reg = await navigator.serviceWorker.register('/api/v1/waitlist/sw.js', {scope:'/api/v1/waitlist/'});
      await navigator.serviceWorker.ready;
      var keyResp = await fetch('/api/v1/waitlist/push/vapid-key');
      if(!keyResp.ok) return;
      var keyData = await keyResp.json();
      var existingSub = await reg.pushManager.getSubscription();
      var sub = existingSub || await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(keyData.publicKey)
      });
      var s = sub.toJSON();
      await fetch('/api/v1/waitlist/push/subscribe/'+entryId, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({endpoint: s.endpoint, keys: s.keys})
      });
    } catch(e){ /* ignore — polling still works */ }
  }
})();
</script>
</body>
</html>
"""
