# Frontend implementation prompt — "Edit Linked Account" screen

> Backend endpoint: `PATCH /api/v1/razorpay-route/linked-account`
> Deployed: commit `6174938` on `ec2-13-206-196-252` (Mumbai).
> See [FE_RZP_LINKED_ACCOUNT_CONTRACT.md](FE_RZP_LINKED_ACCOUNT_CONTRACT.md#2b-patch-linked-account--update-existing-account) §2b for the full request/response contract.

You are implementing the **Edit Linked Account** screen for the Bittu owner app (Flutter) and the merchant dashboard web (Next.js). The screen lets an owner update the Razorpay Linked Account that was created during onboarding.

---

## 1. Scope

Build a single screen / route that:

1. Loads the current linked account via `GET /api/v1/razorpay-route/linked-account`.
2. Pre-fills an editable form with the loaded values.
3. On save, sends **only the changed fields** as `PATCH /api/v1/razorpay-route/linked-account`.
4. On success, refreshes the local cache and shows a success toast.
5. Handles every error code from the contract (400 / 403 / 404 / 422 / 502).

Do **not** build:
- A "create linked account" form — that lives on the onboarding flow already.
- Stakeholder / product / bank editing — those are separate endpoints.
- Any UI for `business_type` or `email` — both are **immutable** on Razorpay and the backend rejects them implicitly (not in the request model at all).

---

## 2. Route / navigation

- **Flutter**: `/settings/payments/linked-account/edit` — push from the existing "Payments" settings tile when `linked_account.status != null`.
- **Next.js**: `/dashboard/settings/payments/linked-account/edit` — link from the same settings card.

Gate the screen behind permission `razorpay.route.write` (you already check `razorpay.route.read` to render the settings card). Owners who lack write permission should see a read-only view with a banner: *"Ask the account owner to edit these details."*

---

## 3. Form sections

Group fields exactly like this — the backend accepts partial PATCHes so each section can be its own collapsible card / accordion that submits independently if you want, but a single Save at the bottom is also fine.

### A. Business identity
| Label | Field | Constraint |
|---|---|---|
| Legal business name | `legal_business_name` | 4..200 |
| Customer-facing name (DBA) | `customer_facing_business_name` | 1..255 |
| Contact name | `contact_name` | 4..255, letters/digits/spaces only — strip other chars on blur |
| Business phone | `phone` | digits only, 8..15 (leading `91` auto-stripped server-side) |
| Partner reference id | `reference_id` | 1..512, `[A-Za-z0-9_-]` only. Show helper: *"Optional — may be ignored by Razorpay if your platform doesn't support partner reference codes."* |

### B. Profile
| Label | Field | Constraint |
|---|---|---|
| Category | `category` | dropdown — see Razorpay business categories |
| Subcategory | `subcategory` | dropdown — filtered by category |
| Business model | `business_model` | freeform 1..255 |

### C. Addresses (two tabs: Registered / Operation)
| Label | Field | Constraint |
|---|---|---|
| Street 1 | `addresses.<slot>.street1` | <=100 |
| Street 2 | `addresses.<slot>.street2` | <=100 |
| City | `addresses.<slot>.city` | <=100 |
| State | `addresses.<slot>.state` | Indian state dropdown — send the full name, backend converts to 2-letter code |
| Postal code | `addresses.<slot>.postal_code` | digits only, 6 chars |
| Country | `addresses.<slot>.country` | Country picker — default `IN` |

If a slot is entirely empty, omit that slot from the payload (don't send `"operation": {}`).

### D. Legal info
| Label | Field | Constraint |
|---|---|---|
| Company PAN | `pan` | `^[A-Za-z]{5}\d{4}[A-Za-z]$` — uppercase on blur. **If invalid, the backend silently drops it**; show inline error before submit so the user knows. |
| GSTIN | `gst` | `^[0-3][0-9][A-Za-z]{5}[0-9]{4}[A-Za-z][0-9][A-Za-z0-9]{2}$` — same drop behaviour. |

### E. Contact info (collapsible — three sub-sections)
For each of `chargeback`, `refund`, `support`:
| Label | Field |
|---|---|
| Email | `contact_info.<type>.email` |
| Phone | `contact_info.<type>.phone` (digits only) |
| Policy URL | `contact_info.<type>.policy_url` (http/https) |

### F. Apps
| Label | Field |
|---|---|
| Websites | `apps.websites[]` — chip / list input, validate `https?://` prefix |
| Android apps | `apps.android[]` — repeatable `{name, url}` |
| iOS apps | `apps.ios[]` — repeatable `{name, url}` |

---

## 4. Submission rules

```pseudo
function buildPatch(original, current):
  patch = {}
  for each top-level key in form:
    if current[key] != original[key] AND current[key] is not empty:
      patch[key] = current[key]
  return patch
```

- **Send only diffs.** Don't echo unchanged fields back — keeps the audit log clean.
- **Empty patch → no network call.** Show a toast: *"Nothing to update."*
- **Phone**: strip everything non-digit before adding to the patch. Don't show the user a 400 for spaces or `+91`.
- **State / country**: pass the human label (`"Karnataka"`, `"India"`); the backend normalises.
- **Nested objects** (`addresses`, `contact_info`, `apps`, `profile`): if any leaf inside changed, send the **whole sub-object** with all current values — Razorpay treats nested objects as replace-not-merge.

---

## 5. Error handling

Map response → UX exactly per this table. Do not invent UX for unlisted codes.

| HTTP | Body | UX |
|---|---|---|
| 200 | account row | Toast: *"Linked account updated."* → re-load form from response |
| 400 | `{detail: "..."}` | Toast: red, show `detail` verbatim. Keep the form open. |
| 403 | `{detail: "..."}` | Modal: *"You don't have permission to edit payment details."* with a single OK button. |
| 404 | `{detail: "..."}` | Modal: *"No linked account exists yet. Complete onboarding first."* → button: *"Go to onboarding"* (navigate to `/settings/payments/onboard`). |
| 422 | `{detail: [{loc, msg, type}, ...]}` | Map each `loc` element back to the corresponding form field and show the `msg` inline. `loc` is `["body", "<field>"]` or `["body", "<field>", "<subfield>"]`. |
| 502, 503 | `{detail: "..."}` | Toast: *"Razorpay is having a moment — try again in a minute."* Re-enable the Save button. |

**Specifically do not** treat these as errors:
- `reference_id` may be missing or different from what you sent in the 200 response (backend retry-without it).
- `pan` / `gst` may be missing from the 200 response (silently dropped if invalid — show a warning chip *"Razorpay rejected this value, please re-check"* if the field was in your patch but not in the response).
- State on the response is the 2-letter code (e.g. `"KA"`) even when you sent `"Karnataka"`.

---

## 6. State & caching

- Treat `GET /api/v1/razorpay-route/linked-account` as the single source of truth.
- Cache it for 60 s; invalidate immediately on any successful `PATCH` (replace cache with the response body).
- Don't poll while the edit screen is open.

---

## 7. Acceptance checklist

- [ ] Form pre-fills from the GET response.
- [ ] Only changed fields go in the PATCH body.
- [ ] Empty patch shows a toast and doesn't hit the network.
- [ ] All Pydantic regex/length rules from §3 are enforced client-side (so the user sees errors before submit, not as 422s).
- [ ] State / country accept the human label and don't show a validation error for `"Karnataka"`.
- [ ] PAN / GSTIN are uppercased on blur and validated against the regexes.
- [ ] Phone field strips `+`, spaces, dashes before adding to the patch.
- [ ] All 4xx/5xx codes produce the UX in §5.
- [ ] On 200, form re-syncs from the response (handles silent state normalisation, reference_id drop, PAN drop).
- [ ] Permission gate: read-only mode when the user lacks `razorpay.route.write`.

---

## 8. Reference: full PATCH payload

```jsonc
PATCH /api/v1/razorpay-route/linked-account
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "phone": "9876543210",
  "legal_business_name": "Acme Corp V2",
  "customer_facing_business_name": "Acme Diner",
  "reference_id": "partner_ref_123",
  "contact_name": "Gaurav Kumar",
  "notes": { "updated_by": "owner" },

  "category": "food",
  "subcategory": "restaurant",
  "business_model": "Restaurant chain",
  "addresses": {
    "registered": { "street1": "12 MG Road", "city": "Bengaluru", "state": "Karnataka", "postal_code": "560038", "country": "IN" },
    "operation":  { "street1": "5071 Koramangala", "city": "Bengaluru", "state": "KA", "postal_code": "560047", "country": "IN" }
  },

  "pan": "BAACL1234C",
  "gst": "10AABCU9603R1ZM",

  "contact_info": {
    "chargeback": { "email": "ops@acme.com",     "phone": "9876543210" },
    "refund":     { "email": "refunds@acme.com", "phone": "9876543210" },
    "support":    { "email": "help@acme.com",    "phone": "9876543210", "policy_url": "https://acme.com/policy" }
  },
  "apps": {
    "websites": ["https://acme.com"],
    "android":  [{ "name": "Acme Eats", "url": "https://play.google.com/store/apps/details?id=com.acme" }],
    "ios":      [{ "name": "Acme Eats", "url": "https://apps.apple.com/app/id1234567890" }]
  }
}
```

### Curl smoke test (paste into terminal to verify your backend works before wiring the UI)

```bash
curl -sS -X PATCH "$HOST/api/v1/razorpay-route/linked-account" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"customer_facing_business_name": "Acme Diner Renamed"}' | jq
```

Expected: 200 with the local account row, `updated_at` advanced by a few seconds.
