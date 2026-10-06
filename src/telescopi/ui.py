"""Telegram inline keyboard shared by command replies and replayed outbox media."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from telescopi import state


def get_main_keyboard():
    """Generates the inline keyboard for bot control."""
    motion_text = "🔴 Stop Motion" if state.is_active else "🟢 Start Motion"
    motion_callback = "stop_motion" if state.is_active else "start_motion"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📸 Photo", callback_data="take_photo"),
            InlineKeyboardButton("📹 Video", callback_data="take_video"),
        ],
        [InlineKeyboardButton(motion_text, callback_data=motion_callback)],
        [
            InlineKeyboardButton("ℹ️ Status", callback_data="show_status"),
            InlineKeyboardButton("❓ Help", callback_data="show_help"),
        ],
    ])
