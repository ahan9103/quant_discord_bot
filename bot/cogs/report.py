import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import logging
from datetime import datetime, time, timezone, timedelta
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import User
from services.ai_analyzer import ai_analyzer
from services.market_service import fetch_and_clean_market_data, fetch_evening_chip_data

logger = logging.getLogger("ReportCog")


async def _send_chunks(first_send, rest_send, text: str):
    if len(text) <= 2000:
        await first_send(text)
    else:
        chunks = [text[i:i + 1990] for i in range(0, len(text), 1990)]
        await first_send(chunks[0])
        for chunk in chunks[1:]:
            await rest_send(chunk)


class ReportCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tz = timezone(timedelta(hours=8))
        self.scheduled_reports.start()

    def cog_unload(self):
        self.scheduled_reports.cancel()

    # ==========================================
    # 手動觸發指令區 (Slash Commands)
    # ==========================================
    @app_commands.command(name="evening_report", description="產生 21:30 盤後籌碼與三大法人分析晚報")
    async def generate_evening(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            inst_data, margin_data = await asyncio.to_thread(fetch_evening_chip_data)
            report_text = await ai_analyzer.generate_evening_report(inst_data, margin_data)
            await _send_chunks(interaction.followup.send, interaction.channel.send, report_text)
        except Exception as e:
            logger.error(f"晚報生成失敗: {e}")
            await interaction.followup.send("晚報生成失敗。")

    @app_commands.command(name="daily_report", description="產生今日盤後資金流向與 AI 分析報告")
    async def generate_report(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            top_value_data, surge_volume_data = await asyncio.to_thread(fetch_and_clean_market_data)
            report_text = await ai_analyzer.generate_market_report(top_value_data, surge_volume_data)
            await _send_chunks(interaction.followup.send, interaction.channel.send, report_text)
        except Exception as e:
            logger.error(f"報告生成失敗: {e}")
            await interaction.followup.send("❌ 報告生成失敗，請檢查系統日誌。")

    # ==========================================
    # 自動排程區 (Cron Jobs)
    # ==========================================
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
                top_value, surge_volume = await asyncio.to_thread(fetch_and_clean_market_data)
                report_text = await ai_analyzer.generate_market_report(top_value, surge_volume)
            else:
                report_type = "晚報"
                inst_data, margin_data = await asyncio.to_thread(fetch_evening_chip_data)
                report_text = await ai_analyzer.generate_evening_report(inst_data, margin_data)

            async with AsyncSessionLocal() as session:
                result = await session.execute(select(User.discord_id))
                user_ids = result.scalars().all()

            if not user_ids:
                logger.warning("資料庫中沒有任何使用者，取消發送。")
                return

            logger.info(f"準備將{report_type}廣播給 {len(user_ids)} 位使用者...")

            for discord_id in user_ids:
                user = self.bot.get_user(discord_id) or await self.bot.fetch_user(discord_id)
                if user:
                    try:
                        await user.send(f"**[系統自動推播] 您的量化{report_type}已出爐！**")
                        await _send_chunks(user.send, user.send, report_text)
                    except Exception as send_err:
                        logger.error(f"無法發送給使用者 {discord_id}: {send_err}")

        except Exception as e:
            logger.error(f"自動排程生成報告時發生整體異常: {e}")

    @scheduled_reports.before_loop
    async def before_scheduled_reports(self):
        await self.bot.wait_until_ready()
        logger.info("✅ 自動報告排程引擎已啟動！等待觸發時間...")


async def setup(bot):
    await bot.add_cog(ReportCog(bot))
