#!/usr/bin/env python3
"""
websocket_handler.py — Real-time WebSocket Notifications
Features:
  • Purchase notifications
  • Cooldown alerts
  • Inventory updates
  • Admin broadcast channel
  • Online user tracking
"""

import time
from datetime import datetime
from typing import Dict, Any, Optional
from flask import request, session
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect

socketio: Optional[SocketIO] = None
online_users: Dict[str, Dict[str, Any]] = {}


def init_socketio(app) -> SocketIO:
    global socketio
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        ping_timeout=60,
        ping_interval=25,
        logger=False,
        engineio_logger=False
    )
    _register_handlers()
    return socketio


def _register_handlers():
    @socketio.on("connect")
    def handle_connect():
        user_id = session.get("user_id")
        if not user_id:
            disconnect()
            return

        sid = request.sid
        join_room(f"user_{user_id}")
        join_room("broadcast")

        online_users[sid] = {
            "user_id": user_id,
            "joined_at": datetime.utcnow().isoformat(),
            "ip": request.remote_addr
        }

        emit("connected", {
            "status": "ok",
            "user_id": user_id,
            "online_count": len(online_users),
            "timestamp": datetime.utcnow().isoformat()
        })

    @socketio.on("disconnect")
    def handle_disconnect():
        sid = request.sid
        if sid in online_users:
            user_id = online_users[sid]["user_id"]
            leave_room(f"user_{user_id}")
            leave_room("broadcast")
            del online_users[sid]

    @socketio.on("ping_server")
    def handle_ping():
        emit("pong_server", {"timestamp": time.time()})

    @socketio.on("subscribe_cooldown")
    def handle_subscribe_cooldown(data=None):
        """Client wants cooldown countdown updates."""
        user_id = session.get("user_id")
        if user_id:
            emit("cooldown_subscribed", {"user_id": user_id})


def notify_purchase(user_id: str, item_type: str, price: int, file_path: str) -> None:
    if socketio:
        socketio.emit("purchase_complete", {
            "user_id": user_id,
            "item_type": item_type,
            "price": price,
            "file_path": file_path,
            "timestamp": datetime.utcnow().isoformat(),
            "message": f"🎉 {item_type.capitalize()} purchased successfully!"
        }, room=f"user_{user_id}")


def notify_cooldown(user_id: str, remaining_seconds: int) -> None:
    if socketio:
        socketio.emit("cooldown_update", {
            "user_id": user_id,
            "remaining_seconds": remaining_seconds,
            "total_seconds": 60,
            "timestamp": datetime.utcnow().isoformat()
        }, room=f"user_{user_id}")


def notify_inventory_update(user_id: str, inventory_count: int) -> None:
    if socketio:
        socketio.emit("inventory_update", {
            "user_id": user_id,
            "inventory_count": inventory_count,
            "timestamp": datetime.utcnow().isoformat()
        }, room=f"user_{user_id}")


def broadcast_message(message: str, message_type: str = "info") -> None:
    if socketio:
        socketio.emit("broadcast", {
            "message": message,
            "type": message_type,
            "timestamp": datetime.utcnow().isoformat()
        }, room="broadcast")


def notify_low_balance(user_id: str, current_coins: int, required: int) -> None:
    if socketio:
        socketio.emit("low_balance_warning", {
            "user_id": user_id,
            "current_coins": current_coins,
            "required": required,
            "message": f"⚠️ Low balance! You have {current_coins} coins but need {required}."
        }, room=f"user_{user_id}")


def admin_notify_new_purchase(user_id: str, item_type: str, price: int) -> None:
    if socketio:
        socketio.emit("admin_new_purchase", {
            "user_id": user_id,
            "item_type": item_type,
            "price": price,
            "timestamp": datetime.utcnow().isoformat()
        }, room="admin_room")


def admin_notify_new_user(user_id: str) -> None:
    if socketio:
        socketio.emit("admin_new_user", {
            "user_id": user_id,
            "timestamp": datetime.utcnow().isoformat()
        }, room="admin_room")


def get_online_stats() -> Dict[str, Any]:
    return {
        "online_count": len(online_users),
        "users": [
            {"user_id": u["user_id"], "joined_at": u["joined_at"]}
            for u in online_users.values()
        ]
    }