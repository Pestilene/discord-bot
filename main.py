import disnake
from disnake.ext import commands, tasks
import aiohttp
import feedparser
import logging
import streamlink
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
import json
import asyncio
import os
from dotenv import load_dotenv
from disnake.ui import Button, View

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

YOUTUBE_CHANNEL_ID = 1374412160939196476
TWITCH_CHANNEL_ID = 1374434150395940965
YOUTUBE_CHANNEL_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id=UCGCE6j2NovYuhXIMlCPhHnQ"
TWITCH_USERNAME = "xkamysh"
CHECK_INTERVAL_MINUTES = 5


logging.basicConfig(
	format='%(asctime)s [%(levelname)s] %(message)s',
	level=logging.INFO
)


intents = disnake.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


last_youtube_video_id = None
twitch_stream_live = False


try:
	with open("youtube_state.json", "r") as f:
		state = json.load(f)
		last_youtube_video_id = state.get("last_video_id")
except Exception:
	last_youtube_video_id = None

with open("youtube_state.json", "w") as f:
	json.dump({"last_video_id": last_youtube_video_id}, f)

def is_valid_url(url):
	try:
		result = urlparse(url) 
		if not all([result.scheme, result.netloc]):
			return False
		if not result.path.endswith(('.jpg', '.png', '.jpeg', '.gif')):
			return False
		return True
	except Exception:
		return False

async def fetch_youtube_rss():
	headers = {
		"User-Agent": "Mozilla/5.0"
	}
	async with aiohttp.ClientSession(headers=headers) as entsession:
		async with entsession.get(YOUTUBE_CHANNEL_RSS) as response:
			if response.status == 200:
				text = await response.text()
				return feedparser.parse(text)
	return None

def get_youtube_thumbnail(video_id):
    return f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"


async def get_latest_youtube_video(retry=3):
	global last_youtube_video_id
	for i in range(retry):
		try:
			feed = await fetch_youtube_rss()
			if not feed.entries:
				logging.warning(f"❌ Нет записей в YouTube RSS (попытка {i+1}/{retry})")
				await asyncio.sleep(10)
				continue
			
			entry = feed.entries[0]
			logging.info(f"Найдено видео: {entry.title}")

			url_parsed = urlparse(entry.link)
			video_id = parse_qs(url_parsed.query).get("v", [None])[0]

			if not video_id:
				logging.warning("❌ Не удалось извлечь video_id")
				return None

			title = entry.title
			link = entry.link

			if video_id != last_youtube_video_id:
				last_youtube_video_id = video_id
				return {"title": title, "link": link}
			return None
		except Exception as e:
			logging.error(f"[Ошибка YouTube] {e}")
			await asyncio.sleep(10)
	return None


async def is_twitch_stream_live():
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor() as pool:
            streams = await bot.loop.run_in_executor(pool, streamlink.streams, f"https://twitch.tv/{TWITCH_USERNAME}")
        return bool(streams)
    except Exception as e:
        logging.warning(f"[Ошибка проверки Twitch] {e}")
        return False

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def check_updates():
	global twitch_stream_live

	youtube_channel = bot.get_channel(YOUTUBE_CHANNEL_ID)
	twitch_channel = bot.get_channel(TWITCH_CHANNEL_ID)

	
	new_video = await get_latest_youtube_video()
	if new_video and youtube_channel:
		video_id = new_video["link"].split("v=")[-1]

		maxres_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
		hq_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
		thumbnail = maxres_url if is_valid_url(maxres_url) else hq_url

		embed = disnake.Embed(
			title=new_video["title"],
			url=new_video["link"],
			description="📽 Новое видео на канале!",
			color=disnake.Color.red()
		)

		embed.set_image(url=thumbnail)

		button = Button(label="📺 Перейти на канал", url="https://www.youtube.com/channel/UCGCE6j2NovYuhXIMlCPhHnQ", style=disnake.ButtonStyle.link)
		view = View()
		view.add_item(button)

		await youtube_channel.send(content="@everyone", embed=embed, view=view)
		logging.info(f"📢 Отправлено новое видео: {new_video['title']}")

	is_live = await is_twitch_stream_live()
	if is_live and not twitch_stream_live and twitch_channel:
		stream_url = f"https://twitch.tv/{TWITCH_USERNAME}"

		embed = disnake.Embed(
			title=f"🔴 {TWITCH_USERNAME} в эфире!",
			description="Залетай на стрим и пообщаемся 🎉",
			url=stream_url,
			color=disnake.Color.from_rgb(145, 70, 255)
		)

		embed.set_image(url="https://i.imgur.com/QZVjbl6.gif")	  #   GIF баннер

		button = Button(label="🔴 Смотреть стрим", url=stream_url, style=disnake.ButtonStyle.link)
		view = View()
		view.add_item(button)

		await twitch_channel.send(content="@everyone", embed=embed, view=view)
		logging.info(f"📢 Стрим в эфире: {TWITCH_USERNAME}")
		twitch_stream_live = True


@bot.event
async def on_ready():
	try:
		print(f"✅ Бот {bot.user} запущен!")
		check_updates.start()
	except Exception as e:
		logging.error(f"[Ошибка при запуске] {e}")


if __name__ == "__main__":
	bot.run(DISCORD_TOKEN)