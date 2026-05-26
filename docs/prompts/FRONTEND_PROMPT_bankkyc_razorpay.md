# Frontend Prompt — Razorpay Linked-Account Batch KYC (`/api/v1/bankkyc_razorpay`)

> Build the merchant-facing **Bank/KYC submission** screen **and** the
> super-admin **Batch Upload Console** for the new
> `/api/v1/bankkyc_razorpay` surface. This **replaces** any existing
> "Connect Razorpay" / "Create Linked Account" direct-API flow.
>
> Razorpay does **not** expose a bulk linked-account API. Onboarding is
> done by uploading a CSV on the Razorpay Dashboard. Backend generates
> one CSV every 30 minutes; an admin downloads it, uploads to Razorpay,
> then marks merchants approved/rejected.

---

## 1. Context & rules

- **Base URL:** `https://<api>/api/v1/bankkyc_razorpay`
- **Auth:**
  - Merchant endpoints require the standard merchant JWT (permission
    `razorpay.route.read` / `razorpay.route.write` — already granted to
    restaurant owners).
  - Admin endpoints require a **platform-admin** token. Hide the entire
    admin console for non-admin users.
- **One active submission per merchant.** After a successful submit
  the form must be **disabled** and replaced by a status card until the
  merchant is `APPROVED` or `REJECTED`. Rejected merchants may resubmit
  (the API will accept a new submission once status = `REJECTED`).
- **ETA messaging (verbatim, do not paraphrase):**
  > "Your details have been submitted successfully. Bittu POS will
  > update your KYC status within 4 to 8 hours."
- **No client-side Razorpay calls.** Everything goes through our API.

---

## 2. Merchant screen — "Razorpay Payouts KYC"

Route suggestion: `/settings/payments/razorpay-kyc`

### 2.1 Form fields (all required unless noted)

| Field | Type | Validation | UI hint |
|---|---|---|---|
| `account_name` | text | 2–200 chars | "Linked-account display name" |
| `account_email` | email | RFC email, 4–200 chars | Contact email Razorpay will use |
| `business_name` | text | 2–200 chars | Legal business name |
| `business_type` | select | one of: `individual`, `proprietorship`, `partnership`, `private_limited`, `public_limited`, `llp`, `ngo`, `trust`, `society`, `not_yet_registered`, `educational_institutes` | **Must match Razorpay's official list exactly.** `huf` and `other` are NOT valid — Razorpay rejects with `values mismatching allowed headers`. |
| `ifsc_code` | text | exact 11 chars, uppercase, regex `^[A-Z]{4}0[A-Z0-9]{6}$` | Auto-uppercase on input |
| `account_number` | text | 4–35 chars, digits only | Mask with show/hide toggle |
| `beneficiary_name` | text | 2–200 chars | "As printed on the bank passbook" |
| `dashboard_access` | checkbox → 0/1 | default 0 | "Give this account Razorpay dashboard access" |
| `customer_refunds` | checkbox → 0/1 | default 0 | "Allow this account to issue customer refunds" |

Submit:
```
POST /api/v1/bankkyc_razorpay
Body: { ...all fields above... }
```

Possible responses:

- `200 OK`
  ```json
  {
    "success": true,
    "message": "Your details have been submitted successfully. Bittu POS will update your KYC status within 4 to 8 hours.",
    "submission_id": 123,
    "status": "PENDING_BATCH_UPLOAD",
    "estimated_processing_window": "4-8 hours",
    "next_batch_slot_utc": "2026-05-26T08:30:00+00:00"
  }
  ```
  → show success toast + ETA, switch view to **status card**.

- `409 Conflict` → merchant already has a live submission. Fetch
  `/status` and show the status card instead.
- `422 Unprocessable Entity` → render field-level errors using the
  `detail` string.

### 2.2 Status card

```
GET /api/v1/bankkyc_razorpay/status
```

Returns the merchant's latest submission. Render based on `status`:

| status | Card title | Body | CTA |
|---|---|---|---|
| `PENDING_BATCH_UPLOAD` | "KYC submitted — awaiting batch" | "Your details are queued for the next 30-minute batch upload." | Disabled "Submitted" pill |
| `IN_BATCH_FILE` | "Included in batch <batch_no>" | "Your KYC is part of the next upload to Razorpay." | Disabled |
| `UPLOADED_TO_RAZORPAY` | "Submitted to Razorpay" | "Razorpay is verifying your bank/KYC details (typically 4–8 hours)." | Disabled |
| `APPROVED` | "✅ KYC Approved" | Show masked `razorpay_account_id`. | "Go to Payouts" |
| `REJECTED` | "❌ KYC Rejected" | Show `rejection_reason`. | "Resubmit" → reopen form |

Show the ETA strip below the card whenever status is not terminal:
*"Estimated approval window: 4–8 hours · Next batch upload: <next_batch_slot_utc local time>"*

---

## 3. Admin Console — "Razorpay Batch Uploads"

