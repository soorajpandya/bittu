"""
Razorpay → internal-ledger bridge (Phase 8).

Phase 1 placeholder. Subsequent phases provide:
  * `apply_payment_capture()` — fee_engine.compute → merchant_ledger credit
    + escrow hold + financial_events.append
  * `apply_refund()` — merchant_ledger debit + escrow consumption
  * `apply_settlement()` — merchant_liability settlement_obligation closeout
  * `apply_route_transfer()` — platform_revenue + commission posting
  * `apply_dispute_lost()` — chargeback debit
"""
