"""
Amazon P&L Snapshot -> Slack
-----------------------------
Runs once a day (via GitHub Actions cron). Most days it does nothing.
Every Monday, it:
  1. Gets a fresh Amazon access token using your existing SP-API refresh token
  2. Pulls every financial transaction (sales, fees, refunds, etc.) from
     Amazon's Finances API for the full week (Monday - Sunday) that just
     ended yesterday
  3. Adds up the totals into a simple bottom-line number (no COGS - that's
     tracked manually by Davis outside this tool)
  4. Posts a readable summary to Slack

Why Monday, and why a full Mon-Sun week: Davis reviews things on Saturday
mornings, and weekends are usually the biggest sales days. A report that
runs Saturday morning would always be missing that same weekend's numbers
(they haven't happened yet), which defeats the point of a P&L check. Running
it Monday morning instead means every report covers one complete week,
weekend included - it'll be a few days old by the following Saturday, but
it's never missing the most important days. You can still manually trigger
a same-day run any time via "Run workflow" with force_run checked.

This intentionally does NOT try to perfectly categorize every single Amazon
fee type. Amazon's fee breakdown is messy and changes often. Instead:
  - The bottom-line "Net from Amazon" number is trustworthy, because it's
    just adding up every transaction's total amount - nothing is guessed.
  - The category breakdown underneath it (Sales, FBA fees, Referral fees,
    Refunds, Other) is a best-effort grouping based on keywords in each
    transaction, labeled "approximate" so it's never mistaken for exact
    accounting.

Ad spend is included as a placeholder for now (get_ad_spend) since the
Amazon Ads API access is still pending approval. Once that's live, that one
function gets filled in with a real API call and everything else here stays
the same.
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
SPAPI_BASE_URL = "https://sellingpartnerapi-na.amazon.com"
FINANCES_TRANSACTIONS_PATH = "/finances/2024-06-19/transactions"

STATE_FILE = "pnl_state.json"

# Set this to "true" (via GitHub Actions workflow_dispatch input) to force a
# report to run today regardless of the date, using the most recently
# completed Monday-Sunday week. Useful for testing.
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Step 1: Figure out if today is a report day, and what period to report on
# ---------------------------------------------------------------------------

def most_recently_completed_week(today):
    """
    Returns (period_start, period_end) for the most recent full Monday-Sunday
    week that has already finished as of `today`. `today.weekday()` is 0 for
    Monday, 6 for Sunday.
    """
    days_since_monday = today.weekday()
    this_monday = today - timedelta(days=days_since_monday)
    period_end = this_monday - timedelta(days=1)      # last Sunday
    period_start = period_end - timedelta(days=6)      # the Monday before that
    return period_start, period_end


def get_report_period(today, forced=False):
    start, end = most_recently_completed_week(today)
    suffix = " (forced test run)" if forced else ""
    label = f"{start.strftime('%b %-d')} - {end.strftime('%b %-d, %Y')}{suffix}"
    return start, end, label


def should_run_today(today):
    if FORCE_RUN:
        return True
    return today.weekday() == 0  # Monday


# ---------------------------------------------------------------------------
# Step 2: Amazon auth + Finances API
# ---------------------------------------------------------------------------

def get_access_token():
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": os.environ["AMAZON_REFRESH_TOKEN"],
            "client_id": os.environ["AMAZON_LWA_CLIENT_ID"],
            "client_secret": os.environ["AMAZON_LWA_CLIENT_SECRET"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_transactions(access_token, period_start, period_end):
    """
    Pulls every financial transaction posted in the given date range,
    following pagination until there's no more data.
    """
    headers = {"x-amz-access-token": access_token}
    posted_after = period_start.strftime("%Y-%m-%dT00:00:00Z")
    posted_before = (period_end + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    transactions = []
    next_token = None

    while True:
        params = {
            "postedAfter": posted_after,
            "postedBefore": posted_before,
        }
        if next_token:
            params = {"nextToken": next_token}

        resp = requests.get(
            SPAPI_BASE_URL + FINANCES_TRANSACTIONS_PATH,
            headers=headers,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()

        batch = payload.get("transactions", payload.get("payload", {}).get("transactions", []))
        transactions.extend(batch)

        next_token = payload.get("nextToken") or payload.get("payload", {}).get("nextToken")
        if not next_token:
            break

    return transactions


# ---------------------------------------------------------------------------
# Step 3: Add everything up
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Sales": ["order", "sale", "shipment", "product charge"],
    "Refunds": ["refund", "return", "chargeback", "reversal"],
    "FBA fees": ["fba", "fulfillment", "storage", "removal", "disposal"],
    "Referral fees": ["referral", "commission"],
    "Advertising": ["advertising", "sponsored", "cpc"],
}


def categorize(description):
    if not description:
        return "Other"
    desc_lower = description.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            return category
    return "Other"


def extract_amount(transaction):
    """
    Handles a couple of possible shapes the Finances API total amount can
    come back in, since Amazon's exact field naming can vary by endpoint
    version.
    """
    total = transaction.get("totalAmount") or transaction.get("total") or {}
    if isinstance(total, dict):
        amount = total.get("currencyAmount", total.get("amount"))
        currency = total.get("currencyCode", "USD")
    else:
        amount = total
        currency = "USD"
    try:
        return float(amount), currency
    except (TypeError, ValueError):
        return 0.0, currency


def summarize_transactions(transactions):
    net_total = 0.0
    currency = "USD"
    category_totals = {}

    for txn in transactions:
        amount, currency = extract_amount(txn)
        net_total += amount

        description = (
            txn.get("description")
            or txn.get("transactionType")
            or ""
        )
        category = categorize(description)
        category_totals[category] = category_totals.get(category, 0.0) + amount

    return net_total, currency, category_totals


def get_ad_spend(period_start, period_end):
    """
    Placeholder until the Amazon Ads API access is approved and wired up.
    Once that's live, this will call the Ads Reporting API for the same
    date range and return a real number instead of None.
    """
    return None


# ---------------------------------------------------------------------------
# Step 4: Slack formatting + sending
# ---------------------------------------------------------------------------

def format_slack_message(label, net_total, currency, category_totals, ad_spend):
    lines = [f"*Amazon P&L Snapshot: {label}*", ""]

    net_line = f"*Net from Amazon: {net_total:,.2f} {currency}*"
    if ad_spend is not None:
        adjusted = net_total - ad_spend
        net_line += f"\n*Net after ad spend: {adjusted:,.2f} {currency}* (ad spend: {ad_spend:,.2f})"
    else:
        net_line += "\n_(Ad spend not yet connected - Ads API access still pending)_"
    lines.append(net_line)
    lines.append("")

    lines.append("_Approximate breakdown (best-effort, not exact accounting):_")
    for category in ["Sales", "Refunds", "FBA fees", "Referral fees", "Advertising", "Other"]:
        if category in category_totals:
            lines.append(f"• {category}: {category_totals[category]:,.2f}")

    lines.append("")
    lines.append("_Note: this does not include cost of goods (COGS) - track that separately._")

    return "\n".join(lines)


def post_to_slack(message):
    webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    resp = requests.post(webhook_url, json={"text": message}, timeout=30)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Step 5: State tracking (avoid double-posting the same period twice)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_reported_period_end": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = datetime.now(timezone.utc).date()
    today = datetime(today.year, today.month, today.day)

    if not should_run_today(today):
        print(f"{today.date()} is not a report day. Nothing to do.")
        return

    period_start, period_end, label = get_report_period(today, forced=(FORCE_RUN and today.weekday() != 0))

    state = load_state()
    period_key = period_end.strftime("%Y-%m-%d")
    if not FORCE_RUN and state.get("last_reported_period_end") == period_key:
        print(f"Already reported for period ending {period_key}. Skipping.")
        return

    print(f"Running P&L report for {label}...")

    access_token = get_access_token()
    transactions = fetch_transactions(access_token, period_start, period_end)
    net_total, currency, category_totals = summarize_transactions(transactions)
    ad_spend = get_ad_spend(period_start, period_end)

    message = format_slack_message(label, net_total, currency, category_totals, ad_spend)
    post_to_slack(message)
    print("Posted to Slack.")

    if not FORCE_RUN:
        state["last_reported_period_end"] = period_key
        save_state(state)


if __name__ == "__main__":
    main()
