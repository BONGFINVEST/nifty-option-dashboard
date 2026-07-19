"""
INSTITUTIONAL NIFTY OI SCANNER
==============================
This automates the exact manual workflow from OI_Analysis_NIFTY.xlsx:

  Zone A (ATM +-6 strikes) -> PCR regime classification
                              (Analysis!B15/B16 -> OverBought/Bullish/Neutral/Bearish/Oversold)
  Zone B (ATM +-3 strikes) -> writer-activity signal from OI-change % contribution
                              (Analysis!A19:H29 -> Buy CE / Write PE / Buy PE / Write CE / Neutral)
  Master Signal            -> the 'Dash Board'!F1 composite formula
                              (Strong CE Buy / Strong PE Buy / PE writers strong /
                               CE writers strong / wait for data confirmation)
  Action tag                -> 'Dash Board'!L3 lookup table (incl. Reversal detection)

Everything above is YOUR proven logic, ported 1:1 from the formulas in your workbook.
Nothing about it has been changed. On top of it this adds a separate, clearly-labeled
CONFLUENCE panel (spot vs VWAP, Max Pain, bid/ask liquidity, days-to-expiry gamma risk)
that never overrides the core signal -- it just tells you how much independent support
that signal has right now, so you can size conviction accordingly.

Data source: Dhan API v2 Option Chain (see fetch_option_chain for schema notes).
Sensibull's CSV had "CE OI change" as a pre-computed column; Dhan gives the same
thing natively via `previous_oi` (oi - previous_oi = today's cumulative change),
so no manual VLOOKUP/diffing is needed anymore.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from pathlib import Path
import requests
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="Institutional NIFTY OI Scanner", layout="wide", initial_sidebar_state="expanded")

# ==========================================
# CONSTANTS
# ==========================================
REFRESH_INTERVAL_MS = 10_000
IDLE_CHECK_INTERVAL_MS = 60_000
GSHEET_WRITE_THROTTLE_SECONDS = 60

IST = ZoneInfo("Asia/Kolkata")
NSE_OPEN = dtime(9, 15)
NSE_CLOSE = dtime(15, 30)

STRIKE_STEP = 50           # NIFTY strike interval
ZONE_A_WIDTH = 6           # PCR classification zone: ATM +- 6 strikes (matches Analysis!A2:E14)
ZONE_B_WIDTH = 3           # OI-change writer-signal zone: ATM +- 3 strikes (matches Analysis!A21:H28)

# Thresholds -- copied verbatim from your workbook's formulas. Editable in the sidebar
# under "Advanced (Excel-equivalent) Settings" so you can retune without touching code.
DEFAULT_PCR_THRESHOLDS = {"overbought": 1.48, "bullish": 1.00, "bearish": 0.80}   # Analysis!B15
DEFAULT_SIGNAL_THRESHOLDS = {"strong": 20, "mild": 10}                            # Analysis!B20
DEFAULT_MASTER_THRESHOLDS = {                                                     # Dash Board!F1
    "pcr_high": 1.0, "pcr_low": 0.8, "pcr_ce_writers": 0.7,
    "vol_imbalance_strong": 5, "vol_imbalance_mild": 1,
}

# ==========================================
# PERSISTENCE (local disk + Google Sheets)
# ==========================================
SNAPSHOT_DIR = Path("nifty_oi_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)
LOG_DIR = Path("nifty_session_logs")
LOG_DIR.mkdir(exist_ok=True)

GSHEET_SNAPSHOT_SHEET = "closing_snapshot"
GSHEET_LOG_SHEET = "session_log"


def save_chain_snapshot(df: pd.DataFrame, fetched_at: datetime, expiry: str):
    try:
        out = df.copy()
        out['_fetched_at'] = fetched_at.isoformat()
        out['_expiry'] = expiry
        out.to_csv(SNAPSHOT_DIR / f"{fetched_at.strftime('%Y-%m-%d')}.csv", index=False)
    except Exception as e:
        st.sidebar.caption(f"⚠️ Chain snapshot save failed: {e}")


def append_log_row(row: dict, date_str: str):
    """Append one poll's summary row to today's local CSV log (the automated
    replacement for manually pasting Dash Board!A3:N3 into a new row)."""
    try:
        path = LOG_DIR / f"{date_str}.csv"
        row_df = pd.DataFrame([row])
        if path.exists():
            row_df.to_csv(path, mode='a', header=False, index=False)
        else:
            row_df.to_csv(path, mode='w', header=True, index=False)
    except Exception as e:
        st.sidebar.caption(f"⚠️ Log append failed: {e}")


def load_today_log(date_str: str) -> pd.DataFrame:
    path = LOG_DIR / f"{date_str}.csv"
    if path.exists():
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def gsheets_configured():
    return "gcp_service_account" in st.secrets and "GOOGLE_SHEET_ID" in st.secrets


@st.cache_resource(show_spinner=False)
def get_gsheet_client():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(dict(st.secrets["gcp_service_account"]), scopes=scopes)
    return gspread.authorize(creds)


def get_gsheet_worksheet(name: str, rows=2000, cols=30):
    import gspread
    client = get_gsheet_client()
    sh = client.open_by_key(st.secrets["GOOGLE_SHEET_ID"])
    try:
        return sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=rows, cols=cols)


def save_chain_snapshot_to_gsheet(df: pd.DataFrame, fetched_at: datetime, expiry: str):
    if not gsheets_configured():
        return
    try:
        from gspread_dataframe import set_with_dataframe
        ws = get_gsheet_worksheet(GSHEET_SNAPSHOT_SHEET)
        out = df.copy()
        out['_fetched_at'] = fetched_at.isoformat()
        out['_expiry'] = expiry
        ws.clear()
        set_with_dataframe(ws, out, include_index=False, resize=True)
    except Exception as e:
        st.sidebar.caption(f"⚠️ Google Sheet snapshot save failed: {e}")


def append_log_row_to_gsheet(row: dict):
    if not gsheets_configured():
        return
    try:
        ws = get_gsheet_worksheet(GSHEET_LOG_SHEET)
        existing = ws.get_all_values()
        if not existing:
            ws.append_row(list(row.keys()))
        ws.append_row([str(v) for v in row.values()])
    except Exception as e:
        st.sidebar.caption(f"⚠️ Google Sheet log append failed: {e}")


def load_latest_chain_snapshot():
    try:
        files = sorted(SNAPSHOT_DIR.glob("*.csv"))
        if not files:
            return None, None
        df = pd.read_csv(files[-1])
        fetched_at = pd.to_datetime(df['_fetched_at'].iloc[0]) if '_fetched_at' in df.columns else None
        df = df.drop(columns=[c for c in ['_fetched_at', '_expiry'] if c in df.columns])
        return df, fetched_at
    except Exception:
        return None, None


def load_latest_chain_snapshot_from_gsheet():
    if not gsheets_configured():
        return None, None
    try:
        from gspread_dataframe import get_as_dataframe
        ws = get_gsheet_worksheet(GSHEET_SNAPSHOT_SHEET)
        df = get_as_dataframe(ws, evaluate_formulas=True).dropna(how='all')
        if df.empty:
            return None, None
        fetched_at = pd.to_datetime(df['_fetched_at'].iloc[0]) if '_fetched_at' in df.columns else None
        df = df.drop(columns=[c for c in ['_fetched_at', '_expiry'] if c in df.columns])
        return df, fetched_at
    except Exception:
        return None, None


# ==========================================
# MARKET HOURS
# ==========================================
def market_status(now_ist=None):
    now_ist = now_ist or datetime.now(IST)
    weekday = now_ist.weekday()
    if weekday >= 5:
        return False, "Weekend — NSE is closed.", now_ist
    if now_ist.time() < NSE_OPEN:
        return False, f"Pre-market — NSE opens at {NSE_OPEN.strftime('%H:%M')} IST.", now_ist
    if now_ist.time() > NSE_CLOSE:
        return False, f"Post-market — NSE closed at {NSE_CLOSE.strftime('%H:%M')} IST.", now_ist
    return True, "Market open.", now_ist


# ==========================================
# CREDENTIALS
# ==========================================
if 'DHAN_CLIENT_ID' not in st.secrets or 'DHAN_ACCESS_TOKEN' not in st.secrets:
    st.error("❌ Dhan API credentials not found in Streamlit Secrets!")
    st.stop()

CLIENT_ID = st.secrets['DHAN_CLIENT_ID']
ACCESS_TOKEN = st.secrets['DHAN_ACCESS_TOKEN']
DHAN_HEADERS = {
    "client-id": CLIENT_ID, "access-token": ACCESS_TOKEN,
    "Accept": "application/json", "Content-Type": "application/json",
}
NIFTY_SCRIP, NIFTY_SEG = 13, "IDX_I"

# ==========================================
# DHAN DATA FETCH
# ==========================================
AUTH_ERROR_PREFIX = "🔑 TOKEN_EXPIRED:"


def dhan_error_message(status_code: int, text: str) -> str:
    """Turns a raw Dhan HTTP error into an actionable message. 401/403 almost
    always means the access token (regenerated daily, per your workflow) has
    expired or wasn't pasted correctly into Streamlit Secrets — that's a
    completely different fix from a genuine API/data problem, so it gets a
    distinct, clearly-flagged message rather than a raw JSON dump."""
    if status_code in (401, 403):
        return (f"{AUTH_ERROR_PREFIX} Dhan rejected the request (HTTP {status_code}) — "
                f"your access token has most likely expired or is missing/incorrect.")
    if status_code == 429:
        return "Rate limited by Dhan (1 req/3s on Option Chain). Will retry on the next poll."
    return f"API Error {status_code}: {text}"


def is_auth_error(msg: str) -> bool:
    return bool(msg) and msg.startswith(AUTH_ERROR_PREFIX)


def fetch_expiry_list():
    try:
        r = requests.post("https://api.dhan.co/v2/optionchain/expirylist", headers=DHAN_HEADERS,
                           json={"UnderlyingScrip": NIFTY_SCRIP, "UnderlyingSeg": NIFTY_SEG}, timeout=15)
        if r.status_code == 200:
            data = r.json().get("data", [])
            return (sorted(data), None) if data else (None, "Expiry list came back empty.")
        return None, dhan_error_message(r.status_code, r.text)
    except Exception as e:
        return None, f"Expiry List Connection Error: {e}"


def get_nearest_expiry(expiry_list):
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    upcoming = [e for e in expiry_list if e >= today_str]
    return upcoming[0] if upcoming else expiry_list[-1]


def fetch_option_chain(expiry_date: str):
    """
    Dhan v2 Option Chain response shape:
      data.last_price               -> underlying spot LTP
      data.oc["<strike>"].ce / .pe  -> per-side dict with:
        oi, previous_oi             -> today's OI change = oi - previous_oi (matches
                                        Sensibull's pre-computed 'OI change' column
                                        your Excel used to VLOOKUP)
        volume, previous_volume
        last_price, previous_close_price
        implied_volatility
        greeks: {delta, theta, gamma, vega}
        top_bid_price, top_bid_quantity, top_ask_price, top_ask_quantity
    """
    try:
        r = requests.post("https://api.dhan.co/v2/optionchain", headers=DHAN_HEADERS,
                           json={"UnderlyingScrip": NIFTY_SCRIP, "UnderlyingSeg": NIFTY_SEG, "Expiry": expiry_date},
                           timeout=30)
        if r.status_code != 200:
            return None, None, dhan_error_message(r.status_code, r.text)

        payload = r.json().get("data", {})
        spot = payload.get("last_price")
        oc = payload.get("oc", {})
        rows = []
        for strike_str, sd in oc.items():
            strike = float(strike_str)
            ce, pe = (sd.get("ce") or {}), (sd.get("pe") or {})
            ce_g, pe_g = (ce.get("greeks") or {}), (pe.get("greeks") or {})

            def g(d, key, default=0):
                v = d.get(key, default)
                return v if v is not None else default

            rows.append({
                'Strike': strike,
                'CE_OI': g(ce, 'oi'), 'CE_OI_prev': g(ce, 'previous_oi'),
                'CE_Volume': g(ce, 'volume'), 'CE_Volume_prev': g(ce, 'previous_volume'),
                'CE_LTP': g(ce, 'last_price'), 'CE_prevClose': g(ce, 'previous_close_price'),
                'CE_IV': g(ce, 'implied_volatility'),
                'CE_Delta': g(ce_g, 'delta'), 'CE_Theta': g(ce_g, 'theta'),
                'CE_Gamma': g(ce_g, 'gamma'), 'CE_Vega': g(ce_g, 'vega'),
                'CE_Bid': g(ce, 'top_bid_price'), 'CE_Ask': g(ce, 'top_ask_price'),
                'PE_OI': g(pe, 'oi'), 'PE_OI_prev': g(pe, 'previous_oi'),
                'PE_Volume': g(pe, 'volume'), 'PE_Volume_prev': g(pe, 'previous_volume'),
                'PE_LTP': g(pe, 'last_price'), 'PE_prevClose': g(pe, 'previous_close_price'),
                'PE_IV': g(pe, 'implied_volatility'),
                'PE_Delta': g(pe_g, 'delta'), 'PE_Theta': g(pe_g, 'theta'),
                'PE_Gamma': g(pe_g, 'gamma'), 'PE_Vega': g(pe_g, 'vega'),
                'PE_Bid': g(pe, 'top_bid_price'), 'PE_Ask': g(pe, 'top_ask_price'),
            })
        if not rows:
            return None, None, "No strikes returned for this expiry."

        df = pd.DataFrame(rows).sort_values('Strike').reset_index(drop=True)
        df['CE_OI_chg'] = df['CE_OI'] - df['CE_OI_prev']
        df['PE_OI_chg'] = df['PE_OI'] - df['PE_OI_prev']
        df['PCR'] = df['PE_OI'] / df['CE_OI'].replace(0, np.nan)
        return spot, df, None
    except Exception as e:
        return None, None, f"Connection Error: {e}"


def render_fetch_error(error: str):
    """Renders a token-expiry error distinctly from a generic API error, since
    the fix is completely different (regenerate token vs. investigate a real
    problem) and a raw JSON dump doesn't make that obvious at a glance."""
    if is_auth_error(error):
        st.session_state.token_status = 'expired'
        st.error(
            "🔑 **Dhan access token has expired or is invalid.**\n\n"
            "This is expected once a day with a 24-hour token — not a bug. To fix:\n\n"
            "1. Dhan app/web → **My Profile → DhanHQ Trading APIs → Generate Token**\n"
            "2. Copy the new token\n"
            "3. Streamlit Cloud → your app → **Settings → Secrets** → update `DHAN_ACCESS_TOKEN` → Save\n\n"
            "The app will reconnect automatically on its next poll once the new token is saved "
            "— no need to redeploy or restart anything manually."
        )
    else:
        st.session_state.token_status = 'ok'
        st.error(f"❌ {error}")
    st.stop()


