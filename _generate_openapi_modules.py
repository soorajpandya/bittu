#!/usr/bin/env python3
"""
Bittu OpenAPI 3.1 Modularizer
==============================
Splits monolithic OpenAPI spec into enterprise-grade modular structure.

Output Structure:
  Bittu_Backend/
    core.yaml                          # Root spec — info, servers, module refs
    components/
      schemas.yaml                     # ALL 190+ schemas (single source of truth)
      responses.yaml                   # Shared HTTP responses (401, 403, 422)
      parameters.yaml                  # Common query parameters
      security.yaml                    # SecuritySchemes + default security
    modules/
      v1/
        operations/                    # Core restaurant operations
          orders.yaml
          kitchen.yaml
          tables.yaml
          dinein.yaml
          inventory.yaml
          delivery.yaml
          waitlist.yaml
        catalog/                       # Menu & item management
          items.yaml
          categories.yaml
          combos.yaml
          modifiers.yaml
          food-images.yaml
          ai-menu.yaml
        finance/                       # Financial operating system
          finance.yaml
          accounting.yaml
          invoices.yaml
          expenses.yaml
          tax.yaml
          bank-recon.yaml
          settlements.yaml
          cash-transactions.yaml
          due-payments.yaml
          billing.yaml
          reports.yaml
        payments/                      # Payment gateways
          payments.yaml
          razorpay.yaml
          cashfree.yaml
          payu.yaml
          paytm.yaml
          phonepe.yaml
          zivonpay.yaml
          webhooks.yaml
        auth/                          # Authentication & staff
          auth.yaml
          staff.yaml
          kyc.yaml
        customers/                     # Customer management
          customers.yaml
          favourites.yaml
          coupons.yaml
          offers.yaml
          feedback.yaml
          notifications.yaml
        platform/                      # Platform & configuration
          restaurants.yaml
          subscriptions.yaml
          erp.yaml
          purchase-orders.yaml
          google.yaml
          health.yaml
          help.yaml
          misc.yaml
          voice.yaml
          audit-logs.yaml
        analytics/                     # Analytics & reporting
          analytics.yaml
      internal/
        _manifest.yaml                 # Lists internal-only API modules
      public/
        _manifest.yaml                 # Lists public (no-auth) API modules
"""

import json
import copy
import os
import sys
from collections import defaultdict, OrderedDict
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# YAML representer fixes — preserve key order, handle $ref cleanly
# ---------------------------------------------------------------------------
class OrderedDumper(yaml.SafeDumper):
    pass

def _dict_representer(dumper, data):
    return dumper.represent_mapping(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, data.items())

OrderedDumper.add_representer(OrderedDict, _dict_representer)
OrderedDumper.add_representer(dict, _dict_representer)

def yaml_dump(data, stream=None):
    return yaml.dump(data, stream, Dumper=OrderedDumper,
                     default_flow_style=False, sort_keys=False,
                     allow_unicode=True, width=120)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
INPUT_SPEC = "_full_openapi.json"
OUTPUT_DIR = "Bittu_Backend"

