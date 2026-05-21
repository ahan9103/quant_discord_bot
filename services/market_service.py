import shioaji as sj
import pandas as pd
import requests
import logging
from datetime import datetime, timedelta
from data_sources.shioaji_client import sj_manager

logger = logging.getLogger("MarketService")


def fetch_and_clean_market_data() -> tuple[str, str]:
    try:
        # 直接用排行 API 取成交值前 50，不需掃全市場合約
        amount_rank = sj_manager.api.scanners(
            scanner_type=sj.constant.ScannerType.AmountRank,
            count=50
        )
    except Exception as e:
        logger.error(f"AmountRank scanner 失敗: {e}")
        return "無法取得成交值排行", "無法取得成交值排行"

    if not amount_rank:
        return "今日目前無交易資料。", "今日目前無交易資料。"

    top_value_str = "【🔥 全市場成交值前 50 大】\n"
    for item in amount_rank:
        val_in_100m = item.total_amount / 100000000
        prev_close = item.close - item.change_price
        change_rate = round(item.change_price / prev_close * 100, 2) if prev_close else 0
        top_value_str += f"- {item.code} {item.name}: 漲幅 {change_rate}%, 成交值 {val_in_100m:.1f}億\n"

    try:
        # 取漲幅排行前 200，rank_value 即漲跌幅 %
        change_rank = sj_manager.api.scanners(
            scanner_type=sj.constant.ScannerType.ChangePercentRank,
            count=200
        )
    except Exception as e:
        logger.error(f"ChangePercentRank scanner 失敗: {e}")
        change_rank = []

    # 放寬條件：成交量 > 5000 張 且 漲幅 > 2%，多抓標的以利觀察族群共通性
    surge_items = [
        s for s in change_rank
        if s.total_volume > 5000 and s.rank_value > 2.0
    ][:50]

    surge_volume_str = "【🚀 強勢放量標的（量>5000張 漲>2%）】\n"
    for item in surge_items:
        surge_volume_str += f"- {item.code} {item.name}: 今日量 {item.total_volume}張, 漲幅 {item.rank_value}%\n"

    if not surge_items:
        surge_volume_str = "今日盤面無明顯放量強勢標的。"

    logger.info("✅ 全市場價量信號清洗完畢！")
    logger.info("=" * 60)
    logger.info(top_value_str)
    logger.info("=" * 60)
    logger.info(surge_volume_str)
    logger.info("=" * 60)

    return top_value_str, surge_volume_str


_TWSE_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _twse_fetch(url_template: str, max_weekdays: int = 5) -> tuple[str | None, dict | None]:
    """
    從 TWSE 官方 API 取資料，自動往回找最近有資料的交易日。
    url_template 中用 {date} 作為日期佔位符（格式 YYYYMMDD）。
    回傳 (date_str, json_data) 或 (None, None)。
    """
    today = datetime.now()
    checked = 0
    for i in range(max_weekdays * 2 + 2):
        candidate = today - timedelta(days=i)
        if candidate.weekday() >= 5:   # 跳過週末
            continue
        date_str = candidate.strftime("%Y%m%d")
        try:
            res = requests.get(url_template.format(date=date_str), headers=_TWSE_HEADERS, timeout=10)
            data = res.json()
            if data.get("stat") == "OK" and data.get("data"):
                return date_str, data
            logger.info(f"TWSE 無資料 (date={date_str}): {data.get('stat')}")
        except Exception as e:
            logger.error(f"TWSE 請求失敗 (date={date_str}): {e}")
        checked += 1
        if checked >= max_weekdays:
            break
    return None, None


def _clean_int(val) -> int:
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0


