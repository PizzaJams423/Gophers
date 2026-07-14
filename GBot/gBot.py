#!/usr/bin/env python3
"""Gophers Handyman Service — estimate bot backend.

Computes ballpark estimates per the locked pricing rules and logs each
completed intake to a local SQLite database (the reliable source of truth,
especially once published — external-tool connector calls only work in the
dev preview sandbox). It also best-effort logs to Google Sheets via the
external-tool CLI; that call is wrapped so its failure never blocks the
response. All Sheets calls happen server-side only.
"""
import asyncio
import json
import math
import os
import sqlite3
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# SQLite — the reliable, always-on lead store (source of truth post-publish)
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        client_name TEXT,
        phone TEXT,
        email TEXT,
        service_type TEXT,
        quantity TEXT,
        unit TEXT,
        tearout TEXT,
        access_notes TEXT,
        miles_outside_city TEXT,
        materials_pickup TEXT,
        timeline TEXT,
        estimate_low TEXT,
        estimate_high TEXT,
        notes TEXT,
        sheets_logged INTEGER DEFAULT 0
    )
    """
)
db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    db.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def allow_framing(request: Request, call_next):
    """Ensure nothing in this app blocks being embedded in a cross-origin
    iframe (the floating chat widget on third-party sites relies on this).
    We never set X-Frame-Options and never set a restrictive
    Content-Security-Policy frame-ancestors directive; explicitly strip them
    here in case any middleware/framework default ever adds one.
    """
    response = await call_next(request)
    if "X-Frame-Options" in response.headers:
        del response.headers["X-Frame-Options"]
    csp = response.headers.get("Content-Security-Policy")
    if csp and "frame-ancestors" in csp:
        parts = [p for p in csp.split(";") if "frame-ancestors" not in p]
        if parts:
            response.headers["Content-Security-Policy"] = ";".join(parts).strip()
        else:
            del response.headers["Content-Security-Policy"]
    return response

SPREADSHEET_TITLE = "Gophers Handyman Leads"
SHEET_NAME = "Leads"
HEADERS = [
    "Timestamp", "Client Name", "Phone", "Email", "Service Type", "Quantity",
    "Unit", "Tear-out", "Access Notes", "Miles Outside City Limits",
    "Materials Pickup", "Timeline", "Estimate Low", "Estimate High", "Notes",
]

# ---------------------------------------------------------------------------
# Pricing spec (see gophers_estimate_bot_rules.md — locked rate table)
# ---------------------------------------------------------------------------

MIN_CHARGE = 50.0

# service key -> config
SERVICES = {
    "lvp": {"label": "LVP flooring install", "kind": "area", "unit": "sq ft", "rate": 2.00},
    "painting": {"label": "Painting (walls)", "kind": "area", "unit": "sq ft", "rate": 1.50},
    "baseboard": {"label": "Baseboard/trim install", "kind": "linear", "unit": "linear ft", "rate": 2.00},
    "tile_install": {"label": "Tile installation (stock/standard)", "kind": "area", "unit": "sq ft", "rate": 3.50},
    "tile_removal": {"label": "Tile removal (old tile + mortar bed)", "kind": "area", "unit": "sq ft", "rate": 2.50},
    "subfloor": {"label": "Subfloor repair/replacement", "kind": "area", "unit": "sq ft", "rate": 4.00},
    "drywall": {"label": "Drywall install/repair (Level 4 finish)", "kind": "area", "unit": "sq ft", "rate": 2.00},
    "countertop_simple": {"label": "Countertop replacement (simple)", "kind": "flat", "unit": "job", "rate": 100.00},
    "door_painting": {"label": "Door painting (front & back)", "kind": "count", "unit": "door", "rate": 40.00},
    "switch_outlet": {"label": "Switch or outlet install/replacement", "kind": "count", "unit": "unit", "rate": 25.00},
    "light_fixture": {"label": "Light fixture install", "kind": "count", "unit": "unit", "rate": 40.00},
    "ceiling_fan": {"label": "Ceiling fan install", "kind": "count", "unit": "unit", "rate": 60.00},
    # Adjustable placeholder rate — labeled clearly in the UI as adjustable.
    "electrical_troubleshoot": {"label": "Electrical troubleshooting/diagnosis", "kind": "hourly", "unit": "hour", "rate": 70.00},
    "general_repair": {"label": "General small repairs (hourly)", "kind": "hourly", "unit": "hour", "rate": 60.00},
}

# Tear-out / removal add-on: charged as tile removal rate per sq ft when the
# job is an area job needing old material removed and isn't already a
# removal service itself. Simple, transparent per-sq-ft add-on.
TEAROUT_RATE_PER_SQFT = 2.50

# Modifiers (defaults reused from the baseline reference form / rules doc)
FURNITURE_MOVING_FEE = 75.0       # room not cleared — flat fee (reference form used $75 "occupied" fee)
STAIRS_LABOR_PCT = 0.12           # +12% to labor for stairs/multi-level access (mid of 10-15% suggested range)
RUSH_FEE = 50.0                   # ASAP/rush job — flat fee (reference form default)
TRAVEL_RATE_PER_MILE = 1.00       # confirmed
MATERIALS_PICKUP_PCT = 0.25       # 25% of material cost
MATERIALS_PICKUP_CAP = 100.0      # capped at $100
DEFAULT_MATERIAL_ESTIMATE_PCT = 0.35  # if material cost unknown, estimate materials as ~35% of labor subtotal

RANGE_LOW_MULT = 0.90
RANGE_HIGH_MULT = 1.15


class EstimateRequest(BaseModel):
    service: str
    quantity: float | None = None
    hours: float | None = None
    tearout: bool = False
    furniture_in_way: bool = False
    stairs_access: bool = False
    materials_pickup: bool = False
    material_cost_estimate: float | None = None
    miles_outside_city: float = 0
    timeline: str = "flexible"  # asap | two_weeks | flexible
    access_notes: str = ""
    name: str = ""
    phone: str = ""
    email: str = ""


def compute_estimate(req: EstimateRequest) -> dict:
    svc = SERVICES.get(req.service)
    if not svc:
        raise ValueError(f"Unknown service: {req.service}")

    qty = req.quantity or 0
    hours = req.hours or 0
    notes = []
    labor = 0.0
    quantity_display = None
    unit_display = svc["unit"]

    if svc["kind"] in ("area", "linear"):
        labor = svc["rate"] * qty
        quantity_display = qty
    elif svc["kind"] == "count":
        labor = svc["rate"] * qty
        quantity_display = qty
    elif svc["kind"] == "flat":
        labor = svc["rate"]
        quantity_display = 1
    elif svc["kind"] == "hourly":
        labor = svc["rate"] * hours
        quantity_display = hours
        unit_display = "hour(s)"

    addons = 0.0

    # Tear-out / removal fee — applies to area-based install jobs where old
    # material needs to come out first (skip for services that ARE removal,
    # or flat/count/hourly services where tear-out doesn't apply the same way).
    if req.tearout and svc["kind"] in ("area", "linear") and req.service not in ("tile_removal", "subfloor"):
        tearout_fee = TEAROUT_RATE_PER_SQFT * qty
        addons += tearout_fee
        notes.append(f"Tear-out/removal of old material: +${tearout_fee:,.2f}")

    if req.furniture_in_way:
        addons += FURNITURE_MOVING_FEE
        notes.append(f"Furniture/appliance moving (room not cleared): +${FURNITURE_MOVING_FEE:,.2f}")

    if req.stairs_access:
        stairs_fee = labor * STAIRS_LABOR_PCT
        addons += stairs_fee
        notes.append(f"Stairs / multi-level access (+{int(STAIRS_LABOR_PCT*100)}% labor): +${stairs_fee:,.2f}")

    if req.timeline == "asap":
        addons += RUSH_FEE
        notes.append(f"Rush/ASAP scheduling: +${RUSH_FEE:,.2f}")

    travel_fee = 0.0
    if req.miles_outside_city and req.miles_outside_city > 0:
        travel_fee = TRAVEL_RATE_PER_MILE * req.miles_outside_city
        addons += travel_fee
        notes.append(f"Travel outside Chattanooga city limits ({req.miles_outside_city} mi): +${travel_fee:,.2f}")

    materials_pickup_fee = 0.0
    if req.materials_pickup:
        material_cost = req.material_cost_estimate
        if material_cost is None or material_cost <= 0:
            # No material cost known — apply to a reasonable estimate based on labor,
            # rather than skipping the fee silently.
            material_cost = max(labor * DEFAULT_MATERIAL_ESTIMATE_PCT, 0)
            if material_cost > 0:
                notes.append("Materials pickup fee estimated (no material cost provided).")
        materials_pickup_fee = min(material_cost * MATERIALS_PICKUP_PCT, MATERIALS_PICKUP_CAP)
        if materials_pickup_fee > 0:
            addons += materials_pickup_fee
            notes.append(f"Materials pickup fee (25% of est. material cost, capped $100): +${materials_pickup_fee:,.2f}")

    subtotal = labor + addons
    final_estimate = max(subtotal, MIN_CHARGE)
    if final_estimate == MIN_CHARGE and subtotal < MIN_CHARGE:
        notes.append(f"$50 minimum job charge applied.")

    low = round(final_estimate * RANGE_LOW_MULT, -1)  # round to nearest 10
    high = round(final_estimate * RANGE_HIGH_MULT, -1)
    low = max(low, 50)
    if high < low:
        high = low

    return {
        "service_label": svc["label"],
        "unit": unit_display,
        "quantity": quantity_display,
        "labor": round(labor, 2),
        "addons": round(addons, 2),
        "subtotal": round(subtotal, 2),
        "final_estimate": round(final_estimate, 2),
        "low": int(low),
        "high": int(high),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# External tool (Google Sheets) helper — server-side only
# ---------------------------------------------------------------------------

async def call_tool(source_id: str, tool_name: str, arguments: dict) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "external-tool", "call", json.dumps({
            "source_id": source_id, "tool_name": tool_name, "arguments": arguments,
        }),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode() or stdout.decode())
    text = stdout.decode().strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


_sheet_id_cache: dict[str, str] = {}


async def get_or_create_spreadsheet() -> str:
    if "id" in _sheet_id_cache:
        return _sheet_id_cache["id"]

    result = await call_tool(
        "google_sheets__pipedream", "google_sheets-list-spreadsheets",
        {"query": SPREADSHEET_TITLE},
    )
    matches = result if isinstance(result, list) else result.get("data", [])
    for item in matches or []:
        title = item.get("name") or item.get("title") or ""
        if title == SPREADSHEET_TITLE:
            sheet_id = item.get("id") or item.get("spreadsheetId")
            if sheet_id:
                _sheet_id_cache["id"] = sheet_id
                return sheet_id

    created = await call_tool(
        "google_sheets__pipedream", "google_sheets-new-spreadsheet",
        {"title": SPREADSHEET_TITLE, "sheetName": SHEET_NAME, "headers": HEADERS},
    )
    sheet_id = created.get("spreadsheetId") or created.get("id")
    _sheet_id_cache["id"] = sheet_id
    return sheet_id


async def log_lead_to_sheets(row: dict) -> dict:
    """Append a lead row to the Gophers Handyman Leads spreadsheet.
    Never raises — returns a status dict instead so the caller can decide
    whether to surface a warning, without ever blocking the user-facing flow.
    """
    try:
        sheet_id = await get_or_create_spreadsheet()
        result = await call_tool(
            "google_sheets__pipedream", "google_sheets-add-rows",
            {
                "spreadsheetId": sheet_id,
                "sheetName": SHEET_NAME,
                "rows": json.dumps([row]),
                "hasHeaders": True,
            },
        )
        return {"ok": True, "result": result, "spreadsheetId": sheet_id}
    except Exception as exc:  # noqa: BLE001
        print("[sheets] failed to log lead:", exc)
        traceback.print_exc()
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/estimate")
def estimate(req: EstimateRequest):
    try:
        return {"ok": True, "estimate": compute_estimate(req)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


class SubmitRequest(EstimateRequest):
    pass


@app.post("/api/submit")
async def submit(req: SubmitRequest):
    try:
        result = compute_estimate(req)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    timeline_label = {"asap": "ASAP / rush", "two_weeks": "Within 2 weeks", "flexible": "Flexible"}.get(
        req.timeline, req.timeline
    )

    row = {
        "Timestamp": datetime.now(timezone.utc).isoformat(),
        "Client Name": req.name,
        "Phone": req.phone,
        "Email": req.email,
        "Service Type": result["service_label"],
        "Quantity": result["quantity"],
        "Unit": result["unit"],
        "Tear-out": "Yes" if req.tearout else "No",
        "Access Notes": req.access_notes,
        "Miles Outside City Limits": req.miles_outside_city,
        "Materials Pickup": "Yes" if req.materials_pickup else "No",
        "Timeline": timeline_label,
        "Estimate Low": result["low"],
        "Estimate High": result["high"],
        "Notes": " | ".join(result["notes"]),
    }

    # SQLite is the reliable source of truth — write it first, regardless of
    # whether the Sheets call below succeeds.
    save_lead_to_db(row, sheets_logged=False)

    sheets_status = await log_lead_to_sheets(row)

    if sheets_status.get("ok"):
        try:
            db.execute(
                "UPDATE leads SET sheets_logged = 1 WHERE id = (SELECT MAX(id) FROM leads)"
            )
            db.commit()
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "estimate": result,
        "sheets_logged": sheets_status.get("ok", False),
        "sheets_error": sheets_status.get("error"),
    }


# ---------------------------------------------------------------------------
# Admin: view leads stored in SQLite (always available, no auth)
# ---------------------------------------------------------------------------

def save_lead_to_db(row: dict, sheets_logged: bool = False) -> None:
    db.execute(
        """
        INSERT INTO leads (
            timestamp, client_name, phone, email, service_type, quantity, unit,
            tearout, access_notes, miles_outside_city, materials_pickup,
            timeline, estimate_low, estimate_high, notes, sheets_logged
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.get("Timestamp"),
            row.get("Client Name"),
            row.get("Phone"),
            row.get("Email"),
            row.get("Service Type"),
            str(row.get("Quantity")),
            row.get("Unit"),
            row.get("Tear-out"),
            row.get("Access Notes"),
            str(row.get("Miles Outside City Limits")),
            row.get("Materials Pickup"),
            row.get("Timeline"),
            str(row.get("Estimate Low")),
            str(row.get("Estimate High")),
            row.get("Notes"),
            1 if sheets_logged else 0,
        ],
    )
    db.commit()