# Tag → (domain, module_name) mapping
TAG_TO_MODULE = {
    # --- operations/ ---
    "Orders":              ("operations", "orders"),
    "Kitchen":             ("operations", "kitchen"),
    "Kitchen Stations":    ("operations", "kitchen"),
    "Tables":              ("operations", "tables"),
    "Table Events":        ("operations", "tables"),
    "Table Sessions":      ("operations", "tables"),
    "Restaurant Tables":   ("operations", "tables"),
    "Dine-In Sessions":    ("operations", "dinein"),
    "Inventory":           ("operations", "inventory"),
    "Ingredients":         ("operations", "inventory"),
    "Delivery":            ("operations", "delivery"),
    "Delivery Partners":   ("operations", "delivery"),
    "Deliverable Pincodes":("operations", "delivery"),
    "Waitlist":            ("operations", "waitlist"),

    # --- catalog/ ---
    "Items":               ("catalog", "items"),
    "Item Addons":         ("catalog", "items"),
    "Item Extras":         ("catalog", "items"),
    "Item Variants":       ("catalog", "items"),
    "Item Station Mapping":("catalog", "items"),
    "Item Ingredients":    ("catalog", "items"),
    "Categories":          ("catalog", "categories"),
    "Combos":              ("catalog", "combos"),
    "Modifiers":           ("catalog", "modifiers"),
    "Modifier Groups":     ("catalog", "modifiers"),
    "Food Images":         ("catalog", "food-images"),
    "AI Ingredients":      ("catalog", "ai-menu"),
    "AI Menu Scanner":     ("catalog", "ai-menu"),

    # --- finance/ ---
    "Financial Operating System": ("finance", "finance"),
    "Accounting":          ("finance", "accounting"),
    "Accounting Rules":    ("finance", "accounting"),
    "Chart of Accounts":   ("finance", "accounting"),
    "Invoices":            ("finance", "invoices"),
    "Invoice Import":      ("finance", "invoices"),
    "Expenses":            ("finance", "expenses"),
    "Tax Liability":       ("finance", "tax"),
    "Sub-Ledger":          ("finance", "tax"),
    "Bank Reconciliation": ("finance", "bank-recon"),
    "Settlements":         ("finance", "settlements"),
    "Cash Transactions":   ("finance", "cash-transactions"),
    "Due Payments":        ("finance", "due-payments"),
    "Billing":             ("finance", "billing"),
    "Reports":             ("finance", "reports"),
    "Financial Reports":   ("finance", "reports"),

    # --- payments/ ---
    "Payments":            ("payments", "payments"),
    "Razorpay Extended":   ("payments", "razorpay"),
    "Cashfree PG":         ("payments", "cashfree"),
    "PayU":                ("payments", "payu"),
    "Paytm":               ("payments", "paytm"),
    "PhonePe":             ("payments", "phonepe"),
    "Zivonpay":            ("payments", "zivonpay"),
    "Webhooks":            ("payments", "webhooks"),

    # --- auth/ ---
    "Auth":                ("auth", "auth"),
    "Staff":               ("auth", "staff"),
    "KYC / Verification":  ("auth", "kyc"),
    "DigiLocker KYC":      ("auth", "kyc"),

    # --- customers/ ---
    "Customers":           ("customers", "customers"),
    "Customer Addresses":  ("customers", "customers"),
    "Favourites":          ("customers", "favourites"),
    "Favourite Items":     ("customers", "favourites"),
    "Coupons":             ("customers", "coupons"),
    "Offers":              ("customers", "offers"),
    "Feedback":            ("customers", "feedback"),
    "Notifications":       ("customers", "notifications"),

    # --- platform/ ---
    "Restaurants":         ("platform", "restaurants"),
    "Restaurant Settings": ("platform", "restaurants"),
    "Subscriptions":       ("platform", "subscriptions"),
    "ERP":                 ("platform", "erp"),
    "Purchase Orders":     ("platform", "purchase-orders"),
    "Google Business Profile": ("platform", "google"),
    "Health":              ("platform", "health"),
    "Help Articles":       ("platform", "help"),
    "Miscellaneous":       ("platform", "misc"),
    "Voice / TTS":         ("platform", "voice"),
    "Audit Logs":          ("platform", "audit-logs"),

    # --- analytics/ ---
    "Analytics":           ("analytics", "analytics"),
}

# Modules with public (no-auth) endpoints
PUBLIC_MODULES = {
    ("auth", "auth"),
    ("operations", "tables"),
    ("operations", "dinein"),
    ("platform", "health"),
    ("operations", "waitlist"),
    ("payments", "webhooks"),
    ("auth", "kyc"),
}

