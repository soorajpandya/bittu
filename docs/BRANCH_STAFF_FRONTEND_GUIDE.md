# Branch & Staff Management — Frontend Implementation Guide

**Base URL:** `https://api.bittupos.com/api/v1`  
**Auth:** All requests require `Authorization: Bearer <supabase_jwt>`  
**Access:** Owner-only (all screens in this guide). Managers get read-only on staff list.

---

## Overview

The restaurant owner manages **Branches** (physical locations) and **Staff** (people who work there). There are two types of staff:

| Concept | DB Table | Has Login? | Purpose |
|---------|----------|-----------|---------|
| **Branch User** | `branch_users` | ✅ Yes (Supabase account) | Can log into the app with their own Google account, sees role-restricted UI |
| **Local Staff** | `staff` | ❌ No | Lightweight record for tracking — attendance, assignments, phone list |

Every restaurant starts with one **Main Branch** (auto-created on signup). Owner can create additional sub-branches.

---

## Auth: Permission-Based UI Adaptation

Before rendering any screen, fetch the user's permissions:

**`GET /auth/permissions/me`**

```json
{
  "staff.branches.read": { "allowed": true, "meta": {} },
  "staff.branches.create": { "allowed": true, "meta": {} },
  "staff.read": { "allowed": true, "meta": {} },
  "billing.discount": { "allowed": true, "meta": { "max_discount_percent": 25 } }
}
```

**Use this to:**
- Hide "Add Branch" button if `staff.branches.create` is missing
- Hide "Add Staff" button if `staff.create` is missing
- Hide entire Staff screen if `staff.read` is missing
- Cap discount slider to `meta.max_discount_percent`

Cache this response locally. Refresh on app resume or after role changes.

---

## 1. Branch Management Screen

### 1.1 List Branches

**`GET /staff/branches`**  
**Permission:** `staff.branches.read`

**Response:**
```json
[
  {
    "id": "uuid-branch-1",
    "restaurant_id": "uuid-rest",
    "name": "Main Branch",
    "is_main_branch": true,
    "is_active": true,
    "created_at": "2026-01-15T10:00:00Z",
    "updated_at": "2026-01-15T10:00:00Z"
  },
  {
    "id": "uuid-branch-2",
    "restaurant_id": "uuid-rest",
    "name": "City Center Outlet",
    "is_main_branch": false,
    "is_active": true,
    "created_at": "2026-03-20T14:30:00Z",
    "updated_at": "2026-03-20T14:30:00Z"
  }
]
```

**UI Layout:**
```
┌─────────────────────────────────────────────┐
│  Branches                        [+ Add]    │
├─────────────────────────────────────────────┤
│  🏢 Main Branch                    ★ Main   │
│     Active · Created Jan 15                 │
│                                    [Edit]   │
├─────────────────────────────────────────────┤
│  🏢 City Center Outlet                      │
│     Active · Created Mar 20                 │
│                              [Edit] [Toggle]│
├─────────────────────────────────────────────┤
│  🏢 Airport Kiosk                           │
│     Inactive · Created Apr 1                │
│                              [Edit] [Toggle]│
└─────────────────────────────────────────────┘
```

**Rules:**
- Main branch always shows first, with a badge/chip — no deactivate button
- Non-main branches show activate/deactivate toggle
- [+ Add] button only if user has `staff.branches.create`
- [Edit] only if user has `staff.branches.update`

---

### 1.2 Create Branch

**`POST /staff/branches`**  
**Permission:** `staff.branches.create`

**Request:**
```json
{
  "name": "City Center Outlet",
  "manager_user_id": "optional-supabase-user-id"
}
```

**Response:**
```json
{
  "id": "uuid-new-branch",
  "restaurant_id": "uuid-rest",
  "name": "City Center Outlet",
  "is_main_branch": false,
  "is_active": true,
  "manager": { "user_id": "...", "role": "manager" }
}
```

