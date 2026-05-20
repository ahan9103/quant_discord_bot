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
                🔥 籌碼晚報 ETL (真實串接 FinMind OpenAPI)
                """
        import requests
        from datetime import datetime

        logger.info("📡 開始透過 FinMind API 獲取盤後三大法人與融資券數據...")

        # 1. 決定資料日期 (實務上若遇到假日，需寫邏輯往前推至最近交易日)
        date_str = datetime.now().strftime("%Y-%m-%d")

        try:
            # ==========================================
            # 📊 [A] 獲取三大法人買賣超資料
            # ==========================================
            inst_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&date={date_str}"
            inst_res = requests.get(inst_url, timeout=10)
            inst_data = inst_res.json().get('data', [])
            inst_df = pd.DataFrame(inst_data)

            # ==========================================
            # 📊 [B] 獲取融資融券餘額資料
            # ==========================================
            margin_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMarginPurchaseShortSale&date={date_str}"
            margin_res = requests.get(margin_url, timeout=10)
            margin_data = margin_res.json().get('data', [])
            margin_df = pd.DataFrame(margin_data)

        except Exception as e:
            logger.error(f"FinMind API 獲取失敗: {e}")
            return "❌ 無法連線至籌碼資料庫", "❌ 無法連線至籌碼資料庫"

        # ==========================================
        #  數據轉換與特徵工程 (Pandas Transform)
        # ==========================================
        inst_str = f"【🏢 土洋(外資+投信)同步買超 Top 5】 (日期: {date_str})\n"
        margin_str = f"【⚠️ 融資單日暴增 Top 5】 (日期: {date_str})\n"

        # 處理法人籌碼
        if not inst_df.empty:
            # 1. 計算各分點的淨買超 (買進 - 賣出)
            inst_df['net_buy'] = inst_df['buy'] - inst_df['sell']

            # 2. 利用 Pandas 萃取出外資與投信的 DataFrame，並將股票代號設為 Index
            foreign_df = inst_df[inst_df['name'] == 'Foreign_Investor'].set_index('stock_id')[['net_buy']].rename(
                columns={'net_buy': 'F_Buy'})
            trust_df = inst_df[inst_df['name'] == 'Investment_Trust'].set_index('stock_id')[['net_buy']].rename(
                columns={'net_buy': 'I_Buy'})

            # 3. 找出土洋都有動作的標的
            chip_df = foreign_df.join(trust_df, how='inner').dropna()

            # 4. 篩選「土洋同買」且排序
            co_buy_df = chip_df[(chip_df['F_Buy'] > 0) & (chip_df['I_Buy'] > 0)].copy()
            co_buy_df['Total_Buy'] = co_buy_df['F_Buy'] + co_buy_df['I_Buy']
            top_co_buy = co_buy_df.sort_values(by='Total_Buy', ascending=False).head(5)

            for stock_id, row in top_co_buy.iterrows():
                inst_str += f"- {stock_id}: 外資買 {row['F_Buy'] / 1000:.0f}張, 投信買 {row['I_Buy'] / 1000:.0f}張\n"
        else:
            inst_str += "- 今日證交所尚未公佈法人資料，或 API 暫無數據。\n"

        # 處理融資數據
        if not margin_df.empty:
            # 1. 計算單日融資增加張數 = (融資買進 - 融資賣出) / 1000
            margin_df['Margin_Net_Increase'] = (margin_df['MarginPurchaseBuy'] - margin_df['MarginPurchaseSell']) / 1000

            # 2. 篩選融資大增的標的
            top_margin = margin_df[margin_df['Margin_Net_Increase'] > 0].sort_values(by='Margin_Net_Increase',
                                                                                     ascending=False).head(5)

            for _, row in top_margin.iterrows():
                margin_str += f"- {row['stock_id']}: 融資單日大增 {row['Margin_Net_Increase']:.0f}張\n"
        else:
            margin_str += "- 今日證交所尚未公佈融資資料，或 API 暫無數據。\n"

        logger.info("✅ 籌碼晚報 ETL 處理完畢！")
        return inst_str, margin_str

    # ==========================================
    # 2. 手動觸發指令區 (Slash Commands)
    # ==========================================
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
            await interaction.followup.send(" 晚報生成失敗。")

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
    # 3. 自動排程區 (Cron Jobs)
    # ==========================================
    # 設定時間：14:00 發午報，21:30 發晚報
    @tasks.loop(time=[
        time(hour=14, minute=0, tzinfo=timezone(timedelta(hours=8))),
        time(hour=22, minute=30, tzinfo=timezone(timedelta(hours=8)))
    ])
    async def scheduled_reports(self):
        now = datetime.now(self.tz)
        if now.weekday() >= 5:
            logger.info("今天是週末，跳過自動報告。")
            return

        logger.info(f"觸發定時報告！目前時間：{now.strftime('%H:%M')}")

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
                        await user.send(f"**[系統自動推播] 您的量化{report_type}已出爐！**")
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