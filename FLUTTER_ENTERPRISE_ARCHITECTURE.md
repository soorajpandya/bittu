# Bittu POS — Enterprise Flutter Architecture Guide

## For: Flutter Developer | From: Backend Architect
## Version: v2 | Date: May 2026

> **This document supersedes Section 6 of `FLUTTER_FRONTEND_GUIDE.md`.**
> Follow every rule here. The patterns here are non-negotiable for production-scale POS operations.

---

## WHY THIS EXISTS

The v1 architecture produced:

- Repeated API calls on every tab switch
- Finance screen rebuilding from scratch repeatedly
- UI skeleton flashing on every navigation
- Providers recreated on every screen push
- Network calls initiated from `initState` and `build` methods
- No in-memory or persistent caching
- Scroll positions and filters lost on tab switch

This is **unacceptable** for a production POS used by restaurants, hotels, and food chains at high transaction volume.

The new architecture follows the same engineering standards as PhonePe, Razorpay Dashboard, and Swiggy Partner App.

---

## ARCHITECTURE OVERVIEW

```
┌─────────────────────────────────────────────────────────────────┐
│                         UI LAYER                                 │
│  Widgets subscribe to state — they NEVER initiate network calls  │
│  Only renders changed components (granular listeners)            │
└──────────────────────────────┬──────────────────────────────────┘
                               │ subscribes
┌──────────────────────────────▼──────────────────────────────────┐
│                      STATE LAYER (Providers)                     │
│  Persistent — survive navigation, tab switches, and rebuilds     │
│  Owned by top-level MultiProvider (never recreated)             │
│  Notify listeners only when specific data changes               │
└──────────────────────────────┬──────────────────────────────────┘
                               │ reads from / writes to
┌──────────────────────────────▼──────────────────────────────────┐
│                      CACHE LAYER                                 │
│  L1: In-memory Map (instant, per session)                        │
│  L2: SharedPreferences / Hive (persists between app launches)   │
│  L3: Background sync from API (stale-while-revalidate)          │
└──────────────────────────────┬──────────────────────────────────┘
                               │ syncs via
┌──────────────────────────────▼──────────────────────────────────┐
│                   NETWORK ORCHESTRATION LAYER                    │
│  Centralized request manager                                     │
│  Deduplicates in-flight requests                                │
│  Throttles repeated calls                                        │
│  Retry with exponential backoff                                  │
│  Offline queue                                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. PROJECT STRUCTURE

```
lib/
  main.dart                          ← App entry: wire all persistent providers here
  app.dart                           ← MaterialApp + persistent navigation scaffold

  core/
    network/
      api_client.dart                ← Dio with auth interceptor + token refresh
      api_endpoints.dart             ← All endpoint constants
      request_manager.dart           ← Dedup / in-flight tracking / throttle
      retry_policy.dart              ← Exponential backoff config
    cache/
      memory_cache.dart              ← L1 in-memory store (Map + TTL)
      local_cache.dart               ← L2 Hive/SharedPrefs store
      cache_policy.dart              ← TTL constants per resource type
    state/
      app_state.dart                 ← Root AppState: auth, permissions, context
    auth/
      auth_provider.dart             ← Token storage, refresh, logout
      permission_guard.dart          ← Permission-based UI guard widget
    navigation/
      app_router.dart                ← GoRouter config
      navigation_state.dart          ← Persistent tab/page state
    websocket/
      ws_manager.dart                ← Singleton WS with auto-reconnect

  features/
    shell/
      main_shell.dart                ← Persistent scaffold: IndexedStack for tabs
      shell_provider.dart            ← Tab index state
    dashboard/
      providers/dashboard_provider.dart
      screens/dashboard_screen.dart
      widgets/
    orders/
      providers/orders_provider.dart
      screens/orders_screen.dart
      widgets/
    finance/
      providers/
        finance_provider.dart        ← Always-hot, never disposed
        statement_provider.dart      ← Persistent statement state
        transactions_provider.dart   ← Paginated, cached, persistent
      screens/
        finance_shell_screen.dart    ← Finance tab wrapper (IndexedStack)
        statement_screen.dart
        transactions_screen.dart
        settlements_screen.dart
      widgets/
    kitchen/
      providers/kitchen_provider.dart
      screens/kitchen_screen.dart
    tables/
      providers/tables_provider.dart
      screens/tables_screen.dart
    menu/
      providers/menu_provider.dart
      screens/
    staff/
      providers/staff_provider.dart
      screens/
    settings/
      providers/settings_provider.dart
      screens/

  shared/
    widgets/
      cached_data_builder.dart       ← Renders cache-first, refreshes silently
      skeleton_guard.dart            ← Shows skeleton ONLY on first load
      stale_banner.dart              ← "Last updated X ago" for offline mode
    utils/
      debouncer.dart
      connectivity_monitor.dart
