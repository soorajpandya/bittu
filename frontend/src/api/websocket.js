/**
 * WebSocket connection manager for Bittu admin dashboard.
 *
 * Usage (React):
 *   import { useTableWebSocket } from '../api/websocket';
 *
 *   function TableDashboard() {
 *     const { connected, events, clearEvents } = useTableWebSocket(token, restaurantId);
 *
 *     useEffect(() => {
 *       events.forEach(evt => {
 *         if (evt.event === 'table.session_started') refetchTables();
 *         if (evt.event === 'table.call_waiter')     addAssistanceRequest(evt.data);
 *       });
 *       clearEvents();
 *     }, [events]);
 *   }
 */

const WS_BASE =
  (import.meta.env.VITE_API_URL || "https://api.merabittu.com").replace(
    /^http/,
    "ws"
  );

// ── Low-level WebSocket manager ──

class BittuWebSocket {
  constructor(token, { onEvent, onConnect, onDisconnect } = {}) {
    this._token = token;
    this._onEvent = onEvent || (() => {});
    this._onConnect = onConnect || (() => {});
    this._onDisconnect = onDisconnect || (() => {});
    this._ws = null;
    this._reconnectTimer = null;
    this._reconnectDelay = 1000;
    this._maxReconnectDelay = 30000;
    this._destroyed = false;
    this._pendingSubscriptions = [];
  }

  connect() {
    if (this._destroyed) return;
    try {
      this._ws = new WebSocket(`${WS_BASE}/ws?token=${this._token}`);
    } catch {
      this._scheduleReconnect();
      return;
    }

    this._ws.onopen = () => {
      this._reconnectDelay = 1000;
      // Re-subscribe to any pending channels
      this._pendingSubscriptions.forEach((ch) => this._send("subscribe", ch));
    };

    this._ws.onmessage = (e) => {
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }

      if (msg.event === "ping") {
        this._sendRaw(JSON.stringify({ action: "pong" }));
        return;
      }

      if (msg.event === "connected") {
        this._onConnect(msg.data);
        return;
      }

      if (msg.event === "subscribed" || msg.event === "unsubscribed") {
        return;
      }

      // All other events — forward to callback
      this._onEvent(msg);
    };

    this._ws.onclose = () => {
      this._onDisconnect();
      this._scheduleReconnect();
    };

    this._ws.onerror = () => {
      // onclose will fire after this
    };
  }

  subscribe(channel) {
    if (!this._pendingSubscriptions.includes(channel)) {
      this._pendingSubscriptions.push(channel);
    }
    this._send("subscribe", channel);
  }

  unsubscribe(channel) {
    this._pendingSubscriptions = this._pendingSubscriptions.filter(
      (c) => c !== channel
    );
    this._send("unsubscribe", channel);
  }

  destroy() {
    this._destroyed = true;
    clearTimeout(this._reconnectTimer);
    if (this._ws) {
      this._ws.onclose = null; // prevent reconnect
      this._ws.close();
      this._ws = null;
    }
  }

  get connected() {
    return this._ws?.readyState === WebSocket.OPEN;
  }

  _send(action, channel) {
    if (this._ws?.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify({ action, channel }));
    }
  }

  _sendRaw(data) {
    if (this._ws?.readyState === WebSocket.OPEN) {
      this._ws.send(data);
    }
  }

  _scheduleReconnect() {
    if (this._destroyed) return;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectDelay = Math.min(
        this._reconnectDelay * 2,
        this._maxReconnectDelay
      );
      this.connect();
    }, this._reconnectDelay);
  }
}

// ── React hook: useTableWebSocket ──

import { useState, useEffect, useRef, useCallback } from "react";

/**
 * React hook for real-time table events.
 *
 * @param {string} token - JWT auth token
 * @param {string} restaurantId - restaurant UUID (for restaurant-wide events)
 * @returns {{ connected: boolean, events: Array, clearEvents: Function, assistanceRequests: Array, dismissRequest: Function }}
 */
export function useTableWebSocket(token, restaurantId) {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState([]);
  const [assistanceRequests, setAssistanceRequests] = useState([]);
  const wsRef = useRef(null);

  useEffect(() => {
    if (!token) return;

    const ws = new BittuWebSocket(token, {
      onEvent: (msg) => {
        setEvents((prev) => [...prev, msg]);

        // Track assistance requests (call waiter)
        if (msg.event === "table.call_waiter") {
          setAssistanceRequests((prev) => [
            ...prev,
            {
              id: `${msg.data?.payload?.table_id}-${Date.now()}`,
              table_number: msg.data?.payload?.table_number,
              request_type: msg.data?.payload?.request_type || "assistance",
              timestamp: msg.data?.timestamp || new Date().toISOString(),
              session_id: msg.data?.payload?.session_id,
            },
          ]);
        }
      },
      onConnect: () => setConnected(true),
      onDisconnect: () => setConnected(false),
    });

    ws.connect();

    // Subscribe to restaurant-wide channel for table events
    if (restaurantId) {
      ws.subscribe(`restaurant:${restaurantId}`);
    }

    wsRef.current = ws;

    return () => {
      ws.destroy();
      wsRef.current = null;
    };
  }, [token, restaurantId]);

  const clearEvents = useCallback(() => setEvents([]), []);

  const dismissRequest = useCallback((requestId) => {
    setAssistanceRequests((prev) => prev.filter((r) => r.id !== requestId));
  }, []);

  return {
    connected,
    events,
    clearEvents,
    assistanceRequests,
    dismissRequest,
  };
}

// ── Table-specific event types (for filtering) ──

export const TABLE_EVENTS = {
  SESSION_STARTED: "table.session_started",
  SESSION_ENDED: "table.session_ended",
  CART_UPDATED: "table.cart_updated",
  ORDER_PLACED: "table.order_placed",
  STATUS_CHANGED: "table.status_changed",
  CALL_WAITER: "table.call_waiter",
};

export const KITCHEN_EVENTS = {
  ORDER_CREATED: "kitchen.order_created",
  STATUS_CHANGED: "kitchen.status_changed",
  ITEM_READY: "kitchen.item_ready",
};

export const ORDER_EVENTS = {
  CREATED: "order.created",
  CONFIRMED: "order.confirmed",
  STATUS_CHANGED: "order.status_changed",
  CANCELLED: "order.cancelled",
};

export default BittuWebSocket;
