#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
SPREADSHEET_TITLE = "Gophers Handyman Leads"
SHEET_NAME = "Leads"
HEADERS = [
    "Timestamp", "Client Name", "Phone", "Email", "Service Type", "Quantity", "Unit",
    "Tear-out", "Access Notes", "Miles Outside City Limits", "Materials Pickup",
    "Timeline", "Estimate Low", "Estimate High", "Notes",
]
MIN_CHARGE = 50.0
TEAROUT_RATE_PER_SQFT = 2.50
FURNITURE_MOVING_FEE = 75.0
STAIRS_LABOR_PCT = 0.12
RUSH_FEE = 50.0
TRAVEL_RATE_PER_MILE = 1.00
MATERIALS_PICKUP_PCT = 0.25
MATERIALS_PICKUP_CAP = 100.0
DEFAULT_MATERIAL_ESTIMATE_PCT = 0.35
RANGE_LOW_MULT = 0.90
RANGE_HIGH_MULT = 1.15

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
    "electrical_troubleshoot": {"label": "Electrical troubleshooting/diagnosis", "kind": "hourly", "unit": "hour", "rate": 70.00},
    "general_repair": {"label": "General small repairs (hourly)", "kind": "hourly", "unit": "hour", "rate": 60.00},
}

SERVICE_ALIASES = {
    "lvp": "lvp",
    "vinyl plank": "lvp",
    "luxury vinyl plank": "lvp",
    "paint": "painting",
    "painting": "painting",
    "baseboard": "baseboard",
    "trim": "baseboard",
    "tile install": "tile_install",
    "tile": "tile_install",
    "tile removal": "tile_removal",
    "subfloor": "subfloor",
    "drywall": "drywall",
    "countertop": "countertop_simple",
    "door painting": "door_painting",
    "switch": "switch_outlet",
    "outlet": "switch_outlet",
    "light fixture": "light_fixture",
    "ceiling fan": "ceiling_fan",
    "electrical": "electrical_troubleshoot",
    "troubleshoot": "electrical_troubleshoot",
    "repair": "general_repair",
}

FOLLOW_UP_QUESTIONS = {
    "service": "What type of work is it? For example: LVP, painting, tile install, drywall, electrical troubleshooting.",
    "quantity": "How many square feet, linear feet, doors, or units is it?",
    "hours": "How many hours do you want to estimate?",
    "name": "What name should I put on the estimate?",
    "phone": "What phone number should I use?",
    "email": "What email should I use?",
}

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
    timeline: str = "flexible"
    access_notes: str = ""
    name: str = ""
    phone: str = ""
    email: str = ""

class SubmitRequest(EstimateRequest):
    pass

chat_sessions: dict[str, dict[str, Any]] = {}

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
            material_cost = max(labor * DEFAULT_MATERIAL_ESTIMATE_PCT, 0)
            notes.append("Materials pickup fee estimated (no material cost provided).")
        materials_pickup_fee = min(material_cost * MATERIALS_PICKUP_PCT, MATERIALS_PICKUP_CAP)
        if materials_pickup_fee > 0:
            addons += materials_pickup_fee
            notes.append(f"Materials pickup fee (25% of est. material cost, capped $100): +${materials_pickup_fee:,.2f}")
    subtotal = labor + addons
    final_estimate = max(subtotal, MIN_CHARGE)
    if final_estimate == MIN_CHARGE and subtotal < MIN_CHARGE:
        notes.append("$50 minimum job charge applied.")
    low = round(final_estimate * RANGE_LOW_MULT, -1)
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

