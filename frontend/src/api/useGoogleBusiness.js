/**
 * Google Business Profile — React Query hooks.
 *
 * Drop-in replacement for raw fetch/axios calls.
 * Prevents:
 *   - Infinite refetch loops (staleTime: 10 min, no window-focus refetch)
 *   - Retry storms on 429/500 (retry: 1, with 429 bail-out)
 *   - Duplicate parallel requests (React Query deduplication)
 *
 * Usage:
 *   import { useGoogleLocations, useGoogleReviews, ... } from "@/api/useGoogleBusiness";
 *   const { data, isLoading, error } = useGoogleLocations(restaurantId);
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "./axios"; // your axios instance — adjust the import path

// ── Shared defaults ──────────────────────────────────────────
const STALE_10_MIN = 10 * 60 * 1000;

/** Custom retry function: retry once, but NEVER retry 429 or 500+ */
function safeRetry(failureCount, error) {
  const status = error?.response?.status ?? error?.status;
  if (status === 429 || status >= 500) return false; // don't retry
  return failureCount < 1; // max 1 retry for other errors
}

/** Base options applied to every Google query */
const queryDefaults = {
  staleTime: STALE_10_MIN,
  refetchOnWindowFocus: false,
  refetchOnMount: false,
  retry: safeRetry,
};

// ── Helper ───────────────────────────────────────────────────

async function googleGet(path, params) {
  const { data } = await api.get(`/api/v1/google${path}`, { params });
  // If the backend signals a 429 via body flag, throw so React Query can surface it
  if (data?.rate_limited) {
    const err = new Error(data.message || "Rate limited — please wait a few seconds.");
    err.status = 429;
    err.isRateLimited = true;
    throw err;
  }
  return data;
}

async function googlePost(path, body) {
  const { data } = await api.post(`/api/v1/google${path}`, body);
  return data;
}

// ── Queries ──────────────────────────────────────────────────

/** Connection status (connected? which location?) */
export function useGoogleStatus(restaurantId) {
  return useQuery({
    queryKey: ["google", "status", restaurantId],
    queryFn: () => googleGet("/status", { restaurant_id: restaurantId }),
    enabled: !!restaurantId,
    ...queryDefaults,
  });
}

/** Locations list — accounts + locations */
export function useGoogleLocations(restaurantId) {
  return useQuery({
    queryKey: ["google", "locations", restaurantId],
    queryFn: () => googleGet("/locations", { restaurant_id: restaurantId }),
    enabled: !!restaurantId,
    ...queryDefaults,
    placeholderData: { connected: false, accounts: [], locations: {} },
  });
}

/** Reviews list */
export function useGoogleReviews(restaurantId, pageSize = 50, pageToken = null) {
  return useQuery({
    queryKey: ["google", "reviews", restaurantId, pageSize, pageToken],
    queryFn: () =>
      googleGet("/reviews", {
        restaurant_id: restaurantId,
        page_size: pageSize,
        ...(pageToken && { page_token: pageToken }),
      }),
    enabled: !!restaurantId,
    ...queryDefaults,
    placeholderData: {
      connected: false,
      reviews: [],
      average_rating: null,
      total_review_count: 0,
      next_page_token: null,
    },
  });
}

/** Posts list */
export function useGooglePosts(restaurantId, pageSize = 20, pageToken = null) {
  return useQuery({
    queryKey: ["google", "posts", restaurantId, pageSize, pageToken],
    queryFn: () =>
      googleGet("/posts", {
        restaurant_id: restaurantId,
        page_size: pageSize,
        ...(pageToken && { page_token: pageToken }),
      }),
    enabled: !!restaurantId,
    ...queryDefaults,
    placeholderData: { connected: false, posts: [], next_page_token: null },
  });
}

/** Performance insights with date range */
export function useGoogleInsights(restaurantId, startDate, endDate) {
  return useQuery({
    queryKey: ["google", "insights", restaurantId, startDate, endDate],
    queryFn: () =>
      googleGet("/insights", {
        restaurant_id: restaurantId,
        ...(startDate && { start_date: startDate }),
        ...(endDate && { end_date: endDate }),
      }),
    enabled: !!restaurantId,
    ...queryDefaults,
    placeholderData: {
      connected: false,
      location_id: null,
      location_name: "",
      period: null,
      metrics: {},
    },
  });
}

/** Insights summary (growth dashboard) */
export function useGoogleInsightsSummary(restaurantId, days = 30) {
  return useQuery({
    queryKey: ["google", "insights-summary", restaurantId, days],
    queryFn: () => googleGet("/insights/summary", { restaurant_id: restaurantId, days }),
    enabled: !!restaurantId,
    ...queryDefaults,
  });
}

// ── Mutations ────────────────────────────────────────────────

/** Disconnect Google account */
export function useGoogleDisconnect() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (restaurantId) => googlePost("/disconnect", { restaurant_id: restaurantId }),
    onSuccess: (_data, restaurantId) => {
      // Invalidate all google queries for this restaurant — once, not a loop
      qc.invalidateQueries({ queryKey: ["google"], exact: false });
    },
  });
}

/** Select a location */
export function useGoogleSelectLocation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ restaurantId, accountId, locationId, locationName }) =>
      googlePost("/locations/select", {
        restaurant_id: restaurantId,
        account_id: accountId,
        location_id: locationId,
        location_name: locationName,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["google"] });
    },
  });
}

/** Reply to a review */
export function useGoogleReplyToReview() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ restaurantId, reviewId, replyText }) =>
      googlePost("/review/reply", {
        restaurant_id: restaurantId,
        review_id: reviewId,
        reply_text: replyText,
      }),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ["google", "reviews", variables.restaurantId] });
    },
  });
}

/** Create a post */
export function useGoogleCreatePost() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body) => googlePost("/post", body),
    onSuccess: (_data, body) => {
      qc.invalidateQueries({ queryKey: ["google", "posts", body.restaurant_id] });
    },
  });
}

/** Trigger full sync */
export function useGoogleSync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (restaurantId) => googlePost("/sync", { restaurant_id: restaurantId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["google"] });
    },
  });
}