```

---

## 2. RULE 1 — PROVIDERS ARE PERSISTENT (NEVER RECREATED)

All feature providers must be injected at the **root** of the widget tree, above `MaterialApp`.
They **must never** be created inside a screen's `build` method or inside a navigator route.

### ✅ Correct: Root-level providers

```dart
// main.dart
void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await LocalCache.init();             // Hive box open once
  await MemoryCache.init();            // warm from disk

  runApp(
    MultiProvider(
      providers: [
        // Singletons: created once, never recreated
        Provider<ApiClient>(create: (_) => ApiClient()),
        Provider<RequestManager>(create: (_) => RequestManager()),
        Provider<MemoryCache>(create: (_) => MemoryCache()),
        Provider<LocalCache>(create: (_) => LocalCache()),
        Provider<WsManager>(create: (_) => WsManager()),

        // Auth: persistent across entire app lifetime
        ChangeNotifierProvider<AuthProvider>(
          create: (ctx) => AuthProvider(ctx.read()),
        ),

        // Feature providers: persistent — never disposed by navigation
        ChangeNotifierProvider<FinanceProvider>(
          create: (ctx) => FinanceProvider(ctx.read(), ctx.read(), ctx.read()),
        ),
        ChangeNotifierProvider<StatementProvider>(
          create: (ctx) => StatementProvider(ctx.read(), ctx.read(), ctx.read()),
        ),
        ChangeNotifierProvider<TransactionsProvider>(
          create: (ctx) => TransactionsProvider(ctx.read(), ctx.read(), ctx.read()),
        ),
        ChangeNotifierProvider<OrdersProvider>(
          create: (ctx) => OrdersProvider(ctx.read(), ctx.read(), ctx.read()),
        ),
        ChangeNotifierProvider<DashboardProvider>(
          create: (ctx) => DashboardProvider(ctx.read(), ctx.read(), ctx.read()),
        ),
        ChangeNotifierProvider<TablesProvider>(
          create: (ctx) => TablesProvider(ctx.read(), ctx.read(), ctx.read()),
        ),
        ChangeNotifierProvider<MenuProvider>(
          create: (ctx) => MenuProvider(ctx.read(), ctx.read(), ctx.read()),
        ),
      ],
      child: const BittuApp(),
    ),
  );
}
```

### ❌ Wrong: Provider inside screen or route

```dart
// NEVER DO THIS — provider is recreated on every navigation
class FinanceScreen extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider(                    // ← WRONG
      create: (_) => FinanceProvider(api, cache),    // ← WRONG: new instance every time
      child: FinanceBody(),
    );
  }
}
```

---

## 3. RULE 2 — TABS USE `IndexedStack` (NEVER `PageView` WITH `AutomaticKeepAliveClientMixin`)

`IndexedStack` keeps all tab widgets alive in the widget tree. Tab switching is pure UI — zero network calls.

```dart
// shell/main_shell.dart
class MainShell extends StatefulWidget {
  final Widget child;
  const MainShell({required this.child, super.key});

  @override
  State<MainShell> createState() => _MainShellState();
}

class _MainShellState extends State<MainShell> {
  int _currentIndex = 0;

  static const List<Widget> _tabs = [
    DashboardScreen(),
    OrdersScreen(),
    TablesScreen(),
    KitchenScreen(),
    FinanceShellScreen(),   // Finance inner tabs also use IndexedStack
    MoreScreen(),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _currentIndex,
        children: _tabs,
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (i) => setState(() => _currentIndex = i),
        // ...
      ),
    );
  }
}
```

### Finance inner tabs (also `IndexedStack`):

```dart
// features/finance/screens/finance_shell_screen.dart
class FinanceShellScreen extends StatefulWidget {
  const FinanceShellScreen({super.key});

  @override
  State<FinanceShellScreen> createState() => _FinanceShellScreenState();
}

class _FinanceShellScreenState extends State<FinanceShellScreen> {
  int _tab = 0;

  static const List<Widget> _financeTabs = [
    StatementScreen(),
    TransactionsScreen(),
    SettlementsScreen(),
    ReportsScreen(),
  ];

  @override
  void initState() {
    super.initState();
    // Warm finance data once — provider handles dedup if already loading
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<FinanceProvider>().ensureLoaded();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        FinanceTabBar(currentIndex: _tab, onTap: (i) => setState(() => _tab = i)),
        Expanded(
          child: IndexedStack(
            index: _tab,
            children: _financeTabs,
          ),
        ),
      ],
    );
  }
}
```

---

## 4. RULE 3 — CACHE-FIRST DATA LOADING

Every data fetch must follow this flow:

```
1. Check L1 in-memory cache → render immediately if hit
2. Check L2 disk cache → render immediately if hit
3. Fire API call in background (do not clear UI during this)
4. Patch in only the changed data (never wipe + reload)
```

### `MemoryCache` (L1)

```dart
// core/cache/memory_cache.dart
class CacheEntry<T> {
  final T data;
  final DateTime fetchedAt;
  final Duration ttl;

  CacheEntry({required this.data, required this.fetchedAt, required this.ttl});

  bool get isStale => DateTime.now().difference(fetchedAt) > ttl;
  bool get isFresh => !isStale;
}

class MemoryCache {
  final Map<String, CacheEntry<dynamic>> _store = {};