def fetch_vwap_bias():
    """Best-effort intraday VWAP for NIFTY spot via Dhan's intraday-candle
    endpoint. Dhan's exact segment/instrument code for the NIFTY *index*
    (as opposed to equity/futures) isn't fully nailed down from public docs,
    so this fails soft: any schema mismatch just disables the VWAP panel
    rather than showing a wrong number. If it errors for you, check
    https://dhanhq.co/docs/v2/historical-data/ for the exact index paylod
    shape and I'll patch the two lines below."""
    try:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        payload = {
            "securityId": str(NIFTY_SCRIP), "exchangeSegment": "IDX_I", "instrument": "INDEX",
            "interval": "5", "oi": False,
            "fromDate": f"{today_str} 09:15:00", "toDate": f"{today_str} 23:59:59",
        }
        r = requests.post("https://api.dhan.co/v2/charts/intraday", headers=DHAN_HEADERS, json=payload, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        closes, highs, lows, vols = data.get('close'), data.get('high'), data.get('low'), data.get('volume')
        if not closes or not vols or sum(vols) == 0:
            return None
        typical = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        vwap = sum(t * v for t, v in zip(typical, vols)) / sum(vols)
        return vwap
    except Exception:
        return None


# ==========================================
# ZONE A — PCR REGIME CLASSIFICATION (Analysis!A2:E16)
# ==========================================
# NOTE ON A FINDING IN YOUR ORIGINAL WORKBOOK:
# Analysis!A15 is '=SUM(A2:A13)', but your strike ladder actually fills A2:A14
# (13 rows, ATM-6 to ATM+6). The SUM range stops one row short, so the ATM+6
# strike is silently excluded from every Zone A total -- the real zone your
# sheet has been computing is ATM-6*50 .. ATM+5*50 (12 strikes), not a
# symmetric ATM+-6. I verified this against your live numbers: only the
# asymmetric range reproduces your actual B16 PCR of 1.5284... / B15
# "OverBought". A symmetric ATM+-6 zone gives a different PCR (1.4344) and
# would have classified as "Bullish" instead -- i.e. it would silently
# change what your calibrated 1.48 OverBought threshold means.
# I've replicated your ACTUAL (asymmetric) range below since that's what
# your live threshold is tuned against. Toggle 'symmetric_zone_a' in the
# sidebar if you'd rather fix it to a clean ATM+-6 going forward -- just
# know your OverBought/Bullish/Bearish/Oversold thresholds may need
# retuning if you do, since the zone's OI totals will shift.
def zone_a_classification(df: pd.DataFrame, atm: float, width: int, thresholds: dict, symmetric: bool = False):
    lower = atm - width * STRIKE_STEP
    upper = atm + width * STRIKE_STEP if symmetric else atm + (width - 1) * STRIKE_STEP
    zone = df[(df['Strike'] >= lower) & (df['Strike'] <= upper)]
    ce_oi_sum, pe_oi_sum = zone['CE_OI'].sum(), zone['PE_OI'].sum()
    pcr = (pe_oi_sum / ce_oi_sum) if ce_oi_sum else np.nan
    if pd.isna(pcr):
        classification = "N/A"
    elif pcr > thresholds['overbought']:
        classification = "OverBought"
    elif pcr > thresholds['bullish']:
        classification = "Bullish"
    elif pcr == thresholds['bullish']:
        classification = "Neutral"
    elif pcr > thresholds['bearish']:
        classification = "Bearish"
    else:
        classification = "Oversold"
    return {'zone': zone, 'ce_oi_sum': ce_oi_sum, 'pe_oi_sum': pe_oi_sum, 'pcr': pcr, 'classification': classification}


# ==========================================
# ZONE B — OI-CHANGE WRITER SIGNAL (Analysis!A19:H29)
# ==========================================
def zone_b_signal(df: pd.DataFrame, atm: float, width: int, thresholds: dict):
    zone = df[(df['Strike'] >= atm - width * STRIKE_STEP) & (df['Strike'] <= atm + width * STRIKE_STEP)]
    ce_chg_sum, pe_chg_sum = zone['CE_OI_chg'].sum(), zone['PE_OI_chg'].sum()
    total_chg_base = ce_chg_sum + pe_chg_sum
    if total_chg_base == 0:
        choi_ce, choi_pe = 0.0, 0.0
    else:
        choi_ce = ce_chg_sum / total_chg_base * 100
        choi_pe = pe_chg_sum / total_chg_base * 100

    ce_vol_sum, pe_vol_sum = zone['CE_Volume'].sum(), zone['PE_Volume'].sum()
    total_vol = ce_vol_sum + pe_vol_sum
    ce_vol_pct = (ce_vol_sum / total_vol * 100) if total_vol else 50.0
    pe_vol_pct = 100 - ce_vol_pct
    ce_vol_imbalance = ce_vol_pct - pe_vol_pct  # Analysis!F19 = E20-F20, E20=CE vol%, F20=PE vol%

    diff = choi_pe - choi_ce
    if diff > thresholds['strong']:
        signal = "Buy CE"
    elif diff > thresholds['mild']:
        signal = "Write PE"
    elif -diff > thresholds['strong']:
        signal = "Buy PE"
    elif -diff > thresholds['mild']:
        signal = "Write CE"
    else:
        signal = "Neutral"

    ce_ltp_avg = zone['CE_LTP'].mean() if len(zone) else np.nan
    pe_ltp_avg = zone['PE_LTP'].mean() if len(zone) else np.nan

    return {
        'zone': zone, 'ce_chg_sum': ce_chg_sum, 'pe_chg_sum': pe_chg_sum,
        'choi_ce': choi_ce, 'choi_pe': choi_pe, 'ce_vol_imbalance': ce_vol_imbalance,
        'signal': signal, 'ce_ltp_avg': ce_ltp_avg, 'pe_ltp_avg': pe_ltp_avg,
    }


# ==========================================
# MASTER SIGNAL — 'Dash Board'!F1
# ==========================================
def master_signal(pcr, ce_vol_imbalance, choi_ce, choi_pe, ce_ltp, pe_ltp, t: dict):
    if pd.isna(pcr):
        return "wait for data confirmation"
    if pcr > t['pcr_high'] and ce_vol_imbalance < -t['vol_imbalance_strong'] and choi_pe > choi_ce and ce_ltp > pe_ltp:
        return "Strong CE Buy"
    if pcr < t['pcr_low'] and ce_vol_imbalance > t['vol_imbalance_strong'] and choi_pe < choi_ce and ce_ltp < pe_ltp:
        return "Strong PE Buy"
    if pcr >= t['pcr_high'] and choi_pe > choi_ce and ce_vol_imbalance < -t['vol_imbalance_mild']:
        return "PE writers strong"
    if pcr < t['pcr_ce_writers'] and choi_ce > choi_pe and ce_vol_imbalance > t['vol_imbalance_mild']:
        return "CE writers strong"
    return "wait for data confirmation"


# ==========================================
# ACTION TAG — 'Dash Board'!L3 (ordered lookup, first match wins)
# ==========================================
ACTION_RULES = [
    ("Oversold", "Neutral", "Write PE"),
    ("Bearish", "Neutral", "wait"),
    ("OverBought", "Neutral", "Write CE"),
    ("Bullish", "Neutral", "wait"),
    ("Bullish", "Write PE", "Buy CE"),
    ("Bearish", "Write CE", "Buy PE"),
    ("Oversold", "Write PE", "Write PE"),
    ("Bearish", "Write PE", "Write PE"),
    ("Bearish", "Buy CE", "Reversal"),
    ("Bullish", "Buy PE", "Reversal"),
    ("Bullish", "Buy CE", "Buy CE"),
    ("Oversold", "Buy PE", "Buy PE/Write CE"),
    ("Oversold", "Buy CE", "Buy CE/Write PE"),
    ("Oversold", "Write CE", "Write CE"),
]


def action_tag(classification: str, signal: str) -> str:
    for cls, sig, action in ACTION_RULES:
        if classification == cls and signal == sig:
            return action
    return ""


# ==========================================
# MAX PAIN (new — not in the original workbook)
# ==========================================
def max_pain(df: pd.DataFrame):
    strikes = df['Strike'].values
    ce_oi = df['CE_OI'].values
    pe_oi = df['PE_OI'].values
    pains = []
    for k in strikes:
        call_writer_loss = np.sum(ce_oi * np.maximum(k - strikes, 0))
        put_writer_loss = np.sum(pe_oi * np.maximum(strikes - k, 0))
        pains.append(call_writer_loss + put_writer_loss)
    idx = int(np.argmin(pains))
    return float(strikes[idx])


# ==========================================
# LIQUIDITY CHECK (new)
# ==========================================
def spread_pct(bid, ask):
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100 if mid else None


# ==========================================
# HIGHEST-PCR STRIKES (auto replacement for manual Analysis!H8:I9)
# ==========================================
def top_pcr_strikes(df: pd.DataFrame, top_n=2, min_ce_oi_frac=0.01):
    """Only considers strikes with at least min_ce_oi_frac of the chain's max CE OI,
    so a near-zero-CE-OI strike doesn't produce a meaningless huge PCR."""
    floor = df['CE_OI'].max() * min_ce_oi_frac
    valid = df[(df['CE_OI'] >= floor) & df['PCR'].notna()]
    return valid.nlargest(top_n, 'PCR')[['Strike', 'PCR']].reset_index(drop=True)


# ==========================================
# SESSION STATE INIT
# ==========================================
for key, default in [
    ('previous_df', None), ('last_fetch', None), ('expiry_list', None),
    ('expiry_list_fetched_at', None), ('selected_expiry', None),
    ('baseline_source', None), ('last_gsheet_write', None),
    ('session_log', []), ('vwap_session_start', None), ('token_status', None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

if st.session_state.previous_df is None:
    recovered_df, recovered_ts, recovered_source = None, None, None
    if gsheets_configured():
        gs_df, gs_ts = load_latest_chain_snapshot_from_gsheet()
        if gs_df is not None:
            recovered_df, recovered_ts, recovered_source = gs_df, gs_ts, 'gsheet'
    if recovered_df is None:
        d_df, d_ts = load_latest_chain_snapshot()
        if d_df is not None:
            recovered_df, recovered_ts, recovered_source = d_df, d_ts, 'disk'
    if recovered_df is not None:
        st.session_state.previous_df = recovered_df
        st.session_state.baseline_source = (recovered_source, recovered_ts)

today_str = datetime.now(IST).strftime("%Y-%m-%d")
if not st.session_state.session_log:
    prior_log = load_today_log(today_str)
    if not prior_log.empty:
        st.session_state.session_log = prior_log.to_dict('records')

# ==========================================
# EXPIRY LIST
# ==========================================
need_refresh = (
    st.session_state.expiry_list is None or st.session_state.expiry_list_fetched_at is None
    or (datetime.now() - st.session_state.expiry_list_fetched_at) > timedelta(hours=1)
)
if need_refresh:
    expiry_list, expiry_error = fetch_expiry_list()
    if expiry_error:
        render_fetch_error(expiry_error)
    st.session_state.expiry_list = expiry_list
    st.session_state.expiry_list_fetched_at = datetime.now()
    if st.session_state.selected_expiry not in expiry_list:
        st.session_state.selected_expiry = get_nearest_expiry(expiry_list)

# ==========================================
# SIDEBAR
# ==========================================
with st.sidebar:
    st.header("📅 Expiry")
    st.session_state.selected_expiry = st.selectbox(
        "Select Expiry", options=st.session_state.expiry_list,
        index=st.session_state.expiry_list.index(st.session_state.selected_expiry))

    st.markdown("---")
    st.header("🔄 Auto-Refresh")
    auto_refresh_on = st.checkbox("Enable 10s auto-refresh", value=True)

    st.markdown("---")
    st.header("🔑 Dhan Token")
    if st.session_state.token_status == 'expired':
        st.error("Expired — see banner above")
    elif st.session_state.token_status == 'ok':
        st.caption("🟢 Valid — last confirmed working this session")
    else:
        st.caption("⚪ Not checked yet")

    st.markdown("---")
    st.header("💾 Persistence")
    st.caption("🟢 Google Sheets connected" if gsheets_configured() else "⚪ Google Sheets not configured (local disk only)")

    st.markdown("---")
    with st.expander("⚙️ Advanced (Excel-equivalent) Settings"):
        st.caption("These map 1:1 to the constants in your Analysis/Dash Board formulas.")
        zone_a_width = st.number_input("Zone A width (ATM ± N strikes, PCR regime)", 1, 15, ZONE_A_WIDTH)
        zone_b_width = st.number_input("Zone B width (ATM ± N strikes, writer signal)", 1, 10, ZONE_B_WIDTH)
        overbought_th = st.number_input("PCR OverBought threshold", value=DEFAULT_PCR_THRESHOLDS['overbought'], step=0.01)
        bullish_th = st.number_input("PCR Bullish threshold", value=DEFAULT_PCR_THRESHOLDS['bullish'], step=0.01)
        bearish_th = st.number_input("PCR Bearish threshold", value=DEFAULT_PCR_THRESHOLDS['bearish'], step=0.01)
        strong_diff_th = st.number_input("Zone B 'strong' %-diff threshold", value=DEFAULT_SIGNAL_THRESHOLDS['strong'])
        mild_diff_th = st.number_input("Zone B 'mild' %-diff threshold", value=DEFAULT_SIGNAL_THRESHOLDS['mild'])
        symmetric_zone_a = st.checkbox(
            "Use symmetric ATM±6 for Zone A (your original sheet is asymmetric ATM-6/+5 — see code comment)",
            value=False)

    st.markdown("---")
    with st.expander("🧭 Confluence filters"):
        use_vwap = st.checkbox("Spot vs VWAP filter", value=True)
        use_max_pain = st.checkbox("Max Pain", value=True)
        use_liquidity = st.checkbox("Bid/Ask liquidity check", value=True)
        liquidity_spread_limit = st.number_input("Flag spread wider than (%)", value=5.0, step=0.5)

pcr_thresholds = {"overbought": overbought_th, "bullish": bullish_th, "bearish": bearish_th}
signal_thresholds = {"strong": strong_diff_th, "mild": mild_diff_th}

# ==========================================
# MARKET STATUS
# ==========================================
is_open, status_reason, now_ist = market_status()
should_poll = auto_refresh_on and is_open
if should_poll:
    st_autorefresh(interval=REFRESH_INTERVAL_MS, key="oi_autorefresh")
elif not is_open:
    st_autorefresh(interval=IDLE_CHECK_INTERVAL_MS, key="idle_clock_check")

st.title("🏛️ Institutional NIFTY OI Scanner")
status_col1, status_col2 = st.columns([3, 1])
with status_col1:
    if is_open:
        st.markdown('<span style="background:#28a745;color:white;padding:5px 15px;border-radius:20px;">● LIVE — '
                     f'Expiry {st.session_state.selected_expiry}</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span style="background:#6c757d;color:white;padding:5px 15px;border-radius:20px;">● MARKET CLOSED</span>',
                     unsafe_allow_html=True)
        st.caption(f"{status_reason} (IST now: {now_ist.strftime('%a %d-%b %H:%M:%S')})")
with status_col2:
    st.caption("🔄 Polling every 10s" if should_poll else ("⏸️ Closed — polling stopped" if not is_open else "⏸️ Paused"))

if not is_open and st.session_state.previous_df is None:
    st.warning("Market is closed and no data is available yet — no live fetch this session, "
               "and no prior snapshot found on Google Sheets or local disk.")
    st.stop()

# ==========================================
# FETCH (only during market hours)
# ==========================================
if is_open:
    with st.spinner("Fetching live option chain..."):
        spot, df, error = fetch_option_chain(st.session_state.selected_expiry)
    if error:
        render_fetch_error(error)
    st.session_state.token_status = 'ok'
    st.session_state.previous_df = df.copy()
    st.session_state.last_fetch = datetime.now()
    st.session_state.baseline_source = 'live'
    save_chain_snapshot(df, st.session_state.last_fetch, st.session_state.selected_expiry)

    last_write = st.session_state.last_gsheet_write
    if gsheets_configured() and (last_write is None or (datetime.now() - last_write).total_seconds() >= GSHEET_WRITE_THROTTLE_SECONDS):
        save_chain_snapshot_to_gsheet(df, st.session_state.last_fetch, st.session_state.selected_expiry)
        st.session_state.last_gsheet_write = datetime.now()
else:
    df = st.session_state.previous_df.copy()
    spot = None
    src = st.session_state.baseline_source
    if isinstance(src, tuple):
        label = {'gsheet': 'Google Sheet', 'disk': 'local disk'}.get(src[0], src[0])
        ts = src[1].strftime('%d-%b %H:%M') if src[1] is not None else "an earlier session"
        st.info(f"Showing last known chain from {label} (as of {ts}). No new API calls while market is closed.")
    else:
        st.info(f"Showing the last snapshot fetched at {st.session_state.last_fetch.strftime('%H:%M:%S') if st.session_state.last_fetch else 'N/A'}.")

# ATM strike: live spot when available, else nearest-to-median-strike fallback for closed market
if spot:
    atm_strike = round(spot / STRIKE_STEP) * STRIKE_STEP
else:
    atm_strike = round(df['Strike'].median() / STRIKE_STEP) * STRIKE_STEP

# ==========================================
# CORE REPLICATED LOGIC
# ==========================================
za = zone_a_classification(df, atm_strike, zone_a_width, pcr_thresholds, symmetric=symmetric_zone_a)
zb = zone_b_signal(df, atm_strike, zone_b_width, signal_thresholds)
sig = master_signal(za['pcr'], zb['ce_vol_imbalance'], zb['choi_ce'], zb['choi_pe'],
                     zb['ce_ltp_avg'], zb['pe_ltp_avg'], DEFAULT_MASTER_THRESHOLDS)
action = action_tag(za['classification'], zb['signal'])

# ==========================================
# CONFLUENCE LAYER (new, additive — never overrides the core signal above)
# ==========================================
vwap_val = fetch_vwap_bias() if (is_open and use_vwap) else None
spot_vs_vwap = None
if vwap_val and spot:
    spot_vs_vwap = "Above VWAP (bullish bias)" if spot > vwap_val else "Below VWAP (bearish bias)"

mp = max_pain(df) if use_max_pain else None

atm_row = df[df['Strike'] == atm_strike]
ce_spread = spread_pct(atm_row['CE_Bid'].iloc[0], atm_row['CE_Ask'].iloc[0]) if use_liquidity and len(atm_row) else None
pe_spread = spread_pct(atm_row['PE_Bid'].iloc[0], atm_row['PE_Ask'].iloc[0]) if use_liquidity and len(atm_row) else None

try:
    dte = (datetime.strptime(st.session_state.selected_expiry, "%Y-%m-%d").date() - datetime.now(IST).date()).days
except Exception:
    dte = None

top_pcr = top_pcr_strikes(df, top_n=2)

# Confluence agreement counter (informational only)
bullish_signals = sig in ("Strong CE Buy", "PE writers strong") or zb['signal'] in ("Buy CE", "Write PE")
bearish_signals = sig in ("Strong PE Buy", "CE writers strong") or zb['signal'] in ("Buy PE", "Write CE")
agree, total_checks = 0, 0
if spot_vs_vwap is not None:
    total_checks += 1
    if (bullish_signals and "bullish" in spot_vs_vwap) or (bearish_signals and "bearish" in spot_vs_vwap):
        agree += 1
if mp is not None and spot:
    total_checks += 1
    if (bullish_signals and spot < mp) or (bearish_signals and spot > mp):
        agree += 1

# ==========================================
# LOG THIS POLL (Dash Board replacement)
# ==========================================
if is_open:
    log_row = {
        'Time': st.session_state.last_fetch.strftime('%H:%M:%S'),
        'PCR_Regime': za['classification'], 'PCR': round(za['pcr'], 4) if pd.notna(za['pcr']) else None,
        'CE_OI_zoneA': int(za['ce_oi_sum']), 'PE_OI_zoneA': int(za['pe_oi_sum']),
        'Choi_CE': round(zb['choi_ce'], 2), 'Choi_PE': round(zb['choi_pe'], 2),
        'CE_Vol_Imbalance': round(zb['ce_vol_imbalance'], 2),
        'ZoneB_Signal': zb['signal'], 'Master_Signal': sig, 'Action': action,
        'CE_LTP': round(zb['ce_ltp_avg'], 2) if pd.notna(zb['ce_ltp_avg']) else None,
        'PE_LTP': round(zb['pe_ltp_avg'], 2) if pd.notna(zb['pe_ltp_avg']) else None,
        'Spot': spot, 'ATM': atm_strike, 'VWAP': round(vwap_val, 2) if vwap_val else None,
        'MaxPain': mp,
    }
    st.session_state.session_log.append(log_row)
    append_log_row(log_row, today_str)
    append_log_row_to_gsheet(log_row)

# ==========================================
# UI — MASTER SIGNAL BANNER
# ==========================================
signal_colors = {
    "Strong CE Buy": "#1e7e34", "PE writers strong": "#28a745",
    "Strong PE Buy": "#c82333", "CE writers strong": "#dc3545",
    "wait for data confirmation": "#6c757d",
}
st.markdown(f"""
<div style='background-color:{signal_colors.get(sig, "#6c757d")};padding:24px;border-radius:10px;
text-align:center;margin:10px 0;'>
    <h2 style='color:white;margin:0;'>{sig}</h2>
    <p style='color:white;margin:6px 0 0 0;'>Action: <b>{action or "—"}</b> &nbsp;|&nbsp;
    Zone B raw signal: <b>{zb['signal']}</b> &nbsp;|&nbsp; PCR regime: <b>{za['classification']}</b></p>
</div>""", unsafe_allow_html=True)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Spot / ATM", f"{spot:.0f}" if spot else "—", f"ATM {atm_strike:.0f}")
c2.metric("Zone A PCR", f"{za['pcr']:.3f}" if pd.notna(za['pcr']) else "—")
c3.metric("Choi_CE / Choi_PE", f"{zb['choi_ce']:.1f}% / {zb['choi_pe']:.1f}%")
c4.metric("CE Vol Imbalance", f"{zb['ce_vol_imbalance']:.1f}")

st.markdown("---")

# ==========================================
# CONFLUENCE PANEL
# ==========================================
st.subheader("🧭 Confluence Layer (supporting evidence — does not override the signal above)")
cf1, cf2, cf3, cf4 = st.columns(4)
with cf1:
    if spot_vs_vwap:
        st.metric("Spot vs VWAP", f"{vwap_val:.1f}", spot_vs_vwap.split(" (")[0])
    else:
        st.metric("Spot vs VWAP", "unavailable")
with cf2:
    if mp is not None:
        st.metric("Max Pain", f"{mp:.0f}", f"Spot is {'above' if spot and spot > mp else 'below'} Max Pain" if spot else "")
    else:
        st.metric("Max Pain", "disabled")
with cf3:
    if ce_spread is not None:
        flag = "⚠️ wide" if ce_spread > liquidity_spread_limit else "OK"
        st.metric("ATM CE Spread %", f"{ce_spread:.1f}%", flag)
    else:
        st.metric("ATM CE Spread %", "—")
with cf4:
    st.metric("Days to Expiry", f"{dte}" if dte is not None else "—",
               "⚠️ Gamma risk — size down" if dte is not None and dte <= 1 else "")

if total_checks:
    st.caption(f"Confluence agreement: **{agree}/{total_checks}** independent filters support the current directional read.")

st.markdown("---")

# ==========================================
# HIGHEST-PCR (SUPPORT) STRIKES — auto replacement for Analysis!H8:I9
# ==========================================
st.subheader("📌 Highest-PCR Strikes (auto-tracked support levels)")
if not top_pcr.empty:
    pcr_cols = st.columns(len(top_pcr))
    for i, (_, row) in enumerate(top_pcr.iterrows()):
        with pcr_cols[i]:
            st.metric(f"Strike {row['Strike']:.0f}", f"PCR {row['PCR']:.2f}")
else:
    st.caption("No qualifying strikes yet.")

st.markdown("---")

# ==========================================
# OI CHANGE CHART (Zone B)
# ==========================================
st.subheader("📈 Zone B OI Change (Call Unwinding/Writing vs Put Writing)")
zone_b_df = zb['zone']
fig = make_subplots(rows=1, cols=1)
fig.add_trace(go.Bar(x=zone_b_df['Strike'], y=zone_b_df['CE_OI_chg'], name='CE OI Change', marker_color='#dc3545'))
fig.add_trace(go.Bar(x=zone_b_df['Strike'], y=zone_b_df['PE_OI_chg'], name='PE OI Change', marker_color='#28a745'))
fig.add_hline(y=0, line_dash="dash", line_color="gray")
fig.update_layout(barmode='group', height=400, legend=dict(orientation="h", y=1.1))
st.plotly_chart(fig, use_container_width=True)

# ==========================================
# FULL CHAIN TABLE (Sensibull-equivalent columns from Dhan data)
# ==========================================
st.subheader("📋 Option Chain (ATM ± 10)")
band = df[(df['Strike'] >= atm_strike - 10 * STRIKE_STEP) & (df['Strike'] <= atm_strike + 10 * STRIKE_STEP)]
display_cols = ['CE_Delta', 'CE_IV', 'CE_Volume', 'CE_OI_chg', 'CE_OI', 'CE_LTP',
                 'Strike', 'PCR',
                 'PE_LTP', 'PE_OI', 'PE_OI_chg', 'PE_Volume', 'PE_IV', 'PE_Delta']
st.dataframe(band[display_cols].style.format(precision=2), use_container_width=True, height=420)

st.markdown("---")

# ==========================================
# SESSION LOG (Dash Board replacement)
# ==========================================
st.subheader("🗒️ Session Log")
log_df = pd.DataFrame(st.session_state.session_log)
if not log_df.empty:
    st.dataframe(log_df.tail(50), use_container_width=True, height=300)
    st.download_button("Download today's full log (CSV)", log_df.to_csv(index=False),
                        file_name=f"nifty_oi_log_{today_str}.csv", mime="text/csv")
else:
    st.caption("No polls logged yet this session.")

st.caption(f"🕐 Last Updated: {st.session_state.last_fetch.strftime('%H:%M:%S') if st.session_state.last_fetch else 'N/A'}")
