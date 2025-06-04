import os
import json
import asyncio
import logging
import threading
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
import disnake
from disnake.ext import commands, tasks
from disnake.ui import Button, View
import aiohttp
import feedparser
import streamlink
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
import datetime as dt
import pytz


TIMEZONE = pytz.timezone('Europe/Moscow')


class TimezoneFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt_obj = dt.datetime.fromtimestamp(record.created, tz=TIMEZONE)
        if datefmt:
            return dt_obj.strftime(datefmt)
        return dt_obj.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]


logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


for handler in logger.handlers:
    handler.setFormatter(TimezoneFormatter('%(asctime)s [%(levelname)s] %(message)s'))


STATE_FILE = "youtube_state.json"
TEMP_IMAGE_DIR = "temp_images"
START_MESSAGE = "Управление уведомлениями в Discord"
TITLE_PROMPT = "1. Введите название сообщения:"
PREVIEW_PROMPT = "2. Отправьте ссылку на превью (картинку) или загрузите изображение:"
VIDEO_URL_PROMPT = "3. Введите ссылку на видео или контент:"
CANCEL_MESSAGE = "Действие отменено."
SENT_MESSAGE = "Сообщение отправлено. Используйте /start для новой отправки."
INVALID_URL_MESSAGE = "❌ Ссылка должна начинаться с HTTP или HTTPS"
INVALID_PREVIEW_MESSAGE = "❌ Пожалуйста, отправьте корректную ссылку, изображение или пропустите шаг."
NO_PERMS_MESSAGE = "⛔️ Недостаточно прав."
DATA_EXPIRED_MESSAGE = "❌ Данные сообщения не найдены или устарели."
SEND_ERROR_MESSAGE = "❌ Ошибка отправки в Discord."
SEND_SUCCESS_MESSAGE = "✅ Отправлено в Discord!"
DEFAULT_MESSAGE = "Новое сообщение"
WEB_SERVER_RESPONSE = "Message sent"
WEB_SERVER_ERROR = "Ошибка сервера"
DISCORD_CHANNEL_ERROR = "Discord канал не найден"


load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "8000"))
YOUTUBE_CHANNEL_ID = int(os.getenv("YOUTUBE_CHANNEL_ID", "1374412160939196476"))
TWITCH_CHANNEL_ID = int(os.getenv("TWITCH_CHANNEL_ID", "1374434150395940965"))
YOUTUBE_CHANNEL_RSS = os.getenv("YOUTUBE_CHANNEL_RSS", "https://www.youtube.com/feeds/videos.xml?channel_id=UCGCE6j2NovYuhXIMlCPhHnQ")
TWITCH_USERNAME = os.getenv("TWITCH_USERNAME", "xKamysh")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))


http_session = None
last_youtube_video_id = None
last_video_title = None
last_youtube_video_sent_time = 0
twitch_stream_live = False
twitch_stream_check = False
send_lock = asyncio.Lock()
temp_storage = {}


if not os.path.exists(TEMP_IMAGE_DIR):
    os.makedirs(TEMP_IMAGE_DIR)


intents = disnake.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix="/", intents=intents, help_command=None)


def load_state():
    global last_youtube_video_id, last_video_title, last_youtube_video_sent_time
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            last_youtube_video_id = state.get("last_video_id")
            last_video_title = state.get("last_video_title")
            last_youtube_video_sent_time = state.get("last_sent_time", 0)
            logger.info(f"Состояние загружено: {last_youtube_video_id}, {last_video_title}")
    except (FileNotFoundError, json.JSONDecodeError):
        logger.info("Файл состояния не найден или повреждён, состояние сброшено.")


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "last_video_id": last_youtube_video_id,
                "last_video_title": last_video_title,
                "last_sent_time": last_youtube_video_sent_time
            }, f)
        logger.info(f"Состояние сохранено: {last_youtube_video_id}, {last_video_title}")
    except Exception as e:
        logger.error(f"Ошибка сохранения состояния: {e}")


async def is_image_available(url: str) -> bool:
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"[Ошибка изображения] {e}")
        return False


