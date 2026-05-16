\echo === webhooks last 30 min ===
SELECT gateway, event_type, processing_state, received_at
  FROM payment_webhook_events
 WHERE received_at > NOW() - INTERVAL '30 minutes'
 ORDER BY received_at DESC LIMIT 10;

\echo === last 3 rzp_orders ===
SELECT razorpay_order_id, status, amount_paise, amount_paid_paise, created_at
  FROM rzp_orders ORDER BY created_at DESC LIMIT 3;

\echo === last 3 qr_codes ===
SELECT qr_id, status, COALESCE(length(image_content),0) AS ic_len,
       COALESCE(length(image_url),0)     AS iu_len,
       close_by, created_at
  FROM rzp_qr_codes ORDER BY created_at DESC LIMIT 3;

\echo === payments last 30 min ===
SELECT id, status, method, razorpay_order_id, razorpay_payment_id, created_at
  FROM payments
 WHERE created_at > NOW() - INTERVAL '30 minutes'
 ORDER BY created_at DESC LIMIT 5;
