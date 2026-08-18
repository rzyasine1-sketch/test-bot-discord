#!/usr/bin/env python3
"""
app.py v3.0 — Discord Bot Dashboard (Legendary Edition)
Features:
  • Passwordless Discord auth
  • Live Discord channel gallery (8 categories)
  • Coin economy with crop/purchase pipeline
  • WebSocket real-time notifications
  • Admin panel
  • Stripe payment integration
"""

import os
import re
import base64
import uuid
import io
import hmac
import hashlib
import sys
from pathlib import Path
from datetime import datetime
from functools import wraps

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, abort, flash, Response, send_from_directory
)
from PIL import Image

try:
    from .database import (
        init_db,
        get_or_create_user,
        get_user,
        validate_login_code,
        update_user_coins,
        cleanup_negative_coin_balances,
        add_inventory_item,
        get_user_inventory,
        get_inventory_count,
        get_db_stats,
        _get_connection,
        get_user_bundle,
        get_profile,
        upsert_discord_identity,
        update_user_settings,
        claim_daily_reward,
        queue_profile_rating_post,
    )
    from .discord_gallery import (
        fetch_gallery,
        get_category_list,
        is_valid_category,
        GALLERY_CATEGORIES,
    )
    from .discord_profile import fetch_discord_user
    from .stripe_integration import (
        create_checkout_session,
        handle_webhook,
        get_packages,
        COIN_PACKAGES
    )
    from .websocket_handler import (
        init_socketio,
        notify_purchase,
        notify_inventory_update,
        get_online_stats,
        broadcast_message,
        admin_notify_new_purchase,
        admin_notify_new_user
    )
except ImportError:
    from database import (
        init_db,
        get_or_create_user,
        get_user,
        validate_login_code,
        update_user_coins,
        cleanup_negative_coin_balances,
        add_inventory_item,
        get_user_inventory,
        get_inventory_count,
        get_db_stats,
        _get_connection,
        get_user_bundle,
        get_profile,
        upsert_discord_identity,
        update_user_settings,
        claim_daily_reward,
        queue_profile_rating_post,
    )
    from discord_gallery import (
        fetch_gallery,
        get_category_list,
        is_valid_category,
        GALLERY_CATEGORIES,
    )
    from discord_profile import fetch_discord_user
    from stripe_integration import (
        create_checkout_session,
        handle_webhook,
        get_packages,
        COIN_PACKAGES
    )
    from websocket_handler import (
        init_socketio,
        notify_purchase,
        notify_inventory_update,
        get_online_stats,
        broadcast_message,
        admin_notify_new_purchase,
        admin_notify_new_user
    )

# ────────────────────────────────────────────────────────────
# Flask Configuration
# ────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SECURE=os.getenv("APP_ENV", "development") == "production",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_NAME="discord_dashboard_sid",
    PERMANENT_SESSION_LIFETIME=3600,
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    PREFERRED_URL_SCHEME="https" if os.getenv("APP_ENV") == "production" else "http",
)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY") or os.urandom(32)

# Upload directories
UPLOAD_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
AVATAR_DIR = os.path.join(UPLOAD_BASE, "avatars")
BANNER_DIR = os.path.join(UPLOAD_BASE, "banners")
for d in [AVATAR_DIR, BANNER_DIR]:
    os.makedirs(d, exist_ok=True)

# Admin credentials (must be set explicitly in production)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

# Initialize SocketIO
socketio = init_socketio(app)


@app.template_filter("item_image_url")
def item_image_url(path):
    """Resolve inventory paths — supports local static files or remote URLs."""
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return url_for("static", filename=path)


@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.tailwindcss.com https://cdnjs.cloudflare.com "
        "https://cdn.socket.io https://js.stripe.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
        "https://cdnjs.cloudflare.com https://cdn.tailwindcss.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self' wss: ws: https://cdn.tailwindcss.com "
        "https://cdn.socket.io https://api.stripe.com https://cdnjs.cloudflare.com; "
        "frame-src https://js.stripe.com"
    )
    return response


@app.before_request
def validate_session_user():
    user_id = session.get("user_id")
    if user_id and get_user(user_id) is None:
        session.clear()

