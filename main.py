import os
import json 
import asyncio
import logging
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


load_dotenv()


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 8000))

YOUTUBE_CHANNEL_ID = 1374412160939196476
TWITCH_CHANNEL_ID = 1374434150395940965
YOUTUBE_CHANNEL_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id=UCGCE6j2NovYuhXIMlCPhHnQ"
TWITCH_USERNAME = "xKamysh"
CHECK_INTERVAL_MINUTES = 5


logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)


intents = disnake.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


STATE_FILE = "youtube_state.json"


last_youtube_video_id = None
last_video_title = None
twitch_stream_live = False


def load_state():
	global last_youtube_video_id, last_video_title
	try:
		with open(STATE_FILE, "r") as f:
			state = json.load(f)
			last_youtube_video_id = state.get("last_video_id")
			last_video_title = state.get("last_video_title")
			logging.info(f"Состояние загружено: {last_youtube_video_id}, {last_video_title}")
	except (FileNotFoundError, json.JSONDecodeError):
		last_youtube_video_id = None
		last_video_title = None
		logging.info("Файл состояния не найден или повреждён, состояние сброшено.")

def save_state():
	global last_youtube_video_id, last_video_title
	try:
		with open(STATE_FILE, "w") as f:
			json.dump({
				"last_video_id": last_youtube_video_id, 
				"last_video_title": last_video_title
			}, f)
		logging.info(f"Состояние сохранено: {last_youtube_video_id}, {last_video_title}")
	except Exception as e:
		logging.error(f"Ошибка сохранения состояния: {e}")


load_state()


async def is_image_available(url):
	try:
		async with aiohttp.ClientSession() as session:
			async with session.head(url) as resp:
				return resp.status == 200
	except Exception as e:
		logging.error(f"[Ошибка изображения] {e}")
		return False

async def fetch_youtube_rss():
	headers = {"User-Agent": "Mozilla/5.0"}
	async with aiohttp.ClientSession(headers=headers) as entsession:
		async with entsession.get(YOUTUBE_CHANNEL_RSS) as response:
			if response.status == 200:
				text = await response.text()
				return feedparser.parse(text)
	return None

def extract_video_id(link: str) -> str | None:
	parsed = urlparse(link)
	return parse_qs(parsed.query).get("v", [None])[0]


async def get_latest_youtube_video(retry=3):
	global last_youtube_video_id, last_video_title
	for attempt in range(retry):
		try:
			feed = await fetch_youtube_rss()
			if not feed or not feed.entries:
				logging.warning(f"❌ Нет записей в YouTube RSS (попытка {attempt+1}/{retry})")
				await asyncio.sleep(10)
				continue
			
			entry = feed.entries[0]
			video_id = extract_video_id(entry.link)
			if not video_id:
				logging.warning("❌ Не удалось извлечь video_id")
				return None

			title = entry.title
			link = entry.link

			if video_id != last_youtube_video_id:
				last_youtube_video_id = video_id
				last_video_title = title
				save_state()
				return {"title": title, "link": link}
			else:
				if not last_video_title:
					last_video_title = title
			return None
		except Exception as e:
			logging.error(f"[Ошибка YouTube] {e}")
			await asyncio.sleep(10)
	return None


async def is_twitch_stream_live() -> bool:
	try:
		loop = asyncio.get_running_loop()
		with ThreadPoolExecutor() as pool:
			streams = await loop.run_in_executor(pool, streamlink.streams, f"https://twitch.tv/{TWITCH_USERNAME}")
		return bool(streams)
	except Exception as e:
		logging.warning(f"[Ошибка проверки Twitch] {e}")
		return False


