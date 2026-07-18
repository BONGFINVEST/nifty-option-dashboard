import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import requests

# Page configuration
st.set_page_config(page_title="NIFTY Pro Trading Dashboard", layout="wide", initial_sidebar_state="expanded")

# Custom CSS
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
st.markdown("---")

# Initialize session state
if 'previous_df' not in st.session_state:
    st.session_state.previous_df = None
if 'last_fetch' not in st.session_state:
    st.session_state.last_fetch = None
if 'expiry_list' not in st.session_state:
    st.session_state.expiry_list = None
if 'expiry_list_fetched_at' not in st.session_state:
    st.session_state.expiry_list_fetched_at = None
if 'selected_expiry' not in st.session_state:
    st.session_state.selected_expiry = None

# Check Credentials
if 'DHAN_CLIENT_ID' not in st.secrets or 'DHAN_ACCESS_TOKEN' not in st.secrets:
    st.error("❌ Dhan API credentials not found in Streamlit Secrets!")
    st.stop()

CLIENT_ID = st.secrets['DHAN_CLIENT_ID']
ACCESS_TOKEN = st.secrets['DHAN_ACCESS_TOKEN']

DHAN_HEADERS = {
    "client-id": CLIENT_ID,
    "access-token": ACCESS_TOKEN,
    "Accept": "application/json",
    "Content-Type": "application/json"
}

