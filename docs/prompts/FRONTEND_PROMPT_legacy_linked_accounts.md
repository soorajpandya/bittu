# Frontend Prompt — Legacy Linked-Account Merchants (pre-batch-KYC)

> Companion to [FRONTEND_PROMPT_bankkyc_razorpay.md](FRONTEND_PROMPT_bankkyc_razorpay.md).
> Handles the cohort of merchants who were onboarded via the **legacy
> direct-API flow** (`POST /api/v1/razorpay-route/linked-account`)
> **before** the batch KYC surface (`/api/v1/bankkyc_razorpay`) shipped.
>
> Symptom from the field (verified May 26, 2026 for
> `ozaurvidipakbhai@gmail.com`, merchant
> `c8b9c75f-457f-45f3-9200-e25d9573dd14`):
> - `GET /api/v1/bankkyc_razorpay/status` → no submission (treated as
>   "needs to fill the form")
> - `GET /api/v1/razorpay-route/linked-account` → returns a real
>   `acc_xxx` (e.g. `acc_StagfS3luInjXk`, status `created`)
>
> If the FE follows section §8 of the batch-KYC prompt blindly, it will
> ask these merchants to **re-submit KYC** — even though they already
> have a Razorpay linked account. That is wrong. This document defines
> the correct precedence.

---

## 1. The rule (one-liner)

> **Existence of a `rzp_route_accounts` row always wins over the
> absence of a `rzp_kyc_submissions` row.**

The legacy linked-account row is the source of truth that "this
merchant is already on Razorpay". The batch-KYC form is only for
merchants who have **never** been linked.

---

## 2. Required check order on the merchant Payments screen

On entry to `/settings/payments/razorpay-kyc` (or any screen that
might show the batch KYC form), run these calls **in parallel**:

```
GET /api/v1/razorpay-route/linked-account
GET /api/v1/bankkyc_razorpay/status
```

Then decide what to render using this precedence table (top wins):

| # | `/linked-account` | `/bankkyc_razorpay/status` | Render |
|---|---|---|---|
| 1 | `200` with `linked_account_id` | any | **Existing Linked-Account card** (legacy Payouts UI). Do NOT show the batch KYC form. |
| 2 | `404` / no row | `APPROVED` | Linked-Account card (bridge populated it). |
| 3 | `404` / no row | `PENDING_BATCH_UPLOAD` / `IN_BATCH_FILE` / `UPLOADED_TO_RAZORPAY` | Status card from §2.2 of the batch-KYC prompt. |
| 4 | `404` / no row | `REJECTED` | Rejection card + Resubmit CTA. |
| 5 | `404` / no row | no submission | **Show the batch KYC form** (the only path to the form). |

Rows **1–4** mean the merchant is already on a path; never offer the
form. Row **5** is the only "show form" case.

> This **supersedes** the §8 transition table in
> [FRONTEND_PROMPT_bankkyc_razorpay.md](FRONTEND_PROMPT_bankkyc_razorpay.md)
> for the "No submission → Show form" line. The line should read
> "No submission **AND** no linked account → Show form".

### 2.1 `effective_status` is for the pill, NOT for a full-screen takeover

> **A 200 from `/linked-account` ALWAYS renders the Linked-Account
> card from §3.** The `effective_status` field on the response only
> controls the **color and label of the status pill** (§3.1) — it
> never causes the FE to hide the card behind a "Verification in
> progress" / "Your KYC is being reviewed" full-screen placeholder.

This explicitly forbids the pattern observed on May 26, 2026 for
`soorajpandya11@gmail.com` (`acc_Stx6Jvw17Q1lXF`): the row was
present, `/linked-account` returned 200, but the app rendered a
full-screen "Verification in Progress" takeover because
`effective_status` happened to be `"submitted"`. The takeover hid the
account id, bank details, and Refresh button — the very fields §3
mandates be visible.

Allowed mapping of `effective_status` → pill color (no other UI
effect):

| `effective_status` | pill | merchant sees |
|---|---|---|
| `under_review` | blue "Awaiting Razorpay activation" | card + bank details (read-only) |
| `needs_clarification` | amber "Razorpay needs more info" | card + inline banner with admin contact |
| `activated` | green "Activated" | card + bank details + Route transfer surface |
| `rejected` | red "Rejected" | card + reason + Resubmit CTA |
| `suspended` | red "Suspended" | card + support contact banner |