**UI:** Bottom sheet or dialog:
```
┌──────────────────────────────────────┐
│  Create New Branch                   │
│                                      │
│  Branch Name *                       │
│  ┌────────────────────────────────┐  │
│  │ e.g. City Center Outlet       │  │
│  └────────────────────────────────┘  │
│                                      │
│  Assign Manager (optional)           │
│  ┌────────────────────────────────┐  │
│  │ Select from existing users ▾  │  │
│  └────────────────────────────────┘  │
│                                      │
│        [Cancel]    [Create Branch]   │
└──────────────────────────────────────┘
```

`manager_user_id` is optional — if provided, that user gets auto-assigned as manager of this branch.

---

### 1.3 Update Branch

**`PATCH /staff/branches/{branch_id}`**  
**Permission:** `staff.branches.update`

**Request:**
```json
{
  "name": "Updated Name",
  "is_active": false
}
```

Both fields optional. Backend rejects deactivating the main branch (409 error).

**UI:** Same bottom sheet as create, pre-filled. Add active/inactive toggle for non-main branches.

---

## 2. Branch Users (Login-Capable Staff)

These are real Supabase users who can log into the app with limited permissions.

### 2.1 List Branch Users

**`GET /staff/branch-users`**  
**Permission:** `staff.branch_users.read`  
**Query Params:** `branch_id` (optional — filter by branch)

**Response:**
```json
[
  {
    "user_id": "supabase-uid-abc",
    "branch_id": "uuid-branch-1",
    "owner_id": "owner-uid",
    "role": "manager",
    "is_active": true,
    "branch_name": "Main Branch"
  },
  {
    "user_id": "supabase-uid-def",
    "branch_id": "uuid-branch-1",
    "owner_id": "owner-uid",
    "role": "cashier",
    "is_active": true,
    "branch_name": "Main Branch"
  }
]
```

**UI Layout:**
```
┌─────────────────────────────────────────────────┐
│  Team Members                    [+ Invite]     │
│                                                 │
│  Branch: [All Branches ▾]                       │
├─────────────────────────────────────────────────┤
│  👤 supabase-uid-abc                            │
│     Role: Manager · Main Branch                 │
│     Active                                      │
│                          [Change Role] [Remove] │
├─────────────────────────────────────────────────┤
│  👤 supabase-uid-def                            │
│     Role: Cashier · Main Branch                 │
│     Active                                      │
│                          [Change Role] [Remove] │
└─────────────────────────────────────────────────┘
```

**Note:** The API returns `user_id` (Supabase UID), not name/email. Currently no endpoint returns display names for branch users — show the user_id or a truncated version. If you add a profile lookup later, enrich here.

---

### 2.2 Add Branch User

**`POST /staff/branch-users`**  
**Permission:** `staff.branch_users.create`

**Request:**
```json
{
  "branch_id": "uuid-branch-1",
  "user_id": "supabase-uid-of-person",
  "role": "cashier"
}
```

**Valid roles:** `manager`, `cashier`, `waiter`, `chef`, `kitchen`, `staff`

**Response:**
```json
{
  "user_id": "supabase-uid-of-person",
  "branch_id": "uuid-branch-1",
  "owner_id": "owner-uid",
  "role": "cashier",
  "is_active": true
}
```

**Error cases:**
- `409` — User already assigned to another branch
- `422` — Invalid role, or trying to add yourself as branch user
- `404` — Branch not found

**UI:** Bottom sheet:
```
┌──────────────────────────────────────┐
│  Add Team Member                     │
│                                      │
│  Branch *                            │
│  ┌────────────────────────────────┐  │
│  │ Main Branch              ▾    │  │
│  └────────────────────────────────┘  │
│                                      │
│  User ID *                           │
│  ┌────────────────────────────────┐  │
│  │ Paste Supabase user ID        │  │
│  └────────────────────────────────┘  │
│                                      │
│  Role *                              │
│  ┌────────────────────────────────┐  │
│  │ ○ Manager                     │  │
│  │ ○ Cashier                     │  │
│  │ ○ Waiter                      │  │
│  │ ○ Chef                        │  │
│  │ ○ Staff                       │  │
│  └────────────────────────────────┘  │
│                                      │
│        [Cancel]      [Add Member]    │
└──────────────────────────────────────┘
```

