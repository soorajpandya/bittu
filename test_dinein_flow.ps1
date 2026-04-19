# Dine-in QR Table Complete Flow Test Script (PowerShell)
# Chains: SCAN -> CART -> PLACE-ORDER

$ApiBase = "https://api.bittupos.com/api/v1/dinein/qr"
$RestaurantId = "751c6d1d-1559-45f2-a24b-7ecd16678113"
$TableId = "8aa06d7b-b596-4450-9df1-01b8ccfd17dd"
$DeviceId = "ps-test-$(Get-Random)"
$ItemId = 221
$Quantity = 1

Write-Host "=== DINEIN TABLE FLOW TEST ===" -ForegroundColor Cyan
Write-Host "Restaurant: $RestaurantId"
Write-Host "Table: $TableId"
Write-Host "Device: $DeviceId"
Write-Host ""

# Step 1: SCAN QR Code
Write-Host "[1/3] SCANNING QR CODE..." -ForegroundColor Yellow
$ScanBody = @{
    restaurant_id = $RestaurantId
    table_id = $TableId
    device_id = $DeviceId
} | ConvertTo-Json

try {
    $ScanResp = Invoke-WebRequest -Uri "$ApiBase/scan" -Method Post -ContentType "application/json" -Body $ScanBody
    $ScanObj = $ScanResp.Content | ConvertFrom-Json
    Write-Host "Response: $($ScanResp.Content)"
    
    $SessionToken = $ScanObj.session_token
    if (-not $SessionToken) {
        throw "No session_token in response"
    }
    Write-Host "✓ Session Token: $SessionToken" -ForegroundColor Green
} catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    exit 1
}
Write-Host ""

# Step 2: ADD TO CART
Write-Host "[2/3] ADDING ITEM TO CART..." -ForegroundColor Yellow
$CartBody = @{
    session_token = $SessionToken
    item_id = $ItemId
    quantity = $Quantity
    device_id = $DeviceId
    request_id = "cart-$(Get-Date -UFormat %s)"
} | ConvertTo-Json

try {
    $CartResp = Invoke-WebRequest -Uri "$ApiBase/cart/add" -Method Post -ContentType "application/json" -Body $CartBody
    $CartObj = $CartResp.Content | ConvertFrom-Json
    Write-Host "Response: $($CartResp.Content)"
    
    $CartCount = $CartObj.cart_contains
    Write-Host "✓ Items in cart: $CartCount" -ForegroundColor Green
} catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    exit 1
}
Write-Host ""

# Step 3: PLACE ORDER
Write-Host "[3/3] PLACING ORDER..." -ForegroundColor Yellow
$PlaceBody = @{
    session_token = $SessionToken
    device_id = $DeviceId
    payment_method = "cash"
    request_id = "place-order-$(Get-Date -UFormat %s)"
} | ConvertTo-Json

try {
    $PlaceResp = Invoke-WebRequest -Uri "$ApiBase/place-order" -Method Post -ContentType "application/json" -Body $PlaceBody
    $PlaceObj = $PlaceResp.Content | ConvertFrom-Json
    Write-Host "Response: $($PlaceResp.Content)"
    
    $OrderId = $PlaceObj.order_id
    Write-Host "✓ Order ID: $OrderId" -ForegroundColor Green
} catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    if ($_.Exception.Response) {
        $sr = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        Write-Host "Response: $($sr.ReadToEnd())" -ForegroundColor Red
    }
    exit 1
}
Write-Host ""

Write-Host "=== COMPLETE ===" -ForegroundColor Cyan
Write-Host "Order successfully placed!" -ForegroundColor Green
