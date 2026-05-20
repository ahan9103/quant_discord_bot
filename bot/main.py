# bot/main.py


import discord
from discord.ext import commands
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import asyncio
from dotenv import load_dotenv
from data_sources.shioaji_client import sj_manager
from database.session import AsyncSessionLocal
from database.models import Watchlist
from sqlalchemy import select

# 引入非同步排程器
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from data_sources.shioaji_client import sj_manager


from database.session import init_db

# 引入我們剛剛寫好的同步任務 (假設您放在 tasks 資料夾)
from tasks.sync_data import sync_us_symbols, sync_tw_symbols

# from tasks.sync_data import sync_tw_symbols # 等您補上台股的部分後取消註解

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("QuantBot")

import os
import logging
from logging.handlers import RotatingFileHandler


# ================= 1. 全局 Log 系統設定 =================
def setup_global_logger():
    # 設定 log 檔案的存放位置 (放在專案根目錄的 bot.log)
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot.log")

    # 建立一個 Logger
    logger = logging.getLogger()  # 抓取 Root Logger，這樣所有模組的 log 都會被捕捉
    logger.setLevel(logging.INFO)

    # 格式化：[時間] - [級別] - [模組] - [訊息]
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s')

    # 1. 寫入檔案的 Handler (最大 5MB，保留 3 份舊檔，避免硬碟塞爆)
    file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(formatter)

    # 2. 顯示在終端機的 Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 把 Handler 裝上去
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# 啟動 Logger
logger = setup_global_logger()
logger.info("系統啟動：全局 Log 系統已掛載！")


class QuantBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

        # 建立排程器實例
        self.scheduler = AsyncIOScheduler()

    async def alert_monitor_task(self):
        await self.wait_until_ready()
        logger.info("警報監聽引擎已啟動，等待行情推播...")

        while not self.is_closed():
            try:
                # 這裡會卡住(非阻塞)，直到 Queue 裡面有新的報價進來
                quote_data = await sj_manager.quote_queue.get()

                symbol = f"{quote_data['symbol']}.TW"
                current_price = quote_data['price']

                # 開啟資料庫，找出所有有追蹤這檔股票，且有設定條件的使用者
                async with AsyncSessionLocal() as session:
                    stmt = select(Watchlist).where(Watchlist.symbol == symbol)
                    result = await session.execute(stmt)
                    watchlists = result.scalars().all()

                    for w in watchlists:
                        # 檢查是否觸發停利
                        if w.target_price and current_price >= w.target_price:
                            user = self.get_user(w.user_id)
                            if user:
                                await user.send(
                                    f"**停利警報**：您追蹤的 {symbol} 已達到目標價 **{w.target_price}**！現價：{current_price}")
                                # 觸發後可以選擇清除條件，避免狂發訊息
                                w.target_price = None

                                # 檢查是否觸發停損
                        elif w.stop_loss and current_price <= w.stop_loss:
                            user = self.get_user(w.user_id)
                            if user:
                                await user.send(
                                    f"**停損警報**：您追蹤的 {symbol} 已跌破停損價 **{w.stop_loss}**！現價：{current_price}")
                                w.stop_loss = None

                    await session.commit()

            except Exception as e:
                logger.error(f"警報監聽引擎發生錯誤: {e}")
                await asyncio.sleep(1)


    async def setup_hook(self):
        logger.info("啟動初始化流程...")

        # 1. 初始化資料庫
        await init_db()
        logger.info("✅ 資料庫初始化完成！")

        # ==========================================
        # 🌟 滿足您的需求：開機時立刻執行一次同步
        # ==========================================
        logger.info("🔄 開始執行開機資料同步...")
        await sync_us_symbols()
        await sync_tw_symbols()
        logger.info("✅ 開機資料同步完成！")

        # ==========================================
        # 🌟 滿足您的需求：每天晚上定時更新
        # ==========================================
        # 設定 cron 排程，這裡設定每天半夜 00:05 執行 (避開整點尖峰)
        self.scheduler.add_job(sync_us_symbols, 'cron', hour=0, minute=5)
        self.scheduler.add_job(sync_tw_symbols, 'cron', hour=0, minute=10)

        # 啟動排程器
        self.scheduler.start()
        logger.info("✅ 每日例行排程 (APScheduler) 已啟動！")

        # 3. 載入 Discord 指令模組 (Cogs)
        await self.load_extension("bot.cogs.watchlist")
        logger.info("✅ 已載入 Watchlist 指令模組")

        # 登入 Shioaji (使用 to_thread 避免卡死開機流程)
        logger.info("啟動 Shioaji 登入程序...")
        await asyncio.to_thread(sj_manager.login)
        # 載入指令模組
        await self.load_extension("bot.cogs.market")
        logger.info("✅ 已載入 Market 指令模組")
        await self.load_extension("bot.cogs.report")
        logger.info("✅ 已載入 Report 指令模組")
        # 啟動背景警報監聽任務
        self.loop.create_task(self.alert_monitor_task())
        await self.load_extension("bot.cogs.youtube_monitor")
        # 4. 同步斜線指令
        await self.tree.sync()
        logger.info("✅ Slash Commands 同步完成！")

bot = QuantBot()


@bot.event
async def on_ready():
    logger.info(f'機器人登入成功：{bot.user}')


if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    bot.run(TOKEN)