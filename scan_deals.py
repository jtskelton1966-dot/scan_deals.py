"""
YVR/YYJ Deal Scanner - checks Google Flights prices (via SerpApi) across a
grid of upcoming dates for each route, saves the results to data/deals.json
(for the mobile dashboard), and emails a summary if anything qualifies as
a deal.

Routes and price targets are NOT hardcoded here - they're read from a
published Google Sheet CSV, so you can add/remove routes or change price
targets from your phone without touching this file. See ROUTES_SHEET_CSV_URL
below.

Required secrets (GitHub Actions secrets, or env vars locally):
  SERPAPI_KEY       - from serpapi.com

Optional, for email alerts:
  RESEND_API_KEY    - from resend.com
  ALERT_EMAIL       - where alerts go

Free-tier budget: SerpApi's free plan is 250 searches/month. With 6 routes
and SAMPLE_WEEKS dates each, checked once a week:
  6 routes * SAMPLE_WEEKS dates * ~4.33 weeks/month <= 250
SAMPLE_WEEKS = 9 keeps this at roughly 234/month, leaving a small buffer.
Adjust SAMPLE_WEEKS or the routes sheet if you add more routes.
"""

import os
import csv
import json
import io
from datetime import date, timedelta

import requests

SERPAPI_URL = "https://serpapi.com/search"

# Published-to-web CSV link for the routes/settings Google Sheet.
# File > Share > Publish to web > choose the sheet > CSV > Publish, then
# paste the resulting link here. Columns expected: origin,dest,label,alert_below,active
ROUTES_SHEET_CSV_URL = os.environ.get("ROUTES_SHEET_CSV_URL", "")

# Fallback routes used only if the sheet can't be reached.
FALLBACK_ROUTES = [
    {"origin": "YVR", "dest": "LAS", "label": "Las Vegas (from YVR)", "alert_below": 180},
    {"origin": "YVR", "dest": "HNL", "label": "Honolulu (from YVR)", "alert_below": 280},
    {"origin": "YVR", "dest": "OGG", "label": "Maui (from YVR)", "alert_below": 350},
    {"origin": "YYJ", "dest": "LAS", "label": "Las Vegas (from YYJ)", "alert_below": 200},
    {"origin": "YYJ", "dest": "HNL", "label": "Honolulu (from YYJ)", "alert_below": 300},
    {"origin": "YYJ", "dest": "OGG", "label": "Maui (from YYJ)", "alert_below": 370},
]

# How many upcoming weekly dates to sample per route.
SAMPLE_WEEKS = 9

DATA_OUTPUT_PATH = "data/deals.json"


def load_routes():
    if not ROUTES_SHEET_CSV_URL:
        print("No ROUTES_SHEET_CSV_URL set - using fallback route list.")
        return FALLBACK_ROUTES

    try:
        resp = requests.get(ROUTES_SHEET_CSV_URL, timeout=20)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        routes = []
        for row in reader:
            active = (row.get("active") or "yes").strip().lower()
            if active not in ("yes", "true", "1", ""):
                continue
            routes.append({
                "origin": row["origin"].strip().upper(),
                "dest": row["dest"].strip().upper(),
                "label": row.get("label", "").strip() or f"{row['origin']}-{row['dest']}",
                "alert_below": float(row.get("alert_below") or 99999),
            })
        if routes:
            return routes
        print("Sheet returned no active rows - using fallback route list.")
        return FALLBACK_ROUTES
    except Exception as e:
        print(f"Could not read routes sheet ({e}) - using fallback route list.")
        return FALLBACK_ROUTES


def sample_dates():
    today = date.today()
    return [today + timedelta(weeks=w) for w in range(1, SAMPLE_WEEKS + 1)]


def search_route(api_key, origin, dest, outbound_date):
    """Return (lowest_price, price_level, airline) for a one-way search on this date."""
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": dest,
        "outbound_date": outbound_date.isoformat(),
        "type": "2",  # one-way
        "currency": "CAD",
        "hl": "en",
        "api_key": api_key,
    }
    resp = requests.get(SERPAPI_URL, params=params, timeout=30)
    if resp.status_code != 200:
        return None, None, None

    data = resp.json()
    flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    priced = [f for f in flights if f.get("price")]
    if not priced:
        return None, None, None

    cheapest = min(priced, key=lambda f: f["price"])
    price = cheapest["price"]
    # Each offer can have multiple legs (flights); use the first leg's airline
    # as the operating carrier name shown to the user.
    legs = cheapest.get("flights") or []
    airline = legs[0].get("airline") if legs else None

    price_level = (data.get("price_insights") or {}).get("price_level")
    return price, price_level, airline


def scan():
    api_key = os.environ["SERPAPI_KEY"]
    routes = load_routes()
    dates = sample_dates()
    results = []

    for route in routes:
        date_entries = []
        for d in dates:
            price, price_level, airline = search_route(api_key, route["origin"], route["dest"], d)
            is_deal = (price_level == "low") or (
                price is not None and price <= route["alert_below"]
            )
            date_entries.append({
                "date": d.isoformat(),
                "price": price,
                "price_level": price_level,
                "airline": airline,
                "is_deal": is_deal,
            })
        results.append({**route, "dates": date_entries})

    return results


def format_report(results):
    lines = ["YVR/YYJ Deal Scanner - weekly results\n"]
    any_deal = False
    for r in results:
        deals = [d for d in r["dates"] if d["is_deal"] and d["price"] is not None]
        if deals:
            any_deal = True
            best = min(deals, key=lambda d: d["price"])
            airline_text = f" on {best['airline']}" if best.get("airline") else ""
            lines.append(
                f"{r['label']} ({r['origin']}-{r['dest']}): DEAL - CAD {best['price']:.0f}{airline_text} "
                f"on {best['date']} (target <= {r['alert_below']})"
            )
        else:
            priced = [d for d in r["dates"] if d["price"] is not None]
            if priced:
                cheapest = min(priced, key=lambda d: d["price"])
                airline_text = f" on {cheapest['airline']}" if cheapest.get("airline") else ""
                lines.append(
                    f"{r['label']} ({r['origin']}-{r['dest']}): no deal - "
                    f"cheapest sampled CAD {cheapest['price']:.0f}{airline_text} on {cheapest['date']}"
                )
            else:
                lines.append(f"{r['label']} ({r['origin']}-{r['dest']}): no fares found")
    return "\n".join(lines), any_deal


def send_email(subject, body):
    api_key = os.environ.get("RESEND_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL")

    if not api_key or not alert_email:
        print("RESEND_API_KEY / ALERT_EMAIL not set - skipping email, printing report instead.")
        return

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": "YVR Deal Scanner <onboarding@resend.dev>",
            "to": [alert_email],
            "subject": subject,
            "text": body,
        },
        timeout=20,
    )
    if resp.status_code >= 300:
        print(f"Email send failed ({resp.status_code}): {resp.text}")
    else:
        print(f"Email sent to {alert_email}")


def main():
    results = scan()
    report, any_deal = format_report(results)
    print(report)

    os.makedirs(os.path.dirname(DATA_OUTPUT_PATH), exist_ok=True)
    with open(DATA_OUTPUT_PATH, "w") as f:
        json.dump({
            "generated_at": date.today().isoformat(),
            "routes": results,
        }, f, indent=2, default=str)

    if any_deal:
        send_email("YVR/YYJ deal alert", report)
    else:
        print("No deals this run - no email sent.")


if __name__ == "__main__":
    main()
