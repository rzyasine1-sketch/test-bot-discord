#!/usr/bin/env python3
"""
app.py — Flask Web Dashboard
Version: 2.0.0
Features: Async DB bridge, decoupled PIN auth, admin panel, shop API, Stripe webhooks.
"""

import os
import re
import base64
import uuid
import io
import hmac
import hashlib
import asyncio
import logging
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, Response
)
from PIL import Image

from database import Database

# Optional integrations (assumed present in project root)
try:
    from smart_search import search_images, get_cooldown_status
    _HAS_SMART_SEARCH = True
except Exception:
    _HAS_SMART_SEARCH = False
    search_images = None
    get_cooldown_status = None

try:
    from stripe_integration import create_checkout_session, handle_webhook, get_packages, COIN_PACKAGES
    _HAS_STRIPE = True
except Exception:
    _HAS_STRIPE = False
    create_checkout_session = None
    handle_webhook = None
    get_packages = None
    COIN_PACKAGES = {}

try:
    from websocket_handler import init_socketio, notify_purchase, notify_inventory_update, get_online_stats, broadcast_message, admin_notify_new_purchase, admin_notify_new_user
    _HAS_WS = True
except Exception:
    _HAS_WS = False
    init_socketio = None
    notify_purchase = lambda *a, **k: None
    notify_inventory_update = lambda *a, **k: None
    get_online_stats = lambda: {"online_count": 0, "users": []}
    broadcast_message = lambda *a, **k: None
    admin_notify_new_purchase = lambda *a, **k: None
    admin_notify_new_user = lambda *a, **k: None

# ── Logging ──
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("FlaskApp")