def parse_chat_message(text: str, state: dict | None = None) -> dict:
    state = dict(state or {})
    raw = text.lower()
    for key, svc in SERVICE_ALIASES.items():
        if key in raw:
            state["service"] = svc
            break
    if any(w in raw for w in ["tearout", "tear out", "remove old", "demo"]):
        state["tearout"] = True
    if any(w in raw for w in ["furniture", "occupied", "not cleared"]):
        state["furniture_in_way"] = True
    if any(w in raw for w in ["stairs", "second floor", "multi-level"]):
        state["stairs_access"] = True
    if any(w in raw for w in ["pickup materials", "materials pickup", "buy materials"]):
        state["materials_pickup"] = True
    if "asap" in raw or "rush" in raw:
        state["timeline"] = "asap"
    elif "2 weeks" in raw or "two weeks" in raw:
        state["timeline"] = "two_weeks"
    qty_match = re.search(r'(\d+(?:\.\d+)?)\s*(sq\s*ft|sf|linear\s*ft|lf|feet|ft|hours?|hrs?|door|doors|unit|units)?', raw)
    if qty_match:
        val = float(qty_match.group(1))
        unit = (qty_match.group(2) or '').replace(' ', '')
        if 'hour' in unit or 'hr' in unit:
            state['hours'] = val
        else:
            state['quantity'] = val
    miles_match = re.search(r'(\d+(?:\.\d+)?)\s*miles?\s*(?:outside|out of)?\s*city', raw)
    if miles_match:
        state['miles_outside_city'] = float(miles_match.group(1))
    phone_match = re.search(r'(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}', text)
    if phone_match:
        state['phone'] = phone_match.group(0)
    email_match = re.search(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', raw)
    if email_match:
        state['email'] = email_match.group(0)
    return state

def missing_fields(state: dict) -> list[str]:
    required = []
    if not state.get('service'):
        required.append('service')
    if state.get('service') in ('electrical_troubleshoot', 'general_repair'):
        if not state.get('hours'):
            required.append('hours')
    else:
        if not state.get('quantity'):
            required.append('quantity')
    for field in ('name', 'phone', 'email'):
        if not state.get(field):
            required.append(field)
    return required

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    db.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
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
""")
db.commit()

@app.middleware("http")
async def allow_framing(request: Request, call_next):
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

async def call_tool(source_id: str, tool_name: str, arguments: dict) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "external-tool", "call", json.dumps({"source_id": source_id, "tool_name": tool_name, "arguments": arguments}),
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
    result = await call_tool("google_sheets__pipedream", "google_sheets-list-spreadsheets", {"query": SPREADSHEET_TITLE})
    matches = result if isinstance(result, list) else result.get("data", [])
    for item in matches or []:
        title = item.get("name") or item.get("title") or ""
        if title == SPREADSHEET_TITLE:
            sheet_id = item.get("id") or item.get("spreadsheetId")
            if sheet_id:
                _sheet_id_cache["id"] = sheet_id
                return sheet_id
    created = await call_tool("google_sheets__pipedream", "google_sheets-new-spreadsheet", {"title": SPREADSHEET_TITLE, "sheetName": SHEET_NAME, "headers": HEADERS})
    sheet_id = created.get("spreadsheetId") or created.get("id")
    _sheet_id_cache["id"] = sheet_id
    return sheet_id

async def log_lead_to_sheets(row: dict) -> dict:
    try:
        sheet_id = await get_or_create_spreadsheet()
        result = await call_tool("google_sheets__pipedream", "google_sheets-add-rows", {"spreadsheetId": sheet_id, "sheetName": SHEET_NAME, "rows": json.dumps([row]), "hasHeaders": True})
        return {"ok": True, "result": result, "spreadsheetId": sheet_id}
    except Exception as exc:
        print("[sheets] failed to log lead:", exc)
        traceback.print_exc()
        return {"ok": False, "error": str(exc)}

def save_lead_to_db(row: dict, sheets_logged: bool = False) -> int:
    cur = db.execute("""
    INSERT INTO leads (
        timestamp, client_name, phone, email, service_type, quantity, unit, tearout,
        access_notes, miles_outside_city, materials_pickup, timeline, estimate_low,
        estimate_high, notes, sheets_logged
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        row.get("Timestamp"), row.get("Client Name"), row.get("Phone"), row.get("Email"), row.get("Service Type"), str(row.get("Quantity")), row.get("Unit"), row.get("Tear-out"), row.get("Access Notes"), str(row.get("Miles Outside City Limits")), row.get("Materials Pickup"), row.get("Timeline"), str(row.get("Estimate Low")), str(row.get("Estimate High")), row.get("Notes"), 1 if sheets_logged else 0,
    ])
    db.commit()
    return int(cur.lastrowid)

