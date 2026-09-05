"""
Lillomilo Ops Tool — Amazon order -> Slack notifier.

What this does, in plain terms:
1. Logs into Amazon's SP-API using your Client ID/Secret/Refresh Token.
2. Asks "any orders created in the last ~20 minutes?"
3. For any order it hasn't already told Slack about, looks up what was
   in the order and posts a message to your Slack channel.
4. Remembers which orders it already announced (in state.json) so it
   never posts the same order twice, even if it overlaps with the
   previous run's time window.

This is meant to be run automatically every ~15 minutes by the GitHub
Actions workflow in .github/workflows/order-alerts.yml — see SETUP.md
for how to wire that up.
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests

STATE_FILE = "state.json"
CHECK_WINDOW_MINUTES = 20  # overlaps the 15-min schedule on purpose, so we never miss one
MAX_REMEMBERED_ORDER_IDS = 1000  # keeps state.json from growing forever
SP_API_BASE_URL = "https://sellingpartnerapi-na.amazon.com"  # North America marketplaces


def get_env(name):
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: missing required environment variable {name}")
        sys.exit(1)
    return value


def get_access_token():
    """Exchange the long-lived Refresh Token for a short-lived Access Token."""
    client_id = get_env("AMAZON_LWA_CLIENT_ID")
    client_secret = get_env("AMAZON_LWA_CLIENT_SECRET")
    refresh_token = get_env("AMAZON_REFRESH_TOKEN")

    resp = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"notified_order_ids": []}


def save_state(state):
    state["notified_order_ids"] = state["notified_order_ids"][-MAX_REMEMBERED_ORDER_IDS:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_orders(access_token, marketplace_id, created_after):
    headers = {
        "x-amz-access-token": access_token,
        "User-Agent": "LillomiloOpsTool/1.0 (Language=Python)",
    }
    orders = []
    params = {"MarketplaceIds": marketplace_id, "CreatedAfter": created_after}
    next_token = None

    while True:
        if next_token:
            params = {"NextToken": next_token}
        resp = requests.get(f"{SP_API_BASE_URL}/orders/v0/orders", headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            print("Rate limited by Amazon, waiting 5 seconds and retrying...")
            time.sleep(5)
            continue
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        orders.extend(payload.get("Orders", []))
        next_token = payload.get("NextToken")
        if not next_token:
            break

    return orders


def get_order_items(access_token, order_id):
    headers = {
        "x-amz-access-token": access_token,
        "User-Agent": "LillomiloOpsTool/1.0 (Language=Python)",
    }
    resp = requests.get(f"{SP_API_BASE_URL}/orders/v0/orders/{order_id}/orderItems", headers=headers, timeout=30)
    if resp.status_code != 200:
        return []
    return resp.json().get("payload", {}).get("OrderItems", [])


def format_slack_message(order, items):
    order_id = order.get("AmazonOrderId", "unknown")
    status = order.get("OrderStatus", "unknown")
    total = order.get("OrderTotal", {})
    amount = total.get("Amount")
    currency = total.get("CurrencyCode", "")
    price_line = f"${amount} {currency}" if amount else "amount unavailable"

    if items:
        item_lines = "\n".join(
            f"  • {item.get('Title', 'Unknown item')} x{item.get('QuantityOrdered', 1)}"
            for item in items
        )
    else:
        item_lines = "  • (item details unavailable)"

    return (
        f"🛒 *New Amazon Order*\n"
        f"Order ID: `{order_id}`\n"
        f"Status: {status}\n"
        f"Total: {price_line}\n"
        f"{item_lines}"
    )


def send_slack_message(text):
    webhook_url = get_env("SLACK_WEBHOOK_URL")
    resp = requests.post(webhook_url, json={"text": text}, timeout=30)
    resp.raise_for_status()


def main():
    send_slack_message("👋 Test message from Lillomilo Ops Tool — if you see this, Slack is wired up correctly!")  # TEMPORARY TEST LINE
    marketplace_id = os.environ.get("AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER")  # ATVPDKIKX0DER = amazon.com (US)
    state = load_state()
    seen_ids = set(state["notified_order_ids"])

    created_after = (datetime.now(timezone.utc) - timedelta(minutes=CHECK_WINDOW_MINUTES)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    print(f"Checking Amazon for orders created after {created_after}...")
    access_token = get_access_token()
    orders = get_orders(access_token, marketplace_id, created_after)
    print(f"Found {len(orders)} order(s) in the window.")

    new_count = 0
    for order in orders:
        order_id = order.get("AmazonOrderId")
        if not order_id or order_id in seen_ids:
            continue
        if order.get("OrderStatus") == "Canceled":
            seen_ids.add(order_id)
            continue

        items = get_order_items(access_token, order_id)
        message = format_slack_message(order, items)
        try:
            send_slack_message(message)
            print(f"Notified Slack about order {order_id}")
        except Exception as e:
            print(f"ERROR sending Slack message for order {order_id}: {e}")
            continue  # don't mark as seen -- we'll retry it next run

        seen_ids.add(order_id)
        new_count += 1

    state["notified_order_ids"] = list(seen_ids)
    save_state(state)
    print(f"Done. {new_count} new order(s) notified.")


if __name__ == "__main__":
    main()
