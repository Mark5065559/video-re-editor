from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Обработать видео", callback_data="menu:upload")],
        [InlineKeyboardButton(text="🗂 История", callback_data="menu:history"),
         InlineKeyboardButton(text="ℹ️ О боте", callback_data="menu:about")],
    ])


def preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Reels · заполнить 9:16", callback_data="preset:reels_fill")],
        [InlineKeyboardButton(text="🖼 Reels · сохранить весь кадр", callback_data="preset:reels_fit")],
        [InlineKeyboardButton(text="▶️ Shorts · 9:16", callback_data="preset:shorts_fill")],
        [InlineKeyboardButton(text="◻️ Квадрат · 1:1", callback_data="preset:square")],
        [InlineKeyboardButton(text="🎞 Сохранить исходный кадр", callback_data="preset:keep")],
        [InlineKeyboardButton(text="🌤 Мягкая тёплая коррекция", callback_data="preset:warm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="job:cancel")],
    ])