def fetch_all_leads() -> list[dict]:
    cur = db.execute("""
    SELECT id, timestamp, client_name, phone, email, service_type, quantity, unit, tearout, access_notes,
           miles_outside_city, materials_pickup, timeline, estimate_low, estimate_high, notes, sheets_logged
    FROM leads ORDER BY id DESC
    """)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/estimate")
def estimate(req: EstimateRequest):
    try:
        return {"ok": True, "estimate": compute_estimate(req)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

@app.post("/api/submit")
async def submit(req: SubmitRequest):
    try:
        result = compute_estimate(req)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    timeline_label = {"asap": "ASAP / rush", "two_weeks": "Within 2 weeks", "flexible": "Flexible"}.get(req.timeline, req.timeline)
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
    lead_id = save_lead_to_db(row, sheets_logged=False)
    sheets_status = await log_lead_to_sheets(row)
    if sheets_status.get("ok"):
        try:
            db.execute("UPDATE leads SET sheets_logged = 1 WHERE id = ?", (lead_id,))
            db.commit()
        except Exception:
            pass
    return {"ok": True, "estimate": result, "sheets_logged": sheets_status.get("ok", False), "sheets_error": sheets_status.get("error")}

@app.post("/api/chat")
async def chat(message: dict):
    session_id = str(message.get("session_id") or "default")
    text = str(message.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "Message text is required."}
    state = chat_sessions.get(session_id, {})
    state = parse_chat_message(text, state)
    missing = missing_fields(state)
    chat_sessions[session_id] = state
    if missing:
        field = missing[0]
        return {"ok": True, "status": "need_more_info", "missing": missing, "question": FOLLOW_UP_QUESTIONS[field], "state": state}
    req = SubmitRequest(**state)
    result = compute_estimate(req)
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
        "Timeline": {"asap": "ASAP / rush", "two_weeks": "Within 2 weeks", "flexible": "Flexible"}.get(req.timeline, req.timeline),
        "Estimate Low": result["low"],
        "Estimate High": result["high"],
        "Notes": " | ".join(result["notes"]),
    }
    lead_id = save_lead_to_db(row, sheets_logged=False)
    sheets_status = await log_lead_to_sheets(row)
    if sheets_status.get("ok"):
        try:
            db.execute("UPDATE leads SET sheets_logged = 1 WHERE id = ?", (lead_id,))
            db.commit()
        except Exception:
            pass
    chat_sessions[session_id] = {}
    return {"ok": True, "status": "complete", "estimate": result, "sheets_logged": sheets_status.get("ok", False), "sheets_error": sheets_status.get("error")}

@app.get("/api/leads")
def api_leads():
    return {"ok": True, "leads": fetch_all_leads()}

@app.get("/admin/leads", response_class=HTMLResponse)
def admin_leads():
    leads = fetch_all_leads()
    rows = []
    for lead in leads:
        badge = "Sheets âœ“" if lead["sheets_logged"] else "Sheets âœ—"
        rows.append(f"<tr><td>{lead['id']}</td><td>{lead['timestamp']}</td><td>{lead['client_name'] or ''}</td><td>{lead['phone'] or ''}</td><td>{lead['service_type'] or ''}</td><td>{lead['quantity'] or ''}</td><td>{lead['tearout'] or ''}</td><td>{lead['access_notes'] or ''}</td><td>{lead['miles_outside_city'] or ''}</td><td>{lead['materials_pickup'] or ''}</td><td>{lead['timeline'] or ''}</td><td>{lead['estimate_low'] or ''}-{lead['estimate_high'] or ''}</td><td>{lead['notes'] or ''}</td><td>{badge}</td></tr>")
    html = f"""<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Gophers Handyman Leads Admin</title><style>body{{font-family:Arial,sans-serif;padding:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:8px;font-size:12px;vertical-align:top}}th{{background:#0f766e;color:#fff;position:sticky;top:0}}</style></head><body><h1>Gophers Handyman Service Submitted Leads</h1><p>{len(leads)} lead(s) total.</p><table><thead><tr><th>ID</th><th>Timestamp</th><th>Name</th><th>Phone</th><th>Service</th><th>Qty</th><th>Tear-out</th><th>Access notes</th><th>Miles out</th><th>Materials pickup</th><th>Timeline</th><th>Estimate</th><th>Notes</th><th>Sync</th></tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"""
    return HTMLResponse(content=html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
