# Google Business Profile — API Reference

Base URL: `https://api.merabittu.com/api/v1`

All requests require: `-H "Authorization: Bearer <JWT_TOKEN>"`

---

## 1. Connect (start OAuth)

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/connect?restaurant_id=REST_ID" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Response: `{"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?...", "state": "..."}`

---

## 2. OAuth Callback

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/callback?code=AUTH_CODE&state=STATE_VALUE" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Response: `{"connected": true, "restaurant_id": "...", "id": "..."}`

---

## 3. Connection Status

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/status?restaurant_id=REST_ID" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Response: `{"connected": true, "account_id": "123", "location_id": "456", "location_name": "My Restaurant"}`

---

## 4. Disconnect

```bash
curl -X POST "https://api.merabittu.com/api/v1/google/disconnect" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"restaurant_id": "REST_ID"}'
```

Response: `{"disconnected": true}`

---

## 5. List Locations

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/locations?restaurant_id=REST_ID" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Response: `{"accounts": [...], "locations": {"123": [...]}}`

---

## 6. Select Location

```bash
curl -X POST "https://api.merabittu.com/api/v1/google/locations/select" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "restaurant_id": "REST_ID",
    "account_id": "123",
    "location_id": "456",
    "location_name": "My Restaurant - Downtown"
  }'
```

Response: `{"selected": true, ...}`

---

## 7. List Reviews

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/reviews?restaurant_id=REST_ID&page_size=50" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Paginate with: `&page_token=NEXT_TOKEN`

Response: `{"reviews": [...], "average_rating": 4.5, "total_review_count": 128, "next_page_token": null}`

---

## 8. Reply to Review

```bash
curl -X POST "https://api.merabittu.com/api/v1/google/review/reply" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "restaurant_id": "REST_ID",
    "review_id": "abc123",
    "reply_text": "Thank you for your kind words!"
  }'
```

Response: `{"comment": "Thank you for your kind words!", "updateTime": "..."}`

Returns 409 Conflict if review already has a reply.

---

## 9. Create Post

Standard post:

```bash
curl -X POST "https://api.merabittu.com/api/v1/google/post" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "restaurant_id": "REST_ID",
    "summary": "20% off all pizzas this weekend!",
    "action_type": "ORDER",
    "action_url": "https://merabittu.com/order",
    "image_url": "https://example.com/pizza.jpg"
  }'
```

action_type options: BOOK, ORDER, SHOP, SIGN_UP, LEARN_MORE, CALL

Event post:

```bash
curl -X POST "https://api.merabittu.com/api/v1/google/post" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "restaurant_id": "REST_ID",
    "summary": "Live music night!",
    "event": {"title": "Live Music", "schedule": {"startDate": {"year":2026,"month":4,"day":1}, "endDate": {"year":2026,"month":4,"day":1}}}
  }'
```

Offer post:

```bash
curl -X POST "https://api.merabittu.com/api/v1/google/post" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "restaurant_id": "REST_ID",
    "summary": "Use code SAVE20",
    "offer": {"couponCode": "SAVE20", "termsConditions": "Min order 500"}
  }'
```

---

## 10. List Posts

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/posts?restaurant_id=REST_ID&page_size=20" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Also works with singular alias:

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/post?restaurant_id=REST_ID" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Response: `{"posts": [...], "next_page_token": null}`

---

## 11. Insights (detailed metrics)

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/insights?restaurant_id=REST_ID&start_date=2026-03-01&end_date=2026-03-27" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Dates are optional — defaults to last 30 days.

Response:

```json
{
  "location_id": "456",
  "period": {"start": "2026-03-01", "end": "2026-03-27"},
  "metrics": {
    "CALL_CLICKS": [{"date": "2026-03-01", "value": 12}],
    "WEBSITE_CLICKS": [{"date": "2026-03-01", "value": 45}],
    "BUSINESS_DIRECTION_REQUESTS": [{"date": "2026-03-01", "value": 8}],
    "BUSINESS_BOOKINGS": [{"date": "2026-03-01", "value": 3}],
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": [...],
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": [...],
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS": [...],
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": [...]
  }
}
```

---

## 12. Insights Summary (dashboard card)

```bash
curl -X GET "https://api.merabittu.com/api/v1/google/insights/summary?restaurant_id=REST_ID&days=30" \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

Response:

```json
{
  "summary": {
    "total_impressions": 15420,
    "total_calls": 312,
    "total_website_clicks": 890,
    "total_direction_requests": 456,
    "total_bookings": 78,
    "period_days": 30
  }
}
```

---

## 13. Manual Sync (refresh all data)

```bash
curl -X POST "https://api.merabittu.com/api/v1/google/sync" \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"restaurant_id": "REST_ID"}'
```

Response: `{"restaurant_id": "...", "locations": 3, "reviews": 48, "posts": 5, "insights": 240}`

Returns `{"skipped": true, "reason": "Sync already in progress..."}` if another sync is running.

---

## Error Responses

| Code | Meaning |
|------|---------|
| 401  | Invalid/expired JWT |
| 403  | User doesn't own this restaurant |
| 404  | Google location not connected yet |
| 409  | Conflict (e.g. duplicate review reply) |
| 422  | Validation error (bad URL, missing field) |