**Flow:** The person must first create a Supabase account (Google login). Then the owner copies their user ID and adds them here. A future invite-by-email flow can simplify this.

---

### 2.3 Change Branch User Role

**`PATCH /staff/branch-users/{target_user_id}`**  
**Permission:** `staff.branch_users.update`

**Request:**
```json
{
  "role": "manager"
}
```

**Response:**
```json
{
  "user_id": "supabase-uid-abc",
  "branch_id": "uuid-branch-1",
  "role": "manager",
  "is_active": true
}
```

**Note:** `chef` is auto-mapped to `kitchen` in the backend. Show "Chef" in UI, send `"chef"` to API.

---

### 2.4 Remove Branch User

**`DELETE /staff/branch-users/{target_user_id}`**  
**Permission:** `staff.branch_users.delete`

**Response:**
```json
{
  "user_id": "supabase-uid-abc",
  "is_active": false
}
```

Soft-delete — user loses login access immediately. Show confirmation dialog before calling.

---

### 2.5 Current User's Info

**`GET /staff/branch-users/me`**  
**No special permission** (any authenticated user)

**Response (branch user):**
```json
{
  "user_id": "supabase-uid-abc",
  "branch_id": "uuid-branch-1",
  "owner_id": "owner-uid",
  "role": "manager",
  "is_active": true,
  "branch_name": "Main Branch",
  "restaurant_name": "Bittu's Kitchen",
  "restaurant_id": "uuid-rest"
}
```

**Response (owner):**
```json
{
  "user_id": "owner-uid",
  "branch_id": "uuid-main-branch",
  "role": "owner",
  "is_active": true,
  "branch_name": "Main Branch",
  "restaurant_name": "Bittu's Kitchen",
  "restaurant_id": "uuid-rest"
}
```

**Use this on app startup** to determine:
- Which branch the user belongs to
- What role they have (for UI adaptation)
- Restaurant name for headers

---

## 3. Local Staff Records (No Login)

Lightweight staff tracking — attendance sheets, phone directories, kitchen assignments.

### 3.1 List Staff

**`GET /staff/staff`**  
**Permission:** `staff.read`  
**Query Params:** `branch_id` (optional filter)

**Response:**
```json
[
  {
    "id": 1,
    "restaurant_id": "uuid-rest",
    "branch_id": "uuid-branch-1",
    "name": "Ravi Kumar",
    "phone": "9876543210",
    "role": "waiter",
    "is_active": true,
    "created_at": "2026-02-10T08:00:00Z",
    "branch_name": "Main Branch"
  },
  {
    "id": 2,
    "restaurant_id": "uuid-rest",
    "branch_id": "uuid-branch-2",
    "name": "Priya Singh",
    "phone": "9988776655",
    "role": "chef",
    "is_active": true,
    "created_at": "2026-03-25T09:00:00Z",
    "branch_name": "City Center Outlet"
  }
]
```

**UI Layout:**
```
┌─────────────────────────────────────────────────┐
│  Staff Directory                 [+ Add Staff]  │
│                                                 │
│  Branch: [All Branches ▾]                       │
│  Role:   [All Roles ▾]                          │
├─────────────────────────────────────────────────┤
│  👤 Ravi Kumar                                  │
│     📞 9876543210 · Waiter · Main Branch        │
│     Active                                      │
│                               [Edit] [Deactivate]│
├─────────────────────────────────────────────────┤
│  👤 Priya Singh                                 │
│     📞 9988776655 · Chef · City Center Outlet   │
│     Active                                      │
│                               [Edit] [Deactivate]│
├─────────────────────────────────────────────────┤
│  👤 Amit Sharma                                 │
│     📞 9112233445 · Staff · Main Branch         │
│     ⚠️ Inactive                                  │
│                               [Edit] [Activate] │
└─────────────────────────────────────────────────┘
```

---

### 3.2 Create Staff

**`POST /staff/staff`**  
**Permission:** `staff.create`

**Request:**
```json
{
  "name": "Ravi Kumar",
  "phone": "9876543210",
  "role": "waiter",
  "branch_id": "uuid-branch-1"
}
```

