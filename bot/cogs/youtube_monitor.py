import os
import glob
import json
import time
import asyncio
import logging
import yt_dlp
import google.generativeai as genai
import discord
from discord import app_commands
from discord.ext import tasks, commands
from sqlalchemy import select
from database.session import AsyncSessionLocal
from database.models import Channel


logger = logging.getLogger("QuantBot.YoutubeMonitor")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_FILE = os.path.join(BASE_DIR, "processed_videos.txt")

class YoutubeMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # 設定 API 與使用者 ID
        genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
        self.target_user_id = int(os.getenv("DISCORD_USER_ID", 0))
        
        # 讀取頻道清單
        self.MARKET_CHANNELS = [u.strip() for u in os.getenv("MARKET_CHANNELS", "").split(",") if u.strip()]
        self.STOCK_CHANNELS = [u.strip() for u in os.getenv("STOCK_CHANNELS", "").split(",") if u.strip()]
        self.ALL_CHANNELS = list(set(self.MARKET_CHANNELS + self.STOCK_CHANNELS))
        
        logger.info(f"🔍 載入頻道清單：共 {len(self.ALL_CHANNELS)} 個頻道準備監控")
        
        # 啟動定時巡邏任務
        self.check_youtube_task.start()

    # ================= 歷史紀錄 =================
    def load_history(self):
        if not os.path.exists(HISTORY_FILE): return set()
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f)

    def save_history(self, video_id):
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{video_id}\n")

    # ================= 開機自動觸發 =================
    @commands.Cog.listener()
    async def on_ready(self):
        """當機器人啟動並連線成功時，強制執行一次大掃除"""
        logger.info("🚀 機器人開機完成，準備執行首次 YouTube 頻道掃描...")
        # 稍微等 5 秒，確保其他模組都載入完畢
        await asyncio.sleep(5)
        # 為了不卡住 on_ready，我們把掃描任務丟到背景執行
        self.bot.loop.create_task(self.run_youtube_sweep(is_startup=True))

    # ================= 核心巡邏邏輯 =================
    async def run_youtube_sweep(self, is_startup=False):
        prefix = "[開機大掃除]" if is_startup else "[例行巡邏]"
        
        # 1. 從資料庫讀取所有啟用的頻道
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Channel).where(Channel.is_active == True))
            db_channels = result.scalars().all()
        
        if not db_channels:
            logger.info(f"🕵️‍♂️ {prefix} 資料庫中尚無監控頻道，跳過任務。")
            return

        logger.info(f"🕵️‍♂️ {prefix} 開始檢查 {len(db_channels)} 個頻道...")
        
        # 2. 獲取發送目標
        target_user = self.bot.get_user(self.target_user_id) or await self.bot.fetch_user(self.target_user_id)
        
        history = self.load_history()  
        
        for ch in db_channels:
            # 這裡的 ch.channel_url 就是資料庫存的網址
            vid, title, url = await asyncio.to_thread(self._sync_get_latest_video, ch.channel_url)
                        
            if vid and vid not in history:
                logger.info(f"⚡ {prefix} 發現新影片: {title}")
                
                audio_path = await asyncio.to_thread(self._sync_download_audio, url, vid)
                if audio_path:
                    json_data = await asyncio.to_thread(self._sync_upload_and_analyze, audio_path, title)
                    embed = self.create_discord_embed(json_data, url)
                    
                    try:
                        await target_user.send(embed=embed)
                        logger.info(f"✅ 已將報告私訊給 {target_user.name}")
                    except discord.Forbidden:
                        logger.error("❌ 無法私訊，請確認該使用者的隱私設定允許機器人傳訊。")
                        
                    self.save_history(vid)
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
            
            await asyncio.sleep(3) # 避免 API 請求過快
            
        logger.info(f"🏁 {prefix} 頻道檢查完畢！")

    # ================= 背景定時任務 =================
    @tasks.loop(minutes=30)
    async def check_youtube_task(self):
        """每 30 分鐘自動執行一次例行巡邏"""
        await self.run_youtube_sweep(is_startup=False)

    @check_youtube_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        # 因為我們已經有 on_ready 負責開機掃描了，定時任務啟動後先睡 30 分鐘再開始
        await asyncio.sleep(1800)

    @check_youtube_task.error
    async def check_youtube_task_error(self, error):
        """終極防護網：如果背景任務崩潰，印出錯誤並自動重啟"""
        logger.error(f"🚨 YouTube 背景任務發生致命錯誤: {error}")
        await asyncio.sleep(60) 
        self.check_youtube_task.restart()

    # ================= yt-dlp 與 Gemini 的同步函數 (保留你原本寫好的) =================
    def _sync_get_latest_video(self, channel_url):
        ydl_opts = {'extract_flat': True, 'playlistend': 3, 'quiet': True, 'no_warnings': True}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                if 'entries' in info and info['entries']:
                    for entry in info['entries']:
                        if not entry: continue
                        v_id = entry.get('id')
                        v_title = entry.get('title')
                        if v_id and not v_id.startswith('UC') and v_title:
                            return v_id, v_title, f"https://www.youtube.com/watch?v={v_id}"
        except Exception as e:
            logger.error(f"讀取頻道失敗: {e}")
        return None, None, None

    def _sync_download_audio(self, url, video_id):
        expected_filename = f"temp_{video_id}"
        base_path = os.path.join(BASE_DIR, expected_filename)
        existing_files = glob.glob(f"{base_path}.*")
        if existing_files: return existing_files[0]

        logger.info(f"📥 下載音訊: {url}")
        ydl_opts = {'format': 'bestaudio/best', 'outtmpl': f'{base_path}.%(ext)s', 'quiet': True, 'no_warnings': True}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
            downloaded_files = glob.glob(f"{base_path}.*")
            if downloaded_files: return downloaded_files[0]
        except Exception as e: logger.error(f"❌ 下載失敗: {e}")
        return None

    def _sync_upload_and_analyze(self, audio_path, title):
        ext = audio_path.split('.')[-1].lower()
        mime_map = {'webm': 'audio/webm', 'mp3': 'audio/mp3'}
        mime = mime_map.get(ext, 'audio/mp4')
        try:
            myfile = genai.upload_file(audio_path, mime_type=mime)
            while myfile.state.name == "PROCESSING":
                time.sleep(3)
                myfile = genai.get_file(myfile.name)
            if myfile.state.name == "FAILED": raise ValueError("Gemini 失敗")

            model = genai.GenerativeModel("gemini-2.5-flash")
            prompt = f"""你是一位專業操盤手。請聽這段語音分析「{title}」。判斷影片是「個股推薦」還是「市場趨勢解析」，並回傳對應的 JSON。
【規則】只回傳 JSON，不要 markdown。
個股格式: {{ "type": "stock", "title": "🎯 標題", "summary": "一句話總結", "advice": "操作建議", "data": [ {{ "name": "名稱", "code": "代號", "action": "買/賣/觀望", "reason": "原因" }} ] }}
市場格式: {{ "type": "market", "title": "📊 觀點", "summary": "一句話總結", "advice": "操作建議", "data": [ {{ "key": "重點標題", "value": "描述" }} ] }}"""
            result = model.generate_content([myfile, prompt])
            raw_text = result.text.replace("```json", "").replace("```", "").strip()
            return json.loads(raw_text)
        except Exception as e:
            return {"type": "market", "title": "分析失敗", "summary": "AI 解析異常", "data": [{"key": "錯誤", "value": str(e)}], "advice": "建議直接觀看影片。"}

    def create_discord_embed(self, json_data, video_url):
        data_type = json_data.get("type", "market")
        color = discord.Color.red() if data_type == "stock" else discord.Color.blue()
        embed = discord.Embed(title=json_data.get("title", "報告"), url=video_url, description=f"📝 **{json_data.get('summary', '無')}**", color=color)

        if data_type == "stock" and json_data.get("data"):
            for item in json_data["data"]:
                act = item.get('action', '')
                emoji = "🔴" if "買" in act else "🟢" if "賣" in act else "🟡"
                embed.add_field(name=f"{emoji} {item.get('name')} ({item.get('code', '')})", value=f"**策略:** {act}\n**理由:** {item.get('reason')}", inline=False)
        elif data_type == "market" and json_data.get("data"):
            for item in json_data["data"]:
                embed.add_field(name=f"📌 {item.get('key')}", value=item.get('value'), inline=False)
        else:
            embed.add_field(name="狀態", value="本集無提及特定標的", inline=False)
            
        embed.set_footer(text=f"🛡️ 建議: {json_data.get('advice', '')}")
        return embed

    @app_commands.command(name="add_channel", description="新增 YouTube 監控頻道")
    @app_commands.describe(
        url="YouTube 頻道網址",
        category="分類 (market: 市場宏觀 / stock: 個股解析)",
        name="頻道名稱 (選填)"
    )
    async def add_channel(self, interaction: discord.Interaction, url: str, category: str, name: str = None):
        await interaction.response.defer()
        
        # 統一轉為大寫方便判斷
        cat = category.upper()
        if cat not in ["MARKET", "STOCK"]:
            await interaction.followup.send("❌ 分類請輸入 `market` 或 `stock`。")
            return

        async with AsyncSessionLocal() as session:
            # 檢查是否已存在
            result = await session.execute(select(Channel).where(Channel.channel_url == url))
            existing = result.scalar_one_or_none()
            
            if existing:
                await interaction.followup.send(f"⚠️ 頻道 `{url}` 已經在監控清單中囉！")
                return

            new_channel = Channel(
                channel_url=url,
                channel_name=name or "未命名頻道",
                category=cat
            )
            session.add(new_channel)
            await session.commit()
            
            await interaction.followup.send(f"✅ 成功新增監控頻道：**{name or url}** [{cat}]")
        
async def setup(bot):
    await bot.add_cog(YoutubeMonitor(bot))