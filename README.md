
# QuantBot-TW: 專業台股量化分析與 AI 盯盤系統

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![Discord.py](https://img.shields.io/badge/Discord.py-2.3+-green.svg)
![Gemini AI](https://img.shields.io/badge/AI-Gemini%202.5%20Flash-orange.svg)
![SQLAlchemy](https://img.shields.io/badge/ORM-SQLAlchemy%202.0-red.svg)

## 📌 專案概述
**QuantBot-TW** 是一個專為台灣股市開發的自動化量化分析系統。透過 Discord 機器人作為介面，結合 **Shioaji (永豐 API)** 與 **FinMind** 數據源，並利用 **Google Gemini 2.5 Flash** 進行非結構化數據（新聞、事件）的語意分析，為交易員提供從「價量掃描」到「籌碼博弈」的全天候情報支援。

## 🚀 核心功能
### 1. 全市場價量掃描管線 (Market Lunch Report - 14:00)
- **毫秒級掃描**：收盤後自動迭代 TSE/OTC 超過 1,700 檔標的之快照數據。
- **特徵提取**：利用 Pandas 進行向量化運算，篩選「成交值前 50 大」與「爆量突破」之標的。
- **AI 趨勢解讀**：將原始數據餵給 LLM，自動歸納當日資金流向與熱門族群。

### 2. 三大法人與籌碼博弈分析 (Chip Evening Report - 21:30)
- **多維度 ETL**：串接 FinMind API 獲取法人買賣超與資券餘額，並進行 Data Join 處理。
- **量化訊號識別**：自動識別「土洋同買（外資與投信共識）」與「資增人賣（籌碼過熱）」等高勝率特徵。
- **策略沙盤推演**：AI 結合籌碼面提供隔日開盤之壓力/支撐觀察點。

### 3. 自選股事件追蹤與 AI 情緒解析 (Watchlist Morning News)
- **多用戶訂閱制**：基於 PostgreSQL 的自選股管理系統，實現個人化推播。
- **NLP 語意過濾**：透過 Google News RSS 爬取中文新聞，利用 LLM 過濾雜訊並標註「🟢 偏多 / 🔴 偏空」情緒。
- **定時健檢**：盤前、盤中三次自動巡檢自選股動態，壓縮交易員閱讀資訊的時間成本。

## 🏗️ 技術架構 (System Architecture)
本專案採用 **服務導向架構 (Service-Oriented Architecture)**，強調模組間的低耦合與高擴展性：
- **Discord Layer**: 使用 `discord.py` 處理非同步事件與 Interaction 指令。
- **Service Layer**: 
  - `AIAnalyzer`: 統一封裝 Gemini SDK，並實作 **指數退避重試機制 (Exponential Backoff)**。
  - `NewsService`: 負責非結構化資料的爬取與資料清洗。
- **Data Layer**: 使用 SQLAlchemy 2.0 配合非同步驅動，管理多用戶與自選股關係模型。

## 🛠️ 安裝與快速開始
1. **環境設定**
   ```bash
   pip install discord.py shioaji pandas sqlalchemy aiopg google-genai requests