All fields optional (defaults: name/email from auth context, role="staff", branch=main branch).

**Response:**
```json
{
  "id": 3,
  "restaurant_id": "uuid-rest",
  "branch_id": "uuid-branch-1",
  "name": "Ravi Kumar",
  "phone": "9876543210",
  "role": "waiter",
  "is_active": true,
  "created_at": "2026-04-20T12:00:00Z"
}
```

**UI:** Bottom sheet:
```
┌──────────────────────────────────────┐
│  Add Staff Member                    │
│                                      │
│  Name *                              │
│  ┌────────────────────────────────┐  │
│  │                               │  │
│  └────────────────────────────────┘  │
│                                      │
│  Phone                               │
│  ┌────────────────────────────────┐  │
│  │                               │  │
│  └────────────────────────────────┘  │
│                                      │
│  Branch                              │
│  ┌────────────────────────────────┐  │
│  │ Main Branch              ▾    │  │
│  └────────────────────────────────┘  │
│                                      │
│  Role                                │
│  ┌────────────────────────────────┐  │
│  │ ○ Manager    ○ Cashier        │  │
│  │ ○ Waiter     ○ Chef           │  │
│  │ ○ Staff                       │  │
│  └────────────────────────────────┘  │
│                                      │
│        [Cancel]    [Add Staff]       │
└──────────────────────────────────────┘
```

---

### 3.3 Update Staff

**`PATCH /staff/staff/{staff_id}`**  
**Permission:** `staff.update`

**Request:**
```json
{
  "name": "Ravi Kumar",
  "role": "cashier",
  "phone": "9876543210"
}
```

All fields optional. Only send changed fields.

---

### 3.4 Deactivate Staff

**`DELETE /staff/staff/{staff_id}`**  
**Permission:** `staff.delete`

**Response:**
```json
{
  "id": 3,
  "is_active": false
}
```

Soft-delete. Show confirmation dialog. Optionally allow "Activate" by calling PATCH with a future endpoint or re-creating.

---

## 4. Role Permissions Reference

Use this to understand what each role can do. Adapt UI accordingly.

### Permission Matrix (Key Areas)

| Permission | Owner | Manager | Cashier | Waiter | Chef | Staff |
|-----------|:-----:|:-------:|:-------:|:------:|:----:|:-----:|
| **Branch & Staff** |
| `staff.branches.read` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.branches.create` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.branches.update` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.read` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `staff.create` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.update` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.delete` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.branch_users.read` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.branch_users.create` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.branch_users.update` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `staff.branch_users.delete` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Orders** |
| `order.create` | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ |
| `order.read` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `order.edit` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `billing.discount` | ✅ 100% | ✅ 25% | ✅ 10% | ❌ | ❌ | ❌ |
| **Menu** |
| `menu.read` | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| `menu.write` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| `menu.delete` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Tables & Dine-in** |
| `table.manage` | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `dinein.manage` | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ |
| **Kitchen** |
| `kitchen.read` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| `kitchen.update` | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ |
| **Waitlist** |
| `waitlist.read` | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| `waitlist.manage` | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `waitlist.admin` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Analytics** |
| `analytics.read` | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

---

## 5. Navigation & Screen Architecture

### Suggested Screen Flow

```
Settings / Profile
  └── Branch & Staff (Owner-only section)
        ├── Branches Tab
        │     ├── Branch List (GET /staff/branches)
        │     ├── Create Branch (POST /staff/branches)
        │     └── Edit Branch (PATCH /staff/branches/{id})
        │
        ├── Team Tab (Branch Users — login-capable)
        │     ├── Team List (GET /staff/branch-users)
        │     ├── Add Member (POST /staff/branch-users)
        │     ├── Change Role (PATCH /staff/branch-users/{uid})
        │     └── Remove Member (DELETE /staff/branch-users/{uid})
        │
        └── Staff Tab (Local records — no login)
              ├── Staff List (GET /staff/staff)
              ├── Add Staff (POST /staff/staff)
              ├── Edit Staff (PATCH /staff/staff/{id})
              └── Deactivate (DELETE /staff/staff/{id})
```

