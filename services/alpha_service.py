# services/alpha_service.py
#
# Alpha 因子引擎：5 大常見有效因子 + 多因子共振標的池
#
# 因子清單：
#   F1 量能爆發 — VolumeRank 前200 且漲幅 > 1%（Shioaji scanner）
#   F2 資金龍頭 — AmountRank 前30 且漲幅 > 0%（Shioaji scanner）
#   F3 強勢動能 — ChangePercentRank 前200 且漲幅 > 3% 且量 > 5000張
#   F4 法人合買 — 外資＋投信同日均買超（TWSE T86）
#   F5 融資萎縮 — 今日融資淨減（散戶退出，法人鎖碼信號，TWSE MI_MARGN）
#
# 多因子共振邏輯：
#   同一標的觸發 >= ALPHA_THRESHOLD 個因子 → 進入 Alpha 標的池
#
import shioaji as sj
import logging
import asyncio
import pandas as pd
from datetime import datetime

from data_sources.shioaji_client import sj_manager
from services.market_service import _twse_fetch, _clean_int

logger = logging.getLogger("AlphaService")

# ─── 因子參數設定 ────────────────────────────────────
FACTOR_CONFIG = {
    "量能爆發": {"min_volume": 3000, "min_change": 1.0},
    "資金龍頭": {"top_n": 30},
    "強勢動能": {"min_change": 3.0, "min_volume": 5000},
    "法人合買": {"min_foreign_lots": 500},   # 外資淨買 > 500 張
    "融資萎縮": {"max_margin_net": -100},     # 融資淨減 < -100 張
}

ALPHA_THRESHOLD = 3   # 觸發幾個因子才進標的池（2=寬鬆, 3=較嚴, 4=最強訊號）


# ─── 因子 1：量能爆發 ───────────────────────────────
def _factor_volume_surge(volume_rank: list, change_rank: list) -> dict[str, dict]:
    """VolumeRank 前200 且漲幅 > min_change%"""
    cfg = FACTOR_CONFIG["量能爆發"]
    change_map = {item.code: item.rank_value for item in change_rank}
    hits = {}
    for item in volume_rank:
        chg = change_map.get(item.code, 0)
        if item.total_volume >= cfg["min_volume"] and chg >= cfg["min_change"]:
            hits[item.code] = {
                "name": item.name,
                "volume": item.total_volume,
                "change": chg,
                "close": getattr(item, "close", 0),
                "amount": getattr(item, "total_amount", 0),
            }
    return hits


# ─── 因子 2：資金龍頭 ───────────────────────────────
def _factor_amount_leader(amount_rank: list) -> dict[str, dict]:
    """AmountRank 前N 且今日上漲（資金主動買入）"""
    cfg = FACTOR_CONFIG["資金龍頭"]
    hits = {}
    for item in amount_rank[:cfg["top_n"]]:
        prev = item.close - item.change_price
        chg = round(item.change_price / prev * 100, 2) if prev else 0
        if chg > 0:
            hits[item.code] = {
                "name": item.name,
                "volume": item.total_volume,
                "change": chg,
                "close": item.close,
                "amount": item.total_amount,
            }
    return hits


# ─── 因子 3：強勢動能 ───────────────────────────────
def _factor_strong_momentum(change_rank: list) -> dict[str, dict]:
    """ChangePercentRank 中漲幅 > min_change% 且量 > min_volume 張"""
    cfg = FACTOR_CONFIG["強勢動能"]
    hits = {}
    for item in change_rank:
        if item.rank_value >= cfg["min_change"] and item.total_volume >= cfg["min_volume"]:
            hits[item.code] = {
                "name": item.name,
                "volume": item.total_volume,
                "change": item.rank_value,
                "close": getattr(item, "close", 0),
                "amount": getattr(item, "total_amount", 0),
            }
    return hits


