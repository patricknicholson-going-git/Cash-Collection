"""
HubSpot enrichment for the cash collection app.

Fetches per-company: MRR (amount field), live status (joincurrent_plan),
AM name and email (hubspot_owner_id → owner lookup).

Results are cached for 1 hour to avoid repeated API calls.
"""

from __future__ import annotations

import requests
import streamlit as st

BASE_URL = "https://api.hubapi.com"


def _headers():
    key = st.secrets.get("hubspot", {}).get("api_key", "")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


@st.cache_data(ttl=3600)
def fetch_owner_map() -> dict:
    """Returns {owner_id: {name, email}} for all HubSpot owners."""
    try:
        r = requests.get(f"{BASE_URL}/crm/v3/owners", headers=_headers(), timeout=10)
        if not r.ok:
            return {}
        out = {}
        for o in r.json().get("results", []):
            name = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip() or o.get("email", "")
            out[str(o["id"])] = {"name": name, "email": o.get("email", "")}
        return out
    except Exception:
        return {}


_HS_PROPS = [
    "name",
    "current_mrr",
    "last_realized_churn_date",
    "renewal_calendar_date",
    "hubspot_owner_id",
    "plan_tier",
]


@st.cache_data(ttl=3600)
def search_company_hs(name: str) -> dict | None:
    """Search HubSpot for a company by name. Returns enrichment dict or None."""
    if not name or not name.strip():
        return None
    try:
        r = requests.post(
            f"{BASE_URL}/crm/v3/objects/companies/search",
            headers=_headers(),
            json={"query": name, "limit": 1, "properties": _HS_PROPS},
            timeout=10,
        )
        if not r.ok:
            return None
        results = r.json().get("results", [])
        if not results:
            return None
        props = results[0]["properties"]
        mrr = float(props.get("current_mrr") or 0)
        churn_raw = props.get("last_realized_churn_date") or ""
        churn_date = churn_raw[:10] if churn_raw else None  # "YYYY-MM-DD" or None
        return {
            "hubspot_id":  results[0]["id"],
            "mrr":         mrr,
            "is_live":     mrr > 0,
            "churn_date":  churn_date,
            "owner_id":    str(props.get("hubspot_owner_id") or ""),
            "plan_tier":   props.get("plan_tier") or "",
        }
    except Exception:
        return None


@st.cache_data(ttl=3600)
def enrich_companies(company_names: tuple) -> dict:
    """
    Bulk enrich a tuple of company names from HubSpot.
    Returns {company_name: {mrr, is_live, churn_date, am_name, am_email, plan_tier}}.
    """
    owner_map = fetch_owner_map()
    result = {}
    for name in company_names:
        if not name:
            continue
        data = search_company_hs(name)
        if data:
            owner = owner_map.get(data["owner_id"], {})
            result[name] = {
                "mrr":       data["mrr"],
                "is_live":   data["is_live"],
                "churn_date": data["churn_date"],
                "am_name":   owner.get("name", ""),
                "am_email":  owner.get("email", ""),
                "plan_tier": data["plan_tier"],
            }
        else:
            result[name] = {
                "mrr": 0.0, "is_live": False, "churn_date": None,
                "am_name": "", "am_email": "", "plan_tier": "",
            }
    return result