Route suggestion: `/admin/payments/razorpay-batches`. Gate behind
`isPlatformAdmin()` check.

### 3.1 Top metrics row

```
GET /api/v1/bankkyc_razorpay/admin/stats
```

Render 4 KPI tiles + an alert banner area:

- **Pending submissions** (`submissions.pending`)
- **In-flight batches** (`batches.in_flight` — i.e. uploaded but not
  yet fully approved/rejected)
- **Approved (last 24h)** (`submissions.approved_24h`)
- **Rejected (last 24h)** (`submissions.rejected_24h`)

Below KPIs, render `alerts[]` (each `{level, message, age_hours}`):
- `WARN` (≥ 30 min) → yellow chip
- `HIGH` (≥ 2 h) → orange chip
- `CRITICAL` (≥ 8 h) → red chip with pulse

### 3.2 Batches table

```
GET /api/v1/bankkyc_razorpay/admin/batches?limit=50&offset=0
```

Columns:
`batch_no | slot_at (local) | record_count | status | created_at | actions`

Status badge colors:
- `GENERATED` — gray
- `DOWNLOADED` — blue
- `UPLOADED` — purple
- `PARTIALLY_APPROVED` — amber
- `APPROVED` — green
- `REJECTED` — red

Per-row action buttons (show conditionally on `status` and `record_count > 0`):

| Button | Endpoint | When |
|---|---|---|
| **Download CSV** | `GET /admin/batches/{id}/csv` | always (if `record_count > 0`) |
| **Download XLSX** | `GET /admin/batches/{id}/xlsx` | always (if `record_count > 0`) |
| **Mark uploaded** | `POST /admin/batches/{id}/mark-uploaded` | status ∈ {GENERATED, DOWNLOADED} |
| **Mark all approved** | `POST /admin/batches/{id}/mark-approved` | status = UPLOADED |
| **Mark batch rejected** | `POST /admin/batches/{id}/mark-rejected` | status = UPLOADED |

Downloads must use the browser-native file download (preserve
`Content-Disposition` filename). Do **not** use `fetch().json()` for
these — use an `<a download>` or a blob fetch + `URL.createObjectURL`.

Header action:
- **"Generate current batch now"** →
  `POST /admin/batches/generate` (force-generate the current 30-min
  slot ahead of schedule). Disabled if the current slot already exists.

### 3.3 Mark-approved modal

Trigger: per-row "Mark all approved".

Razorpay returns one `acc_xxx` id per row after dashboard upload. Let
the admin optionally paste a mapping. UX:

- Show a table of the batch's submissions (`merchant_id`,
  `business_name`) with an input next to each for the `acc_xxx` id.
- "Save" sends:
  ```
  POST /admin/batches/{id}/mark-approved
  Body: { "razorpay_account_ids": { "<submission_id>": "acc_xxx", ... } }
  ```
- "Skip ids" button → POST with empty body `{}` (all marked approved
  without storing acc ids; they can be filled later from individual
  reconcile calls).

### 3.4 Mark-rejected modal

Single textarea (`reason`, ≤ 500 chars). Submits:
```
POST /admin/batches/{id}/mark-rejected
Body: { "reason": "<text>" }
```

### 3.5 Submissions drill-down

Clicking a batch row opens a side panel:
```
GET /admin/submissions?batch_id={id}&limit=100
```
Optionally filterable by `status` query param.

Each row shows merchant info + per-row admin actions:

| Action | Endpoint |
|---|---|
| Approve one | `POST /admin/submissions/{id}/mark-approved` body `{ "razorpay_account_id": "acc_xxx" }` (id optional) |
| Reject one | `POST /admin/submissions/{id}/mark-rejected` body `{ "reason": "..." }` |
| Reconcile from Razorpay | `POST /admin/submissions/{id}/check-account` (no body) — backend calls `GET https://api.razorpay.com/v2/accounts/{acc_xxx}` and updates status automatically. Show returned `account_status` in a toast. |

"Reconcile" is only useful when `razorpay_account_id` is already set;
disable otherwise.

### 3.6 Polling / refresh

- Auto-refresh **stats** every 60 s.
- Auto-refresh the **batches** table every 60 s.
- After any mutation (mark-uploaded / approved / rejected / generate /
  reconcile) refetch stats + batches + the open drill-down panel.

---

## 4. Error & state handling

- Treat any `4xx` with a JSON `{"detail": "..."}` as a user-visible
  error toast.
- `409 Conflict` on merchant submit → switch to status card silently
  (it just means a submission already exists).
- `404 Not Found` from admin endpoints → toast + remove row from
  cache.
- Persist no PII (account number, IFSC) in client storage. Re-fetch on
  each visit.

---

## 5. Acceptance checklist

