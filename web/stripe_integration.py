#!/usr/bin/env python3
"""
stripe_integration.py — Stripe Payment Gateway for Coin Purchases
Features:
  • Create checkout sessions for coin packages
  • Webhook handler for payment confirmation
  • Coin top-up on successful payment
"""

import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional

import stripe

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_your_key_here")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "pk_test_your_key_here")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_your_secret_here")

stripe.api_key = STRIPE_SECRET_KEY

COIN_PACKAGES = {
    "starter": {
        "name": "Starter Pack",
        "coins": 5000,
        "price_cents": 499,
        "description": "5,000 coins — Perfect for beginners!"
    },
    "pro": {
        "name": "Pro Pack",
        "coins": 15000,
        "price_cents": 999,
        "description": "15,000 coins — Best value!"
    },
    "legend": {
        "name": "Legend Pack",
        "coins": 50000,
        "price_cents": 2499,
        "description": "50,000 coins — For the legends!"
    },
    "whale": {
        "name": "Whale Pack",
        "coins": 200000,
        "price_cents": 7999,
        "description": "200,000 coins — Ultimate power!"
    }
}

_pending_payments: Dict[str, Dict[str, Any]] = {}


def create_checkout_session(user_id: str, package_id: str, success_url: str, cancel_url: str) -> Dict[str, Any]:
    package = COIN_PACKAGES.get(package_id)
    if not package:
        return {"success": False, "error": "Invalid package ID."}

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": package["name"],
                        "description": package["description"],
                    },
                    "unit_amount": package["price_cents"],
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=cancel_url,
            metadata={
                "user_id": user_id,
                "package_id": package_id,
                "coins": str(package["coins"]),
                "internal_ref": str(uuid.uuid4())
            }
        )

        _pending_payments[session.id] = {
            "user_id": user_id,
            "package_id": package_id,
            "coins": package["coins"],
            "created_at": datetime.utcnow().isoformat()
        }

        return {
            "success": True,
            "session_id": session.id,
            "checkout_url": session.url
        }

    except stripe.error.StripeError as e:
        return {"success": False, "error": str(e)}


def handle_webhook(payload: bytes, sig_header: str) -> Dict[str, Any]:
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return {"success": False, "error": "Invalid payload."}
    except stripe.error.SignatureVerificationError:
        return {"success": False, "error": "Invalid signature."}

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        return _process_successful_payment(session_obj)

    elif event["type"] == "checkout.session.expired":
        session_id = event["data"]["object"]["id"]
        _pending_payments.pop(session_id, None)
        return {"success": True, "event_type": "expired", "message": "Session expired."}

    return {"success": True, "event_type": event["type"], "message": "Event received."}


def _process_successful_payment(session_obj: Dict) -> Dict[str, Any]:
    from database import get_or_create_user, update_user_coins

    session_id = session_obj["id"]
    metadata = session_obj.get("metadata", {})
    user_id = metadata.get("user_id")
    package_id = metadata.get("package_id")
    coins_str = metadata.get("coins")

    if not all([user_id, package_id, coins_str]):
        return {"success": False, "error": "Missing metadata."}

    try:
        coins = int(coins_str)
    except ValueError:
        return {"success": False, "error": "Invalid coin amount."}

    get_or_create_user(user_id)
    new_balance = update_user_coins(user_id, coins)
    if new_balance is None:
        return {"success": False, "error": "Failed to credit coins."}

    _pending_payments.pop(session_id, None)

    return {
        "success": True,
        "event_type": "checkout.session.completed",
        "message": f"Credited {coins} coins to user {user_id}.",
        "user_id": user_id,
        "coins_credited": coins,
        "package_id": package_id
    }


def get_packages() -> Dict[str, Any]:
    return {
        "success": True,
        "publishable_key": STRIPE_PUBLISHABLE_KEY,
        "packages": {
            k: {
                "id": k,
                "name": v["name"],
                "coins": v["coins"],
                "price_usd": v["price_cents"] / 100,
                "description": v["description"]
            }
            for k, v in COIN_PACKAGES.items()
        }
    }


if __name__ == "__main__":
    print("[stripe] 💳 Stripe Integration module ready.")
    print(f"[stripe] 📦 Available packages: {list(COIN_PACKAGES.keys())}")