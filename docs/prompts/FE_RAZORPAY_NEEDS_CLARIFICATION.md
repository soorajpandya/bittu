# Frontend prompt — Razorpay Route "Action required" / `needs_clarification` flow

> **Backend state at time of writing**: deploy `e6a882f` on `ec2-13-206-196-252`.
> **Endpoint shape**: unchanged. This prompt is purely about FE rendering + a re-submit flow.
> **Affected screen**: the Razorpay Route / linked-account onboarding/status screen
> (the one that today shows *"Action required — could not load your linked account status"*
> or *"Activation pending"*).

---

## 1. The bug the merchant just reported

> *"Inside Razorpay our linked account is activated, but the app keeps saying **action required**."*

This is almost always **not** a bug — the merchant is reading the Razorpay
Dashboard wrong. Razorpay tracks **two separate states**:

| Layer | Razorpay term | DB column | What it means |
|---|---|---|---|
| 1. Linked account | `accounts/{id}.status` | `rzp_route_accounts.status` | The shell account exists; legal/contact info accepted. Often shows `Activated` on the Dashboard even when settlements are not live. |
| 2. **Route product** (the thing that actually moves money) | `accounts/{id}/products/{prod}.activation_status` | `rzp_route_accounts.route_product_status` | The settlements pipe. Until this is `activated`, **no transfers can flow** to the merchant's bank. |

For the merchant we're debugging right now
(linked account `acc_StagfS3luInjXk`, product `acc_prd_StaglJzTRaibZn`),
the backend has the latest gateway truth in
`rzp_route_accounts.route_product_raw`:

```json
{
  "activation_status": "needs_clarification",
  "requirements": [
    { "status": "required",
      "reason_code": "needs_clarification",
      "field_reference": "settlements.ifsc_code",
      "description": "Entered bank details are incorrect, please share company bank account details or authorised signatory details.",
      "resolution_url": "/accounts/acc_StagfS3luInjXk/products/acc_prd_StaglJzTRaibZn" },
    { "field_reference": "settlements.beneficiary_name", "...": "..." },
    { "field_reference": "settlements.account_number",   "...": "..." }
  ],
  "active_configuration": {
    "settlements": {
      "ifsc_code": "ICIC0004040",
      "account_number": "404001500182",
      "beneficiary_name": "Urvi Pandya"
    }
  }
}
```

Razorpay is rejecting the bank account triple because the beneficiary name on
the bank account does **not** match what was submitted, or the IFSC+account
number combination failed penny-drop verification. The merchant must
**re-submit corrected bank details** — the FE is correctly showing "Action
required" because the backend correctly reports
`effective_status: "needs_clarification"`.

---

## 2. Endpoint contract (already deployed — no backend change needed)

### 2.1 `GET /api/v1/razorpay-route/linked-account`

Returns the merchant's local route row + a derived `effective_status`:

```jsonc
{
  "merchant_id": "c8b9c75f-…",
  "linked_account_id": "acc_StagfS3luInjXk",
  "status": "created",                       // raw account status (don't branch on this)
  "kyc_status": "created",
  "route_product_id": "acc_prd_StaglJzTRaibZn",
  "route_product_status": "needs_clarification",
  "route_product_raw": { /* full Razorpay product payload incl. requirements[] */ },
  "raw_payload": { /* full Razorpay account payload */ },
  "effective_status": "needs_clarification", // ← BRANCH UI OFF THIS, NOTHING ELSE
  "tnc_accepted_at": "2026-05-25T12:05:37Z",
  ...
}
```

`effective_status` is one of:

| Value | Meaning | FE screen |
|---|---|---|
| `pending`              | No linked account yet                       | "Start KYC" CTA |
| `submitted`            | Account created, no Route product requested | "Provide bank details" form |
| `under_review`         | Product requested, Razorpay is reviewing    | Spinner + "Usually 1 business day" |
| **`needs_clarification`** | **Razorpay needs corrections**           | **The screen this prompt fixes** |
| `activated`            | Live — transfers will flow                  | Green success state |
| `rejected`             | Hard rejection                              | Contact support state |
| `suspended`            | Account suspended on the gateway            | Contact support state |

### 2.2 `PATCH /api/v1/razorpay-route/linked-account/product`

Re-submits bank details + (optionally) re-accepts TOS. Required permission: `razorpay.route.write` (owners already have it post `e6a882f`).

```jsonc
// request
{
  "bank_account_number": "404001500182",
  "ifsc":                "ICIC0004040",
  "beneficiary_name":    "URVI PANDYA",          // ← match the bank's "as printed on cheque" name
  "tnc_accepted":        true                    // safe to always send true
}

// response: same shape as GET /linked-account, with refreshed route_product_status
```