Note: the legacy value `"submitted"` is **no longer emitted** by the
backend as of May 26, 2026. The backend collapses "account exists,
awaiting activation" into `under_review`. If the FE still has a code
path keyed on `effective_status == "submitted"`, delete it — and in
particular delete any branch that renders a full-screen placeholder
when a `linked_account_id` is present in the response.

---

## 3. The Linked-Account card (row 1 above)

Reuse the existing legacy Payouts card. Fields to render from
`GET /api/v1/razorpay-route/linked-account`:

| Field | Label | Notes |
|---|---|---|
| `linked_account_id` | "Razorpay account id" | Monospace + copy button. Mask middle (`acc_Stag…njXk`). |
| `legal_business_name` | "Business name" | |
| `email` | "Contact email" | |
| `status` | "Account status" | Pill (see §3.1). |
| `kyc_status` | "KYC status" | Pill. |
| `activation_status` | "Activation" | Pill; only show if non-null. |
| `updated_at` | "Last synced" | Relative time. |

Add a footer line:

> "Linked via legacy onboarding. No further KYC submission needed."

### 3.1 Status pill colors

Same scheme as the batch-KYC admin console:

| value | color |
|---|---|
| `created` | gray |
| `under_review` | blue |
| `needs_clarification` | amber |
| `activated` / `approved` | green |
| `rejected` / `suspended` | red |

### 3.2 Refresh action

Single button "Refresh from Razorpay" →
`POST /api/v1/razorpay-route/linked-account/refresh`
(if available) **or** simply refetch
`GET /api/v1/razorpay-route/linked-account`. Do **not** call any
`/sync` endpoint — that endpoint has been removed.

### 3.3 Bank details panel (read-only)

Render directly below the Linked-Account card. Source: same
`GET /api/v1/razorpay-route/linked-account` response — no extra call.

| Field on response | Label | Display |
|---|---|---|
| `bank_account_last4` | "Bank account number (last 4)" | `•••• •••• <last4>` |
| `bank_account_ifsc` | "IFSC code" | Monospace, uppercase. Copy-to-clipboard button. |
| `route_product_raw.active_configuration.settlements.account_number` | "Full account number" (optional reveal) | Eye-toggle reveals the full number when present. Copy button. Fall back to `bank_account_last4` masked display if missing. |
| `route_product_raw.active_configuration.settlements.beneficiary_name` | "Beneficiary (as on passbook)" | Plain text |
| `contact_name` | "Contact name" | Plain text |
| `phone` | "Contact phone" | Plain text + copy button |

Footer line: *"Bank details are stored encrypted. To change them,
contact support — direct edit is intentionally disabled."*

The full `account_number` lives inside the Razorpay-mirrored
`route_product_raw` blob; treat it as **may be missing** (older
records may only have last4 + IFSC) and degrade gracefully.

---

## 4. Explicit DO-NOT list

- **Do not** call `POST /api/v1/razorpay-route/linked-account` from the
  Flutter app. Linked-account creation now goes through the batch KYC
  pipeline + admin approval. The legacy create endpoint is reserved
  for backfill scripts only.
- **Do not** call `POST /api/v1/razorpay-route/linked-account/sync`
  (removed; returns `404`).
- **Do not** show the batch KYC form if `/linked-account` returned a
  row, even if `/bankkyc_razorpay/status` says "no submission".
- **Do not** call `/bankkyc_razorpay/status` on a 60s poll for
  legacy-linked merchants — they will never have a submission. Only
  poll when the screen is actually rendering the batch flow status
  card (rows 3 above).

---

## 5. Error handling for the precedence calls

Both calls are best-effort. Failure modes:

| `/linked-account` result | Action |
|---|---|
| `200` with body | Treat as "linked". Use row 1. |
| `404` | Treat as "not linked". Fall through to `/status`. |
| `5xx` / network | Show a generic "Couldn't load payment status — retry" card with a retry button. **Do not** show the form (avoid the false-positive resubmit). |

| `/bankkyc_razorpay/status` result | Action |
|---|---|
| `200` with submission | Use rows 2–4 per `status`. |
| `200` with no submission (or `404`) | Combined with `/linked-account` 404 → row 5. |
| `5xx` / network | Same retry card as above. |

The retry card guarantees no merchant is ever incorrectly asked to
re-submit KYC because of a transient failure.

---

## 6. Acceptance checklist

- [ ] A merchant with an `rzp_route_accounts` row (legacy) and no
      `rzp_kyc_submissions` row sees the **Linked-Account card**, never
      the form. Verified with `ozaurvidipakbhai@gmail.com`.
