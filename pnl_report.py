"""
Amazon P&L Snapshot -> Slack
-----------------------------
Runs once a day (via GitHub Actions cron). Most days it does nothing.
Every Monday, it:
  1. Gets a fresh Amazon access token using your existing SP-API refresh token
  2. Pulls every financial event (sales, fees, refunds, etc.) from Amazon's
     Finances API for the full week (Monday - Sunday) that just ended
     yesterday
  3. Adds up the totals into a bottom-line number and a breakdown by fee
     type (no COGS - that's tracked manually by Davis outside this tool)
  4. Posts a readable summary to Slack

Why Monday, and why a full Mon-Sun week: Davis reviews things on Saturday
mornings, and weekends are usually the biggest sales days. A report that
runs Saturday morning would always be missing that same weekend's numbers
(they haven't happened yet), which defeats the point of a P&L check. Running
it Monday morning instead means every report covers one complete week,
weekend included - it'll be a few days old by the following Saturday, but
it's never missing the most important days. You can still manually trigger
a same-day run any time via "Run workflow" with force_run checked.

Which Amazon API this uses, and why: Amazon actually has two different
Finances endpoints. The newer "2024-06-19/transactions" endpoint only gives
a simplified two-bucket view (just "Sales" vs "Expenses" - not useful for a
real breakdown). This script instead uses the older, more detailed
"v0/financialEvents" endpoint, which organizes everything into named lists
(ShipmentEventList for sales/order-level fees, ServiceFeeEventList for
account-level fees like storage, RefundEventList for refunds, etc.) with
each individual fee itemized by type (e.g. "Commission" = referral fee,
"FBAPerUnitFulfillmentFee" = FBA fee, "Storage Fee", and so on).

How the totals stay trustworthy even if a category label is ever wrong:
  - `find_currency_amounts()` recursively scans every event for every
    dollar amount it contains, regardless of exactly where it's nested.
    This means the bottom-line "Net from Amazon" number will always include
    every dollar Amazon reports, even if some individual item ends up
    mis-labeled in the breakdown below it.
  - `categorize()` then labels each amount found (Sales, Refunds, FBA fees,
    Referral fees, Storage/inventory fees, etc.) based on the specific
    field names Amazon uses (ChargeType, FeeType, FeeReason, AdjustmentType)
    and which list it came from. Anything that doesn't clearly match a
    known type falls into "Other fees" rather than being dropped.

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
FINANCIAL_EVENTS_PATH = "/finances/v0/financialEvents"

STATE_FILE = "pnl_state.json"

# Set this to "true" (via GitHub Actions workflow_dispatch input) to force a
# report to run today regardless of the date, using the most recently
# completed Monday-Sunday week. Useful for testing.
FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"

# Set this to "true" (via GitHub Actions workflow_dispatch input) to print
# the raw financial event data into the workflow's log (private, never sent
# to Slack), plus a per-list/per-category count-and-total summary. Useful
# any time the category breakdown looks off and we want to see exactly
# what Amazon actually sent back.
DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "false").lower() == "true"


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


def fetch_financial_events(access_token, period_start, period_end):
    """
    Pulls every financial event posted in the given date range from the
    v0/financialEvents endpoint, following pagination until there's no more
    data, and merges every page's named lists (ShipmentEventList,
    ServiceFeeEventList, RefundEventList, etc.) into one combined dict of
    {list_name: [event, event, ...]}.
    """
    headers = {"x-amz-access-token": access_token}
    posted_after = period_start.strftime("%Y-%m-%dT00:00:00Z")
    posted_before = (period_end + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    combined_events = {}
    next_token = None

    while True:
        params = {
            "PostedAfter": posted_after,
            "PostedBefore": posted_before,
            "MaxResultsPerPage": 100,
        }
        if next_token:
            params = {"NextToken": next_token}

        resp = requests.get(
            SPAPI_BASE_URL + FINANCIAL_EVENTS_PATH,
            headers=headers,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        events = payload.get("FinancialEvents", {}) or {}

        for list_name, items in events.items():
            if isinstance(items, list) and items:
                combined_events.setdefault(list_name, []).extend(items)

        next_token = payload.get("NextToken")
        if not next_token:
            break

    return combined_events


# ---------------------------------------------------------------------------
# Step 3: Add everything up
# ---------------------------------------------------------------------------

# Field names Amazon uses, at various nesting depths, to label what kind of
# charge/fee/adjustment a dollar amount represents.
TYPE_LABEL_KEYS = ("ChargeType", "FeeType", "AdjustmentType", "PromotionType", "FeeReason")


def find_currency_amounts(node, current_type_hint=None):
    """
    Recursively walks one financial event (an arbitrarily nested dict/list
    structure) and yields (amount, currency, type_hint) for every dollar
    amount found in it - regardless of exactly how deep it's nested. This is
    what keeps the bottom-line total trustworthy: every CurrencyAmount in the
    event gets counted, even if the category label ends up imperfect.
    `type_hint` is the nearest ChargeType/FeeType/AdjustmentType/FeeReason
    label found on the way down, used later to categorize this amount.
    """
    results = []

    if isinstance(node, dict):
        if "CurrencyAmount" in node:
            try:
                amount = float(node["CurrencyAmount"])
                currency = node.get("CurrencyCode", "USD")
                results.append((amount, currency, current_type_hint))
            except (TypeError, ValueError):
                pass
            return results  # a currency-amount dict has nothing further useful inside it

        type_hint = current_type_hint
        for key in TYPE_LABEL_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value:
                type_hint = value
                break

        for value in node.values():
            results.extend(find_currency_amounts(value, type_hint))

    elif isinstance(node, list):
        for item in node:
            results.extend(find_currency_amounts(item, current_type_hint))

    return results


def categorize(list_name, type_hint):
    """
    Labels one dollar amount based on which named list it came from (most
    reliable signal) and, for the two list types that mix several things
    together (ShipmentEventList, ServiceFeeEventList), the specific
    ChargeType/FeeType/FeeReason found near it.
    """
    hint = (type_hint or "").lower()

    if list_name == "RefundEventList":
        return "Refunds"
    if list_name in ("GuaranteeClaimEventList", "ChargebackEventList"):
        return "Claims & chargebacks"
    if list_name == "FBALiquidationEventList":
        return "Liquidations"
    if list_name == "CouponPaymentEventList":
        return "Coupons"
    if list_name == "AdjustmentEventList":
        return "Inventory adjustments"
    if list_name == "TaxWithholdingEventList":
        return "Taxes withheld"

    if list_name == "ServiceFeeEventList":
        if "storage" in hint:
            return "Storage / inventory fees"
        if "subscription" in hint:
            return "Subscription fee"
        if "removal" in hint or "disposal" in hint:
            return "Removal / disposal fees"
        return "Other fees"

    if list_name == "ShipmentEventList":
        if "commission" in hint:
            return "Referral fees"
        if "fba" in hint or "fulfillment" in hint:
            return "FBA fees"
        if "tax" in hint:
            return "Taxes collected"
        if not hint or "principal" in hint or "shipping" in hint or "giftwrap" in hint:
            return "Sales"
        return "Other fees"

    return "Other fees"


def summarize_financial_events(events):
    """
    Returns (net_total, currency, category_totals, category_subitems).
    category_subitems breaks each category down further by its specific
    Amazon type label (e.g. "Other fees" -> {"FixedClosingFee": -5.00,
    "DigitalServicesFee": -7.34}), so any catch-all category can show
    exactly what's inside it instead of just a lump sum.
    """
    net_total = 0.0
    currency = "USD"
    category_totals = {}
    category_subitems = {}

    for list_name, items in events.items():
        for event in items:
            for amount, cur, type_hint in find_currency_amounts(event):
                net_total += amount
                currency = cur or currency
                category = categorize(list_name, type_hint)
                category_totals[category] = category_totals.get(category, 0.0) + amount

                label = type_hint or list_name
                subitems = category_subitems.setdefault(category, {})
                subitems[label] = subitems.get(label, 0.0) + amount

    return net_total, currency, category_totals, category_subitems


def dump_raw_events_for_debugging(events):
    """
    Prints the raw financial event data (and a per-list amount summary) to
    the workflow log so we can double check the category breakdown against
    real data. Only ever printed to the GitHub Actions log, never sent to
    Slack.
    """
    total_events = sum(len(items) for items in events.values())
    print(f"\n===== DEBUG DUMP: {total_events} events across {len(events)} list types =====\n")

    for list_name, items in events.items():
        print(f"--- {list_name}: {len(items)} event(s), showing up to 3 ---")
        for event in items[:3]:
            print(json.dumps(event, indent=2, default=str))
        print()

    print("===== Per-list totals and category labels found =====")
    for list_name, items in events.items():
        list_total = 0.0
        labels_seen = set()
        for event in items:
            for amount, _cur, type_hint in find_currency_amounts(event):
                list_total += amount
                labels_seen.add(type_hint or "(no type label)")
        print(f"  {list_name}: {len(items)} event(s), total {list_total:,.2f}, labels: {sorted(labels_seen)}")
    print("===== END DEBUG DUMP =====\n")


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

CATEGORY_DISPLAY_ORDER = [
    "Sales",
    "Refunds",
    "FBA fees",
    "Referral fees",
    "Storage / inventory fees",
    "Removal / disposal fees",
    "Subscription fee",
    "Inventory adjustments",
    "Claims & chargebacks",
    "Liquidations",
    "Coupons",
    "Taxes collected",
    "Taxes withheld",
    "Other fees",
]


# Categories that are inherently catch-alls - always show what's actually
# inside them (which specific Amazon fee types) rather than just a lump sum,
# so nothing is a mystery number.
CATEGORIES_TO_EXPAND = {"Other fees", "Claims & chargebacks", "Inventory adjustments"}


def format_slack_message(label, net_total, currency, category_totals, ad_spend, category_subitems=None):
    category_subitems = category_subitems or {}
    lines = [f"*Amazon P&L Snapshot: {label}*", ""]

    net_line = f"*Net from Amazon: {net_total:,.2f} {currency}*"
    if ad_spend is not None:
        adjusted = net_total - ad_spend
        net_line += f"\n*Net after ad spend: {adjusted:,.2f} {currency}* (ad spend: {ad_spend:,.2f})"
    else:
        net_line += "\n_(Ad spend not yet connected - Ads API access still pending)_"
    lines.append(net_line)
    lines.append("")

    def append_category_line(category, amount):
        lines.append(f"• {category}: {amount:,.2f}")
        if category in CATEGORIES_TO_EXPAND:
            subitems = category_subitems.get(category, {})
            for sub_label, sub_amount in sorted(subitems.items(), key=lambda kv: -abs(kv[1])):
                lines.append(f"    - {sub_label}: {sub_amount:,.2f}")

    lines.append("_Breakdown by category:_")
    for category in CATEGORY_DISPLAY_ORDER:
        if category in category_totals:
            append_category_line(category, category_totals[category])
    # Catch any category not in our known display order (shouldn't normally
    # happen, but keeps the total honest if Amazon ever adds a new list type)
    for category, amount in category_totals.items():
        if category not in CATEGORY_DISPLAY_ORDER:
            append_category_line(category, amount)

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
    events = fetch_financial_events(access_token, period_start, period_end)

    if DEBUG_DUMP:
        dump_raw_events_for_debugging(events)

    net_total, currency, category_totals, category_subitems = summarize_financial_events(events)
    ad_spend = get_ad_spend(period_start, period_end)

    message = format_slack_message(label, net_total, currency, category_totals, ad_spend, category_subitems)
    post_to_slack(message)
    print("Posted to Slack.")

    if not FORCE_RUN:
        state["last_reported_period_end"] = period_key
        save_state(state)


if __name__ == "__main__":
    main()
