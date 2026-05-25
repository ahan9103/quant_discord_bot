import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging

from sqlalchemy import select
from database.models import Ticker, Watchlist
from database.session import AsyncSessionLocal
from data_sources.shioaji_client import sj_manager

logger = logging.getLogger("MarketCog")


class MarketCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def symbol_autocomplete(self, interaction: discord.Interaction, current: str) -> list[
        app_commands.Choice[str]]:
        """
        當使用者在 Discord 輸入文字時，這個函式會被即時觸發
        """
        if not current:
            return []  # 如果沒打字就不提供建議，或可回傳熱門標的

        async with AsyncSessionLocal() as session:
            # 使用 ILIKE 進行不區分大小寫的模糊比對 (代號或名稱)
            # Discord API 規定回傳的建議清單最多只能有 25 個
            stmt = select(Ticker).where(
                (Ticker.symbol.ilike(f"%{current}%")) |
                (Ticker.name.ilike(f"%{current}%"))
            ).limit(25)

            result = await session.execute(stmt)
            tickers = result.scalars().all()

            # 組裝回傳給 Discord 的選項
            choices = [
                app_commands.Choice(
                    name=f"{t.symbol} - {t.name}",  # 顯示給使用者看的文字
                    value=t.symbol  # 真正傳給背景程式的值
                )
                for t in tickers
            ]
            return choices

    # ==========================================
    # 報價查詢指令
    # ==========================================
    @app_commands.command(name="quote", description="查詢台股即時報價與收盤資訊")
    @app_commands.describe(symbol="請輸入台股代號 (支援名稱動態搜尋)")
    @app_commands.autocomplete(symbol=symbol_autocomplete)
    async def quote_stock(self, interaction: discord.Interaction, symbol: str):
        # 1. 延遲回覆，避免 API 抓取超過 3 秒導致 Discord 報錯
        await interaction.response.defer()

        # 處理代號 (因為選單傳進來的值已經是 2330.TW 格式，這步做個保險)
        query_symbol = f"{symbol}.TW" if symbol.isdigit() and len(symbol) == 4 else symbol

        # 2. 將阻塞的 Shioaji 查詢丟到背景執行緒
        data = await asyncio.to_thread(sj_manager.get_stock_snapshot, query_symbol)

        if "error" in data:
            logger.warning(f"報價查詢失敗 [{query_symbol}]: {data['error']}")
            await interaction.followup.send(f"❌ 查詢失敗：{data['error']}")
            return

        logger.info(f"報價查詢成功 [{query_symbol}] 現價={data['close']}")

        # 3. 製作精美的報價卡片 (Embed)
        if data['change_price'] > 0:
            color = discord.Color.red()  # 台股紅漲
            emoji = "📈"
        elif data['change_price'] < 0:
            color = discord.Color.green()  # 台股綠跌
            emoji = "📉"
        else:
            color = discord.Color.light_gray()  # 平盤
            emoji = "➖"

        embed = discord.Embed(
            title=f"{emoji} {data['name']} ({data['symbol']}) 報價資訊",
            color=color
        )

        embed.add_field(name="現價 (Close)", value=f"**{data['close']}**", inline=True)
        embed.add_field(name="漲跌", value=f"{data['change_price']} ({data['change_rate']}%)", inline=True)
        embed.add_field(name="總量", value=f"{data['volume']} 張", inline=True)

        embed.add_field(name="開盤", value=str(data['open']), inline=True)
        embed.add_field(name="最高", value=str(data['high']), inline=True)
        embed.add_field(name="最低", value=str(data['low']), inline=True)

        embed.set_footer(text=f"資料更新時間: {data['update_time']} (永豐 Shioaji 提供)")

        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(MarketCog(bot))