### Tab Bar Design
```
┌─────────────────────────────────────────────┐
│  Branch & Staff Management                  │
│                                             │
│  [Branches]    [Team]    [Staff]            │
│  ─────────     ──────    ───────            │
│                                             │
│  (Content changes based on selected tab)    │
└─────────────────────────────────────────────┘
```

---

## 6. App Startup Auth Flow

```
App Launch
  │
  ├─ 1. Check stored access_token
  │     └─ If expired → POST /auth/token/refresh
  │
  ├─ 2. GET /auth/me
  │     └─ Returns: user_id, role, restaurant_id, branch_id, is_branch_user
  │
  ├─ 3. GET /staff/branch-users/me
  │     └─ Returns: branch_name, restaurant_name, role
  │
  ├─ 4. GET /auth/permissions/me
  │     └─ Returns: full permission map → store locally
  │
  └─ 5. Route based on role:
        ├─ Owner → Full app (all tabs visible)
        ├─ Manager → Most tabs (no branch/staff management)
        ├─ Cashier → POS, Orders, Customers, Waitlist
        ├─ Waiter → Orders, Tables, Waitlist, Dine-in
        ├─ Chef → Kitchen Display only
        └─ Staff → Kitchen Display (read-only)
```

### Role-Based Navigation Map

| Screen | Owner | Manager | Cashier | Waiter | Chef | Staff |
|--------|:-----:|:-------:|:-------:|:------:|:----:|:-----:|
| Dashboard/Home | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| Orders | ✅ | ✅ | ✅ | ✅ | 👁️ | 👁️ |
| Menu Management | ✅ | ✅ | 👁️ | ❌ | ❌ | ❌ |
| Tables / Dine-in | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| Kitchen Display | ✅ | ✅ | ❌ | 👁️ | ✅ | 👁️ |
| Waitlist | ✅ | ✅ | ✅ | ✅ | ❌ | 👁️ |
| Customers | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| Analytics | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Branch & Staff | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Settings | ✅ | 👁️ | ❌ | ❌ | ❌ | ❌ |
| Accounting/ERP | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |

👁️ = Read-only access

---

## 7. Branch Switcher (Owner Only)

Owners may have multiple branches. Add a branch selector in the app bar:

```
┌─────────────────────────────────────────────┐
│  🏢 Main Branch ▾         Bittu's Kitchen   │
│     ┌──────────────────────┐                │
│     │ ★ Main Branch    ✓  │                │
│     │   City Center        │                │
│     │   Airport Kiosk      │                │
│     │                      │                │
│     │   ── All Branches ── │                │
│     └──────────────────────┘                │
└─────────────────────────────────────────────┘
```

**Behavior:**
- Fetch branches once via `GET /staff/branches`
- Store selected `branch_id` locally
- Pass `branch_id` as query param to all API calls that support it (orders, analytics, staff, etc.)
- "All Branches" = omit `branch_id` param (owner sees everything)
- Non-owner users are locked to their assigned branch — don't show switcher

---

## 8. Error Handling

| HTTP Code | Meaning | Frontend Action |
|-----------|---------|----------------|
| `401` | Token expired/invalid | Refresh token → retry, or redirect to login |
| `403` | No permission | Show snackbar: "You don't have permission for this action" |
| `404` | Branch/Staff not found | Show snackbar: "Not found" |
| `409` | Conflict (user already assigned) | Show snackbar: "This user is already assigned to another branch" |
| `422` | Validation error (bad role, etc.) | Show inline field errors from response body |

---

## 9. Implementation Checklist

### Phase 1: Core Screens
- [ ] Branch list screen with create/edit
- [ ] Branch user (Team) list with add/change role/remove  
- [ ] Local staff list with add/edit/deactivate
- [ ] Tab navigation between the three sections

### Phase 2: Auth & Permissions
- [ ] Fetch and cache `GET /auth/permissions/me` on login
- [ ] `GET /staff/branch-users/me` on app startup for role/branch context
- [ ] Hide/show navigation items based on role
- [ ] Disable action buttons based on specific permissions

