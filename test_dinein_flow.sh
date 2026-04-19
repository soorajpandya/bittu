#!/bin/bash

# Dine-in QR Table Complete Flow Test Script
# Chains: SCAN → CART → PLACE-ORDER

API_BASE="https://api.bittupos.com/api/v1/dinein/qr"
RESTAURANT_ID="751c6d1d-1559-45f2-a24b-7ecd16678113"
TABLE_ID="8aa06d7b-b596-4450-9df1-01b8ccfd17dd"
DEVICE_ID="curl-test-device-$(date +%s)"
ITEM_ID=221
QUANTITY=1

echo "=== DINEIN TABLE FLOW TEST ==="
echo "Restaurant: $RESTAURANT_ID"
echo "Table: $TABLE_ID"
echo "Device: $DEVICE_ID"
echo ""

# Step 1: SCAN QR Code
echo "[1/3] SCANNING QR CODE..."
SCAN_RESPONSE=$(curl -s -X POST "$API_BASE/scan" \
  -H "Content-Type: application/json" \
  -d "{
    \"restaurant_id\": \"$RESTAURANT_ID\",
    \"table_id\": \"$TABLE_ID\",
    \"device_id\": \"$DEVICE_ID\"
  }")

echo "Response: $SCAN_RESPONSE"
SESSION_TOKEN=$(echo "$SCAN_RESPONSE" | grep -o '"session_token":"[^"]*' | cut -d'"' -f4)

if [ -z "$SESSION_TOKEN" ]; then
  echo "ERROR: Failed to get session token"
  exit 1
fi

echo "✓ Session Token: $SESSION_TOKEN"
echo ""

# Step 2: ADD TO CART
echo "[2/3] ADDING ITEM TO CART..."
CART_RESPONSE=$(curl -s -X POST "$API_BASE/cart/add" \
  -H "Content-Type: application/json" \
  -d "{
    \"session_token\": \"$SESSION_TOKEN\",
    \"item_id\": $ITEM_ID,
    \"quantity\": $QUANTITY,
    \"device_id\": \"$DEVICE_ID\",
    \"request_id\": \"cart-$(date +%s)\"
  }")

echo "Response: $CART_RESPONSE"
CART_COUNT=$(echo "$CART_RESPONSE" | grep -o '"cart_contains":[0-9]*' | cut -d':' -f2)
echo "✓ Items in cart: $CART_COUNT"
echo ""

# Step 3: PLACE ORDER
echo "[3/3] PLACING ORDER..."
PLACE_RESPONSE=$(curl -s -X POST "$API_BASE/place-order" \
  -H "Content-Type: application/json" \
  -d "{
    \"session_token\": \"$SESSION_TOKEN\",
    \"device_id\": \"$DEVICE_ID\",
    \"payment_method\": \"cash\",
    \"request_id\": \"place-order-$(date +%s)\"
  }")

echo "Response: $PLACE_RESPONSE"
ORDER_ID=$(echo "$PLACE_RESPONSE" | grep -o '"order_id":"[^"]*' | cut -d'"' -f4)
echo "✓ Order ID: $ORDER_ID"
echo ""

echo "=== COMPLETE ==="