def fetch_evening_chip_data() -> tuple[str, str]:
    logger.info("📡 開始透過 TWSE 官方 API 獲取盤後三大法人與融資券數據...")

    # ── 三大法人逐股 (T86) ── 約 18:00 後釋出 ───────────────────
    # 回傳欄位: 證券代號, 證券名稱, 外陸資買進股數, 外陸資賣出股數,
    #           外陸資買賣超股數, 投信買進股數, 投信賣出股數,
    #           投信買賣超股數, 自營商買賣超股數, 三大法人買賣超股數
    t86_date, t86 = _twse_fetch(
        "https://www.twse.com.tw/rwd/zh/fund/T86?date={date}&selectType=ALLBUT0999&response=json"
    )

    if t86:
        display = f"{t86_date[:4]}/{t86_date[4:6]}/{t86_date[6:]}"
        inst_str = f"【🏢 土洋(外資+投信)同步買超 Top 5】 (日期: {display})\n"
        fields = t86["fields"]
        inst_df = pd.DataFrame(t86["data"], columns=fields)

        foreign_col = next((c for c in fields if "外陸資買賣超" in c), None)
        trust_col   = next((c for c in fields if "投信買賣超"   in c), None)

        if foreign_col and trust_col:
            inst_df["外資淨"] = inst_df[foreign_col].apply(_clean_int)
            inst_df["投信淨"] = inst_df[trust_col].apply(_clean_int)
            co_buy = inst_df[(inst_df["外資淨"] > 0) & (inst_df["投信淨"] > 0)].copy()
            co_buy["合計"] = co_buy["外資淨"] + co_buy["投信淨"]
            for _, row in co_buy.sort_values("合計", ascending=False).head(5).iterrows():
                # TWSE 單位為股，除以 1000 換算為張
                f_lots = row["外資淨"] // 1000
                i_lots = row["投信淨"] // 1000
                inst_str += f"- {row[fields[0]]} {row[fields[1]]}: 外資買 {f_lots}張, 投信買 {i_lots}張\n"
        else:
            logger.warning(f"T86 欄位結構有異: {fields}")
            inst_str += "- 法人欄位解析失敗，請確認 TWSE API 格式。\n"
    else:
        inst_str = "【🏢 土洋(外資+投信)同步買超 Top 5】\n- 近 5 個交易日均無法取得法人資料。\n"

    # ── 融資融券 (MI_MARGN) ── 約 20:30 後釋出 ──────────────────
    # 回傳欄位: 股票代號, 名稱, 融資買進, 融資賣出, 融資現金償還,
    #           融資餘額, 融資限額, 融券賣出, 融券買進, ...
    margn_date, margn = _twse_fetch(
        "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date}&selectType=MS&response=json"
    )

    if margn:
        display = f"{margn_date[:4]}/{margn_date[4:6]}/{margn_date[6:]}"
        margin_str = f"【⚠️ 融資單日暴增 Top 5】 (日期: {display})\n"
        fields = margn["fields"]
        margin_df = pd.DataFrame(margn["data"], columns=fields)

        buy_col  = next((c for c in fields if "融資" in c and "買進" in c), None)
        sell_col = next((c for c in fields if "融資" in c and "賣出" in c), None)

        if buy_col and sell_col:
            margin_df["融資淨增"] = (
                margin_df[buy_col].apply(_clean_int) - margin_df[sell_col].apply(_clean_int)
            )
            top5 = margin_df[margin_df["融資淨增"] > 0].sort_values("融資淨增", ascending=False).head(5)
            for _, row in top5.iterrows():
                margin_str += f"- {row[fields[0]]} {row[fields[1]]}: 融資單日大增 {row['融資淨增']}張\n"
        else:
            logger.warning(f"MI_MARGN 欄位結構有異: {fields}")
            margin_str += "- 融資欄位解析失敗，請確認 TWSE API 格式。\n"
    else:
        margin_str = "【⚠️ 融資單日暴增 Top 5】\n- 近 5 個交易日均無法取得融資資料。\n"

    logger.info("✅ 籌碼晚報 ETL 處理完畢！")
    return inst_str, margin_str
