import os
import re
import logging
import tempfile
import asyncio
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, FSInputFile, InputMediaPhoto, InputMediaAudio
from aiogram.filters import Command
from typing import Optional, Tuple, List
from PIL import Image
import io
from aiogram.exceptions import TelegramBadRequest
from aiohttp import web

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
API_TOKEN = os.getenv('API_TOKEN')
if not API_TOKEN:
    raise ValueError("Не указан API_TOKEN в переменных окружения")

# Конфигурация
SERVICE_API = "https://www.tikwm.com/api/"
MAX_FILE_SIZE = 45 * 1024 * 1024  # 45 MB
MAX_PHOTOS_PER_GROUP = 10

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Регулярное выражение для TikTok
TIKTOK_URL_PATTERN = r'https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/(?:@[\w.-]+/video/|t/|[\w.-]+/video/|embed/v2/)?\d+(?:/\S+)?|https?://(?:vm|vt)\.tiktok\.com/\S+'

async def download_tiktok_media(url: str) -> Optional[Tuple[List[bytes], Optional[bytes], bool]]:
    """Скачивает медиа через API и определяет тип контента"""
    try:
        async with httpx.AsyncClient() as client:
            payload = {"url": url, "hd": 1}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "application/json"
            }
            
            response = await client.post(SERVICE_API, json=payload, headers=headers, timeout=30.0)
            
            if response.status_code != 200:
                logger.error(f"API Error: {response.status_code} - {response.text}")
                return None
                
            data = response.json()
            
            if data.get("code") != 0:
                logger.error(f"API Error: {data.get('msg')}")
                return None
                
            is_photo_album = False
            media_contents = []
            audio_data = None
            
            # Скачиваем аудио, если есть
            if "music" in data["data"] and "play_url" in data["data"]["music"]:
                try:
                    audio_url = data["data"]["music"]["play_url"]
                    async with client.stream('GET', audio_url) as response:
                        if response.status_code == 200:
                            chunks = []
                            async for chunk in response.aiter_bytes():
                                chunks.append(chunk)
                            audio_data = b''.join(chunks)
                except Exception as e:
                    logger.error(f"Error downloading audio: {e}")
            
            if "images" in data["data"]:
                is_photo_album = True
                for image_url in data["data"]["images"]:
                    try:
                        async with client.stream('GET', image_url) as response:
                            if response.status_code != 200:
                                continue
                            chunks = []
                            async for chunk in response.aiter_bytes():
                                chunks.append(chunk)
                            image_data = b''.join(chunks)
                            
                            if len(image_data) > 10 * 1024 * 1024:
                                with Image.open(io.BytesIO(image_data)) as img:
                                    quality = 85
                                    while True:
                                        buffer = io.BytesIO()
                                        img.save(buffer, format='JPEG', quality=quality)
                                        if buffer.tell() < 10 * 1024 * 1024 or quality <= 50:
                                            image_data = buffer.getvalue()
                                            break
                                        quality -= 5
                            
                            media_contents.append(image_data)
                    except Exception as e:
                        logger.error(f"Error downloading image: {e}")
                        continue
            else:
                video_url = data["data"].get("play", "")
                if not video_url:
                    return None
                    
                try:
                    async with client.stream('GET', video_url) as response:
                        if response.status_code != 200:
                            return None
                        chunks = []
                        async for chunk in response.aiter_bytes():
                            chunks.append(chunk)
                        video_data = b''.join(chunks)
                        
                        if len(video_data) > MAX_FILE_SIZE:
                            return None
                            
                        media_contents.append(video_data)
                except Exception as e:
                    logger.error(f"Error downloading video: {e}")
                    return None
                
            return media_contents, audio_data, is_photo_album
            
    except Exception as e:
        logger.error(f"Error downloading media: {e}")
        return None

async def generate_caption(sender_name: str, sender_username: str) -> str:
    """Генерирует подпись с информацией об отправителе"""
    if sender_username:
        return f'<b>👤 Отправитель: </b><a href="https://t.me/{sender_username}"><b>{sender_name}</b></a>\n<b>🔗 Via @tiktokgassaverbot</b>'
    else:
        return f'<b>👤 Отправитель: {sender_name}\n🔗 Via @tiktokgassaverbot</b>'

