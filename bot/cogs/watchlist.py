# bot/cogs/watchlist.py
import discord
from discord.ext import commands ,tasks
from discord import app_commands
import yfinance as yf
import asyncio
import logging
from sqlalchemy import select, update, delete
from typing import Optional
from datetime import datetime, time, timezone, timedelta
from services.ai_analyzer import ai_analyzer

# 引入資料庫模型與連線 Session
from database.session import AsyncSessionLocal
from database.models import Ticker, Watchlist, User
from services.news_service import NewsService

logger = logging.getLogger("watchlist")

# 這是一個一般的同步函式，用來呼叫 yfinance
def fetch_stock_info_sync(symbol: str) -> dict:
    try:
        ticker = yf.Ticker(symbol)
        # 抓取基本資料 (info 字典)，這裡只取我們需要的欄位
        info = ticker.info
        if 'shortName' not in info and 'longName' not in info:
            return None

        name = info.get('shortName') or info.get('longName')
        # 簡易判斷市場別：如果有 .TW 或 .TWO 結尾就是台股，否則視為美股
        market = 'TW' if symbol.endswith(('.TW', '.TWO')) else 'US'

        return {"name": name, "market": market}
    except Exception as e:
        print(f"yfinance 查詢錯誤: {e}")
        return None

def fetch_current_prices(symbols: list[str]) -> dict:
    if not symbols:
        return {}
    try:
        # 使用 yf.download 批次抓取，效能遠高於單檔抓取
        data = yf.download(symbols, period="1d", progress=False)
        # 如果只有一檔股票，yfinance 回傳的 DataFrame 結構會不一樣，需要做處理
        if len(symbols) == 1:
            price = data['Close'].iloc[-1].item()
            return {symbols[0]: price}
        else:
            # 多檔股票的處理
            prices = data['Close'].iloc[-1].to_dict()
            return prices
    except Exception as e:
        print(f"批次報價抓取失敗: {e}")
        return {}


def fetch_single_stock_info(symbol: str) -> dict:
    try:
        info = yf.Ticker(symbol).info
        name = info.get('shortName') or info.get('longName', symbol)
        market = 'TW' if symbol.endswith(('.TW', '.TWO')) else 'US'
        return {"name": name, "market": market}
    except Exception:
        return None


class WatchlistCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 設定台北時區
        self.tz = timezone(timedelta(hours=8))
        # 啟動背景排程
        self.scheduled_checkups.start()

    def cog_unload(self):
        self.scheduled_checkups.cancel()
    # ==========================================
    # 隱藏的輔助機制
    # ==========================================
    async def _ensure_user(self, session, discord_id: int):
        """靜默註冊機制：確保使用者存在，否則自動建立"""
        user = await session.get(User, discord_id)
        if not user:
            new_user = User(discord_id=discord_id)
            session.add(new_user)
            await session.commit()

    # ==========================================
    # Autocomplete 邏輯 (UX 極致優化)
    # ==========================================
    async def all_symbol_autocomplete(self, interaction: discord.Interaction, current: str) -> list[
        app_commands.Choice[str]]:
        """給 /add 用的：搜尋資料庫內所有標的"""
        if not current:
            return []
        async with AsyncSessionLocal() as session:
            stmt = select(Ticker).where(
                (Ticker.symbol.ilike(f"%{current}%")) |
                (Ticker.name.ilike(f"%{current}%"))
            ).limit(25)
            result = await session.execute(stmt)
            return [app_commands.Choice(name=f"{t.symbol} - {t.name}", value=t.symbol) for t in result.scalars().all()]

    async def tracked_symbol_autocomplete(self, interaction: discord.Interaction, current: str) -> list[
        app_commands.Choice[str]]:
        """給 /update, /remove 用的：只搜尋使用者『正在追蹤』的標的"""
        async with AsyncSessionLocal() as session:
            stmt = select(Watchlist.symbol).where(Watchlist.user_id == interaction.user.id)
            result = await session.execute(stmt)
            tracked_symbols = [row[0] for row in result.all()]

            # 過濾符合輸入字串的標的
            filtered = [sym for sym in tracked_symbols if current.upper() in sym.upper()][:25]
            return [app_commands.Choice(name=sym, value=sym) for sym in filtered]

    # ==========================================
    # C: 新增追蹤 (/add)
    # ==========================================
    @app_commands.command(name="add_stock", description="將股票加入個人追蹤清單")
    @app_commands.describe(
        symbol="輸入代號 (台股請輸入數字如 2330，美股如 AAPL)",
        cost_price="[選填] 輸入您的持有成本價，方便未來計算損益"
    )
    @app_commands.autocomplete(symbol=all_symbol_autocomplete)
    async def add_stock(self, interaction: discord.Interaction, symbol: str, cost_price: Optional[float] = None):
        # 1. 延遲回覆 (Defer) 爭取運算時間
        # 這會讓 Discord 畫面上顯示「機器人正在思考中...」，避免 3 秒 Timeout
        await interaction.response.defer()

        # 2. 處理股票代號格式 (將純數字台股自動加上 .TW)
        symbol = symbol.upper().strip()
        query_symbol = f"{symbol}.TW" if symbol.isdigit() and len(symbol) == 4 else symbol

        # 3. 開啟資料庫 Session
        async with AsyncSessionLocal() as session:
            # --- 檢查 Tickers 字典表是否已經有這檔股票 ---
            result = await session.execute(select(Ticker).where(Ticker.symbol == query_symbol))
            ticker_obj = result.scalar_one_or_none()

            # 如果資料庫沒有這檔股票的資料，就去 yfinance 抓
            if not ticker_obj:
                # 使用 to_thread 將同步的 yfinance 丟到背景執行，不卡死主迴圈
                stock_info = await asyncio.to_thread(fetch_stock_info_sync, query_symbol)

                if not stock_info:
                    await interaction.followup.send(f"❌ 找不到股票代號 **{symbol}** 的資訊，請確認代號是否正確。")
                    return

                # 寫入 Tickers 表
                ticker_obj = Ticker(
                    symbol=query_symbol,
                    name=stock_info['name'],
                    market=stock_info['market']
                )
                session.add(ticker_obj)

            # --- 檢查使用者是否已經追蹤過 ---
            result = await session.execute(
                select(Watchlist).where(
                    Watchlist.user_id == interaction.user.id,
                    Watchlist.symbol == query_symbol
                )
            )
            existing_watch = result.scalar_one_or_none()

            if existing_watch:
                await interaction.followup.send(f"⚠️ 您已經在追蹤 **{ticker_obj.name} ({query_symbol})** 囉！")
                return

            # --- 寫入 Watchlist 使用者追蹤表 ---
            new_watch = Watchlist(
                user_id=interaction.user.id,
                symbol=query_symbol,
                cost_price=cost_price
            )
            session.add(new_watch)

            # 提交資料庫交易 (Commit)
            await session.commit()

            # 4. 回覆成功訊息
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
                return await interaction.followup.send("📭 您的追蹤清單是空的，趕快使用 `/add` 來新增吧！")

            # 抓取所有追蹤中標的的代號
            symbols = [w.symbol for w in watchlists]

            # 背景批次抓取最新報價
            current_prices = await asyncio.to_thread(fetch_current_prices, symbols)

            # 建立精美的 Discord Embed 報表
            embed = discord.Embed(title="📊 個人持股與追蹤清單", color=discord.Color.blue())

            total_cost = 0
            total_value = 0

            for w in watchlists:
                current_price = current_prices.get(w.symbol, 0)

                # 若有設定成本價，計算損益
                if w.cost_price and current_price > 0:
                    pnl = current_price - w.cost_price
                    pnl_pct = (pnl / w.cost_price) * 100

                    total_cost += w.cost_price
                    total_value += current_price

                    # 決定標示符號與顏色
                    emoji = "🟢" if pnl >= 0 else "🔴"
                    sign = "+" if pnl >= 0 else ""

                    details = f"現價: **{current_price:.2f}** | 成本: {w.cost_price:.2f}\n"
                    details += f"損益: {emoji} {sign}{pnl:.2f} ({sign}{pnl_pct:.2f}%)"
                    if w.target_price: details += f" | 停利: {w.target_price}"
                    if w.stop_loss: details += f" | 停損: {w.stop_loss}"
                else:
                    # 純追蹤，無成本價
                    current_str = f"**{current_price:.2f}**" if current_price else "無報價"
                    details = f"現價: {current_str} (未設定成本價)"

                embed.add_field(name=f"[{w.symbol}]", value=details, inline=False)

            # 總結未實現損益
            if total_cost > 0:
                total_pnl = total_value - total_cost
                total_pnl_pct = (total_pnl / total_cost) * 100
                embed.set_footer(text=f"💰 總未實現損益: {total_pnl:.2f} ({total_pnl_pct:.2f}%)")

            await interaction.followup.send(embed=embed)

    # ==========================================
    # U: 更新參數 (/update)
    # ==========================================
    @app_commands.command(name="update", description="[U] 修改持股的成本價、停利或停損點")
    @app_commands.autocomplete(symbol=tracked_symbol_autocomplete)  # 💡 只顯示有追蹤的股票
    async def update_stock(self, interaction: discord.Interaction, symbol: str, cost_price: Optional[float] = None,
                           target_price: Optional[float] = None, stop_loss: Optional[float] = None):
        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            stmt = update(Watchlist).where(Watchlist.user_id == interaction.user.id, Watchlist.symbol == symbol)

            # 動態建立要更新的欄位
            update_values = {}
            if cost_price is not None: update_values['cost_price'] = cost_price
            if target_price is not None: update_values['target_price'] = target_price
            if stop_loss is not None: update_values['stop_loss'] = stop_loss

            if not update_values:
                return await interaction.followup.send("⚠️ 您沒有提供任何要更新的數值 (成本、停利或停損)。")

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
    @app_commands.autocomplete(symbol=tracked_symbol_autocomplete)  # 💡 只顯示有追蹤的股票
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

    @app_commands.command(name="morning_news", description="產生自選股專屬的新聞情緒分析早報")
    async def morning_news(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            # 1. 從資料庫撈取使用者的自選股清單
            user_discord_id = interaction.user.id

            # 1. 從資料庫撈取該使用者的自選股清單
            async with AsyncSessionLocal() as session:
                # 建立查詢語句：SELECT stock_symbol FROM watchlist WHERE user_id = ?
                stmt = select(Watchlist.symbol).where(Watchlist.user_id == user_discord_id)
                result = await session.execute(stmt)

                # scalars().all() 會把 [('2330',), ('2317',)] 這種 Tuple 列表
                # 攤平成乾淨的陣列 ['2330', '2317']
                my_watchlist = result.scalars().all()

            # 💡 [防呆機制] 如果使用者還沒有加入任何自選股
            if not my_watchlist:
                return await interaction.followup.send(
                    "📝 您的自選股清單目前是空的喔！請先使用新增指令（例如 `/add_stock`）加入追蹤標的。"
                )

            msg = await interaction.followup.send(f"🔍 正在為您掃描自選股 `{my_watchlist}` 的最新情報，請稍候...")

            # 2. 爬取新聞
            raw_news = await NewsService.fetch_watchlist_news(my_watchlist)
            logger.info(f"🔍 [Debug] 爬蟲結果長度: {len(raw_news)}")

            if "發生網路錯誤" in raw_news:
                return await msg.edit(content="❌ 網路異常，無法獲取新聞。")

            # 3. AI 分析
            ai_report = await ai_analyzer.analyze_news_sentiment(raw_news)
            logger.info(f"🧠 [Debug] AI 回傳長度: {len(ai_report)}")

            # 4. 💡 [關鍵修改] 直接「編輯」剛剛那條等待訊息，把它變成最終報告！
            if len(ai_report) <= 2000:
                await msg.edit(content=ai_report)
            else:
                # 如果字數超過 2000，先編輯第一則，剩下的用回覆補上
                await msg.edit(content=ai_report[:1990])
                for i in range(1990, len(ai_report), 1990):
                    await interaction.channel.send(ai_report[i:i + 1990])

        except Exception as e:
            logger.error(f"早報生成失敗: {e}")
            # 防呆：確保出錯時 Discord 也能看到提示
            await interaction.followup.send(f"❌ 系統發生錯誤: {e}")



    # ==========================================
    # 2. 自動排程區 (持股健檢推播)
    # ==========================================
    # 設定時間：08:30 (盤前), 10:00, 11:30, 13:00 (盤中三次)
    @tasks.loop(time=[
        time(hour=8, minute=30, tzinfo=timezone(timedelta(hours=8))),
        time(hour=10, minute=0, tzinfo=timezone(timedelta(hours=8))),
        time(hour=11, minute=30, tzinfo=timezone(timedelta(hours=8))),
        time(hour=13, minute=0, tzinfo=timezone(timedelta(hours=8)))
    ])
    async def scheduled_checkups(self):
        now = datetime.now(self.tz)
        # 週末不發送 (0=週一, 4=週五, 5=週六, 6=週日)
        if now.weekday() >= 5:
            return

        logger.info(f"⏰ 觸發定時持股健檢！目前時間：{now.strftime('%H:%M')}")

        try:
            # 1. 取得所有有註冊的使用者
            async with AsyncSessionLocal() as session:
                users_result = await session.execute(select(User.discord_id))
                user_ids = users_result.scalars().all()

            # 2. 迴圈處理每位使用者的專屬報告
            for uid in user_ids:
                async with AsyncSessionLocal() as session:
                    stmt = select(Watchlist.symbol).where(Watchlist.user_id == uid)
                    result = await session.execute(stmt)
                    my_watchlist = result.scalars().all()

                # 如果該使用者沒有自選股，就跳過不發送
                if not my_watchlist:
                    continue

                    # 3. 抓取專屬新聞
                raw_news = await NewsService.fetch_watchlist_news(my_watchlist)

                # 4. 分析並組合推播訊息
                if "發生錯誤" in raw_news or "無重大新聞" in raw_news:
                    report_text = f"**[系統推播] {now.strftime('%H:%M')} 持股動態：**\n{raw_news}"
                else:
                    ai_report = await ai_analyzer.analyze_news_sentiment(raw_news)
                    report_text = f"**[持股健檢] {now.strftime('%H:%M')} 專屬情報解析**\n\n{ai_report}"

                # 5. 發送給使用者
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