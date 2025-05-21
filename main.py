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
from donenv import load_dotenv

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
            streams = await bot.loop.run_in_executor(pool, streamlink.streams, f"https://twitch.tv/ {TWITCH_USERNAME}")
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
		thumbnail = get_youtube_thumbnail(video_id)

		if not is_valid_url(thumbnail):
			logging.warning("❌ Некорректный URL для миниатюры")
			thumbnail = disnake.Embed.Empty

		embed = disnake.Embed(
			title="🔔 **Новое видео на YouTube!**",
			url=new_video["link"],
			description="🔥 СМОТРИТЕ ПРЯМО СЕЙЧАС 🔥",
			color=disnake.Color.gold()
		)

		embed.set_image(url=thumbnail)
		embed.set_author(
			name="YouTube",
			icon_url="https://upload.wikimedia.org/wikipedia/commons/7/75/YouTube_icon_%282013_2017%29.svg"
		)

		embed.add_field(
			name="🎥 Заголовок видео:",
			value=f"**{new_video['title']}**",
			inline=False
		)

		embed.add_field(
			name="🔗 Ссылка:",
			value=f"[Перейти к видео]({new_video['link']})",
			inline=False
		)

		
		embed.set_footer(text="📌 Это автоматическое уведомление")

		await youtube_channel.send("@everyone 🚨 Новое видео доступно!")
		logging.info(f"📢 Отправлено уведомление о новом видео: {new_video['title']}")
		last_youtube_video_id = video_id

	is_live = await is_twitch_stream_live()
	if is_live and not twitch_stream_live and twitch_channel:
		stream_url = f"https://twitch.tv/ {TWITCH_USERNAME}"

		embed = disnake.Embed(
			title="🔴 **СТРИМ НАЧАЛСЯ!** 🔴",
			url=stream_url,
			description="🟢 БЫСТРО ПРИСОЕДИНЯЙТЕСЬ К ЭФИРУ! 🟢",
			color=disnake.Color.red()
		)

		embed.set_author(
			name="Twitch",
			icon_url="https://static-cdn.jtvnw.net/jtv_user_pictures/a500227c-ea24-448f-aa21-911ee63bfa53-profile_image-70x70.png "
		)

		embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/3366/3366069.png ")
		embed.set_image(url="https://i.imgur.com/QZVjbl6.gif ")

		embed.add_field(
			name="🎮 Стримит пользователь:",
			value=f"**{TWITCH_USERNAME}**",
			inline=False
		)

		embed.add_field(
			name="🔗 Прямая ссылка:",
			value=f"_Just Chatting / Играет в игру_",
			inline=True
		)

		embed.add_field(
			name="🎙️ Тема стрима:",
			value="_Just Chatting / Играет в игру_",
			inline=True
		)

		embed.set_image(url="https://i.imgur.com/QZVjbl6.gif ")

		embed.set_footer(text="Автоматическое уведомление • Присоединяйтесь!")

		await twitch_channel.send("@everyone 🚨 В ЭФИРЕ СТРИМ!", embed=embed)
		logging.info(f"📢 {TWITCH_USERNAME} начал стрим!")
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