# ==========================================
# 0. FETCH VALID EXPIRY LIST (this is the actual fix)
# ==========================================
def fetch_expiry_list():
    """
    Dhan requires an EXACT, exchange-recognized expiry date in YYYY-MM-DD
    format. Hand-calculating 'next Tuesday' produces dates that don't
    exist on the exchange (holidays, expiry-day shifts, etc.) and in the
    wrong date format, which is exactly what threw the 'Invalid Expiry
    Date' (error 811) you were seeing.
    """
    try:
        url = "https://api.dhan.co/v2/optionchain/expirylist"
        payload = {
            "UnderlyingScrip": 13,     # NIFTY
            "UnderlyingSeg": "IDX_I"
        }
        response = requests.post(url, headers=DHAN_HEADERS, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            expiries = data.get("data", [])
            if not expiries:
                return None, "Expiry list came back empty."
            return sorted(expiries), None
        else:
            return None, f"Expiry List API Error {response.status_code}: {response.text}"
    except Exception as e:
        return None, f"Expiry List Connection Error: {str(e)}"

def get_nearest_expiry(expiry_list):
    """Pick the closest expiry that is today or in the future."""
    today_str = datetime.today().strftime("%Y-%m-%d")
    upcoming = [e for e in expiry_list if e >= today_str]
    return upcoming[0] if upcoming else expiry_list[-1]

# Fetch the expiry list once per session (or once per hour) instead of
# every rerun — this also respects Dhan's rate limits.
need_refresh = (
    st.session_state.expiry_list is None
    or st.session_state.expiry_list_fetched_at is None
    or (datetime.now() - st.session_state.expiry_list_fetched_at) > timedelta(hours=1)
)

if need_refresh:
    with st.spinner("🔄 Fetching valid expiry dates..."):
        expiry_list, expiry_error = fetch_expiry_list()
    if expiry_error:
        st.error(f"❌ {expiry_error}")
        st.stop()
    st.session_state.expiry_list = expiry_list
    st.session_state.expiry_list_fetched_at = datetime.now()
    st.session_state.selected_expiry = get_nearest_expiry(expiry_list)

# Sidebar Controls
with st.sidebar:
    st.header("📅 Expiry")
    chosen_expiry = st.selectbox(
        "Select Expiry",
        options=st.session_state.expiry_list,
        index=st.session_state.expiry_list.index(st.session_state.selected_expiry)
    )
    st.session_state.selected_expiry = chosen_expiry

    st.markdown("---")
    st.header("⚙️ Risk Parameters")
    risk_reward = st.selectbox("Risk:Reward Ratio", ["1:2", "1:3", "1:1.5"], index=1)
    sl_points = st.slider("Stop Loss (Points)", 10, 100, 30, 5)

    st.markdown("---")
    st.header("📊 Signal Logic")
    st.markdown("- **🟢 BUY CE:** Call OI ↓ + Put OI ↑\n- **🔴 BUY PE:** Call OI ↑ + Put OI ↓\n- **⚪ AVOID:** No clear trend")

st.markdown(f'<span class="live-badge">● LIVE DATA (EXPIRY: {st.session_state.selected_expiry})</span>', unsafe_allow_html=True)

# ==========================================
# 1. FETCH LIVE OPTION CHAIN FOR SELECTED EXPIRY
# ==========================================
def fetch_dhan_option_chain(expiry_date):
    try:
        url = "https://api.dhan.co/v2/optionchain"
        payload = {
            "UnderlyingScrip": 13,      # 13 for NIFTY
            "UnderlyingSeg": "IDX_I",   # IDX_I for Indices
            "Expiry": expiry_date        # exact string from expirylist, YYYY-MM-DD
        }

        response = requests.post(url, headers=DHAN_HEADERS, json=payload, timeout=30)

        if response.status_code == 200:
            data = response.json()
            oc = data.get("data", {}).get("oc", {})
            records = []

            for strike_str, strike_data in oc.items():
                strike = float(strike_str)
                ce = strike_data.get("ce", {}) or {}
                pe = strike_data.get("pe", {}) or {}
                records.append({
                    'Strike': strike,
                    'Call OI': ce.get('oi', 0),
                    'Call Volume': ce.get('volume', 0),
                    'Call LTP': ce.get('last_price', 0),
                    'Put OI': pe.get('oi', 0),
                    'Put Volume': pe.get('volume', 0),
                    'Put LTP': pe.get('last_price', 0),
                })

            if not records:
                return None, "No option chain rows returned for this expiry."

            df = pd.DataFrame(records).groupby('Strike').sum().reset_index()
            df['PCR'] = df['Put OI'] / df['Call OI'].replace(0, 1)
            return df, None
        elif response.status_code == 429:
            return None, "Rate limited by Dhan (1 request / 3s for Option Chain). Slow down auto-refresh."
        else:
            return None, f"API Error {response.status_code}: {response.text}"
    except Exception as e:
        return None, f"Connection Error: {str(e)}"

# Fetch and Process Data
with st.spinner("🔄 Fetching live option chain..."):
    current_df, error = fetch_dhan_option_chain(st.session_state.selected_expiry)

if error:
    st.error(f"❌ {error}")
    st.stop()

# ==========================================
# 2. CALCULATE OI CHANGES USING MEMORY
# ==========================================
if st.session_state.previous_df is not None:
    merged = pd.merge(current_df[['Strike', 'Call OI', 'Put OI']],
                      st.session_state.previous_df[['Strike', 'Call OI', 'Put OI']],
                      on='Strike', suffixes=('_cur', '_prev'))

    merged['Call OI Change'] = merged['Call OI_cur'] - merged['Call OI_prev']
    merged['Put OI Change'] = merged['Put OI_cur'] - merged['Put OI_prev']

    current_df = current_df.merge(merged[['Strike', 'Call OI Change', 'Put OI Change']], on='Strike')
else:
    current_df['Call OI Change'] = 0
    current_df['Put OI Change'] = 0

# Update memory for next run
st.session_state.previous_df = current_df.copy()
st.session_state.last_fetch = datetime.now()

# Clean data
for col in current_df.columns:
    current_df[col] = pd.to_numeric(current_df[col], errors='coerce').fillna(0)

# Find ATM Strike
current_df['Total_Volume'] = current_df['Call Volume'] + current_df['Put Volume']
atm_strike = round(current_df.loc[current_df['Total_Volume'].idxmax()]['Strike'] / 50) * 50

# Get ATM zone data
atm_strikes = [atm_strike - 100, atm_strike - 50, atm_strike, atm_strike + 50, atm_strike + 100]
zone_data = current_df[current_df['Strike'].isin(atm_strikes)].copy()

total_call_oi_chg = zone_data['Call OI Change'].sum()
total_put_oi_chg = zone_data['Put OI Change'].sum()

# ==========================================
# 3. PCR RANKING & WRITER STRENGTH
# ==========================================
st.subheader("📊 PCR Strike Ranking (Smart Money Footprint)")
valid_pcr_df = current_df[(current_df['PCR'] > 0.1) & (current_df['PCR'] < 10.0)].copy()

col1, col2 = st.columns(2)
if not valid_pcr_df.empty:
    highest_pcr_row = valid_pcr_df.loc[valid_pcr_df['PCR'].idxmax()]
    lowest_pcr_row = valid_pcr_df.loc[valid_pcr_df['PCR'].idxmin()]

    with col1:
        st.markdown(f"""
        <div style='background-color: #d4edda; padding: 15px; border-radius: 10px; border-left: 5px solid #28a745;'>
            <h4 style='margin: 0; color: #155724;'>🟢 Highest PCR (Support)</h4>
            <h2 style='margin: 10px 0; color: #155724;'>Strike: {highest_pcr_row['Strike']:.0f}</h2>
            <p style='margin: 0; color: #155724;'>PCR: {highest_pcr_row['PCR']:.2f} | Put OI: {highest_pcr_row['Put OI']:,.0f}</p>
        </div>""", unsafe_allow_html=True)
    with col2:
        st.markdown(f"""
        <div style='background-color: #f8d7da; padding: 15px; border-radius: 10px; border-left: 5px solid #dc3545;'>
            <h4 style='margin: 0; color: #721c24;'>🔴 Lowest PCR (Resistance)</h4>
            <h2 style='margin: 10px 0; color: #721c24;'>Strike: {lowest_pcr_row['Strike']:.0f}</h2>
            <p style='margin: 0; color: #721c24;'>PCR: {lowest_pcr_row['PCR']:.2f} | Call OI: {lowest_pcr_row['Call OI']:,.0f}</p>
        </div>""", unsafe_allow_html=True)

st.markdown("---")

st.subheader("📊 Writer Strength Indicator")
total_activity = abs(total_call_oi_chg) + abs(total_put_oi_chg)
call_strength = (abs(total_call_oi_chg) / total_activity * 100) if total_activity > 0 else 50
put_strength = (abs(total_put_oi_chg) / total_activity * 100) if total_activity > 0 else 50

dominant = "BULLISH" if put_strength > call_strength else "BEARISH"
net_strength = abs(put_strength - call_strength)

fig_gauge = go.Figure(go.Indicator(
    mode="gauge+number+delta", value=put_strength,
    title={'text': f"Writer Strength<br><span>{dominant} ({net_strength:.1f}% net)</span>"},
    gauge={'axis': {'range': [0, 100]}, 'bar': {'color': "darkblue"},
           'steps': [{'range': [0, 30], 'color': '#ffcccc'}, {'range': [30, 70], 'color': '#ffffcc'}, {'range': [70, 100], 'color': '#ccffcc'}]}
))
st.plotly_chart(fig_gauge, use_container_width=True)

# ==========================================
# 4. TRADING SIGNAL & LEVELS
# ==========================================
st.subheader("🚨 TRADING SIGNAL & LEVELS")
atm_data = current_df[current_df['Strike'] == atm_strike].iloc[0] if len(current_df[current_df['Strike'] == atm_strike]) > 0 else None
ce_ltp = atm_data['Call LTP'] if atm_data is not None else 100
pe_ltp = atm_data['Put LTP'] if atm_data is not None else 100

signal, signal_color, option_type, entry_premium, sl_premium = "⚪ AVOID - No Clear Signal", "gray", "NONE", 0, 0

if total_call_oi_chg < -500000 and total_put_oi_chg > 1000000 and put_strength > 60:
    signal, signal_color, option_type, entry_premium = "🟢 HIGH PROBABILITY BUY CE", "green", "CE", ce_ltp
    sl_premium = max(entry_premium - sl_points, 5)
elif total_call_oi_chg > 1000000 and total_put_oi_chg < -500000 and call_strength > 60:
    signal, signal_color, option_type, entry_premium = "🔴 HIGH PROBABILITY BUY PE", "red", "PE", pe_ltp
    sl_premium = max(entry_premium - sl_points, 5)
elif total_call_oi_chg < 0 and total_put_oi_chg > 0 and put_strength > 50:
    signal, signal_color, option_type, entry_premium = "🟢 Mild Bullish - Buy CE on Dips", "lightgreen", "CE", ce_ltp * 0.9
    sl_premium = max(entry_premium - 20, 5)
elif total_call_oi_chg > 0 and total_put_oi_chg < 0 and call_strength > 50:
    signal, signal_color, option_type, entry_premium = "🔴 Mild Bearish - Buy PE on Rallies", "salmon", "PE", pe_ltp * 0.9
    sl_premium = max(entry_premium - 20, 5)

st.markdown(f"""
<div style='background-color: {signal_color}; padding: 30px; border-radius: 10px; text-align: center; margin-bottom: 20px;'>
    <h2 style='color: white; margin: 0;'>{signal}</h2>
</div>""", unsafe_allow_html=True)

if option_type != "NONE":
    target1 = entry_premium + (sl_points * 2)
    target2 = entry_premium + (sl_points * 3)
    col1, col2 = st.columns(2)
    with col1:
        st.markdown('<div style="background-color: #f8f9fa; border-radius: 10px; padding: 15px; border-left: 5px solid #007bff;">', unsafe_allow_html=True)
        st.metric("🎯 Recommended Strike", f"{atm_strike} {option_type}")
        st.metric("💰 Entry Premium", f"₹{entry_premium:.2f}")
        st.markdown('</div>', unsafe_allow_html=True)
    with col2:
        st.markdown('<div style="background-color: #f8f9fa; border-radius: 10px; padding: 15px; border-left: 5px solid #dc3545;">', unsafe_allow_html=True)
        st.metric("🛑 Stop Loss", f"₹{sl_premium:.2f}", delta=f"-{abs(entry_premium - sl_premium):.2f}")
        st.metric("🎯 Target 1", f"₹{target1:.2f}", delta=f"+₹{target1 - entry_premium:.2f}")
        st.metric("🎯 Target 2", f"₹{target2:.2f}", delta=f"+₹{target2 - entry_premium:.2f}")
        st.markdown('</div>', unsafe_allow_html=True)

st.markdown("---")

# ==========================================
# 5. COMBINED OI CHANGE VISUALIZATION
# ==========================================
st.subheader("📈 Combined Call & Put OI Changes")
strikes = zone_data['Strike'].values
call_chg = zone_data['Call OI Change'].values
put_chg = zone_data['Put OI Change'].values
net_oi = put_chg - call_chg

fig_combined = make_subplots(rows=2, cols=1, subplot_titles=('Combined OI Change Analysis', 'Net Sentiment'), vertical_spacing=0.12, row_heights=[0.6, 0.4])
fig_combined.add_trace(go.Bar(x=strikes, y=-call_chg, name='Call Unwinding', marker_color='green', opacity=0.7), row=1, col=1)
fig_combined.add_trace(go.Bar(x=strikes, y=put_chg, name='Put Writing', marker_color='blue', opacity=0.7), row=1, col=1)
colors = ['red' if x < 0 else 'green' for x in net_oi]
fig_combined.add_trace(go.Bar(x=strikes, y=net_oi, name='Net Sentiment', marker_color=colors, opacity=0.8), row=2, col=1)
fig_combined.add_hline(y=0, line_dash="dash", line_color="gray", row=2, col=1)
fig_combined.update_layout(height=800, showlegend=True, legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
st.plotly_chart(fig_combined, use_container_width=True)

st.info(f"🕐 Last Updated: {st.session_state.last_fetch.strftime('%H:%M:%S') if st.session_state.last_fetch else 'N/A'}")