After a successful PATCH the backend immediately re-syncs from Razorpay, so the
response already reflects whether the new bank triple was accepted
(`under_review` again) or rejected on the spot (`needs_clarification`,
typically with the same requirements).

### 2.3 `POST /api/v1/razorpay-route/linked-account/product/sync`

Manual "refresh from Razorpay now" button. Use this when the user taps a
**Refresh** button on the status screen instead of waiting for the 8-second
opportunistic refresh that `GET /linked-account` already does.

---

## 3. UI to render when `effective_status == "needs_clarification"`

Replace the current generic *"Action required — could not load your linked
account status"* error screen with a structured screen:

```
┌─────────────────────────────────────────────────────────────┐
│  ⚠  Razorpay needs a small correction                       │
│                                                             │
│  Your linked account is created, but Razorpay couldn't      │
│  verify the bank account you submitted. You're 1 step       │
│  away from going live.                                      │
│                                                             │
│  What Razorpay is asking for:                               │
│  • Bank account number                                      │
│  • IFSC code                                                │
│  • Beneficiary name (must exactly match your bank's         │
│    "name on cheque")                                        │
│                                                             │
│  Current submission                                         │
│    Account number   •••••• 0182                             │
│    IFSC             ICIC0004040                             │
│    Beneficiary      Urvi Pandya                             │
│                                                             │
│  ┌─────────────────────────────────────────────────┐        │
│  │  [ Re-submit bank details ]                     │        │
│  └─────────────────────────────────────────────────┘        │
│                                                             │
│  After re-submitting, Razorpay usually verifies within      │
│  a few minutes. We'll refresh this screen automatically.    │
└─────────────────────────────────────────────────────────────┘
```

### 3.1 Map Razorpay's `requirements[]` → human bullets

Group `route_product_raw.requirements` by their root section (split
`field_reference` on `.`, take first segment) and dedupe descriptions:

```dart
// pseudocode
final reqs = (data['route_product_raw']?['requirements'] as List?) ?? [];
final grouped = <String, Set<String>>{}; // section -> set of field labels
for (final r in reqs) {
  if (r['status'] != 'required') continue;
  final ref = (r['field_reference'] ?? '') as String;
  final section = ref.split('.').first;           // "settlements"
  final field   = ref.split('.').skip(1).join('.'); // "ifsc_code"
  grouped.putIfAbsent(section, () => {}).add(_humanizeField(field));
}
```

Human labels (`_humanizeField`):

| `field_reference`                | Bullet |
|---|---|
| `settlements.ifsc_code`          | IFSC code |
| `settlements.account_number`     | Bank account number |
| `settlements.beneficiary_name`   | Beneficiary name |
| `settlements.*` (catch-all)      | Bank account details |
| `legal_info.pan`                 | PAN |
| `legal_info.gst`                 | GST |
| `stakeholder.*`                  | Owner / signatory details |
| `profile.addresses.*`            | Registered address |
| anything else                    | The raw `field_reference`, prettified (replace `_` with space, title-case) |