# Domain descriptions for core.yaml tag groups
DOMAIN_DESCRIPTIONS = {
    "operations": "Core restaurant operations — orders, kitchen, tables, delivery",
    "catalog":    "Menu & item management — items, categories, combos, modifiers",
    "finance":    "Financial Operating System — accounting, invoices, GST, reports",
    "payments":   "Payment gateways — Razorpay, Cashfree, PhonePe, PayU",
    "auth":       "Authentication, staff management, KYC verification",
    "customers":  "Customer management — profiles, addresses, coupons, feedback",
    "platform":   "Platform configuration — restaurants, subscriptions, ERP",
    "analytics":  "Analytics & reporting — dashboards, heatmaps, funnels",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_spec(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_refs(obj, prefix="#/components/schemas/"):
    """Recursively find all $ref values matching prefix."""
    refs = set()
    if isinstance(obj, dict):
        if "$ref" in obj and isinstance(obj["$ref"], str) and obj["$ref"].startswith(prefix):
            refs.add(obj["$ref"][len(prefix):])
        for v in obj.values():
            refs.update(find_refs(v, prefix))
    elif isinstance(obj, list):
        for item in obj:
            refs.update(find_refs(item, prefix))
    return refs


def rewrite_refs(obj, old_prefix, new_prefix):
    """Recursively rewrite $ref paths."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == "$ref" and isinstance(v, str) and v.startswith(old_prefix):
                schema_name = v[len(old_prefix):]
                result[k] = f"{new_prefix}{schema_name}"
            else:
                result[k] = rewrite_refs(v, old_prefix, new_prefix)
        return result
    elif isinstance(obj, list):
        return [rewrite_refs(item, old_prefix, new_prefix) for item in obj]
    return obj


def resolve_all_schema_deps(all_schemas):
    """For every schema, resolve transitive dependencies."""
    dep_map = {}
    for name, schema_body in all_schemas.items():
        deps = find_refs(schema_body)
        dep_map[name] = deps
    # Transitive closure
    changed = True
    while changed:
        changed = False
        for name, deps in dep_map.items():
            new_deps = set(deps)
            for d in list(deps):
                if d in dep_map:
                    new_deps.update(dep_map[d])
            if new_deps != deps:
                dep_map[name] = new_deps
                changed = True
    return dep_map


def write_yaml_file(path, data, header_comment=None):
    """Write YAML file with optional header comment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml_dump(data)
    with open(path, "w", encoding="utf-8") as f:
        if header_comment:
            for line in header_comment.strip().split("\n"):
                f.write(f"# {line}\n")
            f.write("\n")
        f.write(content)


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate():
    print("Loading OpenAPI spec...")
    spec = load_spec(INPUT_SPEC)
    base = Path(OUTPUT_DIR)

    all_schemas = spec.get("components", {}).get("schemas", {})
    security_schemes = spec.get("components", {}).get("securitySchemes", {})
    info = spec.get("info", {})
    servers = spec.get("servers", [])

    print(f"  Paths: {len(spec.get('paths', {}))}")
    print(f"  Schemas: {len(all_schemas)}")

    # -----------------------------------------------------------------------
    # 1. Map paths → modules
    # -----------------------------------------------------------------------
    print("\nMapping paths to modules...")
    # module_key → { path → { method → operation } }
    module_paths = defaultdict(lambda: defaultdict(dict))
    module_schemas_used = defaultdict(set)
    module_tags = defaultdict(set)
    unmapped = []

    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags", ["untagged"])
            primary_tag = tags[0]
            module_key = TAG_TO_MODULE.get(primary_tag)
            if module_key is None:
                unmapped.append((primary_tag, method.upper(), path))
                module_key = ("platform", "misc")

            module_paths[module_key][path][method] = copy.deepcopy(operation)
            refs = find_refs(operation)
            module_schemas_used[module_key].update(refs)
            module_tags[module_key].update(tags)

    if unmapped:
        print(f"  WARNING: {len(unmapped)} unmapped routes → platform/misc")
        for tag, method, path in unmapped[:5]:
            print(f"    [{tag}] {method} {path}")

    print(f"  Modules: {len(module_paths)}")
    for key in sorted(module_paths.keys()):
        domain, mod = key
        path_count = len(module_paths[key])
        print(f"    {domain}/{mod}: {path_count} paths")

    # -----------------------------------------------------------------------
    # 2. Write components/schemas.yaml
    # -----------------------------------------------------------------------
    print("\nWriting components/schemas.yaml...")
    # Rewrite internal schema refs: #/components/schemas/X → #/X
    schemas_for_file = {}
    for name in sorted(all_schemas.keys()):
        rewritten = rewrite_refs(
            copy.deepcopy(all_schemas[name]),
            "#/components/schemas/",
            "#/"
        )
        schemas_for_file[name] = rewritten

    write_yaml_file(
        base / "components" / "schemas.yaml",
        schemas_for_file,
        header_comment="""\
Bittu API — Shared Schemas
===========================
Single source of truth for all request/response schemas.
Referenced by modules via: $ref: '../../../components/schemas.yaml#/SchemaName'
Total schemas: """ + str(len(schemas_for_file))
    )
    print(f"  Written {len(schemas_for_file)} schemas")

    # -----------------------------------------------------------------------
    # 3. Write components/responses.yaml
    # -----------------------------------------------------------------------
    print("Writing components/responses.yaml...")
    responses_data = {
        "Unauthorized": {
            "description": "Not authenticated — missing or invalid JWT token",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "string",
                                "example": "Not authenticated"
                            }
                        }
                    }
                }
            }
        },
        "Forbidden": {
            "description": "Permission denied — insufficient RBAC permissions",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "string",
                                "example": "Permission denied"
                            }
                        }
                    }
                }
            }
        },
        "NotFound": {
            "description": "Resource not found",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "string",
                                "example": "Not found"
                            }
                        }
                    }
                }
            }
        },
        "ValidationError": {
            "description": "Request validation error",
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "./schemas.yaml#/HTTPValidationError"
                    }
                }
            }
        },
        "ServerError": {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "detail": {
                                "type": "string",
                                "example": "Internal server error"
                            }
                        }
                    }
                }
            }
        }
    }
    write_yaml_file(
        base / "components" / "responses.yaml",
        responses_data,
        header_comment="""\
Bittu API — Shared Responses
==============================
Common HTTP response definitions shared across all modules.
Referenced via: $ref: '../../../components/responses.yaml#/Unauthorized'"""
    )

    # -----------------------------------------------------------------------
    # 4. Write components/parameters.yaml
    # -----------------------------------------------------------------------
    print("Writing components/parameters.yaml...")
    parameters_data = {
        "BranchId": {
            "name": "branch_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "uuid"},
            "description": "Filter results by branch ID"
        },
        "RestaurantId": {
            "name": "restaurant_id",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "uuid"},
            "description": "Restaurant context (resolved from auth if omitted)"
        },
        "Limit": {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            "description": "Maximum number of results to return"
        },
        "Offset": {
            "name": "offset",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "default": 0, "minimum": 0},
            "description": "Number of results to skip for pagination"
        },
        "FromDate": {
            "name": "from_date",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date"},
            "description": "Start date filter (YYYY-MM-DD)"
        },
        "ToDate": {
            "name": "to_date",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "format": "date"},
            "description": "End date filter (YYYY-MM-DD)"
        },
        "Status": {
            "name": "status",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Filter by status value"
        },
        "Search": {
            "name": "search",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Free-text search query"
        }
    }
    write_yaml_file(
        base / "components" / "parameters.yaml",
        parameters_data,
        header_comment="""\
Bittu API — Shared Parameters
================================
Common query parameters shared across multiple modules.
Referenced via: $ref: '../../../components/parameters.yaml#/Limit'"""
    )

    # -----------------------------------------------------------------------
    # 5. Write components/security.yaml
    # -----------------------------------------------------------------------
    print("Writing components/security.yaml...")
    security_data = {
        "securitySchemes": {
            "HTTPBearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Supabase JWT access token. Obtain via /api/v1/auth/google OAuth flow."
            }
        },
        "defaultSecurity": [
            {"HTTPBearer": []}
        ]
    }
    write_yaml_file(
        base / "components" / "security.yaml",
        security_data,
        header_comment="""\
Bittu API — Security Definitions
===================================
Authentication and authorization configuration.
JWT tokens issued by Supabase Auth (Google OAuth / PKCE flow)."""
    )

    # -----------------------------------------------------------------------
    # 6. Write module YAML files
    # -----------------------------------------------------------------------
    print("\nWriting module files...")
    # $ref prefix: from modules/v1/{domain}/{module}.yaml → components/schemas.yaml
    SCHEMA_REF_PREFIX = "../../../components/schemas.yaml#/"
    RESPONSE_REF_PREFIX = "../../../components/responses.yaml#/"
    PARAM_REF_PREFIX = "../../../components/parameters.yaml#/"
    SECURITY_REF_PREFIX = "../../../components/security.yaml#/"

    module_manifest = {}  # For core.yaml

    for (domain, module_name), paths in sorted(module_paths.items()):
        module_file = base / "modules" / "v1" / domain / f"{module_name}.yaml"

        # Rewrite schema refs in all operations
        rewritten_paths = rewrite_refs(
            copy.deepcopy(dict(paths)),
            "#/components/schemas/",
            SCHEMA_REF_PREFIX
        )

        # Collect unique tags for this module
        tags = sorted(module_tags[(domain, module_name)])

        # Count operations
        op_count = sum(
            len([m for m in methods.values() if isinstance(m, dict)])
            for methods in paths.values()
        )

        # Determine visibility
        visibility = "public" if (domain, module_name) in PUBLIC_MODULES else "internal"

        # Build the module document
        module_doc = {
            # Extension metadata
            "x-module-info": {
                "domain": domain,
                "module": module_name,
                "version": "v1",
                "visibility": visibility,
                "tags": tags,
                "operationCount": op_count,
                "pathCount": len(paths),
            },
            "paths": rewritten_paths,
        }

        write_yaml_file(
            module_file,
            module_doc,
            header_comment=f"""\
Bittu API — {domain.title()}/{module_name.title()} Module
{'=' * (len(domain) + len(module_name) + 22)}
Domain: {domain}
Module: {module_name}
Version: v1
Visibility: {visibility}
Tags: {', '.join(tags)}
Operations: {op_count}
Paths: {len(paths)}

All schema references point to: ../../../components/schemas.yaml
"""
        )

        # Track for core.yaml
        rel_path = f"modules/v1/{domain}/{module_name}.yaml"
        module_manifest.setdefault(domain, []).append({
            "name": module_name,
            "path": rel_path,
            "tags": tags,
            "visibility": visibility,
            "paths": len(paths),
            "operations": op_count,
        })

        print(f"  ✓ {domain}/{module_name}: {len(paths)} paths, {op_count} ops [{visibility}]")

    # -----------------------------------------------------------------------
    # 7. Write internal / public manifests
    # -----------------------------------------------------------------------
    print("\nWriting visibility manifests...")
    internal_modules = []
    public_modules = []
    for domain, modules in sorted(module_manifest.items()):
        for mod in modules:
            entry = {
                "module": f"{domain}/{mod['name']}",
                "path": mod["path"],
                "tags": mod["tags"],
            }
            if mod["visibility"] == "public":
                public_modules.append(entry)
            else:
                internal_modules.append(entry)

    write_yaml_file(
        base / "modules" / "internal" / "_manifest.yaml",
        {
            "description": "Internal API modules — require JWT authentication",
            "modules": internal_modules,
        },
        header_comment="Bittu API — Internal Modules Manifest"
    )

    write_yaml_file(
        base / "modules" / "public" / "_manifest.yaml",
        {
            "description": "Public API modules — some endpoints accessible without authentication",
            "modules": public_modules,
        },
        header_comment="Bittu API — Public Modules Manifest"
    )

    # -----------------------------------------------------------------------
    # 8. Write core.yaml (root document)
    # -----------------------------------------------------------------------
    print("\nWriting core.yaml...")

    # Build path references
    path_refs = {}
    for domain, modules in sorted(module_manifest.items()):
        for mod in modules:
            path_refs[f"x-module-{domain}-{mod['name']}"] = {
                "$ref": mod["path"]
            }

    # Build tag list with domain grouping
    all_tags = []
    tag_groups = []
    for domain, modules in sorted(module_manifest.items()):
        group_tags = []
        for mod in modules:
            for tag in mod["tags"]:
                if tag not in [t["name"] for t in all_tags]:
                    all_tags.append({
                        "name": tag,
                        "description": f"{domain}/{mod['name']} — {tag}",
                    })
                    group_tags.append(tag)
        if group_tags:
            tag_groups.append({
                "name": domain.title(),
                "tags": group_tags,
                "description": DOMAIN_DESCRIPTIONS.get(domain, ""),
            })

    core_doc = {
        "openapi": "3.1.0",
        "info": {
            "title": info.get("title", "Bittu API"),
            "description": (
                "Real-time Restaurant Operating System\n\n"
                "## Architecture\n"
                "This API is organized into domain modules:\n\n"
                "| Domain | Description |\n"
                "| --- | --- |\n"
            ) + "\n".join(
                f"| **{d.title()}** | {DOMAIN_DESCRIPTIONS.get(d, '')} |"
                for d in sorted(DOMAIN_DESCRIPTIONS.keys())
            ) + (
                "\n\n## Authentication\n"
                "All endpoints (except Health and public QR flows) require a Supabase JWT.\n"
                "Obtain tokens via `GET /api/v1/auth/google` → Google OAuth → callback.\n\n"
                "## Versioning\n"
                "Current version: **v1** — all paths prefixed with `/api/v1/`.\n"
                "Module files are in `modules/v1/{domain}/{module}.yaml`."
            ),
            "version": info.get("version", "1.0.0"),
            "contact": {
                "name": "Bittu Engineering",
                "url": "https://www.bittupos.com",
            },
            "license": {
                "name": "Proprietary",
            },
        },
        "servers": servers if servers else [
            {
                "url": "https://api.bittupos.com",
                "description": "Production"
            },
            {
                "url": "http://localhost:8000",
                "description": "Local development"
            }
        ],
        "security": [{"HTTPBearer": []}],
        "tags": all_tags,
        "x-tagGroups": tag_groups,
        "components": {
            "securitySchemes": {
                "$ref": "components/security.yaml#/securitySchemes"
            },
        },
        "x-modules": {
            domain: [
                {
                    "name": mod["name"],
                    "$ref": mod["path"],
                    "visibility": mod["visibility"],
                    "tags": mod["tags"],
                    "paths": mod["paths"],
                    "operations": mod["operations"],
                }
                for mod in modules
            ]
            for domain, modules in sorted(module_manifest.items())
        },
    }

    write_yaml_file(
        base / "core.yaml",
        core_doc,
        header_comment="""\
Bittu API — Core Specification
=================================
OpenAPI 3.1 Root Document

This is the entry point for the modular Bittu API specification.
All paths are defined in domain-specific module files under modules/v1/.
All schemas are centralized in components/schemas.yaml.

To bundle into a single file, use:
  npx @redocly/cli bundle core.yaml -o bundled.yaml

To preview with Redoc:
  npx @redocly/cli preview-docs core.yaml

Structure:
  core.yaml                          ← You are here
  components/
    schemas.yaml                     ← 190+ shared schemas
    responses.yaml                   ← Common HTTP responses
    parameters.yaml                  ← Shared query parameters
    security.yaml                    ← JWT security scheme
  modules/v1/
    {domain}/{module}.yaml           ← Path definitions by domain"""
    )

    # -----------------------------------------------------------------------
    # 9. Write redocly.yaml config (for bundling/preview)
    # -----------------------------------------------------------------------
    print("Writing redocly.yaml...")
    redocly_config = {
        "extends": ["recommended"],
        "apis": {
            "bittu@v1": {
                "root": "core.yaml",
            }
        },
        "theme": {
            "openapi": {
                "showExtensions": True,
                "generateCodeSamples": {
                    "languages": [
                        {"lang": "curl"},
                        {"lang": "Python"},
                        {"lang": "JavaScript"},
                    ]
                }
            }
        },
        "rules": {
            "no-unresolved-refs": "error",
            "no-unused-components": "warn",
            "operation-operationId": "error",
            "tag-description": "warn",
        }
    }
    write_yaml_file(
        base / "redocly.yaml",
        redocly_config,
        header_comment="""\
Redocly CLI Configuration
===========================
Usage:
  npx @redocly/cli lint core.yaml
  npx @redocly/cli bundle core.yaml -o bundled.yaml
  npx @redocly/cli preview-docs core.yaml"""
    )

    # -----------------------------------------------------------------------
    # 10. Write README.md
    # -----------------------------------------------------------------------
    print("Writing README.md...")
    readme = f"""# Bittu API — OpenAPI 3.1 Modular Specification

## Overview

Enterprise-grade modular OpenAPI specification for the Bittu Restaurant Operating System.

| Metric | Count |
| --- | --- |
| **Total Paths** | {len(spec.get('paths', {}))} |
| **Total Schemas** | {len(all_schemas)} |
| **Domains** | {len(module_manifest)} |
| **Modules** | {sum(len(m) for m in module_manifest.values())} |
| **Total Operations** | {sum(mod['operations'] for mods in module_manifest.values() for mod in mods)} |

## Directory Structure

```
Bittu_Backend/
  core.yaml                          # Root OpenAPI 3.1 document
  redocly.yaml                       # Redocly CLI config
  components/
    schemas.yaml                     # {len(all_schemas)} shared schemas (single source of truth)
    responses.yaml                   # Common HTTP responses (401, 403, 422, 404, 500)
    parameters.yaml                  # Shared query parameters
    security.yaml                    # JWT Bearer security scheme
  modules/
    v1/                              # API version 1
"""

    for domain in sorted(module_manifest.keys()):
        modules = module_manifest[domain]
        readme += f"      {domain}/\n"
        for mod in modules:
            readme += f"        {mod['name']}.yaml          # {', '.join(mod['tags'][:3])}\n"

    readme += f"""    internal/
      _manifest.yaml                 # {len(internal_modules)} internal modules
    public/
      _manifest.yaml                 # {len(public_modules)} public modules
```

## Domains

| Domain | Modules | Paths | Operations | Description |
| --- | --- | --- | --- | --- |
"""
    for domain in sorted(module_manifest.keys()):
        modules = module_manifest[domain]
        total_paths = sum(m["paths"] for m in modules)
        total_ops = sum(m["operations"] for m in modules)
        desc = DOMAIN_DESCRIPTIONS.get(domain, "")
        readme += f"| **{domain.title()}** | {len(modules)} | {total_paths} | {total_ops} | {desc} |\n"

    readme += f"""
## Quick Start

### Preview with Redoc
```bash
npx @redocly/cli preview-docs core.yaml
```

### Bundle into single file
```bash
npx @redocly/cli bundle core.yaml -o bundled.yaml
```

### Lint the spec
```bash
npx @redocly/cli lint core.yaml
```

### Validate with Swagger UI
```bash
npx @redocly/cli bundle core.yaml -o bundled.yaml
# Open bundled.yaml in https://editor.swagger.io
```

## Versioning Strategy

- Current: `modules/v1/` — all paths under `/api/v1/`
- Future: `modules/v2/` — breaking changes only, v1 preserved
- Module files include `x-module-info.version` for tracking

## Visibility

Modules are classified as **public** or **internal**:
- **Public**: Endpoints accessible without JWT (OAuth flows, QR scans, health checks, webhooks)
- **Internal**: All other endpoints requiring authenticated JWT

See `modules/internal/_manifest.yaml` and `modules/public/_manifest.yaml`.

## Schema References

All schemas live in `components/schemas.yaml` (single source of truth).
Module files reference schemas via:
```yaml
$ref: '../../../components/schemas.yaml#/SchemaName'
```

Within `components/schemas.yaml`, internal cross-references use:
```yaml
$ref: '#/OtherSchemaName'
```

## Contributing

1. Add new paths to the appropriate `modules/v1/{{domain}}/{{module}}.yaml`
2. Add new schemas to `components/schemas.yaml`
3. Run `npx @redocly/cli lint core.yaml` before committing
4. Bundle with `npx @redocly/cli bundle core.yaml -o bundled.yaml` for deployment
"""

    readme_path = base / "README.md"
    readme_path.parent.mkdir(parents=True, exist_ok=True)
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("GENERATION COMPLETE")
    print("=" * 60)
    total_files = (
        1 +  # core.yaml
        4 +  # components/*
        len(module_paths) +  # module files
        2 +  # manifests
        1 +  # redocly.yaml
        1    # README.md
    )
    print(f"  Output directory: {base.resolve()}")
    print(f"  Total files: {total_files}")
    print(f"  Schemas: {len(all_schemas)}")
    print(f"  Paths: {len(spec.get('paths', {}))}")
    print(f"  Modules: {len(module_paths)}")
    print(f"  Domains: {len(module_manifest)}")
    for domain in sorted(module_manifest.keys()):
        modules = module_manifest[domain]
        print(f"    {domain}: {len(modules)} modules")
        for mod in modules:
            print(f"      {mod['name']}: {mod['paths']}p / {mod['operations']}ops [{mod['visibility']}]")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(generate())