  static Future<void> init() async {}  // placeholder for warmup

  void set<T>(String key, T data, Duration ttl) {
    _store[key] = CacheEntry<T>(
      data: data,
      fetchedAt: DateTime.now(),
      ttl: ttl,
    );
  }

  T? get<T>(String key) {
    final entry = _store[key];
    if (entry == null) return null;
    return entry.data as T?;
  }

  bool isStale(String key) {
    final entry = _store[key];
    if (entry == null) return true;
    return entry.isStale;
  }

  bool has(String key) => _store.containsKey(key);

  DateTime? fetchedAt(String key) => _store[key]?.fetchedAt;

  void invalidate(String key) => _store.remove(key);
  void invalidatePrefix(String prefix) {
    _store.removeWhere((k, _) => k.startsWith(prefix));
  }
}
```

### `LocalCache` (L2 — Hive)

```dart
// core/cache/local_cache.dart
import 'package:hive_flutter/hive_flutter.dart';

class LocalCache {
  static late Box _box;

  static Future<void> init() async {
    await Hive.initFlutter();
    _box = await Hive.openBox('bittu_cache');
  }

  Future<void> set(String key, dynamic value) async {
    await _box.put(key, {
      'data': value,
      'ts': DateTime.now().millisecondsSinceEpoch,
    });
  }

  dynamic get(String key) {
    final raw = _box.get(key);
    if (raw == null) return null;
    return raw['data'];
  }

  DateTime? fetchedAt(String key) {
    final raw = _box.get(key);
    if (raw == null) return null;
    final ts = raw['ts'] as int?;
    if (ts == null) return null;
    return DateTime.fromMillisecondsSinceEpoch(ts);
  }

  Future<void> remove(String key) async => _box.delete(key);
  Future<void> clear() async => _box.clear();
}
```

### `CachePolicy` — TTL constants per resource

```dart
// core/cache/cache_policy.dart
class CachePolicy {
  // Finance data — refresh every 2 minutes
  static const finance = Duration(minutes: 2);

  // Statement summary — refresh every 5 minutes
  static const statementSummary = Duration(minutes: 5);

  // Transactions list — refresh every 3 minutes
  static const transactions = Duration(minutes: 3);

  // Settlements — refresh every 5 minutes
  static const settlements = Duration(minutes: 5);

  // Orders — refresh every 30 seconds (high frequency)
  static const orders = Duration(seconds: 30);

  // Menu — refresh every 10 minutes (changes rarely)
  static const menu = Duration(minutes: 10);

  // Tables — refresh every 1 minute
  static const tables = Duration(minutes: 1);

  // Dashboard counts — refresh every 1 minute
  static const dashboard = Duration(minutes: 1);

  // Permissions — refresh every 15 minutes
  static const permissions = Duration(minutes: 15);
}
```

---

## 5. RULE 4 — CENTRALIZED REQUEST MANAGER (NO DUPLICATE API CALLS)

```dart
// core/network/request_manager.dart
import 'dart:async';

class RequestManager {
  // Tracks in-flight requests: key → Future<dynamic>
  final Map<String, Future<dynamic>> _inflight = {};

  // Last call timestamps for throttling
  final Map<String, DateTime> _lastCall = {};

  /// Execute a deduplicated, optionally throttled API call.
  ///
  /// [key]       Unique key for this request (e.g. 'finance:summary:branch123')
  /// [call]      The actual async API function to execute
  /// [throttle]  Minimum time between actual network calls for this key
  Future<T> execute<T>({
    required String key,
    required Future<T> Function() call,
    Duration throttle = Duration.zero,
  }) async {
    // 1. If already in-flight, return same future (dedup)
    if (_inflight.containsKey(key)) {
      return _inflight[key] as Future<T>;
    }

    // 2. Throttle: if last call was too recent, skip
    if (throttle > Duration.zero) {
      final last = _lastCall[key];
      if (last != null && DateTime.now().difference(last) < throttle) {
        // Return a completed future with no-op — caller should use cached data
        return Future<T>.error(const ThrottledError());
      }
    }

    // 3. Execute and track
    final future = call().whenComplete(() {
      _inflight.remove(key);
    });

    _inflight[key] = future;
    _lastCall[key] = DateTime.now();

    return future;
  }

  bool isInflight(String key) => _inflight.containsKey(key);
}

class ThrottledError implements Exception {
  const ThrottledError();
  @override
  String toString() => 'Request throttled — use cached data';
}
```

---

## 6. RULE 5 — PROVIDER PATTERN (CACHE-FIRST, NON-REBUILDING)

All feature providers follow this template:

```dart
// features/finance/providers/finance_provider.dart
import 'package:flutter/foundation.dart';

enum LoadState { idle, loading, loaded, error }

class FinanceProvider extends ChangeNotifier {
  final ApiClient _api;
  final MemoryCache _cache;
  final RequestManager _req;

  // ── state ──────────────────────────────────────────────
  FinanceSummary? _summary;
  LoadState _summaryState = LoadState.idle;
  String? _summaryError;
  bool _summaryStale = false;