# ─── 因子 4：法人合買 ───────────────────────────────
def _factor_institutional_buy() -> dict[str, dict]:
    """外資 + 投信 同日均買超（TWSE T86，18:00 後才有資料）"""
    cfg = FACTOR_CONFIG["法人合買"]
    _, t86 = _twse_fetch(
        "https://www.twse.com.tw/rwd/zh/fund/T86?date={date}&selectType=ALLBUT0999&response=json"
    )
    if not t86:
        logger.warning("法人因子：T86 無資料（可能尚未釋出）")
        return {}

    fields = t86["fields"]
    df = pd.DataFrame(t86["data"], columns=fields)
    foreign_col = next((c for c in fields if "外陸資買賣超" in c), None)
    trust_col   = next((c for c in fields if "投信買賣超"   in c), None)
    if not foreign_col or not trust_col:
        return {}

    df["外資淨"] = df[foreign_col].apply(_clean_int)
    df["投信淨"] = df[trust_col].apply(_clean_int)
    min_shares = cfg["min_foreign_lots"] * 1000
    co_buy = df[(df["外資淨"] > min_shares) & (df["投信淨"] > 0)]

    hits = {}
    for _, row in co_buy.iterrows():
        code = str(row[fields[0]]).strip()
        hits[code] = {
            "name": str(row[fields[1]]).strip(),
            "volume": 0, "change": 0, "close": 0, "amount": 0,
            "foreign_lots": row["外資淨"] // 1000,
            "trust_lots":   row["投信淨"] // 1000,
        }
    return hits


# ─── 因子 5：融資萎縮 ───────────────────────────────
def _factor_margin_squeeze() -> dict[str, dict]:
    """今日融資淨減 < max_margin_net 張（散戶退出，法人鎖碼信號，TWSE MI_MARGN，20:30 後）"""
    cfg = FACTOR_CONFIG["融資萎縮"]
    _, margn = _twse_fetch(
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=MS&response=json"
    )
    if not margn:
        logger.warning("融資因子：MI_MARGN 無資料（可能尚未釋出）")
        return {}

    fields = margn["fields"]
    df = pd.DataFrame(margn["data"], columns=fields)
    buy_col  = next((c for c in fields if "融資" in c and "買進" in c), None)
    sell_col = next((c for c in fields if "融資" in c and "賣出" in c), None)
    if not buy_col or not sell_col:
        return {}

    df["融資淨"] = df[buy_col].apply(_clean_int) - df[sell_col].apply(_clean_int)
    squeeze = df[df["融資淨"] < cfg["max_margin_net"]]

    hits = {}
    for _, row in squeeze.iterrows():
        code = str(row[fields[0]]).strip()
        hits[code] = {
            "name": str(row[fields[1]]).strip(),
            "volume": 0, "change": 0, "close": 0, "amount": 0,
            "margin_net": row["融資淨"],
        }
    return hits