async def send_photo_album(chat_id: int, photos: List[bytes], sender_name: str, sender_username: str):
    """Отправляет фотоальбом с кликабельным именем отправителя"""
    temp_files = []
    
    try:
        # Сохраняем все фото во временные файлы
        for photo_data in photos:
            try:
                with Image.open(io.BytesIO(photo_data)) as img:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_file:
                        img.save(temp_file, format='PNG', optimize=True)
                        temp_path = temp_file.name
                        temp_files.append(temp_path)
                    
                    file_size = os.path.getsize(temp_path)
                    if file_size > 10 * 1024 * 1024:
                        quality = 85
                        while True:
                            buffer = io.BytesIO()
                            img.save(buffer, format='JPEG', quality=quality)
                            if buffer.tell() < 10 * 1024 * 1024 or quality <= 50:
                                with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as new_temp_file:
                                    new_temp_file.write(buffer.getvalue())
                                    new_temp_path = new_temp_file.name
                                    temp_files.append(new_temp_path)
                                os.unlink(temp_path)
                                temp_files.remove(temp_path)
                                temp_path = new_temp_path
                                break
                            quality -= 5
            except Exception as e:
                logger.error(f"Error processing photo: {e}")
                continue
        
        if not temp_files:
            return False
        
        # Формируем подпись
        caption = await generate_caption(sender_name, sender_username)
        
        # Разбиваем фото на группы
        photo_groups = [temp_files[i:i + MAX_PHOTOS_PER_GROUP] 
                       for i in range(0, len(temp_files), MAX_PHOTOS_PER_GROUP)]
        
        # Отправляем каждую группу с подписью
        for group in photo_groups:
            media_group = []
            for i, temp_path in enumerate(group):
                current_caption = caption if i == 0 else None
                media_group.append(
                    InputMediaPhoto(
                        media=FSInputFile(temp_path),
                        caption=current_caption,
                        parse_mode="HTML"
                    )
                )
            
            try:
                await bot.send_media_group(chat_id=chat_id, media=media_group)
            except TelegramBadRequest as e:
                logger.error(f"Telegram API error: {e}")
                for temp_path in group:
                    try:
                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=FSInputFile(temp_path),
                            caption=caption if group.index(temp_path) == 0 else None,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Error sending single photo: {e}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error in send_photo_album: {e}")
        return False
    finally:
        for temp_path in temp_files:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception as e:
                logger.error(f"Error deleting temp file: {e}")

async def send_audio(chat_id: int, audio_data: bytes, sender_name: str, sender_username: str):
    """Отправляет аудиофайл с подписью"""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as temp_file:
            temp_file.write(audio_data)
            temp_path = temp_file.name
        
        caption = await generate_caption(sender_name, sender_username)
        
        await bot.send_audio(
            chat_id=chat_id,
            audio=FSInputFile(temp_path),
            caption=caption,
            parse_mode="HTML"
        )
        return True
    except Exception as e:
        logger.error(f"Error sending audio: {e}")
        return False
    finally:
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.unlink(temp_path)

@dp.message(Command("start"))
async def start_command(message: Message):
    """Обработчик команды /start"""
    await message.answer(
        "🎵 <b>Привет! Я TikTok Saver Bot</b> 🎵\n\n"
        "📲 Просто отправь мне ссылку на видео или фотоальбом из TikTok\n\n"
        "✅ Скачиваю без водяных знаков\n"
        "🖼️ Фотоальбомы сохраняю в PNG\n"
        "🎵 Отправляю аудио из видео\n"
        "📸 Альбомы отправляются с подписями\n"
        "⚡ Быстро и просто!",
        parse_mode="HTML"
    )

@dp.message(F.text.regexp(TIKTOK_URL_PATTERN))
async def handle_tiktok_link(message: Message):
    """Обработчик ссылок на TikTok"""
    processing_msg = await message.reply("🔍 <b>Начинаю загрузку контента...</b>", parse_mode="HTML")
    
    try:
        urls = re.findall(TIKTOK_URL_PATTERN, message.text)
        if not urls:
            await processing_msg.edit_text("❌ Неверный формат ссылки")
            return

        url = urls[0]
        result = await download_tiktok_media(url)

        if not result:
            await processing_msg.edit_text("❌ Не удалось загрузить контент. Попробуйте другую ссылку")
            return

        media_data, audio_data, is_photo_album = result

        # Получаем данные отправителя
        sender_name = message.from_user.full_name
        sender_username = message.from_user.username

        # Отправляем аудио, если есть
        if audio_data:
            await send_audio(
                message.chat.id,
                audio_data,
                sender_name,
                sender_username
            )

        if is_photo_album:
            if not media_data:
                await processing_msg.edit_text("❌ Не удалось загрузить изображения из альбома")
                return
                
            success = await send_photo_album(
                message.chat.id, 
                media_data, 
                sender_name,
                sender_username
            )
            if not success:
                await processing_msg.edit_text("❌ Не удалось отправить фотоальбом")
        else:
            if not media_data or len(media_data[0]) > MAX_FILE_SIZE:
                await processing_msg.edit_text("⚠️ Видео слишком большое для отправки через бота. Максимальный размер 50 МБ.")
                return
                
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_file:
                temp_file.write(media_data[0])
                temp_path = temp_file.name

            caption = await generate_caption(sender_name, sender_username)

            try:
                await message.reply_video(
                    video=FSInputFile(temp_path),
                    caption=caption,
                    parse_mode="HTML",
                    supports_streaming=True,
                    width=1080,
                    height=1920
                )
            except TelegramBadRequest as e:
                await message.reply("⚠️ Видео слишком большое для отправки через бота. Максимальный размер 50 МБ.")
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

        await processing_msg.delete()

    except Exception as e:
        logger.error(f"Error: {str(e)}")
        await processing_msg.edit_text("⚠️ Ошибка при обработке. Попробуйте позже")
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.unlink(temp_path)

async def start_server():
    """HTTP-сервер для Keep-Alive на Render"""
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()
    logger.info("HTTP server started on port 8080")

async def main():
    """Основная функция запуска бота и сервера"""
    await asyncio.gather(
        dp.start_polling(bot),
        start_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