def fetch_all_leads() -> list[dict]:
    cur = db.execute(
        """
        SELECT id, timestamp, client_name, phone, email, service_type, quantity,
               unit, tearout, access_notes, miles_outside_city, materials_pickup,
               timeline, estimate_low, estimate_high, notes, sheets_logged
        FROM leads ORDER BY id DESC
        """
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.get("/api/leads")
def api_leads():
    return {"ok": True, "leads": fetch_all_leads()}


@app.get("/admin/leads", response_class=HTMLResponse)
def admin_leads():
    leads = fetch_all_leads()
    rows_html = ""
    for lead in leads:
        sheets_badge = (
            '<span class="badge ok">Sheets ✓</span>' if lead["sheets_logged"]
            else '<span class="badge warn">Sheets ✗</span>'
        )
        rows_html += f"""
        <tr>
          <td>{lead['id']}</td>
          <td>{lead['timestamp'] or ''}</td>
          <td>{lead['client_name'] or ''}</td>
          <td>{lead['phone'] or ''}</td>
          <td>{lead['email'] or ''}</td>
          <td>{lead['service_type'] or ''}</td>
          <td>{lead['quantity'] or ''} {lead['unit'] or ''}</td>
          <td>{lead['tearout'] or ''}</td>
          <td>{lead['access_notes'] or ''}</td>
          <td>{lead['miles_outside_city'] or ''}</td>
          <td>{lead['materials_pickup'] or ''}</td>
          <td>{lead['timeline'] or ''}</td>
          <td>${lead['estimate_low'] or ''} – ${lead['estimate_high'] or ''}</td>
          <td>{lead['notes'] or ''}</td>
          <td>{sheets_badge}</td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Gophers Handyman — Leads (Admin)</title>
    <style>
      :root {{ --primary:#01696f; --bg:#f7f6f2; --surface:#ffffff; --border:#d4d1ca; --text:#28251d; --muted:#66645f; }}
      * {{ box-sizing: border-box; }}
      body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 2rem; }}
      h1 {{ font-size:1.4rem; margin:0 0 0.25rem; }}
      p.sub {{ color: var(--muted); margin: 0 0 1.5rem; font-size:0.9rem; }}
      .card {{ background: var(--surface); border:1px solid var(--border); border-radius:12px; overflow:auto; box-shadow: 0 4px 12px rgba(0,0,0,0.06); }}
      table {{ border-collapse: collapse; width:100%; font-size:0.82rem; white-space:nowrap; }}
      th, td {{ text-align:left; padding:0.6rem 0.75rem; border-bottom:1px solid var(--border); }}
      th {{ background: var(--primary); color:#fff; position:sticky; top:0; font-weight:600; }}
      tr:hover td {{ background: #f3f0ec; }}
      .badge {{ display:inline-block; padding:0.15rem 0.5rem; border-radius:999px; font-size:0.72rem; font-weight:700; }}
      .badge.ok {{ background:#e3f0dd; color:#437a22; }}
      .badge.warn {{ background:#f7e3d6; color:#964219; }}
      .empty {{ padding:3rem; text-align:center; color: var(--muted); }}
      .count {{ font-weight:700; color: var(--primary); }}
    </style>
    </head>
    <body>
      <h1>Gophers Handyman Service — Submitted Leads</h1>
      <p class="sub">Read-only admin view, sourced directly from <code>data.db</code> (SQLite) on the server — always accurate regardless of Google Sheets status. <span class="count">{len(leads)}</span> lead(s) total. Also available as JSON at <code>/api/leads</code>.</p>
      <div class="card">
        {'<table><thead><tr><th>ID</th><th>Timestamp</th><th>Name</th><th>Phone</th><th>Email</th><th>Service</th><th>Qty</th><th>Tear-out</th><th>Access notes</th><th>Miles out</th><th>Materials pickup</th><th>Timeline</th><th>Estimate</th><th>Notes</th><th>Sync</th></tr></thead><tbody>' + rows_html + '</tbody></table>' if leads else '<div class="empty">No leads submitted yet.</div>'}
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