- [ ] A merchant with neither row sees the **batch KYC form**.
- [ ] A merchant with a non-terminal submission and no linked account
      sees the **status card** with ETA copy.
- [ ] A merchant whose batch submission was just approved (bridge
      populated `rzp_route_accounts`) sees the **Linked-Account card**
      automatically on the next render.
- [ ] No call is made to `POST /razorpay-route/linked-account` or
      `/linked-account/sync` from the Flutter app under any flow.
- [ ] Transient `5xx` on either probe shows a retry card, not the
      form.

---

## 7. Backend reference (read-only — do not change from FE)

The legacy + batch surfaces share one table: `rzp_route_accounts`
keyed by `merchant_id`. Population sources:

1. **Legacy direct-API onboarding** (pre-May 2026) — wrote the row at
   the moment of `POST /razorpay-route/linked-account`.
2. **Batch KYC bridge** (post-May 2026) — admin "mark approved" with
   an `acc_xxx` triggers a server-side
   `GET https://api.razorpay.com/v2/accounts/{id}` and upserts into
   the same table.
3. **12h background poller** — refreshes `status` / `kyc_status` /
   `activation_status` for all rows.

The FE doesn't need to care which source wrote the row; presence is
all that matters for the precedence rule in §2.

---

## 8. Example responses (live, sanitized)

### 8.1 `GET /api/v1/razorpay-route/linked-account` (legacy merchant, has acc)

Status: `200 OK`

```json
{
  "id": "79e2a50c-4f44-412e-b5f5-feba88384c28",
  "merchant_id": "c8b9c75f-457f-45f3-9200-e25d9573dd14",
  "linked_account_id": "acc_StagfS3luInjXk",
  "legal_business_name": "Brand Clothzy",
  "business_type": "proprietorship",
  "contact_name": "Urvi Pandya",
  "email": "ozaurvidipakbhai@gmail.com",
  "phone": "+918758724262",
  "reference_id": "m_c8b9c75f457f45f3",
  "status": "created",
  "kyc_status": "created",
  "activation_status": null,
  "effective_status": "under_review",
  "bank_account_ifsc": "ICIC0004040",
  "bank_account_last4": "0182",
  "route_product_id": "acc_prd_StaglJzTRaibZn",
  "route_product_status": "under_review",
  "route_product_requested_at": "2026-05-25T12:05:36.551986+00:00",
  "route_product_activated_at": null,
  "route_product_raw": {
    "id": "acc_prd_StaglJzTRaibZn",
    "account_id": "acc_StagfS3luInjXk",
    "product_name": "route",
    "activation_status": "under_review",
    "active_configuration": {
      "settlements": {
        "ifsc_code": "ICIC0004040",
        "account_number": "404001500182",
        "beneficiary_name": "PANDYA URVIBEN SURAJKUMAR"
      }
    },
    "requested_configuration": [],
    "tnc": { "id": "tnc_Stagl7YZrdDfIy", "accepted": true, "accepted_at": 1779710736 },
    "requirements": []
  },
  "stakeholder_id": "sth_StagjdYRx3mLQJ",
  "tnc_accepted_at": "2026-05-25T12:05:36.551986+00:00",
  "created_at": "2026-05-25T12:05:33.546338+00:00",
  "updated_at": "2026-05-26T10:54:21.969380+00:00"
}
```

Key paths the bank-details panel reads (§3.3):

- `bank_account_last4` → `"0182"`
- `bank_account_ifsc` → `"ICIC0004040"`
- `route_product_raw.active_configuration.settlements.account_number` → `"404001500182"` (reveal-on-toggle)
- `route_product_raw.active_configuration.settlements.beneficiary_name` → `"PANDYA URVIBEN SURAJKUMAR"`

### 8.2 `GET /api/v1/razorpay-route/linked-account` — not provisioned

Status: `404 Not Found`

```json
{ "detail": "No linked account provisioned for this merchant" }
```

FE: treat as "no row" → fall through to `/bankkyc_razorpay/status`
per the precedence table in §2.

### 8.3 `GET /api/v1/bankkyc_razorpay/status` — merchant has no submission

Status: `200 OK`

```json
{
  "status": "NOT_SUBMITTED",
  "estimated_processing_window": "4-8 hours",
  "next_batch_slot_utc": "2026-05-26T11:00:00+00:00"
}
```

Combined with row 1 of §2 (linked account exists), still render the
Linked-Account card; do **not** show the form.
