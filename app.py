import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import time
import json

# Page configuration
st.set_page_config(
    page_title="NIFTY Pro Trading Dashboard - LIVE",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .signal-bullish { background-color: #d4edda; color: #155724; padding: 20px; border-radius: 10px; text-align: center; font-size: 24px; font-weight: bold; margin: 10px 0; }
    .signal-bearish { background-color: #f8d7da; color: #721c24; padding: 20px; border-radius: 10px; text-align: center; font-size: 24px; font-weight: bold; margin: 10px 0; }
    .signal-neutral { background-color: #fff3cd; color: #856404; padding: 20px; border-radius: 10px; text-align: center; font-size: 24px; font-weight: bold; margin: 10px 0; }
    .live-indicator { background-color: #ff4444; color: white; padding: 5px 15px; border-radius: 20px; display: inline-block; animation: pulse 2s infinite; }
    @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
</style>
""", unsafe_allow_html=True)

st.title(" NIFTY PRO TRADING DASHBOARD - LIVE")
st.markdown('<span class="live-indicator">● LIVE DATA</span>', unsafe_allow_html=True)
st.markdown("---")

# SIDEBAR
with st.sidebar:
    st.header("⚙️ Settings")
    
    refresh_interval = st.selectbox("Auto-Refresh Interval", ["30 seconds", "60 seconds", "2 minutes", "5 minutes"], index=1)
    
    st.markdown("---")
    st.header(" Risk Parameters")
    risk_reward = st.selectbox("Risk:Reward Ratio", ["1:2", "1:3", "1:1.5"], index=1)
    sl_points = st.slider("Stop Loss (Points)", 10, 100, 30, 5)
    
    st.markdown("---")
    st.header(" Signal Logic")
    st.markdown("""
    - **🟢 BUY CE:** Call OI ↓ + Put OI ↑
    - ** BUY PE:** Call OI ↑ + Put OI ↓  
    - **⚪ AVOID:** No clear trend
    """)

# Function to scrape NSE Option Chain
@st.cache_data(ttl=30)  # Cache for 30 seconds
def fetch_nse_option_chain():
    """Scrape live NIFTY option chain from NSE India"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/javascript, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.nseindia.com/'
        }
        
        # NSE India option chain URL
        url = "https://www.nseindia.com/option-chain"
        
        session = requests.Session()
        session.headers.update(headers)
        
        # First visit the main page to get cookies
        session.get(url, timeout=10)
        
        # Then fetch the option chain data
        option_url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
        response = session.get(option_url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # Parse the data
            records = data['records']['data']
            
            # Prepare lists to store data
            strikes = []
            call_oi = []
            call_oi_change = []
            call_volume = []
            call_ltp = []
            call_iv = []
            put_oi = []
            put_oi_change = []
            put_volume = []
            put_ltp = []
            put_iv = []
            pcr = []
            
            for strike_data in records:
                strike = strike_data['strikePrice']
                
                # Extract Call data
                if 'CE' in strike_data:
                    ce = strike_data['CE']
                    call_oi.append(ce.get('openInterest', 0))
                    call_oi_change.append(ce.get('changeinOI', 0))
                    call_volume.append(ce.get('totalTradedVolume', 0))
                    call_ltp.append(ce.get('lastPrice', 0))
                    call_iv.append(ce.get('impliedVolatility', 0))
                else:
                    call_oi.append(0)
                    call_oi_change.append(0)
                    call_volume.append(0)
                    call_ltp.append(0)
                    call_iv.append(0)
                
                # Extract Put data
                if 'PE' in strike_data:
                    pe = strike_data['PE']
                    put_oi.append(pe.get('openInterest', 0))
                    put_oi_change.append(pe.get('changeinOI', 0))
                    put_volume.append(pe.get('totalTradedVolume', 0))
                    put_ltp.append(pe.get('lastPrice', 0))
                    put_iv.append(pe.get('impliedVolatility', 0))
                else:
                    put_oi.append(0)
                    put_oi_change.append(0)
                    put_volume.append(0)
                    put_ltp.append(0)
                    put_iv.append(0)
                
                strikes.append(strike)
                
                # Calculate PCR
                if call_oi[-1] > 0:
                    pcr.append(put_oi[-1] / call_oi[-1])
                else:
                    pcr.append(0)
            
            # Create DataFrame
            df = pd.DataFrame({
                'Strike': strikes,
                'Call OI': call_oi,
                'Call OI Change': call_oi_change,
                'Call Volume': call_volume,
                'Call LTP': call_ltp,
                'Call IV': call_iv,
                'Put OI': put_oi,
                'Put OI Change': put_oi_change,
                'Put Volume': put_volume,
                'Put LTP': put_ltp,
                'Put IV': put_iv,
                'PCR': pcr
            })
            
            return df, data['records']['underlyingValue']
            
        else:
            st.error(f"Failed to fetch data. Status code: {response.status_code}")
            return None, None
            
    except Exception as e:
        st.error(f"Error fetching data: {e}")
        return None, None

# Fetch data
df, spot_price = fetch_nse_option_chain()

if df is None or df.empty:
    st.warning("⚠️ Unable to fetch live data from NSE. Please try again in a few moments.")
    st.info(" **Note:** NSE might be blocking requests. Please wait a few minutes and refresh.")
    st.stop()

# Display last update time
st.success(f"✅ Last Updated: {datetime.now().strftime('%H:%M:%S')} | Spot Price: {spot_price:.2f}")

# Clean data
df.columns = df.columns.str.strip()
for col in df.columns:
    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

# ============================================
# 1. PCR TRACKING
# ============================================
st.subheader("📊 PCR Analysis - Support & Resistance Levels")

valid_pcr_df = df[(df['PCR'] > 0.1) & (df['PCR'] < 10.0)].copy()

if not valid_pcr_df.empty:
    highest_pcr_row = valid_pcr_df.loc[valid_pcr_df['PCR'].idxmax()]
    highest_pcr_strike = highest_pcr_row['Strike']
    highest_pcr_value = highest_pcr_row['PCR']
    highest_pcr_put_oi = highest_pcr_row.get('Put OI', 0)
    
    lowest_pcr_row = valid_pcr_df.loc[valid_pcr_df['PCR'].idxmin()]
    lowest_pcr_strike = lowest_pcr_row['Strike']
    lowest_pcr_value = lowest_pcr_row['PCR']
    lowest_pcr_call_oi = lowest_pcr_row.get('Call OI', 0)
    
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"""
        <div style='background-color: #d4edda; padding: 20px; border-radius: 10px; border-left: 5px solid #28a745;'>
            <h4 style='margin: 0; color: #155724;'>🟢 Highest PCR (Strongest Support)</h4>
            <h2 style='margin: 10px 0; color: #155724;'>Strike: {highest_pcr_strike:.0f}</h2>
            <p style='margin: 5px 0; color: #155724;'><b>PCR Value:</b> {highest_pcr_value:.2f}</p>
            <p style='margin: 5px 0; color: #155724;'><b>Put OI:</b> {highest_pcr_put_oi:,.0f}</p>
        </div>
        """, unsafe_allow_html=True)
        
    with col2:
        st.markdown(f"""
        <div style='background-color: #f8d7da; padding: 20px; border-radius: 10px; border-left: 5px solid #dc3545;'>
            <h4 style='margin: 0; color: #721c24;'>🔴 Lowest PCR (Strongest Resistance)</h4>
            <h2 style='margin: 10px 0; color: #721c24;'>Strike: {lowest_pcr_strike:.0f}</h2>
            <p style='margin: 5px 0; color: #721c24;'><b>PCR Value:</b> {lowest_pcr_value:.2f}</p>
            <p style='margin: 5px 0; color: #721c24;'><b>Call OI:</b> {lowest_pcr_call_oi:,.0f}</p>
        </div>
        """, unsafe_allow_html=True)

st.markdown("---")

# Find ATM Strike
df['Total_Volume'] = df['Call Volume'] + df['Put Volume']
atm_row = df.loc[df['Total_Volume'].idxmax()]
atm_strike = round(atm_row['Strike'] / 50) * 50

# Get ATM zone data
atm_strikes = [atm_strike - 100, atm_strike - 50, atm_strike, atm_strike + 50, atm_strike + 100]
zone_data = df[df['Strike'].isin(atm_strikes)].copy()

# Calculate OI changes
total_call_oi_chg = zone_data['Call OI Change'].sum()
total_put_oi_chg = zone_data['Put OI Change'].sum()
total_call_oi = zone_data['Call OI'].sum()
total_put_oi = zone_data['Put OI'].sum()

# ============================================
# 2. WRITER STRENGTH INDICATOR
# ============================================
st.subheader(" Writer Strength Indicator")

total_activity = abs(total_call_oi_chg) + abs(total_put_oi_chg)
if total_activity == 0:
    call_strength = 50
    put_strength = 50
else:
    call_strength = (abs(total_call_oi_chg) / total_activity) * 100
    put_strength = (abs(total_put_oi_chg) / total_activity) * 100

if put_strength > call_strength:
    dominant = "BULLISH"
    net_strength = put_strength - call_strength
else:
    dominant = "BEARISH"
    net_strength = call_strength - put_strength

fig_gauge = go.Figure(go.Indicator(
    mode="gauge+number+delta",
    value=put_strength,
    domain={'x': [0, 1], 'y': [0, 1]},
    title={'text': f"Writer Strength Indicator<br><span style='font-size:18px'>{dominant} ({net_strength:.1f}% net)</span>", 
           'font': {'size': 24}},
    delta={'reference': 50, 'increasing': {'color': "green"}, 'decreasing': {'color': "red"}},
    gauge={
        'axis': {'range': [0, 100], 'tickwidth': 1, 'tickcolor': "darkblue"},
        'bar': {'color': "darkblue"},
        'bgcolor': "white",
        'borderwidth': 2,
        'bordercolor': "gray",
        'steps': [
            {'range': [0, 30], 'color': '#ffcccc'},
            {'range': [30, 70], 'color': '#ffffcc'},
            {'range': [70, 100], 'color': '#ccffcc'}
        ],
        'threshold': {
            'line': {'color': "red", 'width': 4},
            'thickness': 0.75,
            'value': 70
        }
    }
))

fig_gauge.update_layout(height=400)
st.plotly_chart(fig_gauge, use_container_width=True)

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Call Writing (Bearish)", f"{call_strength:.1f}%")
with col2:
    st.metric("Put Writing (Bullish)", f"{put_strength:.1f}%")
with col3:
    st.metric("Dominant Side", dominant)
with col4:
    st.metric("Net Strength", f"{net_strength:.1f}%")

st.markdown("---")

# ============================================
# 3. TRADING SIGNAL & LEVELS
# ============================================
st.subheader(" TRADING SIGNAL & LEVELS")

atm_data = df[df['Strike'] == atm_strike].iloc[0] if len(df[df['Strike'] == atm_strike]) > 0 else None
ce_ltp = atm_data.get('Call LTP', 100) if atm_data is not None else 100
pe_ltp = atm_data.get('Put LTP', 100) if atm_data is not None else 100

signal = ""
signal_color = ""
confidence = ""
option_type = ""
entry_premium = 0
sl_premium = 0
target1_premium = 0
target2_premium = 0
recommended_strike = atm_strike
pcr_validation = ""

if total_call_oi_chg < -500000 and total_put_oi_chg > 1000000 and put_strength > 60:
    signal = "🟢 HIGH PROBABILITY BUY CE"
    signal_color = "green"
    confidence = "HIGH"
    option_type = "CE"
    entry_premium = ce_ltp
    sl_premium = max(entry_premium - sl_points, 5)
    target1_premium = entry_premium + (sl_points * 2)
    target2_premium = entry_premium + (sl_points * 3)
    pcr_validation = f"✅ PCR Confirmed: Highest PCR {highest_pcr_value:.2f} at {highest_pcr_strike:.0f}"
    
elif total_call_oi_chg > 1000000 and total_put_oi_chg < -500000 and call_strength > 60:
    signal = "🔴 HIGH PROBABILITY BUY PE"
    signal_color = "red"
    confidence = "HIGH"
    option_type = "PE"
    entry_premium = pe_ltp
    sl_premium = max(entry_premium - sl_points, 5)
    target1_premium = entry_premium + (sl_points * 2)
    target2_premium = entry_premium + (sl_points * 3)
    pcr_validation = f"✅ PCR Confirmed: Lowest PCR {lowest_pcr_value:.2f} at {lowest_pcr_strike:.0f}"
    
elif total_call_oi_chg < 0 and total_put_oi_chg > 0 and put_strength > 50:
    signal = " Mild Bullish - Buy CE on Dips"
    signal_color = "lightgreen"
    confidence = "MEDIUM"
    option_type = "CE"
    entry_premium = ce_ltp * 0.9
    sl_premium = max(entry_premium - 20, 5)
    target1_premium = entry_premium + 40
    target2_premium = entry_premium + 80
    pcr_validation = "⚠️ Wait for price to dip near support"
    
elif total_call_oi_chg > 0 and total_put_oi_chg < 0 and call_strength > 50:
    signal = "🔴 Mild Bearish - Buy PE on Rallies"
    signal_color = "salmon"
    confidence = "MEDIUM"
    option_type = "PE"
    entry_premium = pe_ltp * 0.9
    sl_premium = max(entry_premium - 20, 5)
    target1_premium = entry_premium + 40
    target2_premium = entry_premium + 80
    pcr_validation = "⚠️ Wait for price to rally near resistance"
    
else:
    signal = " AVOID - No Clear Signal"
    signal_color = "gray"
    confidence = "LOW"
    option_type = "NONE"
    pcr_validation = "⚠️ OI signals are mixed or weak"

st.markdown(f"""
<div style='background-color: {signal_color}; padding: 30px; border-radius: 10px; text-align: center; margin-bottom: 20px;'>
    <h2 style='color: white; margin: 0;'>{signal}</h2>
    <p style='color: white; margin: 10px 0;'>Confidence: {confidence}</p>
</div>
""", unsafe_allow_html=True)

st.info(f"🧠 **PCR Validation:** {pcr_validation}")

if option_type != "NONE" and entry_premium > 0:
    st.markdown("### 📋 TRADING PARAMETERS")
    
    col1, col2 = st.columns(2)
    with col1:
        st.markdown('<div style="background-color: #f8f9fa; border-radius: 10px; padding: 15px; border-left: 5px solid #007bff;">', unsafe_allow_html=True)
        st.metric(" Recommended Strike", f"{recommended_strike} {option_type}")
        st.metric(" Entry Premium", f"₹{entry_premium:.2f}")
        st.markdown('</div>', unsafe_allow_html=True)
    
    with col2:
        st.markdown('<div style="background-color: #f8f9fa; border-radius: 10px; padding: 15px; border-left: 5px solid #dc3545;">', unsafe_allow_html=True)
        st.metric("🛑 Stop Loss", f"₹{sl_premium:.2f}", delta=f"-₹{abs(entry_premium - sl_premium):.2f}")
        st.metric("🎯 Target 1", f"{target1_premium:.2f}", delta=f"+₹{target1_premium - entry_premium:.2f}")
        st.metric(" Target 2", f"₹{target2_premium:.2f}", delta=f"+₹{target2_premium - entry_premium:.2f}")
        st.markdown('</div>', unsafe_allow_html=True)
    
    st.markdown("---")
    st.markdown("###  TRADING INSTRUCTIONS")
    
    if option_type == "CE":
        st.markdown(f"""
        ✅ **Action:** Buy NIFTY {recommended_strike} CE (Next Weekly Expiry)<br>
        💰 **Entry Premium:** ₹{entry_premium:.2f}<br>
        🛑 **Stop Loss:** ₹{sl_premium:.2f} (Premium basis) - Strict SL<br>
        🎯 **Target 1:** ₹{target1_premium:.2f} (Book 50% position)<br>
        🎯 **Target 2:** ₹{target2_premium:.2f} (Trail balance)<br>
        📍 **Key Support:** {highest_pcr_strike:.0f} (PCR: {highest_pcr_value:.2f})<br>
        ⏰ **Time Frame:** Intraday/Next day expiry<br>
         **Note:** Maintain strict SL. Do not average down.
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"""
        ✅ **Action:** Buy NIFTY {recommended_strike} PE (Next Weekly Expiry)<br>
         **Entry Premium:** ₹{entry_premium:.2f}<br>
        🛑 **Stop Loss:** ₹{sl_premium:.2f} (Premium basis) - Strict SL<br>
        🎯 **Target 1:** ₹{target1_premium:.2f} (Book 50% position)<br>
        🎯 **Target 2:** ₹{target2_premium:.2f} (Trail balance)<br>
        📍 **Key Resistance:** {lowest_pcr_strike:.0f} (PCR: {lowest_pcr_value:.2f})<br>
        ⏰ **Time Frame:** Intraday/Next day expiry<br>
        💡 **Note:** Maintain strict SL. Do not average down.
        """, unsafe_allow_html=True)

st.markdown("---")

# ============================================
# 4. COMBINED OI CHANGE VISUALIZATION
# ============================================
st.subheader("📈 Combined Call & Put OI Changes")

strikes = zone_data['Strike'].values
call_oi_chg = zone_data.get('Call OI Change', pd.Series([0]*len(zone_data))).values
put_oi_chg = zone_data.get('Put OI Change', pd.Series([0]*len(zone_data))).values
net_oi = put_oi_chg - call_oi_chg

fig_combined = make_subplots(
    rows=2, cols=1,
    subplot_titles=('Combined OI Change Analysis', 'Net Sentiment (Put Writing - Call Writing)'),
    vertical_spacing=0.12, row_heights=[0.6, 0.4]
)

fig_combined.add_trace(go.Bar(x=strikes, y=-call_oi_chg, name='Call Unwinding (Bullish)', marker_color='green', opacity=0.7), row=1, col=1)
fig_combined.add_trace(go.Bar(x=strikes, y=put_oi_chg, name='Put Writing (Bullish)', marker_color='blue', opacity=0.7), row=1, col=1)

colors = ['red' if x < 0 else 'green' for x in net_oi]
fig_combined.add_trace(go.Bar(x=strikes, y=net_oi, name='Net Sentiment', marker_color=colors, opacity=0.8), row=2, col=1)
fig_combined.add_hline(y=0, line_dash="dash", line_color="gray", row=2, col=1)

fig_combined.update_layout(height=800, showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1), hovermode='x unified')
fig_combined.update_xaxes(title_text="Strike", row=2, col=1)
fig_combined.update_yaxes(title_text="OI Change", row=1, col=1)
fig_combined.update_yaxes(title_text="Net OI (Put - Call)", row=2, col=1)

st.plotly_chart(fig_combined, use_container_width=True)

# ============================================
# AUTO-REFRESH LOGIC
# ============================================
interval_seconds = {"30 seconds": 30, "60 seconds": 60, "2 minutes": 120, "5 minutes": 300}[refresh_interval]

if st.checkbox("Enable Auto-Refresh", value=True):
    time.sleep(interval_seconds)
    st.rerun()