Show the **distinct `description`** strings from `requirements[]` verbatim,
under a small "Razorpay's note" caption — the messages from Razorpay are
usually the only specific hint the merchant gets (e.g. *"Entered bank details
are incorrect, please share … authorised signatory details."*).

### 3.2 Show the current submission

Pull from `route_product_raw.active_configuration.settlements` (or fall back to
the local `bank_account_ifsc` + `bank_account_last4` columns):

```dart
final s = data['route_product_raw']?['active_configuration']?['settlements'] ?? {};
final acct = s['account_number']?.toString() ?? '';
final masked = acct.length > 4 ? '•••••• ${acct.substring(acct.length - 4)}' : acct;
```

Never display the **full** account number — always mask to last 4. (The
backend will continue to return the full value; masking is a FE-only concern.)

### 3.3 Re-submit form

A simple bottom-sheet / dialog with three fields:

| Field | Validation |
|---|---|
| Bank account number | digits only, length 6–18 |
| IFSC                | regex `^[A-Z]{4}0[A-Z0-9]{6}$` (uppercase) |
| Beneficiary name    | letters / spaces / `.` only, 2–60 chars, auto-uppercase on submit |
| TOS                 | already accepted at first onboarding → checkbox prefilled + locked-on; payload always sends `tnc_accepted: true` |

Submit handler:

```dart
final res = await api.patch(
  '/api/v1/razorpay-route/linked-account/product',
  body: {
    'bank_account_number': accountController.text.trim(),
    'ifsc':                ifscController.text.trim().toUpperCase(),
    'beneficiary_name':    nameController.text.trim().toUpperCase(),
    'tnc_accepted':        true,
  },
);

// res.body has the same shape as GET /linked-account
final newStatus = res['effective_status'];
if (newStatus == 'needs_clarification') {
  // Razorpay rejected the new triple too → keep merchant on this screen,
  // refresh `requirements[]`, surface the new description.
  showToast('Razorpay still couldn\'t verify these details. Please double-check the beneficiary name on your bank passbook.');
} else {
  // 'under_review' / 'activated' — flip to that screen.
}
```

### 3.4 Always-visible "Refresh" affordance

A small "Refresh status" icon button in the app bar that calls
`POST /razorpay-route/linked-account/product/sync` and re-renders. Useful for
merchants who corrected things directly on the Razorpay Dashboard email link
(see §4).

---

## 4. Copy — the part the user keeps misreading

Add this collapsed *"Why does Razorpay say it's activated?"* help panel below
the form:

> **The Razorpay Dashboard shows two states for every merchant:**
>
> 1. **Account status** — *"Activated"* means Razorpay accepted your
>    business profile. That happens within minutes.
> 2. **Route product status** — this is what actually lets us settle money
>    into your bank. Until this says *"Activated"*, payouts cannot start.
>
> You are at step 2. Razorpay needs to verify your bank account by sending a
> ₹1 test credit (penny-drop). When the beneficiary name or IFSC don't match
> exactly, that test fails and you land here. Re-submit the exact details
> from your bank passbook to clear it.

---

## 5. Status mapping reference (use **only** `effective_status`)

| `effective_status`     | Screen                              | Primary CTA              |
|---|---|---|
| `pending`              | "Start KYC"                         | Start onboarding         |
| `submitted`            | "Add bank details"                  | Open product/bank form   |
| `under_review`         | "Awaiting Razorpay (≈ 1 biz day)"   | Refresh                  |
| `needs_clarification`  | **This prompt's screen**            | **Re-submit bank details** |
| `activated`            | "Live — payouts will flow"          | View payouts             |
| `rejected`             | "Activation rejected"               | Contact support          |
| `suspended`            | "Account suspended"                 | Contact support          |

Do **not** read `status` or `kyc_status` to drive UI state. They are
informational only. Specifically: `status: "created"` while
`effective_status: "activated"` is normal — the V2 accounts API doesn't flip
the account-level status to anything past `created` even after the product
goes live.

---

## 6. Polling / realtime

- While the user is on the linked-account screen and `effective_status` ∈
  `{submitted, under_review, needs_clarification}`, poll
  `GET /razorpay-route/linked-account` every **5–8 seconds**.
  The backend already throttles its upstream Razorpay refresh to once per 8s,
  so this is safe.
- Stop polling immediately once `effective_status` becomes `activated`,
  `rejected`, or `suspended`.
- The `merchant_wallet_updated` WS event also fires on activation — listen
  for it to short-circuit the poll loop.

---

## 7. Acceptance checklist

- [ ] `effective_status == "needs_clarification"` no longer shows the generic
      "could not load your linked account status" error.
- [ ] Each item in `route_product_raw.requirements[]` is rendered as a
      human-readable bullet via the field map in §3.1.
- [ ] Razorpay's `description` strings are shown verbatim under a
      "Razorpay's note" caption.
- [ ] The current bank submission (last-4 account + IFSC + beneficiary) is
      visible.
- [ ] A "Re-submit bank details" form posts to
      `PATCH /api/v1/razorpay-route/linked-account/product` and re-reads the
      response to update the screen.
- [ ] A "Refresh status" button calls `POST /linked-account/product/sync`.
- [ ] Polling stops on terminal states (`activated` / `rejected` / `suspended`).
- [ ] Help panel from §4 explains the dashboard ≠ product distinction.
- [ ] UI branches **only** on `effective_status` (audit: no FE references to
      `status` / `kyc_status` / `route_product_status` for branching).

---

## 8. For the specific merchant blocking us right now

`acc_StagfS3luInjXk` / `acc_prd_StaglJzTRaibZn` — beneficiary submitted as
`Urvi Pandya`. Ask the merchant to:

1. Check the **exact** name printed on their bank passbook / cheque
   (commonly `URVI PANDYA` or `URVI DIPAKBHAI PANDYA`).
2. Confirm IFSC `ICIC0004040` (note: 4 zeros in `0004040` — make sure they
   didn't type `ICICI0004040` with an extra `I`).
3. Confirm the 12-digit account number from the same passbook.
4. Tap **Re-submit bank details** with the exact triple.

If the third resubmission still fails, the next step is to switch the
beneficiary to the proprietor's personal account (single-name proprietorship)
or upload a signed letter via Razorpay support — but that's outside the FE
scope.
