import os
import json
import time
import sqlite3
import smtplib
import logging
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG = {
    "min_price": 380000,
    "max_price": 620000,
    "min_bedrooms": 3,
    "min_land_sqm": 400,
    "min_yield_pct": 5.5,
    "property_type": "house",
    "listing_type": "established",
    "alert_email": os.environ["ALERT_EMAIL"],
    "suburbs": {
        "Kirwan QLD 4817":              560,
        "Annandale QLD 4814":           580,
        "Rasmussen QLD 4815":           540,
        "Thuringowa Central QLD 4817":  550,
        "Mount Louisa QLD 4814":        560,
        "Geraldton WA 6530":            490,
        "Rangeway WA 6530":             470,
    }
}

# Suburb → (state, postcode) for API calls
SUBURB_MAP = {
    "Kirwan QLD 4817":              ("Kirwan",              "QLD", "4817"),
    "Annandale QLD 4814":           ("Annandale",           "QLD", "4814"),
    "Rasmussen QLD 4815":           ("Rasmussen",           "QLD", "4815"),
    "Thuringowa Central QLD 4817":  ("Thuringowa Central",  "QLD", "4817"),
    "Mount Louisa QLD 4814":        ("Mount Louisa",        "QLD", "4814"),
    "Geraldton WA 6530":            ("Geraldton",           "WA",  "6530"),
    "Rangeway WA 6530":             ("Rangeway",            "WA",  "6530"),
}

DB_PATH = "seen_listings.db"
DOMAIN_AUTH_URL = "https://auth.domain.com.au/v1/connect/token"
DOMAIN_API_URL  = "https://api.domain.com.au/v1/listings/residential/_search"


# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            listing_id TEXT PRIMARY KEY,
            address    TEXT,
            seen_at    TEXT
        )
    """)
    conn.commit()
    conn.close()

def is_seen(listing_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM seen WHERE listing_id=?", (listing_id,)).fetchone()
    conn.close()
    return row is not None

def mark_seen(listing_id: str, address: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO seen VALUES (?,?,?)",
        (listing_id, address, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


# ── Domain auth ───────────────────────────────────────────────────────────────
def get_token() -> str:
    client_id     = os.environ["DOMAIN_CLIENT_ID"]
    client_secret = os.environ["DOMAIN_CLIENT_SECRET"]
    r = requests.post(DOMAIN_AUTH_URL, data={
        "grant_type":    "client_credentials",
        "scope":         "api_listings_read",
        "client_id":     client_id,
        "client_secret": client_secret,
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]


# ── Domain search ─────────────────────────────────────────────────────────────
def search_suburb(token: str, suburb: str, state: str, postcode: str) -> list:
    payload = {
        "listingType": "Sale",
        "propertyTypes": ["House"],
        "minBedrooms": CONFIG["min_bedrooms"],
        "minPrice": CONFIG["min_price"],
        "maxPrice": CONFIG["max_price"],
        "locations": [{"state": state, "suburb": suburb, "postCode": postcode}],
        "pageSize": 50,
        "sort": {"sortKey": "dateListed", "direction": "Descending"},
    }
    if CONFIG["listing_type"] == "established":
        payload["newDevOnly"] = False

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(DOMAIN_API_URL, json=payload, headers=headers, timeout=20)
    if r.status_code != 200:
        log.warning(f"API error {r.status_code} for {suburb}: {r.text[:200]}")
        return []
    return r.json()


# ── Yield calc ────────────────────────────────────────────────────────────────
def gross_yield(weekly_rent: int, price: int) -> float:
    if price <= 0:
        return 0.0
    return round((weekly_rent * 52) / price * 100, 2)

def extract_price(listing: dict) -> int:
    pd = listing.get("pricingDetails", {})
    for key in ("price", "displayPrice"):
        val = pd.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    # Try to parse "Offers Over $490,000" style strings
    raw = str(pd.get("displayPrice", ""))
    import re
    nums = re.findall(r"\d[\d,]*", raw)
    if nums:
        return int(nums[0].replace(",", ""))
    return 0

def extract_land(listing: dict) -> int:
    return int(listing.get("landAreaSqm") or listing.get("buildingAreaSqm") or 0)


# ── Filter & score ────────────────────────────────────────────────────────────
def evaluate(listing: dict, suburb_key: str) -> dict | None:
    weekly_rent = CONFIG["suburbs"][suburb_key]
    lid = str(listing.get("id", ""))
    if not lid or is_seen(lid):
        return None

    price = extract_price(listing)
    if price < CONFIG["min_price"] or price > CONFIG["max_price"] or price == 0:
        return None

    beds = listing.get("bedrooms", 0) or 0
    if beds < CONFIG["min_bedrooms"]:
        return None

    land = extract_land(listing)
    if land > 0 and land < CONFIG["min_land_sqm"]:
        return None

    y = gross_yield(weekly_rent, price)
    if y < CONFIG["min_yield_pct"]:
        return None

    addr = listing.get("propertyDetails", {}).get("displayableAddress", "Unknown address")
    url  = f"https://www.domain.com.au/{listing.get('listingSlug', '')}"

    return {
        "id":        lid,
        "address":   addr,
        "price":     price,
        "beds":      beds,
        "land":      land,
        "yield_pct": y,
        "rent_wk":   weekly_rent,
        "suburb":    suburb_key,
        "url":       url,
        "listed":    listing.get("dateFirstListed", "")[:10],
    }


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(matches: list):
    sender   = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    to       = CONFIG["alert_email"]

    subject = f"[Property Bot] {len(matches)} new listing{'s' if len(matches)!=1 else ''} — {datetime.now().strftime('%d %b %Y')}"

    rows = ""
    for m in matches:
        be55 = int((m["rent_wk"] * 52) / 0.055)
        be60 = int((m["rent_wk"] * 52) / 0.060)
        rows += f"""
        <tr>
          <td style="padding:12px 8px;border-bottom:1px solid #f0f0f0">
            <strong style="font-size:14px">{m['address']}</strong><br>
            <span style="color:#666;font-size:12px">{m['suburb']} · Listed {m['listed']}</span>
          </td>
          <td style="padding:12px 8px;border-bottom:1px solid #f0f0f0;text-align:right">
            <strong>${m['price']:,}</strong>
          </td>
          <td style="padding:12px 8px;border-bottom:1px solid #f0f0f0;text-align:center">
            {m['beds']}bd {('· '+str(m['land'])+'m²') if m['land'] else ''}
          </td>
          <td style="padding:12px 8px;border-bottom:1px solid #f0f0f0;text-align:center">
            <span style="background:#e6f4ea;color:#137333;padding:3px 8px;border-radius:12px;font-weight:600;font-size:13px">{m['yield_pct']}%</span><br>
            <span style="color:#888;font-size:11px">${m['rent_wk']}/wk</span>
          </td>
          <td style="padding:12px 8px;border-bottom:1px solid #f0f0f0;text-align:right;font-size:11px;color:#666">
            5.5%≤${ be55//1000}k<br>6.0%≤${be60//1000}k
          </td>
          <td style="padding:12px 8px;border-bottom:1px solid #f0f0f0">
            <a href="{m['url']}" style="background:#1a1a1a;color:#fff;padding:6px 12px;border-radius:6px;text-decoration:none;font-size:12px">View →</a>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:-apple-system,sans-serif;max-width:800px;margin:0 auto;padding:24px;color:#1a1a1a">
      <p style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#888;margin-bottom:4px">Property alert bot</p>
      <h1 style="font-size:22px;font-weight:500;margin:0 0 4px">{len(matches)} new listing{'s' if len(matches)!=1 else ''} found</h1>
      <p style="color:#666;font-size:13px;margin:0 0 24px">Min yield {CONFIG['min_yield_pct']}% · ${CONFIG['min_price']//1000}k–${CONFIG['max_price']//1000}k · {datetime.now().strftime('%d %b %Y %H:%M')}</p>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead>
          <tr style="border-bottom:2px solid #1a1a1a">
            <th style="text-align:left;padding:8px;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Address</th>
            <th style="text-align:right;padding:8px;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Price</th>
            <th style="text-align:center;padding:8px;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Details</th>
            <th style="text-align:center;padding:8px;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Yield</th>
            <th style="text-align:right;padding:8px;font-size:11px;text-transform:uppercase;letter-spacing:.05em">Break-even</th>
            <th style="padding:8px"></th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#aaa;font-size:11px;margin-top:24px">Rent benchmarks are suburb medians — get a written PM appraisal before making any offer.</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, password)
        s.sendmail(sender, to, msg.as_string())
    log.info(f"Email sent → {to} ({len(matches)} listings)")


# ── Main run ──────────────────────────────────────────────────────────────────
def run():
    log.info("── Property bot run started ──")
    init_db()
    token   = get_token()
    matches = []

    for suburb_key, (name, state, postcode) in SUBURB_MAP.items():
        log.info(f"Searching {name} {state} {postcode}…")
        try:
            results = search_suburb(token, name, state, postcode)
            for listing in results:
                m = evaluate(listing, suburb_key)
                if m:
                    matches.append(m)
                    log.info(f"  MATCH: {m['address']} ${m['price']:,} {m['yield_pct']}%")
        except Exception as e:
            log.error(f"Error searching {suburb_key}: {e}")
        time.sleep(1)  # polite delay between suburb calls

    if matches:
        send_email(matches)
        for m in matches:
            mark_seen(m["id"], m["address"])
    else:
        log.info("No new matches found.")

    log.info(f"── Run complete. {len(matches)} match(es) ──")


if __name__ == "__main__":
    run()