### Phase 3: Branch Switcher
- [ ] Branch dropdown in app bar (owner only)
- [ ] Pass `branch_id` to all data-fetching APIs
- [ ] "All Branches" aggregated view

### Phase 4: Polish
- [ ] Empty states for each list
- [ ] Pull-to-refresh on all lists
- [ ] Confirmation dialogs for destructive actions (remove, deactivate)
- [ ] Optimistic UI updates on create/delete
- [ ] Error handling with snackbars for all API failures

---

## 10. Quick API Reference

| Action | Method | Endpoint | Permission |
|--------|--------|----------|------------|
| List branches | GET | `/staff/branches` | `staff.branches.read` |
| Create branch | POST | `/staff/branches` | `staff.branches.create` |
| Update branch | PATCH | `/staff/branches/{id}` | `staff.branches.update` |
| List team (branch users) | GET | `/staff/branch-users` | `staff.branch_users.read` |
| My branch user info | GET | `/staff/branch-users/me` | *(any authenticated)* |
| Add team member | POST | `/staff/branch-users` | `staff.branch_users.create` |
| Change role | PATCH | `/staff/branch-users/{uid}` | `staff.branch_users.update` |
| Remove team member | DELETE | `/staff/branch-users/{uid}` | `staff.branch_users.delete` |
| List local staff | GET | `/staff/staff` | `staff.read` |
| Add local staff | POST | `/staff/staff` | `staff.create` |
| Update local staff | PATCH | `/staff/staff/{id}` | `staff.update` |
| Deactivate local staff | DELETE | `/staff/staff/{id}` | `staff.delete` |
| My permissions | GET | `/auth/permissions/me` | *(any authenticated)* |
| My profile | GET | `/auth/me` | *(any authenticated)* |


# 🧩 Branch & Staff Management — Flutter Implementation Guide (Production-Ready)

**Base URL:** https://api.bittupos.com/api/v1
**Auth:** Authorization: Bearer <supabase_jwt>
**Architecture:** Permission-driven UI (NO role-based logic in frontend)

---

# 🚨 CORE PRINCIPLE (NON-NEGOTIABLE)

> ❗ **Frontend MUST rely on permissions, NOT roles**

DO NOT:

* if (role == "manager") ❌

DO:

* if (permissions["staff.create"].allowed) ✅

---

# 🧠 GLOBAL APP STATE (VERY IMPORTANT)

Create a centralized state (Provider / Riverpod / Bloc):

```dart
class AppContext {
  String userId;
  String role;
  String restaurantId;
  String branchId;
  String restaurantName;

  Map<String, Permission> permissions;

  String? selectedBranchId; // owner only
}

class Permission {
  final bool allowed;
  final Map<String, dynamic> meta;
}
```

👉 This becomes **single source of truth**

---

# 🔐 APP STARTUP FLOW (STRICT ORDER)

```
1. Load token
2. GET /auth/me
3. GET /staff/branch-users/me
4. GET /auth/permissions/me
5. Initialize AppContext
6. Navigate based on permissions
```

👉 If permissions fail → block app (retry screen)

---

# 🔑 PERMISSIONS HANDLING

## Normalize response (IMPORTANT)

Backend returns mixed structure → normalize:

```dart
Permission parsePermission(dynamic value) {
  if (value is bool) {
    return Permission(allowed: value, meta: {});
  }
  return Permission(
    allowed: value["allowed"] ?? false,
    meta: value["meta"] ?? {},
  );
}
```

---

## Helper functions

```dart
bool can(String key) {
  return appContext.permissions[key]?.allowed ?? false;
}

dynamic meta(String key, String field) {
  return appContext.permissions[key]?.meta[field];
}
```

---

# 🏢 BRANCH MANAGEMENT

## Screen visibility

```dart
if (!can("staff.branches.read")) return SizedBox();
```

---

## List Branches

API: `GET /staff/branches`

### State handling

* Store branches in provider
* Cache for session
* Refresh on pull

### Rules:

* Main branch → pinned top
* No deactivate for main branch
* Hide Add button if !can("staff.branches.create")

---

## Create Branch

