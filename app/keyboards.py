from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .unique import COPY_CHOICES, FORMATS, INTENSITIES, UniqueOptions


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Обработать видео", callback_data="menu:upload")],
        [InlineKeyboardButton(text="🗂 История", callback_data="menu:history"),
         InlineKeyboardButton(text="ℹ️ О боте", callback_data="menu:about")],
    ])


def preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Уникализатор", callback_data="uq:open")],
        [InlineKeyboardButton(text="📱 Reels · заполнить 9:16", callback_data="preset:reels_fill")],
        [InlineKeyboardButton(text="🖼 Reels · сохранить весь кадр", callback_data="preset:reels_fit")],
        [InlineKeyboardButton(text="▶️ Shorts · 9:16", callback_data="preset:shorts_fill")],
        [InlineKeyboardButton(text="◻️ Квадрат · 1:1", callback_data="preset:square")],
        [InlineKeyboardButton(text="🎞 Сохранить исходный кадр", callback_data="preset:keep")],
        [InlineKeyboardButton(text="🌤 Мягкая тёплая коррекция", callback_data="preset:warm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="job:cancel")],
    ])


def unique_keyboard(options: UniqueOptions, max_copies: int) -> InlineKeyboardMarkup:
    def check(selected: bool) -> str:
        return " ✓" if selected else ""

    copies = [n for n in COPY_CHOICES if n <= max_copies] or [1]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{title}{check(key == options.intensity)}", callback_data=f"uq:int:{key}")
         for key, title in INTENSITIES.items()],
        [InlineKeyboardButton(text=f"×{n}{check(n == options.copies)}", callback_data=f"uq:cnt:{n}")
         for n in copies],
        [InlineKeyboardButton(text=f"🪞 Зеркало: {'вкл' if options.mirror else 'выкл'}", callback_data="uq:mirror"),
         InlineKeyboardButton(text=f"⏩ Скорость: {'вкл' if options.speed else 'выкл'}", callback_data="uq:speed")],
        [InlineKeyboardButton(text=f"📐 Формат: {FORMATS.get(options.fmt, options.fmt)}", callback_data="uq:fmt")],
        [InlineKeyboardButton(text="🚀 Запустить", callback_data="uq:go")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="uq:back"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="job:cancel")],
    ])


def stop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="job:stop")],
    ])
