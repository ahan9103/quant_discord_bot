import os
import glob
import json
import time
import asyncio
import logging
import yt_dlp
import google.generativeai as genai
import discord
from discord.ext import tasks, commands

# ================= 1. 設定與初始化 =================
logger = logging.getLogger("QuantBot.YoutubeMonitor")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_FILE = os.path.join(BASE_DIR, "processed_videos.txt")


class YoutubeMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # 設定 API
        genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
        self.target_user_id = int(os.getenv("DISCORD_USER_ID", 0))

        # 讀取頻道
        self.MARKET_CHANNELS = [u.strip() for u in os.getenv("MARKET_CHANNELS", "").split(",") if u.strip()]
        self.STOCK_CHANNELS = [u.strip() for u in os.getenv("STOCK_CHANNELS", "").split(",") if u.strip()]

        # 啟動巡邏任務
        self.check_youtube_task.start()

    # ================= 2. 歷史紀錄系統 =================
    def load_history(self):
        if not os.path.exists(HISTORY_FILE): return set()
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f)

    def save_history(self, video_id):
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{video_id}\n")

    # ================= 3. 背景耗時處理 (yt-dlp & Gemini) =================
    def _sync_get_latest_video(self, channel_url):
        """[同步] 獲取最新影片 ID"""
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
        """[同步] 智慧下載影片音訊"""
        expected_filename = f"temp_{video_id}"
        base_path = os.path.join(BASE_DIR, expected_filename)

        # 清除舊的殘留檔
        existing_files = glob.glob(f"{base_path}.*")
        if existing_files:
            return existing_files[0]

        logger.info(f"📥 開始下載音訊: {url}")
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f'{base_path}.%(ext)s',
            'quiet': True,
            'no_warnings': True
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # 抓出實際下載的檔案名稱 (因為副檔名可能不同)
            downloaded_files = glob.glob(f"{base_path}.*")
            if downloaded_files:
                return downloaded_files[0]
        except Exception as e:
            logger.error(f"❌ 下載失敗: {e}")
        return None

    def _sync_upload_and_analyze(self, audio_path, title):
        """[同步] 上傳至 Gemini 並強制回傳 JSON"""
        # 動態判斷 MIME 格式
        ext = audio_path.split('.')[-1].lower()
        mime_map = {'webm': 'audio/webm', 'mp3': 'audio/mp3'}
        mime = mime_map.get(ext, 'audio/mp4')

        logger.info(f"☁️ 上傳音檔至 Gemini ({ext})...")
        try:
            # 1. 上傳並等待處理完成
            myfile = genai.upload_file(audio_path, mime_type=mime)
            while myfile.state.name == "PROCESSING":
                time.sleep(3)
                myfile = genai.get_file(myfile.name)

            if myfile.state.name == "FAILED":
                raise ValueError("Gemini 處理檔案失敗")

            logger.info("🧠 檔案處理完畢，開始生成分析報告...")

            # 2. 進行 AI 分析
            model = genai.GenerativeModel("gemini-1.5-flash-latest")
            prompt = f"""
            你是一位專業操盤手。請聽這段語音分析「{title}」。
            判斷影片是「個股推薦」還是「市場趨勢解析」，並回傳對應的 JSON。
            【規則】只回傳 JSON，不要 markdown。

            如果是個股：
            {{ "type": "stock", "title": "🎯 標題重點", "summary": "一句話總結", "advice": "操作建議", "data": [ {{ "name": "股票名稱", "code": "代號", "action": "買進/賣出/觀望", "reason": "原因" }} ] }}

            如果是市場：
            {{ "type": "market", "title": "📊 市場觀點", "summary": "一句話總結", "advice": "操作建議", "data": [ {{ "key": "重點標題", "value": "詳細描述" }} ] }}
            """

            result = model.generate_content([myfile, prompt])

            # 3. 清理與解析
            raw_text = result.text.replace("```json", "").replace("```", "").strip()
            return json.loads(raw_text)

        except Exception as e:
            logger.error(f"❌ AI 解析失敗: {e}")
            return {
                "type": "market", "title": "分析失敗",
                "summary": "AI 數據解析異常，請稍後再試。",
                "data": [{"key": "錯誤", "value": str(e)}], "advice": "建議直接觀看影片。"
            }

    # ================= 4. Discord 卡片生成 =================
    def create_discord_embed(self, json_data, video_url):
        data_type = json_data.get("type", "market")
        title = json_data.get("title", "投資分析報告")
        summary = json_data.get("summary", "無摘要")
        advice = json_data.get("advice", "無建議")

        color = discord.Color.red() if data_type == "stock" else discord.Color.blue()
        embed = discord.Embed(title=title, url=video_url, description=f"📝 **{summary}**", color=color)

        if data_type == "stock" and json_data.get("data"):
            for item in json_data["data"]:
                act = item.get('action', '')
                emoji = "🔴" if "買" in act else "🟢" if "賣" in act else "🟡"
                embed.add_field(
                    name=f"{emoji} {item.get('name')} ({item.get('code', '無')})",
                    value=f"**動作:** {act}\n**理由:** {item.get('reason')}",
                    inline=False
                )
        elif data_type == "market" and json_data.get("data"):
            for item in json_data["data"]:
                embed.add_field(name=f"📌 {item.get('key')}", value=item.get('value'), inline=False)
        else:
            embed.add_field(name="狀態", value="本集無提及特定標的或重點", inline=False)

        embed.set_footer(text=f"🛡️ 建議: {advice}")
        return embed

    # ================= 5. 定時任務迴圈 =================
    @tasks.loop(minutes=30)
    async def check_youtube_task(self):
        logger.info("⏰ 啟動 YouTube 頻道巡邏 (含音檔分析)...")
        history = self.load_history()

        # 1. 抓取你的 Discord 帳號 (如果快取找不到，就強制從 API 抓取)
        target_user = self.bot.get_user(self.target_user_id)
        if not target_user:
            try:
                target_user = await self.bot.fetch_user(self.target_user_id)
            except Exception as e:
                logger.error(f"❌ 找不到 Discord 用戶 ID ({self.target_user_id}): {e}")
                return

        all_channels = list(set(self.MARKET_CHANNELS + self.STOCK_CHANNELS))

        for channel_url in all_channels:
            vid, title, url = await asyncio.to_thread(self._sync_get_latest_video, channel_url)

            if vid and vid not in history:
                logger.info(f"⚡ 發現新片: {title}")

                audio_path = await asyncio.to_thread(self._sync_download_audio, url, vid)

                if audio_path:
                    json_data = await asyncio.to_thread(self._sync_upload_and_analyze, audio_path, title)
                    embed = self.create_discord_embed(json_data, url)

                    # 2. 改成發送私訊給 target_user
                    try:
                        await target_user.send(embed=embed)
                        logger.info(f"✅ 已私訊發送報告給 {target_user.name}")
                    except discord.Forbidden:
                        logger.error("❌ 無法發送私訊，請檢查你的 Discord 隱私設定是否允許機器人私訊你！")

                    self.save_history(vid)
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                        logger.info(f"🗑️ 已刪除本地暫存檔: {audio_path}")

            await asyncio.sleep(3)

    @check_youtube_task.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(YoutubeMonitor(bot))