UI: BottomSheet

```dart
if (!can("staff.branches.create")) return;
```

### Validation:

* name required
* manager optional

---

## Update Branch

```dart
if (!can("staff.branches.update")) disableEdit();
```

---

# 👥 TEAM (BRANCH USERS)

## 🚨 IMPORTANT: Identity Fix (NEW)

Backend only gives `user_id`.

👉 You MUST enrich UI:

### Option A (recommended)

Extend backend → return:

```json
{
  "user_id": "...",
  "name": "Rahul",
  "email": "rahul@gmail.com"
}
```

### Option B (temporary)

* Show short ID: `abc...xyz`

---

## List Team

API: `GET /staff/branch-users`

### UI Rules:

* Hide screen if !can("staff.branch_users.read")
* Disable actions individually:

  * change role → `staff.branch_users.update`
  * remove → `staff.branch_users.delete`

---

## Add Member

```dart
if (!can("staff.branch_users.create")) return;
```

### UX Improvement (IMPORTANT)

Replace:
❌ paste user_id

With:
✅ email-based invite (future)

---

## Change Role

```dart
if (!can("staff.branch_users.update")) return;
```

---

## Remove Member

```dart
if (!can("staff.branch_users.delete")) return;
```

Show confirmation dialog.

---

# 👨‍🍳 LOCAL STAFF (NO LOGIN)

## List

API: `GET /staff/staff`

```dart
if (!can("staff.read")) return;
```

---

## Create

```dart
if (!can("staff.create")) return;
```

---

## Update

```dart
if (!can("staff.update")) return;
```

---

## Delete

```dart
if (!can("staff.delete")) return;
```

---

# 🔀 BRANCH SWITCHER (OWNER ONLY)

## Show only if:

```dart
appContext.role == "owner"
```

---

## Global Behavior

```dart
appContext.selectedBranchId = newBranchId;

// MUST:
clearAllCachedData();
refetchAllScreens();
```

---

## API usage

```dart
GET /orders?branch_id=selectedBranchId
```

If null → show all branches

---

# ⚡ UI STATE MANAGEMENT (IMPORTANT)

Every screen must support:

### 1. Loading

* skeleton UI

### 2. Empty

* “No data found”

### 3. Error

* retry button

### 4. Success

---

# ⚠️ ERROR HANDLING

| Code | Action                |
| ---- | --------------------- |
| 401  | refresh token → retry |
| 403  | show snackbar         |
| 409  | show conflict message |
| 422  | show inline errors    |

---

# 💣 META-BASED UI CONTROL (ADVANCED)

Example: discount

```dart
final maxDiscount = meta("billing.discount", "max_discount_percent");

Slider(
  max: maxDiscount ?? 0,
)
```

---

# 🧠 NAVIGATION CONTROL

Use permissions → NOT roles

```dart
if (can("analytics.read")) showAnalyticsTab();
if (can("menu.write")) showMenuEdit();
```

---

# 📊 (NEW) ACTIVITY / AUDIT UI (HIGH VALUE)

You already log:

* discounts
* refunds
* actions

👉 Add screen:

```
Activity Log
- ₹500 discount by Rahul
- Refund ₹200 by cashier
```

👉 This builds TRUST

---

# 🚨 ANTI-PATTERNS (DO NOT DO)

❌ if (role == "manager")
❌ show all buttons then block API
❌ rely on static permission list
❌ ignore meta constraints

---

# 📦 IMPLEMENTATION CHECKLIST

## Core

* [ ] AppContext setup
* [ ] Permission parsing
* [ ] Global can() helper

## Screens

* [ ] Branch screen
* [ ] Team screen
* [ ] Staff screen

## UX

* [ ] Loading states
* [ ] Error handling
* [ ] Empty states

## Advanced

* [ ] Branch switcher
* [ ] Permission-based UI
* [ ] Activity logs screen

---

# 💥 FINAL RULE

> Backend controls **security**
> Frontend controls **experience**

If frontend ignores permissions → system feels broken
If backend ignores permissions → system becomes unsafe

👉 You’ve already solved backend. Now make frontend match it.

---

