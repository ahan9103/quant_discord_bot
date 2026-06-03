# bot/cogs/alpha.py
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from datetime import datetime, time, timezone, timedelta

from sqlalchemy import select
from database.session import AsyncSessionLocal
from database.models import User
from services.alpha_service import (
    run_alpha_scan_async, ALPHA_THRESHOLD, FACTOR_CONFIG,
    save_alpha_scan_async,
)
from services.ai_analyzer import ai_analyzer

logger = logging.getLogger("AlphaCog")

_FACTOR_EMOJI = {
    "量能爆發": "🔥",
    "資金龍頭": "💰",
    "強勢動能": "🚀",
    "法人合買": "🏢",
    "融資萎縮": "📉",
}


def _factor_config_summary() -> str:
    lines = []
    for fname, cfg in FACTOR_CONFIG.items():
        emoji = _FACTOR_EMOJI.get(fname, "•")
        parts = []
        if "min_volume" in cfg:
            parts.append(f"量>{cfg['min_volume']}張")
        if "min_change" in cfg:
            parts.append(f"漲>{cfg['min_change']}%")
        if "top_n" in cfg:
            parts.append(f"前{cfg['top_n']}名")
        if "min_foreign_lots" in cfg:
            parts.append(f"外資>{cfg['min_foreign_lots']}張")
        if "max_margin_net" in cfg:
            parts.append(f"融資淨<{cfg['max_margin_net']}張")
        lines.append(f"{emoji} **{fname}**：{' ＆ '.join(parts)}")
    return "\n".join(lines)


def _build_embeds(result: dict) -> list[discord.Embed]:
    pool = result.get("alpha_pool", [])
    factors = result.get("factors", {})
    scan_time = result.get("scan_time", "—")

    # ── Embed 1：掃描總覽 ──────────────────────────
    main = discord.Embed(
        title="📡 Alpha 因子掃描報告",
        description=(
            f"掃描時間：`{scan_time}`\n"
            f"多因子共振門檻：≥ **{ALPHA_THRESHOLD}** 個因子同時觸發"
        ),
        color=discord.Color.gold()
    )
    for fname, count in factors.items():
        emoji = _FACTOR_EMOJI.get(fname, "•")
        main.add_field(name=f"{emoji} {fname}", value=f"{count} 檔", inline=True)

    pool_status = f"🎯 **{len(pool)} 檔** 進入標的池" if pool else "今日無多因子共振標的"
    main.add_field(name="​", value=pool_status, inline=False)

    embeds = [main]

    # ── Embed 2：標的池明細 ───────────────────────
    if pool:
        pool_embed = discord.Embed(
            title=f"🎯 Alpha 標的池（{len(pool)} 檔）",
            color=discord.Color.orange()
        )
        for s in pool[:20]:
            factor_icons = " ".join(_FACTOR_EMOJI.get(f, f) for f in s["factors"])
            val = (
                f"得分 **{s['score']}** | {factor_icons}\n"
                f"漲跌 `{s['change']:+.1f}%` ｜ 量 `{s['volume']:,}` 張 ｜ 收 `{s['close']:.2f}`"
            )
            pool_embed.add_field(
                name=f"[{s['code']}] {s['name']}",
                value=val,
                inline=False
            )
        pool_embed.set_footer(text="得分越高 = 越多因子共振 → 訊號強度越強")
        embeds.append(pool_embed)

    return embeds


class AlphaCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tz = timezone(timedelta(hours=8))
        self._last_result: dict = {}
        self.scheduled_alpha.start()

    def cog_unload(self):
        self.scheduled_alpha.cancel()

    # ──────────────────────────────────────────────────
    # /alpha_scan — 手動觸發全因子掃描
    # ──────────────────────────────────────────────────
    @app_commands.command(name="alpha_scan", description="執行 5 大 Alpha 因子掃描，找出多因子共振強勢標的")
    @app_commands.describe(with_ai="是否附帶 AI 明日操作建議（預設否）")
    async def alpha_scan(self, interaction: discord.Interaction, with_ai: bool = False):
        await interaction.response.defer()
        label = "掃描 + AI 建議" if with_ai else "因子掃描"
        placeholder = await interaction.followup.send(f"⏳ 正在執行 {label}，請稍候…")

        try:
            result = await run_alpha_scan_async()

            if "error" in result:
                await placeholder.edit(content=f"❌ 掃描失敗：{result['error']}")
                return

            self._last_result = result
            embeds = _build_embeds(result)
            await placeholder.edit(content=None, embeds=embeds)

            # 存入 DB（背景執行，不阻塞回覆）
            try:
                await save_alpha_scan_async(result)
            except Exception as db_err:
                logger.warning(f"Alpha 結果存 DB 失敗: {db_err}")

            if with_ai:
                pool = result.get("alpha_pool", [])
                ai_report = await ai_analyzer.generate_alpha_report(pool)
                for i in range(0, len(ai_report), 1990):
                    await interaction.channel.send(ai_report[i:i + 1990])

        except Exception as e:
            logger.error(f"/alpha_scan 發生錯誤: {e}", exc_info=True)
            await placeholder.edit(content=f"❌ 系統錯誤: {e}")

    # ──────────────────────────────────────────────────
    # /alpha_pool — 查看目前標的池（快取）
    # ──────────────────────────────────────────────────
    @app_commands.command(name="alpha_pool", description="查看最近一次掃描的 Alpha 標的池")
    async def alpha_pool(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not self._last_result:
            await interaction.followup.send(
                "📭 標的池尚無資料，請先執行 `/alpha_scan` 或等待每日自動掃描（14:30 / 22:30）。"
            )
            return

        embeds = _build_embeds(self._last_result)
        await interaction.followup.send(embeds=embeds)

    # ──────────────────────────────────────────────────
    # /alpha_factors — 說明各因子邏輯
    # ──────────────────────────────────────────────────
    @app_commands.command(name="alpha_factors", description="查看 Alpha 因子的條件定義")
    async def alpha_factors(self, interaction: discord.Interaction):
        await interaction.response.defer()

        embed = discord.Embed(
            title="📋 Alpha 因子說明",
            description=(
                f"共 **5** 個因子，標的同時觸發 ≥ **{ALPHA_THRESHOLD}** 個才進標的池。\n\n"
                + _factor_config_summary()
            ),
            color=discord.Color.blue()
        )
        embed.add_field(
            name="資料來源",
            value=(
                "🔥💰🚀 Shioaji 即時 Scanner（盤中有效）\n"
                "🏢 TWSE T86 法人資料（收盤後 18:00 起）\n"
                "📉 TWSE MI_MARGN 融資資料（收盤後 20:30 起）"
            ),
            inline=False
        )
        await interaction.followup.send(embed=embed)

    # ──────────────────────────────────────────────────
    # 自動排程：週一～五 14:30（盤後初掃）＋ 22:30（完整報告含 AI 建議）
    # ──────────────────────────────────────────────────
    @tasks.loop(time=[
        time(hour=14, minute=30, tzinfo=timezone(timedelta(hours=8))),
        time(hour=22, minute=30, tzinfo=timezone(timedelta(hours=8))),
    ])
    async def scheduled_alpha(self):
        now = datetime.now(self.tz)
        if now.weekday() >= 5:
            return

        is_evening = now.hour == 22
        logger.info(f"⏰ 排程 Alpha {'晚間完整報告' if is_evening else '盤後初掃'} @ {now.strftime('%H:%M')}")

        try:
            result = await run_alpha_scan_async()
            if "error" in result:
                return

            self._last_result = result
            pool = result.get("alpha_pool", [])

            # 無論有無入選標的，都存 DB（紀錄當天市況）
            try:
                await save_alpha_scan_async(result)
            except Exception as db_err:
                logger.warning(f"排程 Alpha 存 DB 失敗: {db_err}")

            if not pool:
                logger.info("Alpha 排程：標的池為空，略過廣播")
                return

            embeds = _build_embeds(result)

            # 22:30 額外生成 AI 明日操作建議
            ai_report = None
            if is_evening:
                logger.info("生成 AI 明日操作建議...")
                try:
                    ai_report = await ai_analyzer.generate_alpha_report(pool)
                except Exception as e:
                    logger.error(f"AI 報告生成失敗: {e}")

            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User.discord_id))
                user_ids = res.scalars().all()

            for uid in user_ids:
                try:
                    user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                    if not user:
                        continue

                    header = "**[📡 Alpha 晚間完整報告]**" if is_evening else "**[📡 Alpha 盤後初掃]**"
                    await user.send(header, embeds=embeds)

                    if ai_report:
                        # 分段發送（Discord 單則上限 2000 字）
                        for i in range(0, len(ai_report), 1990):
                            await user.send(ai_report[i:i + 1990])

                except Exception as e:
                    logger.warning(f"Alpha 推播 {uid} 失敗: {e}")

        except Exception as e:
            logger.error(f"排程 Alpha 掃描失敗: {e}", exc_info=True)

    @scheduled_alpha.before_loop
    async def before_alpha(self):
        await self.bot.wait_until_ready()
        logger.info("✅ Alpha 因子排程引擎已啟動（14:30 / 22:30）")


async def setup(bot):
    await bot.add_cog(AlphaCog(bot))
