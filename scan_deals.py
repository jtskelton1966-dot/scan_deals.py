VR Deal Scanner — checks Google Flights prices (via SerpApi) for a set of
routes and reports anything Google itself flags as "low" for that route/date,
or that beats your manual price target.

Requires one secret (set as a GitHub Actions secret, or env var locally):
  SERPAPI_KEY

Optional, for email alerts:
  SMTP_USER        e.g. yourname@gmail.com
  SMTP_PASS        an app password (not your normal password)
  ALERT_EMAIL      where to send alerts (can be the same as SMTP_USER)

SerpApi's free tier is 250 searches/month. This script uses 1 search per
route per run, so a daily run (3 routes) = ~90/month, well inside the limit.
Docs: https://serpapi.com/google-flights-api
"""

import os
import json
from datetime import date, timedelta

import requests

SERPAPI_URL = "https://serpapi.com/search"

# ---- Routes and manual fallback targets (CAD, one-way) ----
# A route is flagged as a deal if EITHER Google's own price_level says "low"
# OR the lowest price found is at/below your manual target.
ROUTES = [
    {"origin": "YVR", "dest": "LAS", "label": "Las Vegas (from YVR)", "alert_below": 180},
    {"origin": "YVR", "dest": "HNL", "label": "Honolulu (from YVR)",  "alert_below": 280},
    {"origin": "YVR", "dest": "OGG", "label": "Maui (from YVR)",       "alert_below": 350},
    {"origin": "YYJ", "dest": "LAS", "label": "Las Vegas (from YYJ)", "alert_below": 200},
    {"origin": "YYJ", "dest": "HNL", "label": "Honolulu (from YYJ)",  "alert_below": 300},
    {"origin": "YYJ", "dest": "OGG", "label": "Maui (from YYJ)",       "alert_below": 370},
]
# 6 routes x 1 search/day = ~180 SerpApi calls/month, still under the free 250/month limit.

# How far out to search. One representative date per run keeps API usage low;
# feel free to change this to whatever you actually want to fly.
DAYS_AHEAD = 45


def search_route(api_key, origin, dest, outbound_date):
    """Return (lowest_price, price_level, typical_range) for a one-way search."""
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
    resp.raise_for_status()
    data = resp.json()

    flights = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    prices = [f["price"] for f in flights if f.get("price")]
    lowest = min(prices) if prices else None

    insights = data.get("price_insights", {})
    price_level = insights.get("price_level")
    typical_range = insights.get("typical_price_range")

    return lowest, price_level, typical_range


def scan():
    api_key = os.environ["SERPAPI_KEY"]
    target_date = date.today() + timedelta(days=DAYS_AHEAD)
    results = []

    for route in ROUTES:
        lowest, price_level, typical_range = search_route(
            api_key, route["origin"], route["dest"], target_date
        )
        is_deal = (price_level == "low") or (
            lowest is not None and lowest <= route["alert_below"]
        )
        results.append({
            **route,
            "date_checked": target_date.isoformat(),
            "lowest_price": lowest,
            "price_level": price_level,
            "typical_range": typical_range,
            "is_deal": is_deal,
        })

    return results


def format_report(results):
    lines = ["YVR Deal Scanner — results\n"]
    any_deal = False
    for r in results:
        if r["lowest_price"] is None:
            lines.append(f"{r['label']} ({r['origin']}-{r['dest']}): no fares found for {r['date_checked']}")
            continue
        if r["is_deal"]:
            any_deal = True
        flag = "DEAL" if r["is_deal"] else "—"
        typical = f", typical CAD {r['typical_range'][0]}-{r['typical_range'][1]}" if r["typical_range"] else ""
        lines.append(
            f"{r['label']} ({r['origin']}-{r['dest']}): CAD {r['lowest_price']:.0f} "
            f"on {r['date_checked']}  [{flag}, Google says '{r['price_level']}'{typical}]"
        )
    return "\n".join(lines), any_deal


def send_email(subject, body):
    """Send the report via Resend's free email API (no app password needed)."""
    api_key = os.environ.get("RESEND_API_KEY")
    alert_email = os.environ.get("ALERT_EMAIL")

    if not api_key or not alert_email:
        print("RESEND_API_KEY / ALERT_EMAIL not set — skipping email, printing report instead.")
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

    with open("last_scan.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    if any_deal:
        send_email("✈️ YVR deal alert", report)
    else:
        print("No deals this run — no email sent.")


if __name__ == "__main__":
    main()
