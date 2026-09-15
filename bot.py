import os
import io
import discord
from discord.ext import commands
from google import genai
from PIL import Image

# 從環境變數讀取金鑰與 Token
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not GEMINI_API_KEY or not DISCORD_TOKEN:
    raise ValueError("⚠️ 找不到 GEMINI_API_KEY 或 DISCORD_TOKEN，請檢查環境變數設定！")

# 初始化 Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 初始化 Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"🤖 機器人已順利上線：{bot.user.name}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.attachments:
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                await message.channel.send("🔍 正在辨識圖片中的掉落寶物...")
                
                try:
                    image_bytes = await attachment.read()
                    image = Image.open(io.BytesIO(image_bytes))

                    prompt = """
                    你是一個遊戲掉落紀錄助手。請讀取這張圖片中的文字（包含繁體中文與英文），
                    只提取寶物名稱與數量。
                    請直接回傳 JSON 格式，格式範例如下：
                    [{"item": "寶物名稱", "amount": 1}]
                    如果圖片中沒有寶物資訊，請回傳空陣列 []。不要包含任何 Markdown 標記或額外說明。
                    """

                    response = gemini_client.models.generate_content(
                        model='gemini-1.5-flash',
                        contents=[image, prompt]
                    )
                    
                    result_text = response.text.strip()
                    await message.channel.send(f"**辨識結果：**\n```{result_text}```")

                except Exception as e:
                    await message.channel.send(f"❌ 辨識失敗，錯誤原因：{e}")

    await bot.process_commands(message)

bot.run(DISCORD_TOKEN)