def create_social_buttons() -> View:
	view = View()
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
	video_id = extract_video_id(video["link"])
	thumbnail = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
	if not await is_image_available(thumbnail):
		thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

	embed = disnake.Embed(
		title=f"✨ Новое видео: {video['title']}",
		url=video["link"],
		description="🔔 На канале вышел свежий ролик — зацени первым!",
		color=disnake.Color.from_rgb(229, 57, 53)
	)
	embed.set_image(url=thumbnail)
	await channel.send(content="@everyone", embed=embed, view=create_social_buttons())
	logging.info(f"📢 Отправлено новое видео: {video['title']}")


async def send_twitch_notification(channel):
	global twitch_stream_live
	stream_url = f"https://twitch.tv/{TWITCH_USERNAME}"

	embed = disnake.Embed(
		title=f"🔴 {TWITCH_USERNAME} сейчас стримит!",
		url=stream_url,
		description="🎉 Заходи на эфир! Общение, атмосфера и веселье ждут тебя!",
		color=disnake.Color.from_rgb(138, 43, 226)
	)
	embed.set_image(url="https://i.imgur.com/QZVjbl6.gif")
	embed.set_footer(text="Twitch • Камыш", icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png")
	await channel.send(content="@everyone", embed=embed, view=create_social_buttons())
	logging.info(f"📢 Стрим в эфире: {TWITCH_USERNAME}")	
	twitch_stream_live = True


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_updates():
	global twitch_stream_live
	yt_channel = bot.get_channel(YOUTUBE_CHANNEL_ID)
	tw_channel = bot.get_channel(TWITCH_CHANNEL_ID)

	if yt_channel:
		new_video = await get_latest_youtube_video()
		if new_video:
			await send_youtube_notification(yt_channel, new_video)

	is_live = await is_twitch_stream_live()
	if tw_channel:
		if is_live and not twitch_stream_live:
			await send_twitch_notification(tw_channel)
		elif not is_live
			twitch_stream_live = False

	await update_presence(is_live)



@bot.command(name="проверка")
async def manual_check(ctx):
	global twitch_stream_live
	await ctx.send("🔍 Выполняю ручную проверку обновлений...")

	yt_channel = bot.get_channel(YOUTUBE_CHANNEL_ID)
	tw_channel = bot.get_channel(TWITCH_CHANNEL_ID)

	if yt_channel is None or tw_channel is None:
		await ctx.send("❌ Не могу получить нужные каналы для уведомлений.")
		return

	new_video = await get_latest_youtube_video()
	if new_video and yt_channel:
		await send_youtube_notification(yt_channel, new_video)
		await ctx.send("✅ Видео найдено и отправлено.")
	else:
		await ctx.send("ℹ️ Новых видео не найдено.")

	is_live = await is_twitch_stream_live()
	if is_live and not twitch_stream_live and tw_channel:
		await send_twitch_notification(tw_channel)
		await ctx.send("📡 Стрим в эфире! Уведомление отправлено.")
	elif is_live:
		await ctx.send("📡 Стрим уже идёт.")
	else:
		await ctx.send("📴 Стрим сейчас не идёт.")


@bot.event
async def on_ready():
	logging.info(f"✅ Бот {bot.user} запущен!")
	if not check_updates.is_running():
		check_updates.start()
	bot.loop.create_task(run_webserver())


async def update_presence(is_live: bool):
	activity = None
	if is_live:
		activity = disnake.Activity(type=disnake.ActivityType.watching, name="xKamysh")
	await bot.change_presence(activity=activity)


async def handle(request):
	return web.Response(text="ОК")


async def run_webserver():
	app = web.Application()
	app.add_routes([web.get('/', handle)])
	runner = web.AppRunner(app)
	await runner.setup()
	site = web.TCPSite(runner, '0.0.0.0', PORT)
	await site.start()
	logging.info(f"🌐 HTTP сервер запущен на порту {PORT}")


if __name__ == "__main__":
	if not DISCORD_TOKEN:
		logging.error("❌ Переменная окружения DISCORD_TOKEN не найдена.")
		exit(1)
	bot.run(DISCORD_TOKEN)