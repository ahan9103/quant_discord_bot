import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import logging
from sqlalchemy import select, update, delete
from typing import Optional
from datetime import datetime, time, timezone, timedelta

from database.session import AsyncSessionLocal
from database.models import Ticker, Watchlist, User
from services.ai_analyzer import ai_analyzer
from services.news_service import NewsService
from data_sources.yfinance_client import fetch_stock_info, fetch_current_prices

logger = logging.getLogger("watchlist")


class WatchlistCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tz = timezone(timedelta(hours=8))
        self.scheduled_checkups.start()

    def cog_unload(self):
        self.scheduled_checkups.cancel()

    # ==========================================
    # 隱藏的輔助機制
    # ==========================================
    async def _ensure_user(self, session, discord_id: int):
        user = await session.get(User, discord_id)
        if not user:
            session.add(User(discord_id=discord_id))
            await session.commit()

    # ==========================================
    # Autocomplete 邏輯
    # ==========================================
    async def all_symbol_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not current:
            return []
        async with AsyncSessionLocal() as session:
            stmt = select(Ticker).where(
                (Ticker.symbol.ilike(f"%{current}%")) |
                (Ticker.name.ilike(f"%{current}%"))
            ).limit(25)
            result = await session.execute(stmt)
            return [app_commands.Choice(name=f"{t.symbol} - {t.name}", value=t.symbol) for t in result.scalars().all()]

    async def tracked_symbol_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        async with AsyncSessionLocal() as session:
            stmt = select(Watchlist.symbol).where(Watchlist.user_id == interaction.user.id)
            result = await session.execute(stmt)
            tracked_symbols = [row[0] for row in result.all()]
        filtered = [sym for sym in tracked_symbols if current.upper() in sym.upper()][:25]
        return [app_commands.Choice(name=sym, value=sym) for sym in filtered]

    # ==========================================
    # C: 新增追蹤 (/add_stock)
    # ==========================================
    @app_commands.command(name="add_stock", description="將股票加入個人追蹤清單")
    @app_commands.describe(
        symbol="輸入代號 (台股請輸入數字如 2330，美股如 AAPL)",
        cost_price="[選填] 輸入您的持有成本價，方便未來計算損益"
    )
    @app_commands.autocomplete(symbol=all_symbol_autocomplete)
    async def add_stock(self, interaction: discord.Interaction, symbol: str, cost_price: Optional[float] = None):
        await interaction.response.defer()

        symbol = symbol.upper().strip()
        query_symbol = f"{symbol}.TW" if symbol.isdigit() and len(symbol) == 4 else symbol

        async with AsyncSessionLocal() as session:
            user_result = await session.execute(select(User).where(User.discord_id == interaction.user.id))
            user_obj = user_result.scalar_one_or_none()

            if not user_obj:
                user_obj = User(discord_id=interaction.user.id, is_premium=False)
                session.add(user_obj)
                await session.flush()

            result = await session.execute(select(Ticker).where(Ticker.symbol == query_symbol))
            ticker_obj = result.scalar_one_or_none()

            if not ticker_obj:
                stock_info = await asyncio.to_thread(fetch_stock_info, query_symbol)
                if not stock_info:
                    await interaction.followup.send(f"❌ 找不到股票代號 **{symbol}** 的資訊，請確認代號是否正確。")
                    return
                ticker_obj = Ticker(symbol=query_symbol, name=stock_info['name'], market=stock_info['market'])
                session.add(ticker_obj)

            result = await session.execute(
                select(Watchlist).where(Watchlist.user_id == interaction.user.id, Watchlist.symbol == query_symbol)
            )
            if result.scalar_one_or_none():
                await interaction.followup.send(f"⚠️ 您已經在追蹤 **{ticker_obj.name} ({query_symbol})** 囉！")
                return

            session.add(Watchlist(user_id=interaction.user.id, symbol=query_symbol, cost_price=cost_price))
            await session.commit()

        msg = f"✅ 成功將 **{ticker_obj.name} ({query_symbol})** 加入追蹤清單！"
        if cost_price:
            msg += f"\n💰 紀錄持有成本：{cost_price}"
        await interaction.followup.send(msg)

    # ==========================================
    # R: 讀取清單與損益 (/list)
    # ==========================================
    @app_commands.command(name="list", description="[R] 查看持股清單與即時損益")
    async def list_stock(self, interaction: discord.Interaction):
        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Watchlist).where(Watchlist.user_id == interaction.user.id))
            watchlists = result.scalars().all()

        if not watchlists:
            return await interaction.followup.send("📭 您的追蹤清單是空的，趕快使用 `/add_stock` 來新增吧！")

        symbols = [w.symbol for w in watchlists]
        current_prices = await asyncio.to_thread(fetch_current_prices, symbols)

        embed = discord.Embed(title="📊 個人持股與追蹤清單", color=discord.Color.blue())
        total_cost = 0
        total_value = 0

        for w in watchlists:
            current_price = current_prices.get(w.symbol, 0)

            if w.cost_price and current_price > 0:
                pnl = current_price - w.cost_price
                pnl_pct = (pnl / w.cost_price) * 100
                total_cost += w.cost_price
                total_value += current_price
                emoji = "🟢" if pnl >= 0 else "🔴"
                sign = "+" if pnl >= 0 else ""
                details = f"現價: **{current_price:.2f}** | 成本: {w.cost_price:.2f}\n"
                details += f"損益: {emoji} {sign}{pnl:.2f} ({sign}{pnl_pct:.2f}%)"
                if w.target_price: details += f" | 停利: {w.target_price}"
                if w.stop_loss: details += f" | 停損: {w.stop_loss}"
            else:
                current_str = f"**{current_price:.2f}**" if current_price else "無報價"
                details = f"現價: {current_str} (未設定成本價)"

            embed.add_field(name=f"[{w.symbol}]", value=details, inline=False)

        if total_cost > 0:
            total_pnl = total_value - total_cost
            total_pnl_pct = (total_pnl / total_cost) * 100
            embed.set_footer(text=f"💰 總未實現損益: {total_pnl:.2f} ({total_pnl_pct:.2f}%)")

        await interaction.followup.send(embed=embed)

    # ==========================================
    # U: 更新參數 (/update)
    # ==========================================
    @app_commands.command(name="update", description="[U] 修改持股的成本價、停利或停損點")
    @app_commands.autocomplete(symbol=tracked_symbol_autocomplete)
    async def update_stock(self, interaction: discord.Interaction, symbol: str, cost_price: Optional[float] = None,
                           target_price: Optional[float] = None, stop_loss: Optional[float] = None):
        await interaction.response.defer()

        update_values = {}
        if cost_price is not None: update_values['cost_price'] = cost_price
        if target_price is not None: update_values['target_price'] = target_price
        if stop_loss is not None: update_values['stop_loss'] = stop_loss

        if not update_values:
            return await interaction.followup.send("⚠️ 您沒有提供任何要更新的數值 (成本、停利或停損)。")

        async with AsyncSessionLocal() as session:
            stmt = update(Watchlist).where(Watchlist.user_id == interaction.user.id, Watchlist.symbol == symbol)
            result = await session.execute(stmt.values(**update_values))
            if result.rowcount == 0:
                await interaction.followup.send(f"❌ 找不到追蹤紀錄 **{symbol}**，請先確認是否已加入清單。")
            else:
                await session.commit()
                await interaction.followup.send(f"✅ 成功更新 **{symbol}** 的追蹤設定！")

    # ==========================================
    # D: 刪除追蹤 (/remove)
    # ==========================================
    @app_commands.command(name="remove", description="[D] 將股票移出追蹤清單")
    @app_commands.autocomplete(symbol=tracked_symbol_autocomplete)
    async def remove_stock(self, interaction: discord.Interaction, symbol: str):
        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            stmt = delete(Watchlist).where(Watchlist.user_id == interaction.user.id, Watchlist.symbol == symbol)
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await interaction.followup.send(f"❌ 您的清單中沒有 **{symbol}**。")
            else:
                await session.commit()
                await interaction.followup.send(f"🗑️ 已將 **{symbol}** 移出追蹤清單。")

    # ==========================================
    # 新聞情緒分析 (/morning_news)
    # ==========================================
    @app_commands.command(name="morning_news", description="產生自選股專屬的新聞情緒分析早報")
    async def morning_news(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Watchlist.symbol).where(Watchlist.user_id == interaction.user.id)
                result = await session.execute(stmt)
                my_watchlist = result.scalars().all()

            if not my_watchlist:
                return await interaction.followup.send(
                    "📝 您的自選股清單目前是空的喔！請先使用 `/add_stock` 加入追蹤標的。"
                )

            msg = await interaction.followup.send(f"🔍 正在為您掃描自選股 `{my_watchlist}` 的最新情報，請稍候...")

            raw_news = await NewsService.fetch_watchlist_news(my_watchlist)
            if "發生網路錯誤" in raw_news:
                return await msg.edit(content="❌ 網路異常，無法獲取新聞。")

            ai_report = await ai_analyzer.analyze_news_sentiment(raw_news)

            if len(ai_report) <= 2000:
                await msg.edit(content=ai_report)
            else:
                await msg.edit(content=ai_report[:1990])
                for i in range(1990, len(ai_report), 1990):
                    await interaction.channel.send(ai_report[i:i + 1990])

        except Exception as e:
            logger.error(f"早報生成失敗: {e}")
            await interaction.followup.send(f"❌ 系統發生錯誤: {e}")

    # ==========================================
    # 自動排程區 (持股健檢推播)
    # ==========================================
    @tasks.loop(time=[
        time(hour=8, minute=30, tzinfo=timezone(timedelta(hours=8))),
        time(hour=10, minute=0, tzinfo=timezone(timedelta(hours=8))),
        time(hour=11, minute=30, tzinfo=timezone(timedelta(hours=8))),
        time(hour=13, minute=0, tzinfo=timezone(timedelta(hours=8)))
    ])
    async def scheduled_checkups(self):
        now = datetime.now(self.tz)
        if now.weekday() >= 5:
            return

        logger.info(f"⏰ 觸發定時持股健檢！目前時間：{now.strftime('%H:%M')}")

        try:
            async with AsyncSessionLocal() as session:
                users_result = await session.execute(select(User.discord_id))
                user_ids = users_result.scalars().all()

            for uid in user_ids:
                async with AsyncSessionLocal() as session:
                    stmt = select(Watchlist.symbol).where(Watchlist.user_id == uid)
                    result = await session.execute(stmt)
                    my_watchlist = result.scalars().all()

                if not my_watchlist:
                    continue

                raw_news = await NewsService.fetch_watchlist_news(my_watchlist)

                if "發生錯誤" in raw_news or "無重大新聞" in raw_news:
                    report_text = f"**[系統推播] {now.strftime('%H:%M')} 持股動態：**\n{raw_news}"
                else:
                    ai_report = await ai_analyzer.analyze_news_sentiment(raw_news)
                    report_text = f"**[持股健檢] {now.strftime('%H:%M')} 專屬情報解析**\n\n{ai_report}"

                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if user:
                    try:
                        if len(report_text) <= 2000:
                            await user.send(report_text)
                        else:
                            for i in range(0, len(report_text), 1990):
                                await user.send(report_text[i:i + 1990])
                    except Exception as send_err:
                        logger.error(f"無法發送給使用者 {uid}: {send_err}")

        except Exception as e:
            logger.error(f"定時持股健檢發生異常: {e}")

    @scheduled_checkups.before_loop
    async def before_checkups(self):
        await self.bot.wait_until_ready()
        logger.info("✅ 定時持股健檢排程引擎已啟動！等待觸發時間...")


async def setup(bot):
    await bot.add_cog(WatchlistCog(bot))