# ── Flask App ──
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32))
app.config.update(
    SESSION_COOKIE_SECURE=False,      # Set True in production with HTTPS
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=3600,
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

# Upload directories
UPLOAD_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
AVATAR_DIR = os.path.join(UPLOAD_BASE, "avatars")
BANNER_DIR = os.path.join(UPLOAD_BASE, "banners")
for d in (AVATAR_DIR, BANNER_DIR):
    os.makedirs(d, exist_ok=True)

# Admin credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")

# WebSocket
socketio = init_socketio(app) if _HAS_WS and init_socketio else None

# ── Async Bridge ──
def _run(coro):
    """Execute an async coroutine from Flask's synchronous context."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Decorators ──
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.is_json:
                return jsonify({"success": False, "message": "Authentication required."}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("is_admin"):
            if request.is_json:
                return jsonify({"success": False, "message": "Admin access required."}), 403
            return redirect(url_for("admin_login_page"))
        return f(*args, **kwargs)
    return decorated_function


def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return _run(Database.get_user(uid))


# ── Public Routes ──

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        if not code or len(code) != 6 or not re.match(r"^[A-Z0-9]{6}$", code):
            return render_template("login.html", error="Invalid code format. Must be 6 characters.")

        user_id = _run(Database.validate_login_code(code))
        if user_id is None:
            return render_template("login.html", error="Invalid or expired login code. Request a new one via Discord.")

        session.permanent = True
        session["user_id"] = user_id
        _run(Database.get_or_create_user(user_id))
        admin_notify_new_user(user_id)
        flash("Successfully logged in! Welcome to your dashboard.", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# ── Protected Routes ──

@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    inventory = _run(Database.get_user_inventory(user["user_id"]))
    avatar_count = _run(Database.get_inventory_count(user["user_id"], "avatar"))
    banner_count = _run(Database.get_inventory_count(user["user_id"], "banner"))
    return render_template(
        "dashboard.html",
        user=user,
        inventory=inventory,
        avatar_count=avatar_count,
        banner_count=banner_count,
        total_items=len(inventory)
    )


@app.route("/profile")
@login_required
def profile():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    inventory = _run(Database.get_user_inventory(user["user_id"]))
    return render_template("profile.html", user=user, inventory=inventory)


@app.route("/inventory")
@login_required
def inventory_page():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    inventory = _run(Database.get_user_inventory(user["user_id"]))
    avatar_count = _run(Database.get_inventory_count(user["user_id"], "avatar"))
    banner_count = _run(Database.get_inventory_count(user["user_id"], "banner"))
    return render_template(
        "inventory.html",
        user=user,
        inventory=inventory,
        avatar_count=avatar_count,
        banner_count=banner_count,
        total_items=len(inventory)
    )


@app.route("/shop")
@login_required
def shop():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    packages = _run(get_packages()) if _HAS_STRIPE and get_packages else {"success": False, "packages": {}}
    stripe_key = packages.get("publishable_key", "") if packages.get("success") else ""
    return render_template("shop.html", user=user, stripe_key=stripe_key)


# ── Admin Routes ──

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login_page():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        pwd_hash = hmac.new(app.secret_key, password.encode(), hashlib.sha256).hexdigest()
        expected = ADMIN_PASSWORD_HASH or hmac.new(app.secret_key, "admin".encode(), hashlib.sha256).hexdigest()
        if username == ADMIN_USERNAME and hmac.compare_digest(pwd_hash, expected):
            session["is_admin"] = True
            flash("Admin access granted.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Admin logged out.", "info")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = _run(Database.get_db_stats())
    online = get_online_stats()
    users = []
    purchases = []
    try:
        import sqlite3
        conn = sqlite3.connect(os.getenv("DB_PATH", "profiles.db"))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT user_id, coins, xp, created_at FROM users ORDER BY coins DESC LIMIT 20")
        users = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT id, user_id, item_type, local_file_path, purchased_at FROM inventory ORDER BY purchased_at DESC LIMIT 50")
        purchases = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Admin dashboard DB read error: {e}")
    return render_template(
        "admin/dashboard.html",
        stats=stats,
        users=users,
        purchases=purchases,
        online=online,
        packages=COIN_PACKAGES
    )


@app.route("/admin/api/users")
@admin_required
def admin_api_users():
    try:
        import sqlite3
        conn = sqlite3.connect(os.getenv("DB_PATH", "profiles.db"))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT user_id, coins, xp, created_at FROM users ORDER BY created_at DESC")
        users = [dict(row) for row in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify({"success": True, "users": users})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/admin/api/user/<user_id>/coins", methods=["POST"])
@admin_required
def admin_update_user_coins(user_id):
    data = request.get_json(silent=True) or {}
    delta = data.get("delta", 0)
    if not isinstance(delta, int):
        return jsonify({"success": False, "message": "Invalid delta."}), 400
    success = _run(Database.update_user_coins(user_id, delta))
    if success:
        user = _run(Database.get_user(user_id))
        return jsonify({"success": True, "message": "Coins updated.", "new_balance": user["coins"] if user else 0})
    return jsonify({"success": False, "message": "Failed to update coins."}), 500


@app.route("/admin/api/stats/live")
@admin_required
def admin_api_stats_live():
    return jsonify({
        "success": True,
        "db_stats": _run(Database.get_db_stats()),
        "online": get_online_stats()
    })


@app.route("/admin/api/broadcast", methods=["POST"])
@admin_required
def admin_broadcast():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    msg_type = data.get("type", "info")
    if not message:
        return jsonify({"success": False, "message": "Message required."}), 400
    broadcast_message(message, msg_type)
    return jsonify({"success": True, "message": "Broadcast sent."})


# ── Shop API ──

@app.route("/api/shop/search", methods=["GET"])
@login_required
def api_shop_search():
    if not _HAS_SMART_SEARCH or search_images is None:
        return jsonify({"success": False, "message": "Search module unavailable."}), 503
    user = get_current_user()
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip().lower()
    page = request.args.get("page", "1")
    per_page = request.args.get("per_page", "12")
    if not query:
        return jsonify({"success": False, "message": "Search query required."}), 400
    try:
        page_num = max(1, int(page))
        per_page_num = min(30, max(1, int(per_page)))
    except ValueError:
        return jsonify({"success": False, "message": "Invalid pagination."}), 400

    cd = get_cooldown_status(user["user_id"])
    if cd["on_cooldown"]:
        return jsonify({"success": False, "message": f"Cooldown active. Wait {cd['remaining_seconds']}s.", "cooldown": cd}), 429

    result = search_images(
        user_id=user["user_id"], query=query,
        category=category if category in ("anime", "nature", "cyberpunk") else None,
        per_page=per_page_num, page=page_num
    )
    return jsonify(result)


@app.route("/shop/purchase", methods=["POST"])
@login_required
def shop_purchase():
    user = get_current_user()
    user_id = user["user_id"]
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid JSON body."}), 400

    image_data = data.get("image_data", "")
    item_type = data.get("item_type", "").lower()
    price = data.get("price", 0)
    mask_type = data.get("mask_type", "square")

    if not image_data:
        return jsonify({"success": False, "message": "No image data."}), 400
    if item_type not in ("avatar", "banner"):
        return jsonify({"success": False, "message": "Invalid item type."}), 400
    if not isinstance(price, int) or price < 0:
        return jsonify({"success": False, "message": "Invalid price."}), 400
    if user["coins"] < price:
        return jsonify({"success": False, "message": f"Insufficient funds. You have {user['coins']:,} coins."}), 402

    # Decode Base64
    try:
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]
        image_bytes = base64.b64decode(image_data)
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
    except Exception as e:
        return jsonify({"success": False, "message": f"Invalid image data: {e}"}), 400

    # Resize
    if item_type == "avatar":
        target_size = (512, 512)
        save_dir = AVATAR_DIR
    else:
        target_size = (1200, 400)
        save_dir = BANNER_DIR

    try:
        img = img.resize(target_size, Image.Resampling.LANCZOS)
    except Exception as e:
        return jsonify({"success": False, "message": f"Resize failed: {e}"}), 500

    # Mask
    mask_img = _generate_mask_image(target_size, mask_type)
    if mask_img:
        r, g, b, a = img.split()
        a = Image.composite(a, Image.new("L", target_size, 0), mask_img)
        img = Image.merge("RGBA", (r, g, b, a))

    # Save
    filename = f"{user_id}_{uuid.uuid4().hex[:8]}_{int(datetime.utcnow().timestamp())}.png"
    filepath = os.path.join(save_dir, filename)
    try:
        img.save(filepath, "PNG", optimize=True)
    except Exception as e:
        return jsonify({"success": False, "message": f"Save failed: {e}"}), 500

    # Deduct coins
    success = _run(Database.update_user_coins(user_id, -price))
    if not success:
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({"success": False, "message": "Transaction failed."}), 500

    # Log inventory
    relative_path = f"uploads/{item_type}s/{filename}"
    inventory_id = _run(Database.add_inventory_item(user_id, item_type, relative_path))

    # Notify
    notify_purchase(user_id, item_type, price, relative_path)
    notify_inventory_update(user_id, _run(Database.get_inventory_count(user_id)))
    admin_notify_new_purchase(user_id, item_type, price)

    return jsonify({
        "success": True,
        "message": f"{item_type.capitalize()} purchased!",
        "inventory_id": inventory_id,
        "item_type": item_type,
        "price_paid": price,
        "remaining_coins": user["coins"] - price,
        "file_path": relative_path,
        "dimensions": target_size,
        "mask_applied": mask_type
    })


def _generate_mask_image(size, mask_type):
    if mask_type == "square" or not mask_type:
        return None
    width, height = size
    mask = Image.new("L", size, 0)
    pixels = mask.load()
    cx, cy = width // 2, height // 2

    if mask_type == "circle":
        radius = min(cx, cy)
        for y in range(height):
            for x in range(width):
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                    pixels[x, y] = 255
    elif mask_type == "squircle":
        a, b = cx, cy
        for y in range(height):
            for x in range(width):
                dx = abs(x - cx) / max(a, 1)
                dy = abs(y - cy) / max(b, 1)
                if dx ** 4 + dy ** 4 <= 1:
                    pixels[x, y] = 255
    elif mask_type == "hexagon":
        radius = min(cx, cy)
        for y in range(height):
            for x in range(width):
                dx = abs(x - cx)
                dy = abs(y - cy)
                if dx <= radius * 0.866 and dy <= radius - dx / 1.732:
                    pixels[x, y] = 255
    elif mask_type == "octagon":
        radius = min(cx, cy)
        cutoff = radius * 0.293
        for y in range(height):
            for x in range(width):
                dx = abs(x - cx)
                dy = abs(y - cy)
                if dx + dy <= radius + cutoff and dx <= radius and dy <= radius:
                    pixels[x, y] = 255
    return mask


# ── Stripe ──

@app.route("/api/shop/packages", methods=["GET"])
@login_required
def api_shop_packages():
    if not _HAS_STRIPE or get_packages is None:
        return jsonify({"success": False, "message": "Stripe module unavailable."}), 503
    return jsonify(_run(get_packages()))


@app.route("/api/shop/checkout", methods=["POST"])
@login_required
def api_shop_checkout():
    if not _HAS_STRIPE or create_checkout_session is None:
        return jsonify({"success": False, "message": "Stripe module unavailable."}), 503
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    package_id = data.get("package_id", "")
    result = create_checkout_session(
        user_id=user["user_id"],
        package_id=package_id,
        success_url=request.host_url + "shop?success=1",
        cancel_url=request.host_url + "shop?cancel=1"
    )
    return jsonify(result)


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    if not _HAS_STRIPE or handle_webhook is None:
        return jsonify({"success": False, "message": "Stripe module unavailable."}), 503
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    result = handle_webhook(payload, sig_header)
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code


# ── User API ──

@app.route("/api/user/me", methods=["GET"])
@login_required
def api_user_me():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "User not found."}), 404
    return jsonify({"success": True, "user": user})


@app.route("/api/user/inventory", methods=["GET"])
@login_required
def api_user_inventory():
    user = get_current_user()
    inventory = _run(Database.get_user_inventory(user["user_id"]))
    return jsonify({"success": True, "inventory": inventory})


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "2.0.0",
        "websocket": socketio is not None
    })


@app.route("/api/stats", methods=["GET"])
def api_stats():
    return jsonify({"success": True, "stats": _run(Database.get_db_stats())})


# ── Error Handlers ──

@app.errorhandler(404)
def not_found(e):
    if request.is_json:
        return jsonify({"success": False, "message": "Endpoint not found."}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    logger.exception("Internal server error")
    if request.is_json:
        return jsonify({"success": False, "message": "Internal server error."}), 500
    return render_template("500.html"), 500


@app.context_processor
def inject_globals():
    return {"app_name": "Discord Bot Dashboard", "app_version": "2.0.0"}


# ── Startup ──

if __name__ == "__main__":
    logger.info("Initializing Discord Bot Dashboard v2.0...")
    _run(Database.init_db())
    logger.info("Database ready.")
    if socketio:
        socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
    else:
        app.run(host="0.0.0.0", port=5000, debug=False)
