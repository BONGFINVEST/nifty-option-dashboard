import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import requests

st.set_page_config(page_title="NIFTY Pro Trading Dashboard - LIVE", layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
    .signal-bullish { background-color: #d4edda; color: #155724; padding: 20px; border-radius: 10px; text-align: center; font-size: 24px; font-weight: bold; margin: 10px 0; }
    .signal-bearish { background-color: #f8d7da; color: #721c24; padding: 20px; border-radius: 10px; text-align: center; font-size: 24px; font-weight: bold; margin: 10px 0; }
    .signal-neutral { background-color: #fff3cd; color: #856404; padding: 20px; border-radius: 10px; text-align: center; font-size: 24px; font-weight: bold; margin: 10px 0; }
    .live-badge { background-color: #28a745; color: white; padding: 5px 15px; border-radius: 20px; display: inline-block; animation: pulse 2s infinite; }
    @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
</style>
""", unsafe_allow_html=True)

st.title("📊 NIFTY PRO TRADING DASHBOARD")
st.markdown('<span class="live-badge">● LIVE DATA</span>', unsafe_allow_html=True)
st.markdown("---")

# Check credentials
if 'DHAN_CLIENT_ID' not in st.secrets or 'DHAN_ACCESS_TOKEN' not in st.secrets:
    st.error(" Dhan API credentials not found in Streamlit Secrets!")
    st.info("Please add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN to your Streamlit Secrets.")
    st.stop()

CLIENT_ID = st.secrets['DHAN_CLIENT_ID']
ACCESS_TOKEN = st.secrets['DHAN_ACCESS_TOKEN']

with st.sidebar:
    st.header("⚙️ Risk Parameters")
    risk_reward = st.selectbox("Risk:Reward Ratio", ["1:2", "1:3", "1:1.5"], index=1)
    sl_points = st.slider("Stop Loss (Points)", 10, 100, 30, 5)
    
    st.markdown("---")
    st.header("📊 Signal Logic")
    st.markdown("- **🟢 BUY CE:** Call OI ↓ + Put OI ↑\n- **🔴 BUY PE:** Call OI ↑ + Put OI ↓\n- **⚪ AVOID:** No clear trend")

# Function to fetch NIFTY option chain from Dhan
def fetch_dhan_option_chain():
    """Fetch NIFTY option chain from Dhan API"""
    try:
        # Dhan API v2 headers
        headers = {
            "client-id": CLIENT_ID,
            "access-token": ACCESS_TOKEN,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        # Calculate next Thursday expiry
        today = datetime.today()
        days_ahead = 3 - today.weekday()  # 3 = Thursday
        if days_ahead <= 0:
            days_ahead += 7
        next_expiry = today + timedelta(days=days_ahead)
        expiry_date = next_expiry.strftime("%Y-%m-%d")
        
        # Dhan API v2 endpoint for option chain
        url = "https://api.dhan.co/v2/optionchain"
        
        params = {
            "symbol": "NIFTY",
            "expiry": expiry_date
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            
            # Parse the response
            records = []
            for item in data:
                strike = item.get('strikePrice')
                opt_type = item.get('optionType')  # 'CE' or 'PE'
                
                row = {
                    'Strike': strike,
                    'Call OI': 0, 'Call OI Change': 0, 'Call Volume': 0, 'Call LTP': 0,
                    'Put OI': 0, 'Put OI Change': 0, 'Put Volume': 0, 'Put LTP': 0
                }
                
                if opt_type == 'CE':
                    row['Call OI'] = item.get('openInterest', 0)
                    row['Call OI Change'] = item.get('changeInOI', 0)
                    row['Call Volume'] = item.get('totalTradedVolume', 0)
                    row['Call LTP'] = item.get('lastPrice', 0)
                elif opt_type == 'PE':
                    row['Put OI'] = item.get('openInterest', 0)
                    row['Put OI Change'] = item.get('changeInOI', 0)
                    row['Put Volume'] = item.get('totalTradedVolume', 0)
                    row['Put LTP'] = item.get('lastPrice', 0)
                
                records.append(row)
            
            # Group by Strike to combine CE and PE
            df_raw = pd.DataFrame(records)
            df = df_raw.groupby('Strike').sum().reset_index()
            
            # Calculate PCR
            df['PCR'] = df['Put OI'] / df['Call OI']
            df['PCR'] = df['PCR'].replace([float('inf'), -float('inf')], 0).fillna(0)
            
            return df, None
            
        elif response.status_code == 405:
            return None, "❌ API Error 405: Method Not Allowed. Please check your API endpoint and credentials."
        elif response.status_code == 401:
            return None, "❌ API Error 401: Unauthorized. Your Access Token may be expired or invalid."
        elif response.status_code == 403:
            return None, "❌ API Error 403: Forbidden. Please check your API permissions."
        else:
            return None, f"❌ API Error {response.status_code}: {response.text}"
            
    except Exception as e:
        return None, f"❌ Connection Error: {str(e)}"

# Fetch data
with st.spinner("🔄 Fetching live NIFTY Option Chain data..."):
    df, error = fetch_dhan_option_chain()

if error:
    st.error(error)
    st.info("💡 **Troubleshooting:**\n1. Check if your Access Token is valid (expires every 24 hours)\n2. Verify your Client ID in Streamlit Secrets\n3. Ensure your Dhan API subscription is active\n4. Check Dhan API documentation for correct endpoint")
    st.stop()

st.success("✅ Live data fetched successfully!")

# Clean data
df.columns = df.columns.str.strip()
for col in df.columns:
    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

# Find ATM Strike
df['Total_Volume'] = df['Call Volume'] + df['Put Volume']
atm_row = df.loc[df['Total_Volume'].idxmax()]
atm_strike = round(atm_row['Strike'] / 50) * 50

atm_strikes = [atm_strike - 100, atm_strike - 50, atm_strike, atm_strike + 50, atm_strike + 100]
zone_data = df[df['Strike'].isin(atm_strikes)].copy()

total_call_oi_chg = zone_data['Call OI Change'].sum()
total_put_oi_chg = zone_data['Put OI Change'].sum()
total_call_oi = zone_data['Call OI'].sum()
total_put_oi = zone_data['Put OI'].sum()

# ============================================
# 1. PCR TRACKING
# ============================================
st.subheader(" PCR Analysis - Support & Resistance Levels")
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

# ============================================
# 2. WRITER STRENGTH INDICATOR
# ============================================
st.subheader("📊 Writer Strength Indicator")
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
    title={'text': f"Writer Strength Indicator<br><span style='font-size:18px'>{dominant} ({net_strength:.1f}% net)</span>", 'font': {'size': 24}},
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
        'threshold': {'line': {'color': "red", 'width': 4}, 'thickness': 0.75, 'value': 70}
    }
))
fig_gauge.update_layout(height=400)
st.plotly_chart(fig_gauge, use_container_width=True)

col1, col2, col3, col4 = st.columns(4)
with col1: st.metric("Call Writing (Bearish)", f"{call_strength:.1f}%")
with col2: st.metric("Put Writing (Bullish)", f"{put_strength:.1f}%")
with col3: st.metric("Dominant Side", dominant)
with col4: st.metric("Net Strength", f"{net_strength:.1f}%")

st.markdown("---")

# ============================================
# 3. TRADING SIGNAL & LEVELS
# ============================================
st.subheader("🚨 TRADING SIGNAL & LEVELS")

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
    signal = "🟢 Mild Bullish - Buy CE on Dips"
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
    signal = "⚪ AVOID - No Clear Signal"
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
        st.metric("🎯 Recommended Strike", f"{recommended_strike} {option_type}")
        st.metric("💰 Entry Premium", f"₹{entry_premium:.2f}")
        st.markdown('</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<div style="background-color: #f8f9fa; border-radius: 10px; padding: 15px; border-left: 5px solid #dc3545;">', unsafe_allow_html=True)
        st.metric("🛑 Stop Loss", f"₹{sl_premium:.2f}", delta=f"-₹{abs(entry_premium - sl_premium):.2f}")
        st.metric("🎯 Target 1", f"₹{target1_premium:.2f}", delta=f"+₹{target1_premium - entry_premium:.2f}")
        st.metric("🎯 Target 2", f"₹{target2_premium:.2f}", delta=f"+₹{target2_premium - entry_premium:.2f}")
        st.markdown('</div>', unsafe_allow_html=True)
    
    st.markdown("---")
    st.markdown("### 📝 TRADING INSTRUCTIONS")
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
else:
    st.warning("⚠️ No trading signal generated. Wait for clear OI patterns.")

st.markdown("---")

# ============================================
# 4. COMBINED OI CHANGE VISUALIZATION
# ============================================
st.subheader("📈 Combined Call & Put OI Changes")
st.markdown("""
**How to read this chart:**
- **Top Chart:** Shows Call Unwinding (green, inverted) vs Put Writing (blue)
- **Bottom Chart:** Net Sentiment - Green bars = Bullish dominance, Red bars = Bearish dominance
- **Higher bars** = Stronger writer activity at that strike
""")

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

st.markdown("---")
st.subheader("🎯 Signal Interpretation")
if signal == "🟢 HIGH PROBABILITY BUY CE":
    st.success(f"**STRONG BULLISH SIGNAL** - Put writers dominating with {put_strength:.1f}% strength")
    st.info("Recommended: Look for **BUY CE** opportunities on dips")
elif signal == "🔴 HIGH PROBABILITY BUY PE":
    st.error(f"**STRONG BEARISH SIGNAL** - Call writers dominating with {call_strength:.1f}% strength")
    st.info("Recommended: Look for **BUY PE** opportunities on rallies")
elif signal == "🟢 Mild Bullish - Buy CE on Dips":
    st.warning(f"⚠️ **MILD BULLISH** - Put writers slightly stronger ({put_strength:.1f}%)")
    st.info("Recommended: Wait for confirmation before entering")
elif signal == "🔴 Mild Bearish - Buy PE on Rallies":
    st.warning(f"⚠️ **MILD BEARISH** - Call writers slightly stronger ({call_strength:.1f}%)")
    st.info("Recommended: Wait for confirmation before entering")
else:
    st.warning(f"⚪ **NEUTRAL/CHURN** - No clear dominance (Call: {call_strength:.1f}%, Put: {put_strength:.1f}%)")
    st.info("Recommended: **AVOID** fresh positions, wait for clarity")