- [ ] Merchant can submit; form locks; status card appears with verbatim ETA copy.
- [ ] Resubmitting after `REJECTED` works; resubmitting any other state shows the lock.
- [ ] Admin sees KPI tiles + alert chips colored by severity.
- [ ] Batches table renders status badges and only shows actions allowed for current status.
- [ ] CSV and XLSX downloads open as files with the server's filename.
- [ ] Force-generate creates a batch immediately and the table updates.
- [ ] Mark-uploaded → Mark-approved (with optional acc-id map) → row turns green and submissions flip to APPROVED on drill-down.
- [ ] Reconcile button calls Razorpay via backend and updates the row.
- [ ] Non-admin users cannot even see the admin route.

---

## 6. Out of scope

- Direct calls to `api.razorpay.com` from the browser.
- Editing submissions in place — admins can only approve/reject/reconcile.
- Bulk row selection on submissions (single-row actions only for v1).

---

## 7. Wiring with the existing `/razorpay-route/linked-account` surface

The legacy "Payouts / Linked Account" screen on the merchant app reads
from `GET /api/v1/razorpay-route/linked-account` and
`GET /api/v1/razorpay-route/linked-account/details`. **Keep that screen
as-is** — the backend now auto-mirrors the Razorpay account into
`rzp_route_accounts` the moment an admin calls
`POST /admin/submissions/{id}/mark-approved` (or the batch-level
equivalent) with an `acc_xxx` id. The bridge calls Razorpay's
`GET /v2/accounts/{id}` server-side and populates the same row the
legacy screen reads from. **No additional sync call is needed.**

### 7.1 REMOVE the legacy sync call after submit

The current Flutter code (per `flutter run` logs) makes this call
immediately after a successful `POST /api/v1/bankkyc_razorpay`:

```
POST /api/v1/razorpay-route/linked-account/sync
→ 404 { "detail": "No linked account provisioned for this merchant" }
```

That call is **wrong for the batch flow** and must be deleted from the
submit success path. The batch flow does not create a linked account
at submit time — it only queues the row for the next CSV upload. The
linked account is created hours later by the admin via the dashboard.

**What to do instead after `POST /bankkyc_razorpay` succeeds:**
1. Show the verbatim ETA toast.
2. Render the status card (section 2.2) via `GET /bankkyc_razorpay/status`.
3. Do **not** touch `/razorpay-route/linked-account/*` until the
   merchant's status flips to `APPROVED`.

### 7.2 Polling for activation on the merchant side

While the status card is showing a non-terminal status
(`PENDING_BATCH_UPLOAD`, `IN_BATCH_FILE`, `UPLOADED_TO_RAZORPAY`):

- Poll `GET /api/v1/bankkyc_razorpay/status` **every 60s** while the
  screen is visible (foreground only — pause when backgrounded).
- When `status` becomes `APPROVED`, replace the status card with the
  existing "Linked Account" card (driven by
  `GET /api/v1/razorpay-route/linked-account`). That endpoint will now
  return the populated account because the backend bridge already
  upserted it during admin approval.
- When `status` becomes `REJECTED`, show the rejection reason + a
  "Resubmit" CTA that reopens the form.

### 7.3 Handling pre-activation visibility (optional v1.1)

In rare cases a merchant may want to see their `acc_xxx` id before
Razorpay flips it to `activated`. The status payload already includes
`razorpay_account_id` once an admin attaches it. Show it as:

> Linked account id: `acc_xxx…lXF` (created — awaiting Razorpay activation)

Use a monospace font and a copy-to-clipboard button.

---

## 8. Status → screen transition (merchant)

```
No submission              →  Show form
PENDING_BATCH_UPLOAD       →  Status card (queued)
IN_BATCH_FILE              →  Status card (in batch)
UPLOADED_TO_RAZORPAY       →  Status card (uploaded)
APPROVED                   →  Existing linked-account card
                              (GET /razorpay-route/linked-account)
REJECTED                   →  Status card with reason + Resubmit CTA
```

The `APPROVED` transition is the single integration point with the
existing Payouts UI. Everything else lives entirely on the new
`/api/v1/bankkyc_razorpay` surface.

---

## 9. Admin: "Mark approved" data contract recap

When the admin pastes the `acc_xxx` ids and submits:

```
POST /api/v1/bankkyc_razorpay/admin/submissions/{submission_id}/mark-approved
Content-Type: application/json
{ "razorpay_account_id": "acc_Stx6Jvw17Q1lXF" }
```

Backend will:
1. Set submission `status = APPROVED`, persist the `acc_xxx`.
2. **Call Razorpay** `GET /v2/accounts/acc_Stx6Jvw17Q1lXF` server-side.
3. Upsert the full response into `rzp_route_accounts` keyed by
   `merchant_id` so the merchant's app immediately sees it via the
   legacy `GET /linked-account` endpoint.

If step 2 fails (Razorpay 4xx/5xx) the approval still succeeds; the
bridge logs a warning and the 12h background poller will retry. Admin
UI should show a toast "Approved — linked account mirror pending" if
the response includes any warning field (currently it doesn't — treat
a 200 as full success).
