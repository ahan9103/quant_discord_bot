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