# ────────────────────────────────────────────────────────────
# Auth Decorators
# ────────────────────────────────────────────────────────────

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
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user_bundle(user_id)


def refresh_discord_identity(user_id: str) -> None:
    identity = fetch_discord_user(user_id)
    if not identity:
        return
    upsert_discord_identity(
        user_id,
        username=identity["username"],
        display_name=identity["display_name"],
        avatar_url=identity["avatar_url"],
        avatar_hash=identity["avatar_hash"],
    )


# ────────────────────────────────────────────────────────────
# Routes: Public
# ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        if not code:
            flash("Please enter your login code.", "error")
            return render_template("login.html")
        if len(code) != 6 or not re.match(r"^[A-Z0-9]{6}$", code):
            flash("Invalid login code format.", "error")
            return render_template("login.html")
        user_id = validate_login_code(code)
        if user_id is None:
            flash("Invalid or expired login code. Please request a new one via Discord.", "error")
            return render_template("login.html")

        session.permanent = True
        session["user_id"] = user_id
        get_or_create_user(user_id)
        refresh_discord_identity(user_id)
        admin_notify_new_user(user_id)
        flash("✅ Successfully logged in! Welcome to your dashboard.", "success")
        return redirect(url_for("dashboard"))

    # GET request — show the empty login form
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("👋 You have been logged out.", "info")
    return redirect(url_for("index"))

# ────────────────────────────────────────────────────────────
# Routes: User Dashboard
# ────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    inventory = get_user_inventory(user["user_id"])
    avatar_count = get_inventory_count(user["user_id"], "avatar")
    banner_count = get_inventory_count(user["user_id"], "banner")
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
    inventory = get_user_inventory(user["user_id"])
    return render_template("profile.html", user=user, inventory=inventory)


@app.route("/shop")
@login_required
def shop():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    return render_template("shop.html", user=user)


@app.route("/settings")
@login_required
def settings_page():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    if not user.get("username"):
        refresh_discord_identity(user["user_id"])
        user = get_current_user()
    return render_template("settings.html", user=user)


@app.route("/support")
@login_required
def support_page():
    user = get_current_user()
    if not user:
        session.clear()
        return redirect(url_for("index"))
    return render_template("support.html", user=user)


