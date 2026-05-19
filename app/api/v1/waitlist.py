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
from typing import Optional
from uuid import UUID
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from app.core.auth import UserContext, require_permission
from app.core.cache import cached_route, invalidate_prefix
from app.core.database import get_connection
from app.services.waitlist_service import WaitlistService

router = APIRouter(prefix="/waitlist", tags=["Dine-In"])
_svc = WaitlistService()
_CACHE_PREFIX = "waitlist"


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


@router.post("/{entry_id}/seat")
async def seat_customer(
    entry_id: UUID,
    user: UserContext = Depends(require_permission("waitlist.manage")),
):
    """Seat a waitlisted customer."""
    result = await _svc.seat_customer(user, entry_id)
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

    target = f"{_public_base(request)}/api/v1/waitlist/qr/{restaurant_id}"
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
    icon: '/api/v1/waitlist/sw-icon.png',
    badge: '/api/v1/waitlist/sw-icon.png',
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
  <title>Join the Waitlist — __NAME__</title>
  <style>
    *{box-sizing:border-box}
    body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#f4f5f7;color:#1f2933;min-height:100vh;display:flex;align-items:center;
         justify-content:center;padding:20px}
    .card{background:#fff;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,.08);
          padding:28px;max-width:420px;width:100%}
    h1{margin:0 0 4px;font-size:22px}
    .sub{color:#52606d;margin:0 0 24px;font-size:14px}
    label{display:block;font-size:13px;margin:14px 0 6px;color:#3e4c59;font-weight:600}
    input{width:100%;padding:12px 14px;border:1px solid #cbd2d9;border-radius:10px;
          font-size:16px;background:#fff}
    input:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.15)}
    .party{display:flex;align-items:center;gap:14px}
    .party button{width:42px;height:42px;border-radius:50%;border:1px solid #cbd2d9;
                  background:#fff;font-size:22px;cursor:pointer}
    .party button:disabled{opacity:.4;cursor:not-allowed}
    .party span{font-size:22px;font-weight:600;min-width:30px;text-align:center}
    .submit{margin-top:24px;width:100%;padding:14px;background:#2563eb;color:#fff;
            border:0;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer}
    .submit:disabled{opacity:.6;cursor:wait}
    .error{color:#b91c1c;background:#fee2e2;padding:10px 12px;border-radius:8px;
           font-size:14px;margin-top:14px}
    .status{text-align:center}
    .status .pos{font-size:64px;font-weight:700;color:#2563eb;line-height:1;margin:8px 0}
    .status .eta{color:#52606d;font-size:14px}
    .badge{display:inline-block;padding:4px 10px;border-radius:20px;font-size:12px;
           font-weight:600;text-transform:uppercase;letter-spacing:.5px}
    .badge.waiting{background:#e0e7ff;color:#3730a3}
    .badge.notified{background:#dcfce7;color:#15803d}
    .ready{margin-top:18px;padding:16px;background:#dcfce7;border-radius:10px;
           color:#14532d;font-weight:600;text-align:center}
    .hint{color:#9aa5b1;font-size:12px;text-align:center;margin-top:18px}
  </style>
</head>
<body>
  <div class="card">
    <div id="form-view">
      <h1>Join the Waitlist</h1>
      <p class="sub">at __NAME__</p>
      <form id="f">
        <label for="n">Your name</label>
        <input id="n" required maxlength="100" autocomplete="name" />
        <label for="p">Phone number</label>
        <input id="p" required minlength="6" maxlength="20" inputmode="tel" autocomplete="tel" />
        <label>Party size</label>
        <div class="party">
          <button type="button" id="minus" aria-label="decrease">−</button>
          <span id="ps">2</span>
          <button type="button" id="plus" aria-label="increase">+</button>
        </div>
        <button class="submit" id="go" type="submit">Join Queue</button>
        <div id="err" class="error" style="display:none"></div>
      </form>
    </div>
    <div id="status-view" class="status" style="display:none">
      <h1 id="hi">Hi!</h1>
      <p class="sub">at __NAME__</p>
      <div class="pos" id="pos">—</div>
      <div><span class="badge waiting" id="st">waiting</span></div>
      <div class="eta" id="eta"></div>
      <div class="ready" id="ready" style="display:none"></div>
      <p class="hint">This page updates automatically. Keep it open.</p>
    </div>
  </div>
<script>
(function(){
  var RID = "__RID__";
  var party = 2;
  var psEl = document.getElementById('ps');
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
      localStorage.setItem('wl_'+RID, data.id);
      showStatus(name, data);
      startPolling(data.id, name);
      enablePush(data.id);
    } catch(e){
      showErr('Network error. Please try again.');
      btn.disabled=false; btn.textContent='Join Queue';
    }
  });
  function showStatus(name, e){
    document.getElementById('form-view').style.display='none';
    document.getElementById('status-view').style.display='block';
    document.getElementById('hi').textContent = 'Hi '+name+'!';
    document.getElementById('pos').textContent = '#'+e.position;
    var st = document.getElementById('st');
    st.textContent = e.status; st.className = 'badge '+e.status;
    var eta = document.getElementById('eta');
    eta.textContent = e.estimated_wait_minutes ? ('~'+e.estimated_wait_minutes+' min wait') : '';
    var ready = document.getElementById('ready');
    if(e.status === 'notified'){
      ready.style.display='block';
      ready.textContent = 'Your table is ready'+(e.table_number? ' — Table '+e.table_number:'')+'. Please come in!';
    } else if(e.status === 'seated'){
      ready.style.display='block';
      ready.textContent = 'Enjoy your meal!';
    } else if(e.status === 'skipped' || e.status === 'cancelled'){
      ready.style.display='block';
      ready.style.background='#fee2e2'; ready.style.color='#7f1d1d';
      ready.textContent = 'Your entry was '+e.status+'.';
    } else {
      ready.style.display='none';
    }
  }
  function startPolling(id, name){
    setInterval(async function(){
      try {
        var r = await fetch('/api/v1/waitlist/status/'+id);
        if(r.ok){ showStatus(name, await r.json()); }
      } catch(e){}
    }, 15000);
  }
  // Resume if customer already joined on this device
  var existing = localStorage.getItem('wl_'+RID);
  if(existing){
    fetch('/api/v1/waitlist/status/'+existing).then(function(r){
      if(r.ok) return r.json();
      localStorage.removeItem('wl_'+RID); return null;
    }).then(function(d){
      if(d && (d.status==='waiting'||d.status==='notified')){
        showStatus(d.customer_name || 'there', d);
        startPolling(existing, d.customer_name || 'there');
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
      var existing = await reg.pushManager.getSubscription();
      var sub = existing || await reg.pushManager.subscribe({
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