  // ── public getters (UI reads these) ────────────────────
  FinanceSummary? get summary => _summary;
  LoadState get summaryState => _summaryState;
  String? get summaryError => _summaryError;
  bool get summaryStale => _summaryStale;
  bool get hasSummary => _summary != null;

  FinanceProvider(this._api, this._cache, this._req);

  static const _summaryKey = 'finance:summary';

  /// Call once when the Finance tab is first mounted.
  /// Safe to call repeatedly — deduplicates automatically.
  Future<void> ensureLoaded() async {
    // Already loaded fresh — do nothing
    if (_summary != null && !_cache.isStale(_summaryKey)) return;

    // Already loading — do nothing (will notify when done)
    if (_summaryState == LoadState.loading) return;

    await _loadSummary();
  }

  /// Force refresh (pull-to-refresh or manual retry)
  Future<void> refresh() async {
    _cache.invalidate(_summaryKey);
    await _loadSummary(forceRefresh: true);
  }

  Future<void> _loadSummary({bool forceRefresh = false}) async {
    // 1. Render from L1 cache instantly (no skeleton flash)
    final cached = _cache.get<FinanceSummary>(_summaryKey);
    if (cached != null && !forceRefresh) {
      _summary = cached;
      _summaryState = LoadState.loaded;
      _summaryStale = _cache.isStale(_summaryKey);
      notifyListeners();           // render immediately from cache
    } else {
      // Only show loading state if we have nothing to show
      if (_summary == null) {
        _summaryState = LoadState.loading;
        notifyListeners();
      }
    }

    // 2. Fetch from network (background if we already have cached data)
    try {
      final result = await _req.execute<FinanceSummary>(
        key: _summaryKey,
        call: () async {
          final raw = await _api.get('/finance/dashboard');
          return FinanceSummary.fromJson(raw.data);
        },
        throttle: CachePolicy.finance,
      );

      _cache.set(_summaryKey, result, CachePolicy.finance);
      _summary = result;
      _summaryState = LoadState.loaded;
      _summaryStale = false;
      notifyListeners();           // patch only changed data
    } on ThrottledError {
      // Throttled — cached data is recent enough, nothing to do
    } catch (e) {
      _summaryState = LoadState.error;
      _summaryError = e.toString();
      // Keep existing _summary data visible — never wipe on error
      notifyListeners();
    }
  }
}
```

---

## 7. RULE 6 — UI NEVER CALLS NETWORK (SUBSCRIBE ONLY)

### ✅ Correct: UI subscribes, does not initiate

```dart
// features/finance/screens/statement_screen.dart
class StatementScreen extends StatelessWidget {
  const StatementScreen({super.key});

  @override
  Widget build(BuildContext context) {
    // No initState, no FutureBuilder, no loadData() here
    return Selector<FinanceProvider, (FinanceSummary?, LoadState, bool)>(
      selector: (_, p) => (p.summary, p.summaryState, p.summaryStale),
      builder: (context, state, _) {
        final (summary, loadState, stale) = state;
        return SkeletonGuard(
          isFirstLoad: summary == null && loadState == LoadState.loading,
          skeleton: const FinanceSummarySkeleton(),
          child: FinanceSummaryCard(
            summary: summary,
            isStale: stale,
          ),
        );
      },
    );
  }
}
```

### ✅ Correct: `initState` calls `ensureLoaded` via `addPostFrameCallback`

When a tab becomes active **for the first time**, the screen shell triggers loading via `ensureLoaded()` — not `loadData()`. This call is idempotent.

```dart
// Only in the SHELL wrapper, not in every screen widget:
@override
void initState() {
  super.initState();
  WidgetsBinding.instance.addPostFrameCallback((_) {
    context.read<FinanceProvider>().ensureLoaded();
    context.read<StatementProvider>().ensureLoaded();
    context.read<TransactionsProvider>().ensureLoaded();
  });
}
```

### ❌ Wrong: network call in build or initState of a leaf widget

```dart
// NEVER DO THIS
class StatementScreen extends StatefulWidget {
  @override
  State<StatementScreen> createState() => _StatementScreenState();
}

class _StatementScreenState extends State<StatementScreen> {
  @override
  void initState() {
    super.initState();
    // ← WRONG: fires every time this widget mounts (tab switch, navigation)
    context.read<FinanceProvider>().loadSummary();
  }
}
```

---

## 8. RULE 7 — SKELETON GUARD (SHOW SKELETON ONCE ONLY)

```dart
// shared/widgets/skeleton_guard.dart
class SkeletonGuard extends StatelessWidget {
  final bool isFirstLoad;    // true only when: no cached data AND loading
  final Widget skeleton;
  final Widget child;

  const SkeletonGuard({
    required this.isFirstLoad,
    required this.skeleton,
    required this.child,
    super.key,
  });

