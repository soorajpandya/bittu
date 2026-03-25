/**
 * Subscription API hooks for Bittu frontend.
 * Uses the backend at VITE_API_URL.
 */

const API_BASE = import.meta.env.VITE_API_URL || "https://api.merabittu.com";

function getHeaders(token) {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function handleResponse(res) {
  const data = await res.json();
  if (!res.ok) {
    const error = new Error(data.detail || "Request failed");
    error.status = res.status;
    error.code = data.error_code;
    throw error;
  }
  return data;
}

// ─── Public (no auth) ────────────────────────────────────────

export async function fetchPlans() {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/plans`);
  return handleResponse(res);
}

export async function fetchAddons() {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/addons`);
  return handleResponse(res);
}

// ─── Authenticated ───────────────────────────────────────────

export async function getSubscription(token) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions`, {
    headers: getHeaders(token),
  });
  return handleResponse(res);
}

export async function getSubscriptionStatus(token) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/status`, {
    headers: getHeaders(token),
  });
  return handleResponse(res);
}

export async function verifySubscription(token) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/verify`, {
    method: "POST",
    headers: getHeaders(token),
  });
  return handleResponse(res);
}

export async function startFreeTrial(token) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/free-trial`, {
    method: "POST",
    headers: getHeaders(token),
  });
  return handleResponse(res);
}

export async function subscribe(token, planSlug) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/subscribe`, {
    method: "POST",
    headers: getHeaders(token),
    body: JSON.stringify({ plan_slug: planSlug }),
  });
  return handleResponse(res);
}

export async function cancelSubscription(token) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/cancel`, {
    method: "POST",
    headers: getHeaders(token),
  });
  return handleResponse(res);
}

export async function upgradePlan(token, newPlanSlug) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/upgrade`, {
    method: "POST",
    headers: getHeaders(token),
    body: JSON.stringify({ new_plan_slug: newPlanSlug }),
  });
  return handleResponse(res);
}

export async function downgradePlan(token, newPlanSlug) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/downgrade`, {
    method: "POST",
    headers: getHeaders(token),
    body: JSON.stringify({ new_plan_slug: newPlanSlug }),
  });
  return handleResponse(res);
}

export async function purchaseAddon(token, addonSlug, quantity = 1, shippingAddress = null) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/addons/purchase`, {
    method: "POST",
    headers: getHeaders(token),
    body: JSON.stringify({
      addon_slug: addonSlug,
      quantity,
      shipping_address: shippingAddress,
    }),
  });
  return handleResponse(res);
}
