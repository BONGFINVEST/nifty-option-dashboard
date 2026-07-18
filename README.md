# NIFTY Pro Trading Dashboard

A professional option chain analysis dashboard for NIFTY options trading.

## Features
- ✅ PCR Analysis (Highest/Lowest PCR tracking)
- ✅ Writer Strength Indicator (Gauge chart)
- ✅ Combined Call & Put OI Change Visualization
- ✅ Automated Trading Signals (BUY CE/PE/AVOID)
- ✅ Entry, Stop Loss, and Target Levels
- ✅ Support & Resistance Levels

## How to Use

### 1. Deploy to Streamlit Cloud
1. Create a GitHub repository
2. Upload `app.py` and `requirements.txt`
3. Go to [share.streamlit.io](https://share.streamlit.io)
4. Connect your repository and deploy

### 2. Using the Dashboard
1. Upload your NIFTY Option Chain CSV file (from Dhan/Sensibull)
2. Set your Risk:Reward ratio and Stop Loss points
3. View the generated trading signal
4. Follow the trading instructions

## CSV Format
The dashboard expects CSV files with the following columns:
- Strike, PCR, Call OI, Call OI Change, Call Volume, Call LTP
- Put OI, Put OI Change, Put Volume, Put LTP

## Signal Logic
- **🟢 BUY CE:** Call OI ↓ + Put OI ↑ (Bullish)
- **🔴 BUY PE:** Call OI ↑ + Put OI ↓ (Bearish)
- ** AVOID:** No clear trend or mixed signals

## Risk Management
- Always maintain strict Stop Loss
- Do not average down in option buying
- Book 50% at Target 1, trail balance for Target 2

---
**Disclaimer:** This dashboard is for educational purposes only. Trading in options involves risk. Please consult a financial advisor before trading.