  @override
  Widget build(BuildContext context) {
    if (isFirstLoad) return skeleton;
    return child;
  }
}
```

Usage rule:
- `isFirstLoad = summary == null && loadState == LoadState.loading`
- If `summary != null` (even stale), show the data — **never** the skeleton
- If stale, show the stale banner

---

## 9. RULE 8 — STALE BANNER (OFFLINE-FIRST)

```dart
// shared/widgets/stale_banner.dart
class StaleBanner extends StatelessWidget {
  final DateTime? lastUpdated;
  final bool isVisible;
  final VoidCallback? onRefresh;

  const StaleBanner({
    required this.lastUpdated,
    required this.isVisible,
    this.onRefresh,
    super.key,
  });

  @override
  Widget build(BuildContext context) {
    if (!isVisible || lastUpdated == null) return const SizedBox.shrink();
    final diff = DateTime.now().difference(lastUpdated!);
    final label = diff.inMinutes < 1
        ? 'just now'
        : diff.inMinutes < 60
            ? '${diff.inMinutes}m ago'
            : '${diff.inHours}h ago';

    return Container(
      color: const Color(0xFFFFF3CD),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
      child: Row(
        children: [
          const Icon(Icons.wifi_off, size: 14, color: Color(0xFF856404)),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              'Showing data from $label. Tap to refresh.',
              style: const TextStyle(fontSize: 12, color: Color(0xFF856404)),
            ),
          ),
          if (onRefresh != null)
            GestureDetector(
              onTap: onRefresh,
              child: const Text(
                'Retry',
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: Color(0xFF856404),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
```

---

## 10. RULE 9 — API CLIENT (WITH AUTO-RETRY AND TOKEN REFRESH)

```dart
// core/network/api_client.dart
import 'package:dio/dio.dart';

class ApiClient {
  late final Dio _dio;
  final AuthProvider _auth;

  static const _baseUrl = 'https://api.bittupos.com/api/v1';
  static const _connectTimeout = Duration(seconds: 10);
  static const _receiveTimeout = Duration(seconds: 30);

  ApiClient(this._auth) {
    _dio = Dio(BaseOptions(
      baseUrl: _baseUrl,
      connectTimeout: _connectTimeout,
      receiveTimeout: _receiveTimeout,
    ));

    _dio.interceptors.addAll([
      _AuthInterceptor(_auth, _dio),
      _RetryInterceptor(_dio),
      _LoggingInterceptor(),
    ]);
  }

  Future<Response<T>> get<T>(String path, {Map<String, dynamic>? params}) =>
      _dio.get(path, queryParameters: params);

  Future<Response<T>> post<T>(String path, {dynamic data}) =>
      _dio.post(path, data: data);

  Future<Response<T>> patch<T>(String path, {dynamic data}) =>
      _dio.patch(path, data: data);

  Future<Response<T>> delete<T>(String path) => _dio.delete(path);
}

class _AuthInterceptor extends Interceptor {
  final AuthProvider _auth;
  final Dio _dio;

  _AuthInterceptor(this._auth, this._dio);

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    options.headers['Authorization'] = 'Bearer ${_auth.accessToken}';
    handler.next(options);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    if (err.response?.statusCode == 401) {
      final refreshed = await _auth.refreshToken();
      if (refreshed) {
        err.requestOptions.headers['Authorization'] =
            'Bearer ${_auth.accessToken}';
        final response = await _dio.fetch(err.requestOptions);
        handler.resolve(response);
        return;
      }
      _auth.logout();
    }
    handler.next(err);
  }
}

class _RetryInterceptor extends QueuedInterceptor {
  final Dio dio;
  static const _maxRetries = 3;
  static const _baseDelay = Duration(milliseconds: 500);

  _RetryInterceptor(this.dio);

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    final retries = err.requestOptions.extra['_retries'] as int? ?? 0;
    final isRetryable = err.type == DioExceptionType.connectionTimeout ||
        err.type == DioExceptionType.receiveTimeout ||
        (err.response?.statusCode ?? 0) >= 500;

    if (isRetryable && retries < _maxRetries) {
      final delay = _baseDelay * (1 << retries); // exponential backoff
      await Future.delayed(delay);
      err.requestOptions.extra['_retries'] = retries + 1;
      try {
        // Re-use the same Dio instance so auth + other interceptors still run
        final response = await dio.fetch(err.requestOptions);
        handler.resolve(response);
        return;
      } catch (_) {}
    }
    handler.next(err);
  }
}

class _LoggingInterceptor extends Interceptor {
  @override
  void onError(DioException err, ErrorInterceptorHandler handler) {
    debugPrint('[API ERROR] ${err.requestOptions.method} '
        '${err.requestOptions.path} → ${err.response?.statusCode}');
    handler.next(err);
  }
}
```

---

## 11. RULE 10 — WEBSOCKET MANAGER (SINGLETON, AUTO-RECONNECT)

```dart
// core/websocket/ws_manager.dart
import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

typedef WsEventHandler = void Function(Map<String, dynamic> data);

class WsManager {
  WebSocketChannel? _channel;
  Timer? _reconnectTimer;
  Timer? _pingWatchdog;
  bool _intentionalClose = false;
  final Map<String, List<WsEventHandler>> _handlers = {};

  void connect(String baseUrl, String token) {
    _intentionalClose = false;
    _channel = WebSocketChannel.connect(
      Uri.parse('wss://$baseUrl/ws?token=$token'),
    );
    _channel!.stream.listen(
      _onMessage,
      onDone: _onDisconnected,
      onError: (_) => _onDisconnected(),
    );
    _startPingWatchdog();
  }

  void on(String event, WsEventHandler handler) {
    _handlers.putIfAbsent(event, () => []).add(handler);
  }

  void off(String event, WsEventHandler handler) {
    _handlers[event]?.remove(handler);
  }

  void _onMessage(dynamic raw) {
    final msg = jsonDecode(raw as String) as Map<String, dynamic>;
    final event = msg['event'] as String?;
    if (event == 'ping') {
      _channel?.sink.add(jsonEncode({'action': 'pong'}));
      _resetPingWatchdog();
      return;
    }
    if (event != null) {
      for (final handler in List.of(_handlers[event] ?? [])) {
        handler(msg['data'] as Map<String, dynamic>? ?? {});
      }
    }
  }

  void _onDisconnected() {
    if (_intentionalClose) return;
    _reconnectTimer = Timer(const Duration(seconds: 3), () {
      // Re-connect with stored credentials via AuthProvider
    });
  }

  void _startPingWatchdog() {
    _pingWatchdog = Timer.periodic(const Duration(seconds: 45), (_) {
      // Server sends ping every 30s; if 45s pass with no ping → reconnect
      _onDisconnected();
    });
  }

  void _resetPingWatchdog() {
    _pingWatchdog?.cancel();
    _startPingWatchdog();
  }

  void subscribe(String channel) {
    _channel?.sink.add(jsonEncode({'action': 'subscribe', 'channel': channel}));
  }

  void dispose() {
    _intentionalClose = true;
    _reconnectTimer?.cancel();
    _pingWatchdog?.cancel();
    _channel?.sink.close();
  }
}
```

---

## 12. RULE 11 — PAGINATION THAT PERSISTS

Paginated lists must persist their loaded pages across tab switches.

```dart
// features/finance/providers/transactions_provider.dart
class TransactionsProvider extends ChangeNotifier {
  final ApiClient _api;
  final MemoryCache _cache;
  final RequestManager _req;

  // ── persistent pagination state ──
  final List<Transaction> _items = [];
  int _page = 1;
  bool _hasMore = true;
  LoadState _state = LoadState.idle;
  String? _error;

  // ── filters persist across tab switches ──
  DateRange? _dateRange;
  String? _statusFilter;

  List<Transaction> get items => List.unmodifiable(_items);
  int get page => _page;
  bool get hasMore => _hasMore;
  LoadState get state => _state;
  bool get isEmpty => _items.isEmpty;
  DateRange? get dateRange => _dateRange;
  String? get statusFilter => _statusFilter;

  TransactionsProvider(this._api, this._cache, this._req);

  Future<void> ensureLoaded() async {
    if (_items.isNotEmpty) return;   // Already loaded — do nothing
    if (_state == LoadState.loading) return;
    await _loadPage(1);
  }

  Future<void> loadMore() async {
    if (!_hasMore || _state == LoadState.loading) return;
    await _loadPage(_page + 1);
  }

  Future<void> refresh() async {
    _page = 1;
    _hasMore = true;
    _items.clear();
    await _loadPage(1, forceRefresh: true);
  }

  void setDateRange(DateRange range) {
    _dateRange = range;
    refresh();
  }

  void setStatusFilter(String? status) {
    _statusFilter = status;
    refresh();
  }

  Future<void> _loadPage(int page, {bool forceRefresh = false}) async {
    final cacheKey = _buildCacheKey(page);

    // Render from cache instantly
    if (!forceRefresh) {
      final cached = _cache.get<List<Transaction>>(cacheKey);
      if (cached != null) {
        if (page == 1) {
          _items
            ..clear()
            ..addAll(cached);
        } else {
          _items.addAll(cached.where((t) => !_items.any((e) => e.id == t.id)));
        }
        _page = page;
        _state = LoadState.loaded;
        notifyListeners();
        if (!_cache.isStale(cacheKey) && !forceRefresh) return;
      }
    }

    if (_state != LoadState.loaded) {
      _state = LoadState.loading;
      notifyListeners();
    }

    try {
      final result = await _req.execute<_PageResult>(
        key: cacheKey,
        call: () async {
          final params = <String, dynamic>{
            'page': page,
            'page_size': 50,
            if (_dateRange != null) ...{
              'from_date': _dateRange!.from.toIso8601String(),
              'to_date': _dateRange!.to.toIso8601String(),
            },
            if (_statusFilter != null) 'status': _statusFilter,
          };
          final raw = await _api.get('/statements/transactions', params: params);
          final items = (raw.data['items'] as List)
              .map((j) => Transaction.fromJson(j as Map<String, dynamic>))
              .toList();
          final hasMore = raw.data['has_more'] as bool? ?? false;
          return _PageResult(items: items, hasMore: hasMore);
        },
        throttle: CachePolicy.transactions,
      );

      _cache.set(cacheKey, result.items, CachePolicy.transactions);

      if (page == 1) {
        _items
          ..clear()
          ..addAll(result.items);
      } else {
        _items.addAll(
          result.items.where((t) => !_items.any((e) => e.id == t.id)),
        );
      }
      _page = page;
      _hasMore = result.hasMore;
      _state = LoadState.loaded;
      notifyListeners();
    } on ThrottledError {
      // Use cached data, nothing to update
    } catch (e) {
      _state = LoadState.error;
      _error = e.toString();
      notifyListeners();
    }
  }

  String _buildCacheKey(int page) =>
      'transactions:p$page'
      ':${_dateRange?.from.toIso8601String() ?? ''}'
      ':${_dateRange?.to.toIso8601String() ?? ''}'
      ':${_statusFilter ?? ''}';
}

class _PageResult {
  final List<Transaction> items;
  final bool hasMore;
  _PageResult({required this.items, required this.hasMore});
}
```

---

## 13. RULE 12 — GRANULAR SELECTORS (NO FULL REBUILDS)

Use `Selector` instead of `Consumer` everywhere. Rebuild only the widgets that need the changed data.

```dart
// ✅ Correct: Only rebuilds when summary data changes
Selector<FinanceProvider, FinanceSummary?>(
  selector: (_, p) => p.summary,
  builder: (ctx, summary, _) => FinanceSummaryCard(summary: summary),
)

// ✅ Correct: Only rebuilds when order count changes
Selector<OrdersProvider, int>(
  selector: (_, p) => p.activeCount,
  builder: (ctx, count, _) => OrdersBadge(count: count),
)

// ❌ Wrong: Rebuilds on any change to the provider
Consumer<FinanceProvider>(
  builder: (ctx, provider, _) => FinanceSummaryCard(summary: provider.summary),
)
```

---

## 14. RULE 13 — REAL-TIME PATCHING VIA WEBSOCKET

WebSocket events should patch specific items in the provider — never trigger a full reload.

```dart
// features/orders/providers/orders_provider.dart (fragment)
class OrdersProvider extends ChangeNotifier {
  // ...

  void _listenWebSocket(WsManager ws) {
    ws.on('order.created', (data) {
      final order = Order.fromJson(data);
      _orders.insert(0, order);      // prepend only
      notifyListeners();
    });

    ws.on('order.status_changed', (data) {
      final id = data['order_id'] as String;
      final status = data['status'] as String;
      final idx = _orders.indexWhere((o) => o.id == id);
      if (idx != -1) {
        _orders[idx] = _orders[idx].copyWith(status: status);
        notifyListeners();          // patch only the changed order
      }
    });
  }
}
```

---

## 15. RULE 14 — FINANCE UI DESIGN (FLAT, LIGHTWEIGHT)

Finance screens should feel like Razorpay or PhonePe — clean and fast.

**DO:**
- Flat cards with 1dp border (`Color(0xFFE5E7EB)`)
- `Color(0xFFF9FAFB)` background
- Inter or SF Pro typography
- Whitespace as separator
- Subtle `Color(0xFF10B981)` green for positive amounts
- Subtle `Color(0xFFEF4444)` red for negative amounts

**DO NOT:**
- `BoxShadow` with `blurRadius > 4`
- Nested cards inside cards
- Gradient backgrounds on finance data
- Animated counters on finance numbers (use static `Text`)
- `shimmer` animations on data that is already cached

```dart
// shared/widgets/finance_stat_card.dart
class FinanceStatCard extends StatelessWidget {
  final String label;
  final String value;
  final Color? valueColor;

  const FinanceStatCard({
    required this.label,
    required this.value,
    this.valueColor,
    super.key,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border.all(color: const Color(0xFFE5E7EB)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontSize: 12,
              color: Color(0xFF6B7280),
              fontWeight: FontWeight.w500,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            value,
            style: TextStyle(
              fontSize: 20,
              fontWeight: FontWeight.w600,
              color: valueColor ?? const Color(0xFF111827),
            ),
          ),
        ],
      ),
    );
  }
}
```

---

## 16. RULE 15 — CONNECTIVITY MONITOR (OFFLINE-FIRST)

```dart
// shared/utils/connectivity_monitor.dart
import 'dart:async';
import 'package:connectivity_plus/connectivity_plus.dart';

class ConnectivityMonitor extends ChangeNotifier {
  bool _isOnline = true;
  bool get isOnline => _isOnline;

