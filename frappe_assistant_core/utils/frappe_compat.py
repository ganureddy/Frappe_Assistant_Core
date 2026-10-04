# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Frappe version compatibility layer.

Frappe Assistant Core targets Frappe v13 (Python 3.8). This module provides
shims for framework APIs that only exist in newer Frappe releases, so the
rest of the codebase can call one function regardless of the running
version. Each shim prefers the native implementation when it is available.
"""

import hashlib
import json
import time
from functools import wraps

import frappe

# ---------------------------------------------------------------------------
# Caching decorators (frappe.utils.caching was added in v14)
# ---------------------------------------------------------------------------

try:
    from frappe.utils.caching import redis_cache, site_cache  # noqa: F401
except ImportError:

    def _make_call_key(func_key, args, kwargs):
        try:
            payload = json.dumps([args, sorted(kwargs.items())], default=str, sort_keys=True)
        except Exception:
            payload = repr((args, sorted(kwargs.items())))
        return f"{func_key}::{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"

    def redis_cache(ttl=3600, user=None):
        """Cache the result of a function in Redis for ``ttl`` seconds.

        Mirrors frappe.utils.caching.redis_cache from Frappe v14+, including the
        ``module.qualname::hash`` key format so wildcard invalidation such as
        ``delete_keys("*get_cached_server_settings*")`` keeps working.
        """

        def decorator(func):
            func_key = f"{func.__module__}.{func.__qualname__}"

            @wraps(func)
            def wrapper(*args, **kwargs):
                cache = frappe.cache()
                call_key = _make_call_key(func_key, args, kwargs)
                cached = cache.get_value(call_key, user=user, expires=True)
                if cached is not None:
                    return cached

                result = func(*args, **kwargs)
                cache.set_value(call_key, result, user=user, expires_in_sec=ttl)
                return result

            def clear_cache():
                frappe.cache().delete_keys(func_key)

            wrapper.clear_cache = clear_cache
            return wrapper

        return decorator

    def site_cache(ttl=None, maxsize=None):
        """Process-local, per-site cache with an optional TTL (Frappe v14+ backport)."""

        def decorator(func):
            store = {}

            @wraps(func)
            def wrapper(*args, **kwargs):
                site = getattr(frappe.local, "site", None)
                key = (site, _make_call_key("", args, kwargs))
                entry = store.get(key)
                now = time.monotonic()
                if entry is not None and (ttl is None or now - entry[0] < ttl):
                    return entry[1]

                result = func(*args, **kwargs)
                if maxsize and len(store) >= maxsize:
                    store.clear()
                store[key] = (now, result)
                return result

            wrapper.clear_cache = store.clear
            return wrapper

        return decorator


# ---------------------------------------------------------------------------
# Request JSON body (Werkzeug 0.16, used by Frappe v13, has no JSON mixin)
# ---------------------------------------------------------------------------


def get_request_json(request=None, force=False, silent=False):
    """Parse the JSON body of a request, like Werkzeug's ``Request.get_json``.

    Frappe v13 ships Werkzeug 0.16 whose ``Request`` has no ``get_json`` /
    ``json`` attribute, so the body is parsed manually there.

    Args:
        request: Werkzeug request; defaults to ``frappe.request``.
        force: Parse even if the mimetype is not JSON.
        silent: Return None instead of raising on invalid JSON.
    """
    request = request if request is not None else frappe.request

    if hasattr(request, "get_json"):
        return request.get_json(force=force, silent=silent)

    mimetype = (request.mimetype or "").lower()
    if not force and not (mimetype == "application/json" or mimetype.endswith("+json")):
        return None

    try:
        return json.loads(request.get_data(cache=True, as_text=True))
    except ValueError:
        if silent:
            return None
        raise


# ---------------------------------------------------------------------------
# Database savepoints (release_savepoint / rollback_savepoint added in v14)
# ---------------------------------------------------------------------------


def release_savepoint(save_point):
    if hasattr(frappe.db, "release_savepoint"):
        return frappe.db.release_savepoint(save_point)
    frappe.db.sql(f"release savepoint {save_point}")


def rollback_savepoint(save_point):
    if hasattr(frappe.db, "rollback_savepoint"):
        return frappe.db.rollback_savepoint(save_point)
    frappe.db.rollback(save_point=save_point)


# ---------------------------------------------------------------------------
# Prepared reports (helpers were reworked in v15)
# ---------------------------------------------------------------------------


def get_completed_prepared_report(filters, user, report_name):
    """Return the name of a completed Prepared Report matching the filters, if any."""
    try:
        from frappe.core.doctype.prepared_report.prepared_report import (
            get_completed_prepared_report as native,
        )

        return native(filters=filters, user=user, report_name=report_name)
    except ImportError:
        if isinstance(filters, str):
            filters = json.loads(filters)
        # v13 stores filters as json.dumps(filters) (see background_enqueue_run)
        names = frappe.get_all(
            "Prepared Report",
            filters={
                "status": "Completed",
                "filters": json.dumps(filters or {}),
                "owner": user,
                "report_name": report_name,
            },
            order_by="creation desc",
            limit=1,
            pluck="name",
        )
        return names[0] if names else None


def make_prepared_report(report_name, filters=None):
    """Queue a Prepared Report and return ``{"name": <prepared report name>}``."""
    try:
        from frappe.core.doctype.prepared_report.prepared_report import make_prepared_report as native

        return native(report_name=report_name, filters=filters)
    except ImportError:
        from frappe.desk.query_report import background_enqueue_run

        if not isinstance(filters, str):
            filters = json.dumps(filters or {})
        result = background_enqueue_run(report_name, filters=filters, user=frappe.session.user)
        return {"name": result.get("name")}


def get_report_timeout(report_name, default=120):
    """Report.timeout only exists on v15+; fall back to ``default`` elsewhere."""
    if frappe.get_meta("Report").has_field("timeout"):
        return frappe.db.get_value("Report", report_name, "timeout") or default
    return default


# ---------------------------------------------------------------------------
# Link search (v13 writes results into frappe.response instead of returning)
# ---------------------------------------------------------------------------


def search_link(doctype, txt, filters=None, page_length=20):
    from frappe.desk.search import search_link as native

    results = native(doctype=doctype, txt=txt, filters=filters, page_length=page_length)
    if results is None:
        results = frappe.response.pop("results", None) or []
    return results


# ---------------------------------------------------------------------------
# Workflow actions (role-based permitted roles table added in v14)
# ---------------------------------------------------------------------------


def has_workflow_action_permitted_roles():
    return bool(frappe.db.exists("DocType", "Workflow Action Permitted Role"))
