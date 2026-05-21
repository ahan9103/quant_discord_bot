import pandas as pd
import requests
import logging
from datetime import datetime
from data_sources.shioaji_client import sj_manager

logger = logging.getLogger("MarketService")


def fetch_and_clean_market_data() -> tuple[str, str]:
    stock_list = []
    for exchange in [sj_manager.api.Contracts.Stocks.TSE, sj_manager.api.Contracts.Stocks.OTC]:
        for contract in exchange:
            if len(contract.code) == 4:
                stock_list.append(contract)

    name_map = {c.code: c.name for c in stock_list}
    logger.info(f"總共掃描 {len(stock_list)} 檔普通股合約...")

    try:
        snapshots = sj_manager.api.snapshots(stock_list)
    except Exception as e:
        logger.error(f"獲取快照失敗: {e}")
        return "無法取得全市場快照", "無法取得全市場快照"

    data = []
    for snap in snapshots:
        if snap.total_volume > 0:
            data.append({
                'Symbol': snap.code,
                'Name': name_map.get(snap.code, "未知"),
                'Close': snap.close,
                'Volume': snap.total_volume,
                'Turnover_Value': snap.close * snap.total_volume * 1000,
                'Pct_Change': snap.change_rate
            })

    market_df = pd.DataFrame(data)

    if market_df.empty:
        return "今日目前無交易資料。", "今日目前無交易資料。"

    top_value_df = market_df.sort_values(by='Turnover_Value', ascending=False).head(50)
    top_value_str = "【🔥 全市場成交值前 50 大】\n"
    for _, row in top_value_df.iterrows():
        val_in_100m = row['Turnover_Value'] / 100000000
        top_value_str += f"- {row['Symbol']} {row['Name']}: 漲幅 {row['Pct_Change']}%, 成交值 {val_in_100m:.1f}億\n"

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


def fetch_evening_chip_data() -> tuple[str, str]:
    logger.info("📡 開始透過 FinMind API 獲取盤後三大法人與融資券數據...")

    date_str = datetime.now().strftime("%Y-%m-%d")

    try:
        inst_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&date={date_str}"
        inst_res = requests.get(inst_url, timeout=10)
        inst_data = inst_res.json().get('data', [])
        inst_df = pd.DataFrame(inst_data)

        margin_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMarginPurchaseShortSale&date={date_str}"
        margin_res = requests.get(margin_url, timeout=10)
        margin_data = margin_res.json().get('data', [])
        margin_df = pd.DataFrame(margin_data)

    except Exception as e:
        logger.error(f"FinMind API 獲取失敗: {e}")
        return "❌ 無法連線至籌碼資料庫", "❌ 無法連線至籌碼資料庫"

    inst_str = f"【🏢 土洋(外資+投信)同步買超 Top 5】 (日期: {date_str})\n"
    margin_str = f"【⚠️ 融資單日暴增 Top 5】 (日期: {date_str})\n"

    if not inst_df.empty:
        inst_df['net_buy'] = inst_df['buy'] - inst_df['sell']
        foreign_df = inst_df[inst_df['name'] == 'Foreign_Investor'].set_index('stock_id')[['net_buy']].rename(columns={'net_buy': 'F_Buy'})
        trust_df = inst_df[inst_df['name'] == 'Investment_Trust'].set_index('stock_id')[['net_buy']].rename(columns={'net_buy': 'I_Buy'})
        chip_df = foreign_df.join(trust_df, how='inner').dropna()
        co_buy_df = chip_df[(chip_df['F_Buy'] > 0) & (chip_df['I_Buy'] > 0)].copy()
        co_buy_df['Total_Buy'] = co_buy_df['F_Buy'] + co_buy_df['I_Buy']
        top_co_buy = co_buy_df.sort_values(by='Total_Buy', ascending=False).head(5)
        for stock_id, row in top_co_buy.iterrows():
            inst_str += f"- {stock_id}: 外資買 {row['F_Buy'] / 1000:.0f}張, 投信買 {row['I_Buy'] / 1000:.0f}張\n"
    else:
        inst_str += "- 今日證交所尚未公佈法人資料，或 API 暫無數據。\n"

    if not margin_df.empty:
        margin_df['Margin_Net_Increase'] = (margin_df['MarginPurchaseBuy'] - margin_df['MarginPurchaseSell']) / 1000
        top_margin = margin_df[margin_df['Margin_Net_Increase'] > 0].sort_values(by='Margin_Net_Increase', ascending=False).head(5)
        for _, row in top_margin.iterrows():
            margin_str += f"- {row['stock_id']}: 融資單日大增 {row['Margin_Net_Increase']:.0f}張\n"
    else:
        margin_str += "- 今日證交所尚未公佈融資資料，或 API 暫無數據。\n"

    logger.info("✅ 籌碼晚報 ETL 處理完畢！")
    return inst_str, margin_str