  late final StreamSubscription<List<ConnectivityResult>> _sub;

  ConnectivityMonitor() {
    _sub = Connectivity().onConnectivityChanged.listen((results) {
      final wasOnline = _isOnline;
      _isOnline = results.any((r) => r != ConnectivityResult.none);
      if (_isOnline != wasOnline) notifyListeners();
    });
  }

  @override
  void dispose() {
    _sub.cancel();
    super.dispose();
  }
}
```

Usage in providers — only fire API calls when online, queue when offline:

```dart
Future<void> ensureLoaded() async {
  // Always render from cache first
  final cached = _cache.get<FinanceSummary>(_summaryKey);
  if (cached != null) {
    _summary = cached;
    _summaryStale = _cache.isStale(_summaryKey);
    _summaryState = LoadState.loaded;
    notifyListeners();
  }

  // Only sync if online
  if (!_connectivity.isOnline) return;
  if (_summary != null && !_cache.isStale(_summaryKey)) return;

  await _loadSummary();
}
```

---

## 17. RULE 16 — MEMORY MANAGEMENT (NO LEAKS)

```dart
// ✅ Correct: Providers registered at root level — Flutter manages lifecycle
// Providers added to MultiProvider in main.dart are disposed when app closes.

// ✅ Correct: WsManager listeners removed when provider disposes
class OrdersProvider extends ChangeNotifier {
  final WsManager _ws;
  late final void Function(Map<String, dynamic>) _handler;