# ─── 主掃描函式 ─────────────────────────────────────
def run_alpha_scan() -> dict:
    """
    執行全因子掃描。

    Returns:
        {
          "scan_time": str,
          "factors": {"因子名": hit_count},
          "alpha_pool": [
              {"code", "name", "score", "factors", "volume", "change", "close", "amount"}
          ]
        }
    """
    if not sj_manager.is_logged_in:
        logger.error("❌ Shioaji 未登入，無法執行 Alpha 掃描")
        return {"error": "Shioaji 未登入"}

    # ── 取 Shioaji Scanner 資料 ──────────────────────
    def _safe_scan(scanner_type, count):
        try:
            return sj_manager.api.scanners(scanner_type=scanner_type, count=count)
        except Exception as e:
            logger.warning(f"Scanner {scanner_type} 失敗: {e}")
            return []

    volume_rank = _safe_scan(sj.constant.ScannerType.VolumeRank, 200)
    amount_rank = _safe_scan(sj.constant.ScannerType.AmountRank, 50)
    change_rank = _safe_scan(sj.constant.ScannerType.ChangePercentRank, 200)

    # ── 執行各因子 ──────────────────────────────────
    raw: dict[str, dict[str, dict]] = {
        "量能爆發": _factor_volume_surge(volume_rank, change_rank),
        "資金龍頭": _factor_amount_leader(amount_rank),
        "強勢動能": _factor_strong_momentum(change_rank),
        "法人合買": _factor_institutional_buy(),
        "融資萎縮": _factor_margin_squeeze(),
    }

    # ── 多因子共振匯整 ──────────────────────────────
    board: dict[str, dict] = {}
    for factor_name, stocks in raw.items():
        for code, data in stocks.items():
            if code not in board:
                board[code] = {
                    "code": code, "name": data["name"],
                    "score": 0, "factors": [],
                    "volume": data["volume"], "change": data["change"],
                    "close": data["close"], "amount": data["amount"],
                }
            entry = board[code]
            entry["score"] += 1
            entry["factors"].append(factor_name)
            if data["volume"] > entry["volume"]:
                entry["volume"] = data["volume"]
            if data["change"] > entry["change"]:
                entry["change"] = data["change"]
            if data["close"] > 0 and entry["close"] == 0:
                entry["close"] = data["close"]

    alpha_pool = sorted(
        [v for v in board.values() if v["score"] >= ALPHA_THRESHOLD],
        key=lambda x: (-x["score"], -x["volume"])
    )

    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 日誌輸出 ────────────────────────────────────
    logger.info("=" * 65)
    logger.info(f"📡 Alpha 因子掃描 @ {scan_time}")
    for fname, stocks in raw.items():
        logger.info(f"  【{fname}】觸發 {len(stocks)} 檔")
    logger.info(f"  🎯 多因子共振 (≥{ALPHA_THRESHOLD}) → 標的池 {len(alpha_pool)} 檔")
    if alpha_pool:
        logger.info("-" * 65)
        for s in alpha_pool:
            logger.info(
                f"  [{s['code']}] {s['name']:<8} 得分={s['score']} "
                f"因子={'＋'.join(s['factors'])} "
                f"漲{s['change']:.1f}% 量{s['volume']:,}張 收{s['close']:.2f}"
            )
    logger.info("=" * 65)

    return {
        "scan_time": scan_time,
        "factors": {k: len(v) for k, v in raw.items()},
        "alpha_pool": alpha_pool,
    }


async def run_alpha_scan_async() -> dict:
    """非同步包裝"""
    return await asyncio.to_thread(run_alpha_scan)


# ════════════════════════════════════════════════════════
# DB 存取
# ════════════════════════════════════════════════════════

async def save_alpha_scan_async(result: dict) -> None:
    """將掃描結果寫入 alpha_scans + alpha_pool_entries"""
    from datetime import date as date_type
    from database.session import AsyncSessionLocal
    from database.models import AlphaScan, AlphaPoolEntry

    pool = result.get("alpha_pool", [])
    factors = result.get("factors", {})
    today = date_type.today()

    async with AsyncSessionLocal() as session:
        scan = AlphaScan(
            scan_date=today,
            scan_time=datetime.now(),
            factors_hit=factors,
            pool_size=len(pool),
        )
        session.add(scan)
        await session.flush()          # 取得 scan.id

        for s in pool:
            session.add(AlphaPoolEntry(
                scan_id=scan.id,
                scan_date=today,
                code=s["code"],
                name=s["name"],
                score=s["score"],
                factors=s["factors"],
                change_pct=s.get("change", 0),
                volume=s.get("volume", 0),
                close_price=s.get("close", 0),
                amount=s.get("amount", 0),
            ))

        await session.commit()
    logger.info(f"✅ Alpha 掃描已存入 DB (scan_id={scan.id}, {len(pool)} 筆)")


