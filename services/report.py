# bot/cogs/report.py
import discord
from discord.ext import commands
from discord import app_commands
import pandas as pd
import asyncio
import logging

from data_sources.shioaji_client import sj_manager
from services.ai_analyzer import ai_analyzer

# from data_sources.shioaji_client import sj_manager # 實務上從這裡要資料

logger = logging.getLogger("ReportCog")


class ReportCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # 模擬資料撈取與清洗 (實戰中您會替換成真實的 Pandas 運算)
    def fetch_and_clean_market_data(self):

         # 實戰邏輯提示：
         # df = sj_manager.get_all_snapshots()
         # df['成交值'] = df['close'] * df['volume']
         # top_value_df = df.sort_values(by='成交值', ascending=False).head(50)
         # surge_df = df[df['volume'] > df['5ma_volume'] * 2]

        # 這裡我們建立一小段模擬字串，用來餵給 Gemini
        top_value_data = """
        2330 台積電 (漲幅 1.5%, 成交值 200億)
        2317 鴻海 (漲幅 2.0%, 成交值 150億)
        3231 緯創 (漲幅 4.5%, 成交值 80億)
        2382 廣達 (漲幅 3.2%, 成交值 75億)
        3324 雙鴻 (漲幅 8.1%, 成交值 60億)
        1519 華城 (漲跌 0.0%, 成交值 50億)
        ... (其餘省略)
        """

        surge_volume_data = """
        3324 雙鴻 (今日量 50000張, 5日均量 10000張, 創歷史新高)
        3017 奇鋐 (今日量 45000張, 5日均量 15000張, 帶量長紅)
        2368 金像電 (今日量 60000張, 5日均量 20000張, 突破均線糾結)
        ... (其餘省略)
        """

        return top_value_data, surge_volume_data

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


async def setup(bot):
    await bot.add_cog(ReportCog(bot))