  OrdersProvider(this._ws) {
    _handler = (data) { /* ... */ };
    _ws.on('order.created', _handler);
    _ws.on('order.status_changed', _handler);
  }

  @override
  void dispose() {
    _ws.off('order.created', _handler);
    _ws.off('order.status_changed', _handler);
    super.dispose();
  }
}

// ✅ Correct: Timers always stored and cancelled on dispose
class DashboardProvider extends ChangeNotifier {
  Timer? _pollingTimer;

  void startPolling() {
    _pollingTimer?.cancel();   // prevent duplicate
    _pollingTimer = Timer.periodic(
      CachePolicy.dashboard,
      (_) => refresh(),
    );
  }

  @override
  void dispose() {
    _pollingTimer?.cancel();
    super.dispose();
  }
}
```

---

## 18. REQUIRED `pubspec.yaml` DEPENDENCIES

```yaml
dependencies:
  flutter:
    sdk: flutter

  # State management
  provider: ^6.1.2

  # Networking
  dio: ^5.6.0

  # WebSocket
  web_socket_channel: ^3.0.1

  # Local cache (L2)
  hive_flutter: ^1.1.0

  # Connectivity
  connectivity_plus: ^6.0.3

  # Secure token storage
  flutter_secure_storage: ^9.2.2