async def fetch_youtube_rss():
    try:
        async with http_session.get(YOUTUBE_CHANNEL_RSS, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as response:
            if response.status == 200:
                return feedparser.parse(await response.text())
            logger.warning(f"Ошибка при получении RSS, статус: {response.status}")
    except Exception as e:
        logger.error(f"Ошибка при загрузке RSS: {e}")
    return None


def extract_video_id(link: str) -> str | None:
    try:
        parsed = urlparse(link)
        if "shorts" in parsed.path.lower() or "shorts" in link.lower():
            return None
        if parsed.hostname in ("youtu.be",):
            video_id = parsed.path.lstrip("/")
            if video_id:
                return video_id
        elif parsed.hostname in ("www.youtube.com", "youtube.com"):
            video_id = parse_qs(parsed.query).get("v", [None])[0]
            if video_id:
                return video_id
            logger.warning(f"Невалидный YouTube URL (нет video_id): {link}")
            return None
        logger.warning(f"Невалидный YouTube URL (неподдерживаемый домен): {link}")
        return None
    except Exception as e:
        logger.error(f"Ошибка при извлечении video_id из {link}: {e}")
        return None



async def get_video_preview_url(video_url: str) -> str | None:
    parsed = urlparse(video_url)
    if parsed.hostname in ("www.youtube.com", "youtube.com", "youtu.be"):
        video_id = extract_video_id(video_url)
        if video_id:
            thumbnail_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
            if await is_image_available(thumbnail_url):
                return thumbnail_url
            thumbnail_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            if await is_image_available(thumbnail_url):
                return thumbnail_url
    logger.warning(f"Не удалось получить превью для URL: {video_url}")
    return None


async def get_latest_youtube_video(retry=3):
    global last_youtube_video_id, last_video_title, last_youtube_video_sent_time
    for attempt in range(retry):
        feed = await fetch_youtube_rss()
        if not feed or not feed.entries:
            logger.warning(f"❌ Нет записей в YouTube RSS (попытка {attempt+1}/{retry})")
            await asyncio.sleep(10)
            continue
        invalid_urls = []
        for entry in feed.entries:
            video_url = entry.link.lower()
            if "shorts" in video_url or "/shorts/" in video_url:
                continue
            video_id = extract_video_id(entry.link)
            if not video_id:
                invalid_urls.append(entry.link)
                continue
            if video_id != last_youtube_video_id:
                last_youtube_video_id = video_id
                last_video_title = entry.title
                last_youtube_video_sent_time = asyncio.get_event_loop().time()
                save_state()
                logger.info(f"Найдено новое полное видео: {entry.title} ({video_id})")
                return {"title": entry.title, "link": entry.link}
            elif asyncio.get_event_loop().time() - last_youtube_video_sent_time > 300:
                logger.info("Новое полное видео не найдено, последнее видео уже отправлено.")
                return None
        if invalid_urls:
            logger.warning(f"Пропущены невалидные URL ({len(invalid_urls)}): {', '.join(invalid_urls[:3])}{'...' if len(invalid_urls) > 3 else ''}")
        logger.info("Все видео в RSS являются Shorts или уже отправлены.")
        return None
    return None


async def is_twitch_stream_live() -> bool:
    try:
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as pool:
            streams = await loop.run_in_executor(pool, streamlink.streams, f"https://twitch.tv/{TWITCH_USERNAME}")
        return bool(streams)
    except Exception as e:
        logger.warning(f"[Ошибка проверки Twitch] {e}")
        return False


def create_social_buttons(video_url="") -> View:
    view = View()
    dynamic_label = "🔗 Ссылка" if video_url else "🔗 Нет ссылки"
    view.add_item(Button(label=dynamic_label, url=video_url or "https://t.me/kamyshovnik", style=disnake.ButtonStyle.link))
    links = {
        "💬 Telegram": "https://t.me/kamyshovnik",
        "📘 VK": "https://vk.com/kamyshovnik",
        "🎵 TikTok": "https://www.tiktok.com/@xkamysh",
        "💖 Boosty": "https://boosty.to/xkamysh",
    }
    for label, url in links.items():
        view.add_item(Button(label=label, url=url, style=disnake.ButtonStyle.link))
    return view


async def send_youtube_notification(channel, video):
    global last_youtube_video_sent_time
    async with send_lock:
        now = asyncio.get_event_loop().time()
        video_id = extract_video_id(video["link"])
        if video_id == last_youtube_video_id and (now - last_youtube_video_sent_time < 300):
            logger.info("Повторное видео не отправляется из-за ограничения по времени.")
            return
        last_youtube_video_sent_time = now
        save_state()
        thumbnail = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
        if not await is_image_available(thumbnail):
            thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            if not await is_image_available(thumbnail):
                logger.warning(f"Не удалось загрузить превью для видео {video_id}. Используется embed без изображения.")
                thumbnail = None
        embed = disnake.Embed(
            title=f"✨ Новое видео: {video['title']}",
            url=video["link"],
            color=disnake.Color.from_rgb(229, 57, 53),
            timestamp=dt.datetime.now(dt.UTC)
        )
        if thumbnail:
            embed.set_image(url=thumbnail)
        await channel.send(content="@everyone", embed=embed, view=create_social_buttons(video["link"]))
        logger.info(f"📢 Отправлено новое видео: {video['title']}")


async def send_twitch_notification(channel):
    global twitch_stream_live
    embed = disnake.Embed(
        title=f"🔴 {TWITCH_USERNAME} сейчас стримит!",
        url=f"https://twitch.tv/{TWITCH_USERNAME}",
        description="🎉 Заходи на эфир! Общение, атмосфера и веселье ждут тебя!",
        color=disnake.Color.from_rgb(138, 43, 226),
        timestamp=dt.datetime.now(dt.UTC)
    ).set_image(url="https://media.discordapp.net/attachments/722745907279298590/1378720172734545970/134799723_thumbnail.webp?ex=683e4978&is=683cf7f8&hm=199afc43df2a8def8a11ce6bbbe3d7fe79cf3351fa916f1f930428d90a2ca6e2&=&format=webp&width=1522&height=856")
    embed.set_footer(text=f"Twitch • {TWITCH_USERNAME}", icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png")
    await channel.send(content="@everyone", embed=embed, view=create_social_buttons(f"https://twitch.tv/{TWITCH_USERNAME}"))
    logger.info(f"📢 Стрим в эфире: {TWITCH_USERNAME}")
    twitch_stream_live = True


def start_telegram_bot():
    class Form(StatesGroup):
        waiting_for_title = State()
        waiting_for_preview = State()
        waiting_for_video_url = State()
        confirming = State()


    async def telegram_main():
        telegram_bot = Bot(token=TELEGRAM_TOKEN)
        dp = Dispatcher(storage=MemoryStorage())

        @dp.errors()
        async def on_error(update, error):
            logger.error(f"Error processing update {update.update_id if update else 'N/A'}: {error}", exc_info=True)
            return True

        start_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Создать сообщение", callback_data="create_message")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ])

        cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏩ Пропустить", callback_data="skip")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ])


        async def send_preview(message: Message, form_data: dict, confirm_keyboard: InlineKeyboardMarkup):
            preview_text = (
                f"<b>Предпросмотр сообщения</b>\n\n"
                f"<b>Заголовок:</b> {form_data['title'] or 'Не указано'}\n"
                f"<b>Превью:</b> {'Изображение загружено' if form_data.get('preview_path') else form_data.get('preview_url', 'Не указано')}\n"
                f"<b>Ссылка:</b> {form_data['video_url'] or 'Не указано'}"
            )
            if form_data.get("preview_path"):
                await message.answer_photo(
                    photo=FSInputFile(form_data["preview_path"]),
                    caption=preview_text,
                    parse_mode="HTML",
                    reply_markup=confirm_keyboard
                )
            elif form_data.get("preview_url"):
                await message.answer_photo(
                    photo=form_data["preview_url"],
                    caption=preview_text,
                    parse_mode="HTML",
                    reply_markup=confirm_keyboard
                )
            else:
                await message.answer(
                    text=preview_text,
                    parse_mode="HTML",
                    reply_markup=confirm_keyboard
                )


        @dp.message(F.text == "/start")
        async def start_command(message: Message):
            if message.from_user.id != TELEGRAM_ADMIN_ID:
                await message.answer(NO_PERMS_MESSAGE)
                return
            await message.answer(START_MESSAGE, reply_markup=start_keyboard)


        @dp.message(F.text == "/cancel")
        async def cancel_command(message: Message, state: FSMContext):
            await state.clear()
            await message.answer(CANCEL_MESSAGE, reply_markup=start_keyboard)


        @dp.callback_query(F.data == "create_message")
        async def forward_to_discord(callback: CallbackQuery, state: FSMContext):
            if callback.from_user.id != TELEGRAM_ADMIN_ID:
                await callback.answer(NO_PERMS_MESSAGE)
                logger.info(f"User {callback.from_user.id} does not have permission (Admin ID: {TELEGRAM_ADMIN_ID})")
                return
            current_state = await state.get_state()
            logger.info(f"Current state before transition: {current_state}")
            await state.clear()
            try:
                if callback.message.text:
                    await callback.message.edit_text(TITLE_PROMPT, reply_markup=cancel_keyboard)
                else:
                    await callback.message.answer(TITLE_PROMPT, reply_markup=cancel_keyboard)
            except Exception as e:
                logger.error(f"Error editing message in forward_to_discord: {e}")
                await callback.message.answer(TITLE_PROMPT, reply_markup=cancel_keyboard)
            await state.set_state(Form.waiting_for_title)
            new_state = await state.get_state()
            logger.info(f"State after transition: {new_state}")
            await callback.answer()


        @dp.message(Form.waiting_for_title)
        async def process_title(message: Message, state: FSMContext):
            logger.info(f"Received message in waiting_for_title state: {message.text} from user {message.from_user.id}")
            if message.from_user.id != TELEGRAM_ADMIN_ID:
                logger.info(f"User {message.from_user.id} does not have permission (Admin ID: {TELEGRAM_ADMIN_ID})")
                return
            await state.update_data(title=message.text[:256] if message.text else "")
            await message.answer(PREVIEW_PROMPT, reply_markup=cancel_keyboard)
            await state.set_state(Form.waiting_for_preview)


        @dp.callback_query(F.data == "skip")
        async def skip_step(callback: CallbackQuery, state: FSMContext):
            if callback.from_user.id != TELEGRAM_ADMIN_ID:
                await callback.answer(NO_PERMS_MESSAGE)
                return
            current_state = await state.get_state()
            if current_state == Form.waiting_for_title.state:
                await state.update_data(title="")
                await callback.message.edit_text(PREVIEW_PROMPT, reply_markup=cancel_keyboard)
                await state.set_state(Form.waiting_for_preview)
            elif current_state == Form.waiting_for_preview.state:
                await state.update_data(preview_url="", preview_path="")
                await callback.message.edit_text(VIDEO_URL_PROMPT, reply_markup=cancel_keyboard)
                await state.set_state(Form.waiting_for_video_url)
            elif current_state == Form.waiting_for_video_url.state:
                await state.update_data(video_url="")
                form_data = await state.get_data()
                temp_storage[str(callback.message.message_id)] = {
                    "message": form_data["title"],
                    "preview_url": form_data.get("preview_url", ""),
                    "preview_path": form_data.get("preview_path", ""),
                    "video_url": form_data["video_url"]
                }
                confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📤 Отправить в Discord", callback_data=f"confirm_{callback.message.message_id}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
                ])
                await send_preview(callback.message, form_data, confirm_keyboard)
                await state.set_state(Form.confirming)
            await callback.answer()


        @dp.message(Form.waiting_for_preview)
        async def process_preview(message: Message, state: FSMContext):
            if message.from_user.id != TELEGRAM_ADMIN_ID:
                return
            preview_url = ""
            preview_path = ""
            if message.photo:
                photo = message.photo[-1]
                file = await telegram_bot.get_file(photo.file_id)
                file_path = file.file_path
                dest_path = os.path.join(TEMP_IMAGE_DIR, f"{photo.file_unique_id}.jpg")
                await telegram_bot.download_file(file_path, dest_path)
                preview_path = dest_path
            elif message.text and message.text.startswith(('http', 'https')):
                preview_url = message.text
            else:
                await message.answer(INVALID_PREVIEW_MESSAGE)
                return
            await state.update_data(preview_url=preview_url, preview_path=preview_path)
            await message.answer(VIDEO_URL_PROMPT, reply_markup=cancel_keyboard)
            await state.set_state(Form.waiting_for_video_url)


        @dp.message(Form.waiting_for_video_url)
        async def process_video_url(message: Message, state: FSMContext):
            if message.from_user.id != TELEGRAM_ADMIN_ID:
                return
            if message.text and not message.text.startswith(('http', 'https')):
                await message.answer(INVALID_URL_MESSAGE)
                return
            video_url = message.text if message.text else ""
            form_data = await state.get_data()
            preview_url = form_data.get("preview_url", "")
            preview_path = form_data.get("preview_path", "")
            if not preview_url and not preview_path and video_url:
                preview_url = await get_video_preview_url(video_url) or ""
                await state.update_data(preview_url=preview_url)
            await state.update_data(video_url=video_url)
            form_data = await state.get_data()
            temp_storage[str(message.message_id)] = {
                "message": form_data["title"],
                "preview_url": form_data["preview_url"],
                "preview_path": form_data["preview_path"],
                "video_url": form_data["video_url"]
            }
            confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Отправить в Discord", callback_data=f"confirm_{message.message_id}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
            ])
            await send_preview(message, form_data, confirm_keyboard)
            await state.set_state(Form.confirming)


        @dp.callback_query(Form.confirming, F.data.startswith("confirm_"))
        async def confirm_send(callback: CallbackQuery, state: FSMContext):
            if callback.from_user.id != TELEGRAM_ADMIN_ID:
                await callback.answer(NO_PERMS_MESSAGE)
                return
            try:
                message_id = callback.data.split("_")[1]
                form_data = temp_storage.get(message_id)
                if not form_data:
                    await callback.answer(DATA_EXPIRED_MESSAGE)
                    return
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"http://localhost:{PORT}/telegram",
                        json={
                            "message": form_data["message"],
                            "preview_url": form_data["preview_url"],
                            "preview_path": form_data["preview_path"],
                            "video_url": form_data["video_url"]
                        }
                    ) as resp:
                        if resp.status == 200:
                            await callback.answer("✅ Отправлено в Discord!")
                            if message_id in temp_storage:
                                del temp_storage[message_id]
                        else:
                            error_text = await resp.text()
                            logger.error(f"Ошибка при отправке в Discord: {resp.status} - {error_text}")
                            await callback.answer("❌ Ошибка отправки в Discord.")
                await state.clear()
                success_message = "Сообщение отправлено в Discord. Используйте /start для новой отправки."
                try:
                    if callback.message.text:
                        await callback.message.edit_text(success_message, reply_markup=start_keyboard)
                    elif callback.message.photo:
                        await callback.message.edit_caption(caption=success_message, reply_markup=start_keyboard)
                    else:
                        await callback.message.answer(success_message, reply_markup=start_keyboard)
                except Exception as e:
                    logger.error(f"Error editing message: {e}")
                    await callback.message.answer(success_message, reply_markup=start_keyboard)
            except Exception as e:
                logger.error(f"Ошибка отправки из Telegram в Discord: {e}")
                await callback.answer("❌ Ошибка.")


        @dp.callback_query(F.data == "cancel")
        async def cancel_callback(callback: CallbackQuery, state: FSMContext):
            await state.clear()
            try:
                if callback.message.text:
                    await callback.message.edit_text(CANCEL_MESSAGE, reply_markup=start_keyboard)
                else:
                    await callback.message.answer(CANCEL_MESSAGE, reply_markup=start_keyboard)
            except Exception as e:
                logger.error(f"Error editing message: {e}")
                await callback.message.answer(CANCEL_MESSAGE, reply_markup=start_keyboard)
            await callback.answer()

        await dp.start_polling(telegram_bot)

    asyncio.run(telegram_main())


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_updates():
    global twitch_stream_live
    max_retries = 3
    for attempt in range(max_retries):
        try:
            yt_channel = bot.get_channel(YOUTUBE_CHANNEL_ID)
            tw_channel = bot.get_channel(TWITCH_CHANNEL_ID)
            if yt_channel is None:
                logger.warning(f"Канал YouTube не найден (ID: {YOUTUBE_CHANNEL_ID})")
            else:
                new_video = await get_latest_youtube_video()
                if new_video:
                    await send_youtube_notification(yt_channel, new_video)
                else:
                    logger.info("Видео не обновлялось с последней проверки.")
            if tw_channel is None:
                logger.warning(f"Канал Twitch не найден (ID: {TWITCH_CHANNEL_ID})")
            else:
                is_live = await is_twitch_stream_live()
                if is_live and not twitch_stream_live:
                    await send_twitch_notification(tw_channel)
                elif not is_live:
                    logger.info("Twitch стрим закончен.")
                    twitch_stream_live = False
                await update_presence(is_live)
            break
        except Exception as e:
            logger.error(f"Error in check_updates (attempt {attempt + 1}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries - 1:
                await asyncio.sleep(5)
            else:
                logger.error("Max retries reached in check_updates. Task failed.")


@bot.command(name="help")
async def custom_help(ctx):
    help_text = (
        "📋 **Команды Discord бота**:\n"
        "/help - Показать это сообщение\n"
        "/проверка - Ручная проверка обновлений YouTube и Twitch\n"
        "/testvideo (или /тествидео) - Отправить тестовое уведомление о видео\n"
        "/teststream (или /тестстрим) - Отправить тестовое уведомление о стриме\n"
    )
    await ctx.send(help_text)


@bot.command(name="проверка")
async def manual_check(ctx):
    global twitch_stream_check
    await ctx.send("🔍 Проверка обновлений...")
    yt_channel = bot.get_channel(YOUTUBE_CHANNEL_ID)
    twitch_channel = bot.get_channel(TWITCH_CHANNEL_ID)
    if yt_channel is None or twitch_channel is None:
        await ctx.send("❌ Не могу получить нужные каналы для уведомлений.")
        return
    new_video = await get_latest_youtube_video()
    if new_video and yt_channel:
        await send_youtube_notification(yt_channel, new_video)
        await ctx.send("✅ Видео найдено и отправлено.")
    else:
        await ctx.send("ℹ️ Новых видео не найдено.")
    is_live = await is_twitch_stream_live()
    if is_live and not twitch_stream_check and twitch_channel:
        await send_twitch_notification(twitch_channel)
        await ctx.send("📡 Стрим в эфире! Уведомление отправлено.")
    elif is_live:
        await ctx.send("📡 Стрим уже идёт.")
    else:
        await ctx.send("📴 Стрим сейчас не идёт.")


@bot.command(name="testvideo", aliases=["тествидео"])
async def test_video(ctx):
    yt_channel = bot.get_channel(YOUTUBE_CHANNEL_ID)
    if yt_channel:
        fake_video = {
            "title": "🎬 Тестовое видео: Камыш возвращается!",
            "link": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        }
        await send_youtube_notification(yt_channel, fake_video)
        await ctx.send("✅ Тестовое видео отправлено.")
    else:
        await ctx.send("❌ Канал YouTube не найден.")


@bot.command(name="teststream", aliases=["тестстрим"])
async def test_stream(ctx):
    twitch_channel = bot.get_channel(TWITCH_CHANNEL_ID)
    if twitch_channel:
        await send_twitch_notification(twitch_channel)
        await ctx.send("📡 Тестовое уведомление о стриме отправлено.")
    else:
        await ctx.send("❌ Канал Twitch не найден.")


@bot.event
async def on_ready():
    global http_session
    if http_session is None:
        http_session = aiohttp.ClientSession()
    logger.info(f"✅ Бот {bot.user} запущен!")
    if not check_updates.is_running():
        check_updates.start()
    bot.loop.create_task(run_webserver())


async def update_presence(is_live: bool):
    try:
        if is_live:
            activity = disnake.Activity(type=disnake.ActivityType.watching, name=TWITCH_USERNAME)
        else:
            activity = None
        await bot.change_presence(activity=activity)
        logger.info(f"Обновлена активность: {'Смотрит ' + TWITCH_USERNAME if is_live else 'нет активности'}")
    except Exception as e:
        logger.error(f"Ошибка обновления активности: {e}")


async def handle(request):
    return web.Response(text="ОК")


async def telegram_news_handler(request):
    try:
        data = await request.json()
        logger.info(f"Получен запрос от Telegram: {data}")
        channel = bot.get_channel(YOUTUBE_CHANNEL_ID)
        if not channel:
            logger.error(f"Канал Discord не найден (ID: {YOUTUBE_CHANNEL_ID})")
            return web.Response(status=404, text=DISCORD_CHANNEL_ERROR)
        
        message = data.get("message") or DEFAULT_MESSAGE
        embed = disnake.Embed(
            title=message,
            color=disnake.Color.from_rgb(229, 57, 53),
            timestamp=dt.datetime.now(dt.UTC)
        )
        
        preview_url = data.get("preview_url")
        preview_path = data.get("preview_path")
        video_url = data.get("video_url")
        files = []

        if preview_path and os.path.exists(preview_path):
            filename = os.path.basename(preview_path)
            embed.set_image(url=f"attachment://{filename}")
            files.append(disnake.File(preview_path, filename=filename))
        elif preview_url and await is_image_available(preview_url):
            embed.set_image(url=preview_url)
            if video_url:
                embed.url = video_url
        elif video_url:
            video_id = extract_video_id(video_url)
            if video_id:
                thumbnail = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                if await is_image_available(thumbnail):
                    embed.url = video_url
                    embed.set_image(url=thumbnail)
                else:
                    thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
                    if await is_image_available(thumbnail):
                        embed.url = video_url
                        embed.set_image(url=thumbnail)
                    else:
                        logger.warning(f"Не удалось загрузить превью для видео {video_url} (ID: {video_id}).")
        
        view = create_social_buttons(video_url or "")
        
        await channel.send(content="@everyone", embed=embed, view=view, files=files)
        logger.info("📢 Сообщение из Telegram отправлено в Discord с упоминанием!")
        
        if preview_path and os.path.exists(preview_path):
            try:
                os.remove(preview_path)
                logger.info(f"Удалён временный файл: {preview_path}")
            except Exception as e:
                logger.error(f"Ошибка при удалении файла {preview_path}: {e}")
                
        return web.Response(text=WEB_SERVER_RESPONSE)
    except Exception as e:
        logger.error(f"Ошибка при обработке запроса: {e}", exc_info=True)
        return web.Response(status=500, text=WEB_SERVER_ERROR)


async def run_webserver():
    app = web.Application()
    app.add_routes([
        web.get('/', handle),
        web.post('/telegram', telegram_news_handler)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 HTTP сервер запущен на порту {PORT}")


async def close_http_session():
    global http_session
    if http_session:
        await http_session.close()


if __name__ == "__main__":
    if not all([DISCORD_TOKEN, TELEGRAM_TOKEN]):
        logger.error("❌ Не указаны необходимые переменные окружения (DISCORD_TOKEN, TELEGRAM_TOKEN).")
        exit(1)
    load_state()
    threading.Thread(target=start_telegram_bot, daemon=True).start()
    try:
        bot.run(DISCORD_TOKEN)
    finally:
        asyncio.run(close_http_session())