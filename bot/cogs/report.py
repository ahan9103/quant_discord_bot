# bot/cogs/report.py
import discord
from discord.ext import commands, tasks
from discord import app_commands
import pandas as pd
import asyncio
import logging
import yfinance as yf
from datetime import datetime, time, timezone, timedelta
from database.session import AsyncSessionLocal
from database.models import User # 假設您之前有建立 User 表
from sqlalchemy import select

from services.ai_analyzer import ai_analyzer

from data_sources.shioaji_client import sj_manager # 實務上從這裡要資料

logger = logging.getLogger("ReportCog")


class ReportCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 設定台北時區 (UTC+8)
        self.tz = timezone(timedelta(hours=8))

        # 啟動背景定時排程
        self.scheduled_reports.start()

    def cog_unload(self):
        # 當模組被卸載時，停止排程避免重複執行
        self.scheduled_reports.cancel()

    def fetch_and_clean_market_data(self):
        """
        🔥 全市場掃描管線 (Market Screener Pipeline)
        """
        stock_list = []

        for exchange in [sj_manager.api.Contracts.Stocks.TSE, sj_manager.api.Contracts.Stocks.OTC]:
            for contract in exchange:
                # 過濾出 4 碼的普通股 (排除權證、ETF等)
                if len(contract.code) == 4:
                    stock_list.append(contract)

        # 💡 [面試亮點：時間複雜度優化 O(1) 查表法]
        # 建立 {代號: 名稱} 的字典，避免在處理快照時做昂貴的迴圈搜尋
        name_map = {c.code: c.name for c in stock_list}

        logger.info(f"總共掃描 {len(stock_list)} 檔普通股合約...")

        # 2. 批次抓取今日快照 (Snapshot)
        try:
            snapshots = sj_manager.api.snapshots(stock_list)
        except Exception as e:
            logger.error(f"獲取快照失敗: {e}")
            return "無法取得全市場快照", "無法取得全市場快照"

        # 3. 將資料轉換為 Pandas DataFrame 進行極速運算
        data = []
        for snap in snapshots:
            if snap.total_volume > 0:
                data.append({
                    'Symbol': snap.code,
                    'Name': name_map.get(snap.code, "未知"),
                    'Close': snap.close,
                    'Volume': snap.total_volume,
                    # 計算今日成交值 (收盤價 * 總張數 * 1000股)
                    'Turnover_Value': snap.close * snap.total_volume * 1000,
                    'Pct_Change': snap.change_rate
                })

        market_df = pd.DataFrame(data)

        if market_df.empty:
            return "今日目前無交易資料。", "今日目前無交易資料。"

        # ==========================================
        # 🎯 信號 1：萃取「交易值最大的 50 檔標的」
        # ==========================================
        top_value_df = market_df.sort_values(by='Turnover_Value', ascending=False).head(50)

        top_value_str = "【🔥 全市場成交值前 50 大】\n"
        for _, row in top_value_df.iterrows():
            val_in_100m = row['Turnover_Value'] / 100000000  # 換算為「億」
            top_value_str += f"- {row['Symbol']} {row['Name']}: 漲幅 {row['Pct_Change']}%, 成交值 {val_in_100m:.1f}億\n"

        # ==========================================
        # 🎯 信號 2：交易量突然大增 (爆量)
        # ==========================================
        # 這裡設定過濾條件：今日成交量大於 20,000 張，且漲幅大於 4.0%
        surge_df = market_df[
            (market_df['Volume'] > 20000) &
            (market_df['Pct_Change'] > 4.0)
            ].sort_values(by='Turnover_Value', ascending=False).head(20)

        surge_volume_str = "【🚀 量大且強勢突破標的】\n"
        for _, row in surge_df.iterrows():
            surge_volume_str += f"- {row['Symbol']} {row['Name']}: 今日量 {row['Volume']}張, 漲幅 {row['Pct_Change']}%\n"

        if surge_df.empty:
            surge_volume_str = "今日盤面無明顯放量強勢標的。"

        logger.info("✅ 全市場價量信號清洗完畢！")
        return top_value_str, surge_volume_str

    def fetch_evening_chip_data(self):
        """
        🔥 籌碼晚報 ETL (模擬串接 FinMind 或 TWSE 證交所 API)
        """
        logger.info("📡 開始獲取盤後三大法人與融資券數據...")

        # 💡 面試亮點說明：
        # 實務上 Shioaji 盤後籌碼較難抓全市場，通常會串接 FinMind API 或自己爬證交所。
        # 這裡用精煉的模擬數據展示我們對於「籌碼過濾」的量化邏輯 (例如：土洋合作、融資異常)。

        inst_data_str = """
        【土洋(外資+投信)同步買超 Top 5】
        - 2330 台積電: 外資買 15000張, 投信買 2000張 (外資認錯回補)
        - 3324 雙鴻: 外資買 3000張, 投信買 1500張 (散熱族群持續吸金)
        - 2382 廣達: 外資買 8000張, 投信買 1000張 (AI 伺服器建倉)
        - 1519 華城: 外資買 1200張, 投信買 800張 (重電政策護盤)
        - 3017 奇鋐: 外資買 2500張, 投信買 1200張
        """

        margin_data_str = """
        【融資單日暴增 Top 5】
        - 2603 長榮: 融資大增 8000張 (當沖與散戶搶進航運)
        - 2368 金像電: 融資增 3000張, 但法人買超 (主力疑似利用融資鎖碼)
        - 3406 玉晶光: 融資增 2500張, 法人賣超 1000張 (散戶接刀風險高)
        """

        return inst_data_str, margin_data_str

    # ==========================================
    # 🤖 2. 手動觸發指令區 (Slash Commands)
    # ==========================================
    # ... (保留您原本的 @app_commands.command(name="daily_report") ) ...

    @app_commands.command(name="evening_report", description="產生 21:30 盤後籌碼與三大法人分析晚報")
    async def generate_evening(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            inst_data, margin_data = await asyncio.to_thread(self.fetch_evening_chip_data)
            report_text = await ai_analyzer.generate_evening_report(inst_data, margin_data)

            # 發送處理 (若字數過長)
            if len(report_text) <= 2000:
                await interaction.followup.send(report_text)
            else:
                chunks = [report_text[i:i + 1990] for i in range(0, len(report_text), 1990)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await interaction.followup.send(chunk)
                    else:
                        await interaction.channel.send(chunk)
        except Exception as e:
            logger.error(f"晚報生成失敗: {e}")
            await interaction.followup.send("❌ 晚報生成失敗。")

    @app_commands.command(name="daily_report", description="產生今日盤後資金流向與 AI 分析報告")
    async def generate_report(self, interaction: discord.Interaction):
        # 1. 報告生成需要數秒到十幾秒，必須 Defer
        await interaction.response.defer()

        try:
            # 2. 數據清洗與準備 (丟到背景執行)
            top_value_data, surge_volume_data = await asyncio.to_thread(self.fetch_and_clean_market_data)

            # 3. 呼叫 Gemini AI 進行分析
            report_text = await ai_analyzer.generate_market_report(top_value_data, surge_volume_data)

            # 4. Discord 訊息長度限制處理 (超過 2000 字元需分段發送)
            if len(report_text) <= 2000:
                await interaction.followup.send(report_text)
            else:
                # 簡易切分訊息發送
                chunks = [report_text[i:i + 1990] for i in range(0, len(report_text), 1990)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await interaction.followup.send(chunk)
                    else:
                        await interaction.channel.send(chunk)

        except Exception as e:
            logger.error(f"報告生成失敗: {e}")
            await interaction.followup.send("❌ 報告生成失敗，請檢查系統日誌。")

    # ==========================================
    # ⏰ 3. 自動排程區 (Cron Jobs)
    # ==========================================
    # 設定時間：14:00 發午報，21:30 發晚報 (需注意機器人主機時區，這裡我們強制綁定台北時間)
    @tasks.loop(time=[
        time(hour=14, minute=0, tzinfo=timezone(timedelta(hours=8))),
        time(hour=21, minute=30, tzinfo=timezone(timedelta(hours=8)))
    ])
    async def scheduled_reports(self):
        now = datetime.now(self.tz)
        if now.weekday() >= 5:
            logger.info("今天是週末，跳過自動報告。")
            return

        logger.info(f"⏰ 觸發定時報告！目前時間：{now.strftime('%H:%M')}")

        try:
            if now.hour == 14:
                report_type = "午報"
                top_value, surge_volume = await asyncio.to_thread(self.fetch_and_clean_market_data)
                report_text = await ai_analyzer.generate_market_report(top_value, surge_volume)
            else:
                report_type = "晚報"
                inst_data, margin_data = await asyncio.to_thread(self.fetch_evening_chip_data)
                report_text = await ai_analyzer.generate_evening_report(inst_data, margin_data)

            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User.discord_id))
                user_ids = result.scalars().all()

            if not user_ids:
                logger.warning("資料庫中沒有任何使用者，取消發送。")
                return

            logger.info(f"準備將{report_type}廣播給 {len(user_ids)} 位使用者...")

            # 3. 迴圈發送給每一位使用者
            for discord_id in user_ids:
                user = self.bot.get_user(discord_id) or await self.bot.fetch_user(discord_id)
                if user:
                    try:
                        await user.send(f"📊 **[系統自動推播] 您的量化{report_type}已出爐！**")
                        # 如果字數過長一樣做切分處理
                        if len(report_text) <= 2000:
                            await user.send(report_text)
                        else:
                            chunks = [report_text[i:i + 1990] for i in range(0, len(report_text), 1990)]
                            for chunk in chunks:
                                await user.send(chunk)
                    except Exception as send_err:
                        # 實務上使用者可能關閉私訊功能，這裡要把錯誤捕捉起來，避免影響下一個使用者的發送
                        logger.error(f"無法發送給使用者 {discord_id}: {send_err}")

        except Exception as e:
            logger.error(f"自動排程生成報告時發生整體異常: {e}")


    @scheduled_reports.before_loop
    async def before_scheduled_reports(self):
        """確保機器人準備好後才啟動排程"""
        await self.bot.wait_until_ready()
        logger.info("✅ 自動報告排程引擎已啟動！等待觸發時間...")


async def setup(bot):
    await bot.add_cog(ReportCog(bot))