  # Navigation
  go_router: ^14.2.7

  # Utilities
  intl: ^0.19.0

dev_dependencies:
  flutter_test:
    sdk: flutter
  hive_generator: ^2.0.1
  build_runner: ^2.4.11
```

---

## 19. PERFORMANCE CHECKLIST

Before shipping any screen, verify:

| Check | Expected |
|-------|----------|
| Tab switch | < 100ms perceived — no API call fired |
| Finance screen open (after first load) | Instant — rendered from L1 cache |
| Skeleton shown | Only when no cached data exists |
| API call on tab switch | Zero |
| API call on app resume (data fresh) | Zero |
| API call on app resume (data stale) | One background call, no UI flicker |
| Scroll FPS | 60fps on mid-range Android (Pixel 4a equivalent) |
| Memory on finance screen | No leak after 10 tab switches |
| WS reconnect on disconnect | Within 3 seconds, no duplicate subscriptions |

---

## 20. ANTI-PATTERNS (DO NOT DO)

| Anti-pattern | Why it's wrong | Correct approach |
|---|---|---|
| `initState` calls `loadData()` | Fires on every tab switch / navigation | Use `ensureLoaded()` in shell's `initState` only |
| `build` triggers API call | Called on every rebuild | Move to provider; UI only reads state |
| `setState` on full list | Rebuilds entire screen | Use `Selector` with granular field |
| `PageView` + `AutomaticKeepAliveClientMixin` | State kept but widget remounted | Use `IndexedStack` — widget never unmounts |
| Provider created inside screen widget | New provider on every navigation | Create all providers in `main.dart` |
| Show skeleton on refresh | UI flickers | Keep showing old data during background refresh |
| Cancel in-flight request on tab switch | Wastes work, causes gaps | Keep request running; patch when done |
| WS listener added in `build` | Duplicate listeners on rebuild | Add listener in provider constructor or `initState` once |
| Polling without cancel | Timer leak | Store timer reference; cancel in `dispose()` |
| Clear list before reload | UI goes blank | Append/patch only; clear only on explicit filter change |

---

## SUMMARY

This architecture ensures Bittu POS behaves like enterprise financial software:

1. **State persists** — providers live at root, survive all navigation
2. **Cache-first** — UI renders cached data in < 16ms, refreshes silently in background
3. **Zero API spam** — `RequestManager` deduplicates and throttles all calls
4. **Tabs are instant** — `IndexedStack` keeps all tab widgets alive
5. **Finance is always-hot** — `FinanceProvider.ensureLoaded()` is idempotent; data never wiped
6. **Offline works** — stale data shown with timestamp; background retry when online
7. **No skeleton abuse** — skeleton appears only on true first load with empty cache
8. **Clean UI** — flat cards, no excessive shadows, whitespace hierarchy
9. **No memory leaks** — all timers and WS listeners explicitly cancelled on dispose
10. **60fps scrolling** — granular `Selector` rebuilds prevent unnecessary widget tree diffs