# ────────────────────────────────────────────────────────────
# Routes: Admin Panel
# ────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login_page():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if not ADMIN_PASSWORD_HASH:
            flash("❌ Admin credentials are not configured in this environment.", "error")
            return render_template("admin/login.html")

        pwd_hash = hmac.new(
            app.secret_key,
            password.encode(),
            hashlib.sha256
        ).hexdigest()
        expected = ADMIN_PASSWORD_HASH

        if username == ADMIN_USERNAME and hmac.compare_digest(pwd_hash, expected):
            session["is_admin"] = True
            flash("✅ Admin access granted.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("❌ Invalid credentials.", "error")
    return render_template("admin/login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("👋 Admin logged out.", "info")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = get_db_stats()
    online = get_online_stats()
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, coins, xp, created_at FROM users ORDER BY coins DESC LIMIT 20"
    )
    users = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT id, user_id, item_type, local_file_path, purchased_at
           FROM inventory ORDER BY purchased_at DESC LIMIT 50"""
    )
    purchases = [dict(row) for row in cursor.fetchall()]
    cursor.close()
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
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, coins, xp, created_at FROM users ORDER BY created_at DESC")
    users = [dict(row) for row in cursor.fetchall()]
    cursor.close()
    return jsonify({"success": True, "users": users})


@app.route("/admin/api/user/<user_id>/coins", methods=["POST"])
@admin_required
def admin_update_user_coins(user_id):
    data = request.get_json(silent=True) or {}
    delta = data.get("delta", 0)
    if not isinstance(delta, int):
        return jsonify({"success": False, "message": "Invalid delta."}), 400
    new_balance = update_user_coins(user_id, delta)
    if new_balance is not None:
        return jsonify({
            "success": True,
            "message": "Coins updated.",
            "new_balance": int(new_balance)
        })
    return jsonify({"success": False, "message": "Failed to update coins."}), 500


@app.route("/admin/api/stats/live")
@admin_required
def admin_api_stats_live():
    return jsonify({
        "success": True,
        "db_stats": get_db_stats(),
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


# ────────────────────────────────────────────────────────────
# API: Discord Gallery (Legendary Edition)
# ────────────────────────────────────────────────────────────

@app.route("/api/gallery/categories", methods=["GET"])
@login_required
def api_gallery_categories():
    return jsonify({"success": True, "categories": get_category_list()})


@app.route("/api/gallery/<category>", methods=["GET"])
@login_required
def api_gallery(category):
    if not is_valid_category(category):
        return jsonify({"success": False, "message": f"Unknown category: {category}"}), 404

    before = request.args.get("before", "").strip() or None
    result = fetch_gallery(category, before=before)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/api/anime-banners", methods=["GET"])
@login_required
def api_anime_banners():
    before = request.args.get("before", "").strip() or None
    result = fetch_gallery("rome9", before=before)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


# ────────────────────────────────────────────────────────────
# API: Purchase (Crop + Coin Economy)
# ────────────────────────────────────────────────────────────

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
    source_url = data.get("source_url", "")
    is_gif = bool(data.get("is_gif"))

    if is_gif and source_url and not image_data:
        if item_type not in ("avatar", "banner"):
            return jsonify({"success": False, "message": "Invalid item type."}), 400
        if not isinstance(price, int) or price < 0:
            return jsonify({"success": False, "message": "Invalid price."}), 400
        if user["coins"] < price:
            return jsonify({
                "success": False,
                "message": "Insufficient coins balance",
            }), 400

        new_balance = update_user_coins(user_id, -price)
        if new_balance is None:
            return jsonify({"success": False, "message": "Insufficient coins balance"}), 400

        inventory_id = add_inventory_item(user_id, item_type, source_url)
        notify_purchase(user_id, item_type, price, source_url)
        notify_inventory_update(user_id, get_inventory_count(user_id))
        admin_notify_new_purchase(user_id, item_type, price)
        remaining = int(new_balance)
        return jsonify({
            "success": True,
            "message": f"🎉 {item_type.capitalize()} GIF unlocked!",
            "inventory_id": inventory_id,
            "item_type": item_type,
            "price_paid": price,
            "remaining_coins": remaining,
            "new_balance": remaining,
            "file_path": source_url,
            "mask_applied": "none",
        })

    if not image_data:
        return jsonify({"success": False, "message": "No image data."}), 400
    if item_type not in ("avatar", "banner"):
        return jsonify({"success": False, "message": "Invalid item type."}), 400
    if mask_type not in ("circle", "square", ""):
        return jsonify({"success": False, "message": "Invalid mask type."}), 400
    if not isinstance(price, int) or price < 0:
        return jsonify({"success": False, "message": "Invalid price."}), 400
    if user["coins"] < price:
        return jsonify({
            "success": False,
            "message": "Insufficient coins balance",
        }), 400

    # Process Base64
    try:
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]
        image_bytes = base64.b64decode(image_data)
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
    except Exception as e:
        return jsonify({"success": False, "message": f"Invalid image data: {str(e)}"}), 400

    # Resize
    if item_type == "avatar":
        target_size = (512, 512)
        save_dir = AVATAR_DIR
    else:
        target_size = (1200, 675)
        save_dir = BANNER_DIR

    try:
        img = img.resize(target_size, Image.Resampling.LANCZOS)
    except Exception as e:
        return jsonify({"success": False, "message": f"Resize failed: {str(e)}"}), 500

    # Apply mask
    mask_img = _generate_mask_image(target_size, mask_type)
    if mask_img:
        r, g, b, a = img.split()
        a = Image.composite(a, Image.new("L", target_size, 0), mask_img)
        img = Image.merge("RGBA", (r, g, b, a))

    # Save locally
    filename = f"{user_id}_{uuid.uuid4().hex[:8]}_{int(datetime.utcnow().timestamp())}.png"
    filepath = os.path.join(save_dir, filename)
    try:
        img.save(filepath, "PNG", optimize=True)
    except Exception as e:
        return jsonify({"success": False, "message": f"Save failed: {str(e)}"}), 500

    # Deduct coins
    new_balance = update_user_coins(user_id, -price)
    if new_balance is None:
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({"success": False, "message": "Insufficient coins balance"}), 400

    # Log inventory
    relative_path = f"uploads/{item_type}s/{filename}"
    inventory_id = add_inventory_item(user_id, item_type, relative_path)

    # Notify via WebSocket
    notify_purchase(user_id, item_type, price, relative_path)
    notify_inventory_update(user_id, get_inventory_count(user_id))
    admin_notify_new_purchase(user_id, item_type, price)

    remaining = int(new_balance)

    return jsonify({
        "success": True,
        "message": f"🎉 {item_type.capitalize()} purchased!",
        "inventory_id": inventory_id,
        "item_type": item_type,
        "price_paid": price,
        "remaining_coins": remaining,
        "new_balance": remaining,
        "file_path": relative_path,
        "dimensions": target_size,
        "mask_applied": mask_type
    })


@app.route("/api/gallery/download", methods=["POST"])
@login_required
def api_gallery_download():
    """Deduct coins and process a cropped download (also saves to inventory)."""
    user = get_current_user()
    user_id = user["user_id"]
    data = request.get_json(silent=True) or {}

    image_data = data.get("image_data", "")
    item_type = data.get("item_type", "").lower()
    price = data.get("price", 0)
    mask_type = data.get("mask_type", "square")
    source_url = data.get("source_url", "")
    is_gif = bool(data.get("is_gif"))

    if is_gif and source_url and not image_data:
        if item_type not in ("avatar", "banner"):
            return jsonify({"success": False, "message": "Invalid item type."}), 400
        if user["coins"] < price:
            return jsonify({
                "success": False,
                "message": "Insufficient coins balance",
            }), 400

        new_balance = update_user_coins(user_id, -price)
        if new_balance is None:
            return jsonify({"success": False, "message": "Insufficient coins balance"}), 400
        inventory_id = add_inventory_item(user_id, item_type, source_url)
        notify_purchase(user_id, item_type, price, source_url)
        remaining = int(new_balance)
        return jsonify({
            "success": True,
            "message": "GIF download unlocked!",
            "inventory_id": inventory_id,
            "remaining_coins": remaining,
            "new_balance": remaining,
            "file_path": source_url,
            "direct_url": source_url,
        })

    if not image_data:
        return jsonify({"success": False, "message": "No image data."}), 400
    if item_type not in ("avatar", "banner"):
        return jsonify({"success": False, "message": "Invalid item type."}), 400
    if not isinstance(price, int) or price < 0:
        return jsonify({"success": False, "message": "Invalid price."}), 400
    if user["coins"] < price:
        return jsonify({
            "success": False,
            "message": "Insufficient coins balance",
        }), 400

    try:
        relative_path = _save_cropped_image(user_id, image_data, item_type, mask_type)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except OSError as exc:
        return jsonify({"success": False, "message": f"Save failed: {exc}"}), 500

    new_balance = update_user_coins(user_id, -price)
    if new_balance is None:
        filepath = os.path.join(app.static_folder, relative_path)
        try:
            os.remove(filepath)
        except OSError:
            pass
        return jsonify({"success": False, "message": "Insufficient coins balance"}), 400

    inventory_id = add_inventory_item(user_id, item_type, relative_path)
    notify_purchase(user_id, item_type, price, relative_path)
    notify_inventory_update(user_id, get_inventory_count(user_id))
    admin_notify_new_purchase(user_id, item_type, price)

    remaining = int(new_balance)

    return jsonify({
        "success": True,
        "message": "Download unlocked!",
        "inventory_id": inventory_id,
        "remaining_coins": remaining,
        "new_balance": remaining,
        "file_path": relative_path,
    })


def _save_cropped_image(user_id, image_data, item_type, mask_type):
    if "," in image_data:
        image_data = image_data.split(",", 1)[1]
    image_bytes = base64.b64decode(image_data)
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    if item_type == "avatar":
        target_size = (512, 512)
        save_dir = AVATAR_DIR
    else:
        target_size = (1200, 675)
        save_dir = BANNER_DIR

    img = img.resize(target_size, Image.Resampling.LANCZOS)
    mask_img = _generate_mask_image(target_size, mask_type)
    if mask_img:
        r, g, b, a = img.split()
        a = Image.composite(a, Image.new("L", target_size, 0), mask_img)
        img = Image.merge("RGBA", (r, g, b, a))

    filename = f"{user_id}_{uuid.uuid4().hex[:8]}_{int(datetime.utcnow().timestamp())}.png"
    filepath = os.path.join(save_dir, filename)
    img.save(filepath, "PNG", optimize=True)
    return f"uploads/{item_type}s/{filename}"


def _generate_mask_image(size, mask_type):
    """Only circle (avatars) and square/rectangle (banners — no mask)."""
    if mask_type in ("square", "", None):
        return None
    if mask_type != "circle":
        return None

    width, height = size
    mask = Image.new("L", size, 0)
    pixels = mask.load()
    cx, cy = width // 2, height // 2
    radius = min(cx, cy)
    for y in range(height):
        for x in range(width):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                pixels[x, y] = 255
    return mask


# ────────────────────────────────────────────────────────────
# API: Stripe
# ────────────────────────────────────────────────────────────

@app.route("/api/shop/packages", methods=["GET"])
@login_required
def api_shop_packages():
    return jsonify(get_packages())


@app.route("/api/shop/checkout", methods=["POST"])
@login_required
def api_shop_checkout():
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
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    result = handle_webhook(payload, sig_header)
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code


# ────────────────────────────────────────────────────────────
# API: User Data
# ────────────────────────────────────────────────────────────

@app.route("/api/user/me", methods=["GET"])
@login_required
def api_user_me():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "User not found."}), 404
    return jsonify({"success": True, "user": user})


@app.route("/api/user/settings", methods=["POST"])
@login_required
def api_user_settings():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    bio = data.get("bio")
    hide_balance = data.get("hide_balance")
    accent_theme = data.get("accent_theme")
    show_toasts = data.get("show_toasts")
    if bio is not None and not isinstance(bio, str):
        return jsonify({"success": False, "message": "Invalid bio."}), 400
    profile = update_user_settings(
        user["user_id"],
        bio=bio,
        hide_balance=hide_balance if isinstance(hide_balance, bool) else None,
        accent_theme=accent_theme if isinstance(accent_theme, str) else None,
        show_toasts=show_toasts if isinstance(show_toasts, bool) else None,
    )
    rating_status = queue_profile_rating_post(user["user_id"])
    return jsonify({
        "success": True,
        "profile": profile,
        "message": "Settings saved.",
        "rating_status": rating_status,
    })


@app.route("/api/user/daily", methods=["POST"])
@login_required
def api_user_daily():
    user = get_current_user()
    result = claim_daily_reward(user["user_id"])
    status = 200 if result.get("success") else 429
    return jsonify(result), status


@app.route("/api/user/inventory", methods=["GET"])
@login_required
def api_user_inventory():
    user = get_current_user()
    inventory = get_user_inventory(user["user_id"])
    return jsonify({"success": True, "inventory": inventory})


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.svg",
        mimetype="image/svg+xml",
    )


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "3.0.0",
        "websocket": socketio is not None
    })


# ────────────────────────────────────────────────────────────
# Error Handlers
# ────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.is_json:
        return jsonify({"success": False, "message": "Endpoint not found."}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    if request.is_json:
        return jsonify({"success": False, "message": "Internal server error."}), 500
    return render_template("500.html"), 500


@app.context_processor
def inject_globals():
    nav_user = None
    user_id = session.get("user_id")
    if user_id:
        nav_user = get_user_bundle(user_id)
    return {
        "app_name": "Discord Bot Dashboard",
        "app_version": "3.0.0",
        "nav_user": nav_user,
    }


if __name__ == "__main__":
    print("[FLASK] 🚀 Initializing Discord Bot Dashboard v3.0 (Legendary)...")
    init_db()
    cleaned = cleanup_negative_coin_balances()
    if cleaned:
        print(f"[FLASK] 🧹 Clamped negative coin balances to 0 (rows: {cleaned}).")
    print("[FLASK] ✅ Database ready.")
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    print(f"[FLASK] 🌐 Starting server with WebSocket support on http://{host}:{port}")
    socketio.run(app, host=host, port=port, debug=False)