async def get_stocks_alpha_history(codes: list[str], days: int = 30) -> dict[str, list[dict]]:
    """
    批量查詢多支股票近 N 天在 Alpha 標的池的出現記錄。
    codes: 純代號（"2330"），不含 .TW / .TWO 後綴。
    回傳: {code: [entry_dict, ...]}，按日期降序排列。
    """
    from datetime import date as date_type, timedelta
    from database.session import AsyncSessionLocal
    from database.models import AlphaPoolEntry
    from sqlalchemy import select, desc

    if not codes:
        return {}

    since = date_type.today() - timedelta(days=days)

    async with AsyncSessionLocal() as session:
        stmt = (
            select(AlphaPoolEntry)
            .where(AlphaPoolEntry.code.in_(codes), AlphaPoolEntry.scan_date >= since)
            .order_by(desc(AlphaPoolEntry.scan_date))
        )
        rows = (await session.execute(stmt)).scalars().all()

    history: dict[str, list[dict]] = {c: [] for c in codes}
    for r in rows:
        history[r.code].append({
            "date":        r.scan_date.isoformat(),
            "score":       r.score,
            "factors":     r.factors or [],
            "change_pct":  r.change_pct or 0.0,
            "volume":      r.volume or 0,
            "close_price": r.close_price or 0.0,
        })
    return history


# ════════════════════════════════════════════════════════
# 演算法：走勢估算
# ════════════════════════════════════════════════════════

def estimate_stock_trend(entries: list[dict]) -> dict:
    """
    純函式，從 get_stocks_alpha_history 的結果推估個股走勢。
    entries: 按日期降序（最新在前）。

    演算法：
      1. 出現次數（persistence）→ 訊號延續性
      2. 平均得分（avg_score）→ 因子共振強度
      3. 收盤價線性回歸斜率 → 價格趨勢方向
      4. 近期 vs 早期成交量比值 → 量能動向
      5. 綜合訊號強度評級
    """
    if not entries:
        return {"in_pool": False, "signal": "近期無 Alpha 訊號"}

    n = len(entries)
    avg_score = sum(e["score"] for e in entries) / n

    # ── 價格趨勢（線性回歸，由舊到新）──────────────
    closes = [e["close_price"] for e in reversed(entries)]
    if n >= 3 and any(c > 0 for c in closes):
        x = list(range(n))
        mx = sum(x) / n
        my = sum(closes) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, closes))
        den = sum((xi - mx) ** 2 for xi in x)
        slope = num / den if den else 0
        slope_pct = (slope / my) * 100 if my else 0   # 每次出現的平均漲跌%

        if slope_pct > 0.3:
            price_trend = f"上漲 (+{slope_pct:.1f}%/次)"
        elif slope_pct < -0.3:
            price_trend = f"下跌 ({slope_pct:.1f}%/次)"
        else:
            price_trend = "盤整"
    else:
        price_trend = "資料不足"

    # ── 量能趨勢（近3筆 vs 更早）──────────────────
    recent_vols = [e["volume"] for e in entries[:3] if e["volume"] > 0]
    older_vols  = [e["volume"] for e in entries[3:] if e["volume"] > 0]
    if recent_vols and older_vols:
        ratio = (sum(recent_vols) / len(recent_vols)) / (sum(older_vols) / len(older_vols))
        vol_trend = "放量" if ratio > 1.2 else ("縮量" if ratio < 0.8 else "持平")
    else:
        vol_trend = "資料不足"

    latest = entries[0]

    # ── 訊號強度評級 ────────────────────────────────
    if n >= 4 and avg_score >= 3.5:
        signal_level = "🔴 極強"
        signal_desc  = f"連續 {n} 次入選，均分 {avg_score:.1f}，趨勢延續機率高"
    elif n >= 3 and avg_score >= 3:
        signal_level = "🟠 強"
        signal_desc  = f"近期 {n} 次共振，動能持續中"
    elif n >= 2:
        signal_level = "🟡 中"
        signal_desc  = f"出現 {n} 次，觀察是否延續"
    else:
        signal_level = "⚪ 弱"
        signal_desc  = "僅 1 次出現，訊號尚不穩定"

    return {
        "in_pool":       True,
        "persistence":   n,
        "avg_score":     round(avg_score, 1),
        "latest_score":  latest["score"],
        "latest_factors": latest["factors"],
        "latest_change": latest["change_pct"],
        "latest_close":  latest["close_price"],
        "price_trend":   price_trend,
        "vol_trend":     vol_trend,
        "signal_level":  signal_level,
        "signal_desc":   signal_desc,
    }
