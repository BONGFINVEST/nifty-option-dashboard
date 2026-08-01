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

It also adds a REAL-TIME CANDLESTICK CHART panel (spot OHLC + VWAP + Max Pain trend),
built from Dhan's intraday-candle endpoint and your own session log -- purely visual,
never feeds back into the Master Signal.

On top of that, a VWAP TREND READ panel tracks the pattern you watch manually: while
price keeps *closing* on one side of the running session VWAP, that bias has tended to
persist for the next ~30-60 minutes. It surfaces the current streak (consecutive candles
closed on one side), flags VWAP "touch-and-hold" retests (price dips into VWAP intrabar
but still closes through in the trend direction), and confirms once the streak crosses a
threshold you set. This is descriptive of what price has actually done, not a prediction,
and it's entirely separate from the OI-based Master Signal above.

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

# Candlestick chart settings -- separate concern from the OI-based Master Signal above.
# This is purely a visual overlay of spot price action + intraday VWAP + Max Pain trend.
CANDLE_FETCH_THROTTLE_SECONDS = 30   # don't hammer Dhan's intraday-candle endpoint every 10s OI poll
DEFAULT_CANDLE_INTERVAL = "5"        # minutes -- matches your M5 Fibonacci Pine Script granularity

# Institutional Footprint settings -- a THIRD, fully independent read (IV Skew,
# ChgPCR momentum, Vol/OI conviction) layered alongside the Master Signal and the
# VWAP Trend Read above. Never feeds into either of those; purely additive.
FOOTPRINT_WIDTH = 5                                    # ATM +- N strikes for the footprint zone/table
DEFAULT_FOOTPRINT_THRESHOLDS = {
    "iv_skew_bearish": -2.0,     # CE_IV - PE_IV <= this -> aggressive Put buying -> look for breakdown
    "iv_skew_bullish": 2.0,      # CE_IV - PE_IV >= this -> Put writers running -> look for short-covering rally
    "chgpcr_bear_trap": 1.5,     # ChgPCR spikes above this while price is FALLING -> Bear Trap (dip being bought)
    "chgpcr_bull_trap": 0.5,     # ChgPCR collapses below this while price is RISING -> Bull Trap (rally being sold into)
    "vol_oi_fresh": 0.6,         # Vol/OI >= this -> fresh institutional money, regime is "real"
    "vol_oi_fakeout": 0.2,       # Vol/OI < this -> just intraday squaring off, ignore the breakout
    "trend_flat_band_pct": 0.1,  # spot within +-this% of today's open counts as "sideways", not rising/falling
    "chgpcr_min_ce_chg_abs": 300,       # minimum |net CE OI change| (contracts) in the zone before trusting ChgPCR
    "chgpcr_min_ce_chg_pct_of_oi": 0.3, # ...OR at least this % of the zone's total OI, whichever floor is higher
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


def fetch_intraday_ohlc(interval: str = DEFAULT_CANDLE_INTERVAL):
    """Pulls today's NIFTY spot INDEX intraday OHLCV candles from Dhan's
    intraday-candle endpoint. This is the single source now used for both
    the candlestick chart and the VWAP figure in the Confluence panel
    (previously a second, separate call to this same endpoint just to
    collapse it into one VWAP scalar -- consolidated here to halve the
    API hits against this endpoint).

    Dhan's exact segment/instrument code for the NIFTY *index* (as opposed
    to equity/futures) isn't fully nailed down from public docs, so this
    fails soft: any schema mismatch just disables the candle panel rather
    than showing wrong data. If it errors for you, check
    https://dhanhq.co/docs/v2/historical-data/ for the exact index payload
    shape and I'll patch the two lines below."""
    try:
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        payload = {
            "securityId": str(NIFTY_SCRIP), "exchangeSegment": "IDX_I", "instrument": "INDEX",
            "interval": interval, "oi": False,
            "fromDate": f"{today_str} 09:15:00", "toDate": f"{today_str} 23:59:59",
        }
        r = requests.post("https://api.dhan.co/v2/charts/intraday", headers=DHAN_HEADERS, json=payload, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        opens, highs, lows, closes = data.get('open'), data.get('high'), data.get('low'), data.get('close')
        vols, ts = data.get('volume'), data.get('timestamp')
        if not opens or not ts:
            return None
        out = pd.DataFrame({
            'time': pd.to_datetime(ts, unit='s', utc=True).tz_convert(IST),
            'open': opens, 'high': highs, 'low': lows, 'close': closes,
            'volume': vols if vols else [0] * len(opens),
        })
        return out
    except Exception:
        return None


def compute_cumulative_vwap(ohlc_df: pd.DataFrame) -> pd.DataFrame:
    """Adds a 'vwap' column: a running (session-to-date) volume-weighted
    typical-price average, matching how VWAP is conventionally plotted
    intraday (each point = VWAP-so-far, not VWAP-of-that-single-candle).

    Falls back to a cumulative simple average of typical price -- labelled
    distinctly in the UI -- if the feed carries no real volume, since
    Dhan's INDEX candle feed for NIFTY itself typically reports 0 volume
    (the index has no traded volume; only its constituents/futures do)."""
    out = ohlc_df.copy()
    typical = (out['high'] + out['low'] + out['close']) / 3
    if out['volume'].sum() > 0:
        vol = out['volume'].replace(0, np.nan)
        out['vwap'] = (typical * vol).cumsum() / vol.cumsum()
    else:
        out['vwap'] = typical.expanding().mean()
    return out


def analyze_vwap_trend(ohlc_df: pd.DataFrame, interval_minutes: int, confirm_candles: int = 3):
    """Reads the VWAP-respect pattern: while price keeps *closing* on one side
    of the running session VWAP, that directional bias has historically tended
    to persist for the next ~30-60 minutes. This tracks the current streak of
    consecutive candles closed on one side, and separately flags "touch-and-
    hold" events -- candles where price dipped/spiked into VWAP intrabar
    (low <= vwap <= high) but still closed on the streak's side, which is the
    institutional retest-and-continue pattern that makes a hold more credible
    than a streak with no retest at all.

    A streak resets the moment a candle *closes* on the opposite side --
    intrabar wicks through VWAP don't break it, only a close does. That
    matches "respecting VWAP" as a level, not treating every wick as a flip.

    Descriptive only: reports what price has actually done so far this
    session. Does not guarantee continuation, and is entirely independent of
    the OI-based Master Signal."""
    out = ohlc_df.dropna(subset=['vwap']).copy()
    if out.empty or len(out) < 2:
        return None

    out['side'] = np.where(out['close'] > out['vwap'], 'above', 'below')
    out['touched_vwap'] = (out['low'] <= out['vwap']) & (out['high'] >= out['vwap'])

    current_side = out['side'].iloc[-1]
    streak = 0
    streak_start_time = out['time'].iloc[-1]
    for s, t in zip(out['side'].values[::-1], out['time'].values[::-1]):
        if s == current_side:
            streak += 1
            streak_start_time = t
        else:
            break

    streak_slice = out.iloc[-streak:]
    touch_hold_events = streak_slice[streak_slice['touched_vwap']]

    opposite_side = 'below' if current_side == 'above' else 'above'
    breaks = out[out['side'] == opposite_side]
    last_break_time = breaks['time'].iloc[-1] if not breaks.empty else None

    last_close, last_vwap = out['close'].iloc[-1], out['vwap'].iloc[-1]
    distance_pts = last_close - last_vwap
    distance_pct = (distance_pts / last_vwap * 100) if last_vwap else None

    return {
        'side': current_side, 'streak_candles': streak,
        'streak_minutes': streak * interval_minutes,
        'streak_start_time': pd.to_datetime(streak_start_time),
        'confirmed': streak >= confirm_candles,
        'touch_hold_count': len(touch_hold_events),
        'touch_hold_df': touch_hold_events[['time', 'vwap']].reset_index(drop=True),
        'last_break_time': pd.to_datetime(last_break_time) if last_break_time is not None else None,
        'distance_pts': distance_pts, 'distance_pct': distance_pct,
        'touched_vwap_now': bool(out['touched_vwap'].iloc[-1]),
    }


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
# SIGNAL PROGRESS — how close is each tier to firing?
# ==========================================
# Answers "why hasn't a signal fired" with the actual numbers, instead of a
# silent 'wait for data confirmation'. This evaluates each of the 4 named
# tiers from master_signal() against the SAME thresholds, condition by
# condition, and reports a pass/fail + numeric gap for each. Deliberately
# NOT framed as a 'ladder' toward Strong -- the mild and Strong tiers in
# your original formula don't share a strict subset relationship (e.g. 'CE
# writers strong' actually requires a MORE extreme PCR than 'Strong PE Buy'
# does), so showing it as a staircase would misrepresent your own formula.
# Instead each tier gets its own honest checklist.
def evaluate_signal_tiers(pcr, ce_vol_imbalance, choi_ce, choi_pe, ce_ltp, pe_ltp, t: dict):
    if pd.isna(pcr):
        return {}

    tier_defs = {
        "Strong CE Buy": [
            (f"PCR > {t['pcr_high']:.2f}", pcr > t['pcr_high'], pcr - t['pcr_high']),
            (f"CE Vol Imbalance < -{t['vol_imbalance_strong']:.0f}",
             ce_vol_imbalance < -t['vol_imbalance_strong'], (-t['vol_imbalance_strong']) - ce_vol_imbalance),
            ("Choi_PE > Choi_CE", choi_pe > choi_ce, choi_pe - choi_ce),
            ("CE LTP > PE LTP", ce_ltp > pe_ltp, ce_ltp - pe_ltp),
        ],
        "Strong PE Buy": [
            (f"PCR < {t['pcr_low']:.2f}", pcr < t['pcr_low'], t['pcr_low'] - pcr),
            (f"CE Vol Imbalance > {t['vol_imbalance_strong']:.0f}",
             ce_vol_imbalance > t['vol_imbalance_strong'], ce_vol_imbalance - t['vol_imbalance_strong']),
            ("Choi_PE < Choi_CE", choi_pe < choi_ce, choi_ce - choi_pe),
            ("CE LTP < PE LTP", ce_ltp < pe_ltp, pe_ltp - ce_ltp),
        ],
        "PE writers strong": [
            (f"PCR >= {t['pcr_high']:.2f}", pcr >= t['pcr_high'], pcr - t['pcr_high']),
            ("Choi_PE > Choi_CE", choi_pe > choi_ce, choi_pe - choi_ce),
            (f"CE Vol Imbalance < -{t['vol_imbalance_mild']:.0f}",
             ce_vol_imbalance < -t['vol_imbalance_mild'], (-t['vol_imbalance_mild']) - ce_vol_imbalance),
        ],
        "CE writers strong": [
            (f"PCR < {t['pcr_ce_writers']:.2f}", pcr < t['pcr_ce_writers'], t['pcr_ce_writers'] - pcr),
            ("Choi_CE > Choi_PE", choi_ce > choi_pe, choi_ce - choi_pe),
            (f"CE Vol Imbalance > {t['vol_imbalance_mild']:.0f}",
             ce_vol_imbalance > t['vol_imbalance_mild'], ce_vol_imbalance - t['vol_imbalance_mild']),
        ],
    }

    report = {}
    for tier, conditions in tier_defs.items():
        met = sum(1 for _, ok, _ in conditions if ok)
        report[tier] = {'met': met, 'total': len(conditions), 'conditions': conditions}
    return report


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
# INSTITUTIONAL FOOTPRINT (new — a third, independent read alongside the
# Master Signal and the VWAP Trend Read. Never feeds back into either.)
# ==========================================
def compute_footprint_table(df: pd.DataFrame, atm: float, width: int) -> pd.DataFrame:
    """Per-strike table for the ATM +- width band:
      IV_Skew   = CE_IV - PE_IV        (negative = Call IV crashing/Put IV rising = Put buying;
                                         positive = Put writers running = short-covering setup)
      ChgPCR    = today's PE_OI_chg / today's CE_OI_chg, per strike (today's FLOW, not
                                         the standing PCR which reflects yesterday's positions)
      Vol/OI    = (CE_Volume + PE_Volume) / (CE_OI + PE_OI)  (fresh money vs stale positions)
    """
    zone = df[(df['Strike'] >= atm - width * STRIKE_STEP) & (df['Strike'] <= atm + width * STRIKE_STEP)].copy()
    zone['IV_Skew'] = zone['CE_IV'] - zone['PE_IV']
    zone['ChgPCR'] = np.where(
        zone['CE_OI_chg'] != 0, zone['PE_OI_chg'] / zone['CE_OI_chg'].replace(0, np.nan), np.nan
    )
    zone['Total_OI'] = zone['CE_OI'] + zone['PE_OI']
    zone['Total_Vol'] = zone['CE_Volume'] + zone['PE_Volume']
    zone['Vol_OI'] = np.where(zone['Total_OI'] > 0, zone['Total_Vol'] / zone['Total_OI'], np.nan)
    cols = ['Strike', 'CE_IV', 'PE_IV', 'IV_Skew', 'CE_OI_chg', 'PE_OI_chg', 'ChgPCR',
            'CE_Volume', 'PE_Volume', 'Total_OI', 'Vol_OI']
    return zone[cols].reset_index(drop=True)


def aggregate_footprint_metrics(footprint_df: pd.DataFrame, t: dict = None) -> dict:
    """Rolls the per-strike table up into the three 'cheat code' numbers.
    IV Skew is OI-weighted (so a thinly-traded far strike doesn't skew the
    read); ChgPCR and Vol/OI are sum-then-ratio across the zone (matches how
    Zone A/B already aggregate in this app), not an average-of-ratios, since
    that's far less sensitive to one strike's near-zero-OI-change outlier.

    ChgPCR is a ratio of two OI-change numbers, so when net CE OI change in
    the zone is tiny (thin flow -- pre-market, first few minutes after open,
    or a stale/closing snapshot), the ratio can blow up to a huge, meaningless
    value even though nothing unusual actually happened. This guards against
    that: ChgPCR is only reported when the zone's net CE OI change clears a
    minimum floor (both an absolute contract count and a % of zone OI);
    otherwise it's returned as unreliable so the UI can say so instead of
    showing a number like -24 or +18 that looks like a strong signal but
    is really just noise from a near-zero denominator."""
    if footprint_df.empty:
        return {'iv_skew': np.nan, 'chg_pcr': np.nan, 'vol_oi': np.nan, 'chg_pcr_reliable': False}

    weights = footprint_df['Total_OI'].replace(0, np.nan)
    if weights.sum() > 0:
        iv_skew = (footprint_df['IV_Skew'] * weights).sum() / weights.sum()
    else:
        iv_skew = footprint_df['IV_Skew'].mean()

    ce_chg_sum = footprint_df['CE_OI_chg'].sum()
    pe_chg_sum = footprint_df['PE_OI_chg'].sum()
    oi_sum = footprint_df['Total_OI'].sum()

    min_abs_floor = (t or {}).get('chgpcr_min_ce_chg_abs', 300)
    min_pct_floor = (t or {}).get('chgpcr_min_ce_chg_pct_of_oi', 0.3)  # percent
    floor = max(min_abs_floor, oi_sum * min_pct_floor / 100) if oi_sum > 0 else min_abs_floor
    chg_pcr_reliable = abs(ce_chg_sum) >= floor
    chg_pcr = (pe_chg_sum / ce_chg_sum) if (ce_chg_sum != 0 and chg_pcr_reliable) else np.nan

    vol_sum = footprint_df['CE_Volume'].sum() + footprint_df['PE_Volume'].sum()
    vol_oi = (vol_sum / oi_sum) if oi_sum > 0 else np.nan

    return {
        'iv_skew': iv_skew, 'chg_pcr': chg_pcr, 'vol_oi': vol_oi,
        'chg_pcr_reliable': chg_pcr_reliable,
        'ce_chg_sum': ce_chg_sum, 'pe_chg_sum': pe_chg_sum, 'oi_sum': oi_sum,
    }


def market_direction_today(spot, day_open, flat_band_pct: float) -> str:
    """Cheap directional read used only to contextualize the ChgPCR trap
    check ('spiking ChgPCR while price FALLS' needs to know price is
    falling). Uses today's first candle open vs current spot -- not a
    prediction, just today's realized move so far."""
    if not spot or not day_open:
        return "unknown"
    change_pct = (spot - day_open) / day_open * 100
    if change_pct > flat_band_pct:
        return "rising"
    if change_pct < -flat_band_pct:
        return "falling"
    return "sideways"


def institutional_footprint_signal(iv_skew, chg_pcr, vol_oi, market_direction, t: dict, chg_pcr_reliable: bool = True):
    """Combines the three reads into one headline + explanation, exactly per
    the prop-desk playbook:
      1. IV Skew cheat code -> directional bias
      2. ChgPCR vs price direction -> trap detection (overrides the IV Skew
         bias when it fires, since a trap is a higher-conviction, more
         specific read than a standing skew)
      3. Vol/OI -> conviction tag on whichever headline above is chosen
    Returns (headline, color_key, explanation_lines: list[str])."""
    lines = []

    # -- 1. IV Skew --
    if pd.isna(iv_skew):
        iv_bias, iv_line = "Neutral", "IV Skew unavailable."
    elif iv_skew <= t['iv_skew_bearish']:
        iv_bias = "Bearish"
        iv_line = f"IV Skew {iv_skew:+.2f} — Call IV crashing / Put IV rising → aggressive Put buying. Watch for a downside breakdown."
    elif iv_skew >= t['iv_skew_bullish']:
        iv_bias = "Bullish"
        iv_line = f"IV Skew {iv_skew:+.2f} — Put writers running. Watch for a short-covering rally."
    else:
        iv_bias = "Neutral"
        iv_line = f"IV Skew {iv_skew:+.2f} — no extreme skew, no directional edge from this read alone."
    lines.append(iv_line)

    # -- 2. ChgPCR trap check (context-dependent on today's price direction) --
    trap_bias = None
    if not chg_pcr_reliable:
        lines.append("ChgPCR skipped — net CE OI change in this zone is too thin right now to trust the ratio "
                      "(common right after open or on stale/closing data). Will resume once flow builds up.")
    elif not pd.isna(chg_pcr):
        if market_direction == "falling" and chg_pcr > t['chgpcr_bear_trap']:
            trap_bias = "Bullish"
            lines.append(f"ChgPCR {chg_pcr:.2f} spiking while price FALLS → Bear Trap. Institutions are buying the dip.")
        elif market_direction == "rising" and chg_pcr < t['chgpcr_bull_trap']:
            trap_bias = "Bearish"
            lines.append(f"ChgPCR {chg_pcr:.2f} collapsing while price RISES → Bull Trap. Possible distribution into strength.")
        else:
            lines.append(f"ChgPCR {chg_pcr:.2f} (today's flow) — no trap condition against the {market_direction} price action.")
    else:
        lines.append("ChgPCR unavailable (no net OI change yet this poll).")

    # -- 3. Vol/OI conviction --
    if pd.isna(vol_oi):
        conviction, conv_line = "Unknown", "Vol/OI unavailable."
    elif vol_oi >= t['vol_oi_fresh']:
        conviction = "Confirmed"
        conv_line = f"Vol/OI {vol_oi:.2f} — massive fresh money entering; the regime above is REAL."
    elif vol_oi < t['vol_oi_fakeout']:
        conviction = "Fakeout risk"
        conv_line = f"Vol/OI {vol_oi:.2f} — just intraday squaring off; treat any breakout as suspect."
    else:
        conviction = "Moderate"
        conv_line = f"Vol/OI {vol_oi:.2f} — moderate participation, no strong confirmation either way."
    lines.append(conv_line)

    # Trap read takes precedence (more specific, higher-conviction signal) over the standing skew bias
    headline_bias = trap_bias if trap_bias else iv_bias
    if headline_bias == "Bullish":
        headline, color_key = "🟢 Institutional Footprint: BULLISH", "bullish"
    elif headline_bias == "Bearish":
        headline, color_key = "🔴 Institutional Footprint: BEARISH", "bearish"
    else:
        headline, color_key = "⚪ Institutional Footprint: NEUTRAL", "neutral"

    if conviction == "Fakeout risk" and headline_bias != "Neutral":
        headline += " (low conviction — Vol/OI thin)"

    return headline, color_key, lines


# ==========================================
# SESSION STATE INIT
# ==========================================
for key, default in [
    ('previous_df', None), ('last_fetch', None), ('expiry_list', None),
    ('expiry_list_fetched_at', None), ('selected_expiry', None),
    ('baseline_source', None), ('last_gsheet_write', None),
    ('session_log', []), ('vwap_session_start', None), ('token_status', None),
    ('ohlc_df', None), ('last_candle_fetch', None), ('vwap_confirmed_alert_side', None),
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

    st.markdown("---")
    with st.expander("🕯️ Live Candlestick Chart", expanded=True):
        show_candle_chart = st.checkbox("Show real-time candlestick chart", value=True)
        candle_interval = st.selectbox(
            "Candle interval (minutes)", options=["1", "3", "5", "15"], index=2,
            help="Matches Dhan's intraday-candle granularity. 5-min mirrors your M5 Fibonacci Pine Script.")
        vwap_confirm_candles = st.number_input(
            "VWAP trend confirm after N consecutive candles", min_value=1, max_value=10, value=3,
            help="E.g. 3 candles at 5-min = 15 min of price closing on one side of VWAP before the trend "
                 "is flagged 'Confirmed'.")

    st.markdown("---")
    with st.expander("🕵️ Institutional Footprint", expanded=True):
        show_footprint_panel = st.checkbox("Show live Institutional Footprint signal", value=True)
        footprint_width = st.number_input(
            "Footprint zone width (ATM ± N strikes)", min_value=1, max_value=15, value=FOOTPRINT_WIDTH)
        fp_iv_skew_bearish = st.number_input(
            "IV Skew ≤ this → Bearish (Put buying)", value=DEFAULT_FOOTPRINT_THRESHOLDS['iv_skew_bearish'], step=0.5)
        fp_iv_skew_bullish = st.number_input(
            "IV Skew ≥ this → Bullish (Put writing)", value=DEFAULT_FOOTPRINT_THRESHOLDS['iv_skew_bullish'], step=0.5)
        fp_chgpcr_bear_trap = st.number_input(
            "ChgPCR > this while FALLING → Bear Trap", value=DEFAULT_FOOTPRINT_THRESHOLDS['chgpcr_bear_trap'], step=0.1)
        fp_chgpcr_bull_trap = st.number_input(
            "ChgPCR < this while RISING → Bull Trap", value=DEFAULT_FOOTPRINT_THRESHOLDS['chgpcr_bull_trap'], step=0.1)
        fp_vol_oi_fresh = st.number_input(
            "Vol/OI ≥ this → fresh money confirmed", value=DEFAULT_FOOTPRINT_THRESHOLDS['vol_oi_fresh'], step=0.05)
        fp_vol_oi_fakeout = st.number_input(
            "Vol/OI < this → fakeout / just squaring off", value=DEFAULT_FOOTPRINT_THRESHOLDS['vol_oi_fakeout'], step=0.05)
        fp_chgpcr_min_abs = st.number_input(
            "ChgPCR min |net CE OI change| to trust (contracts)",
            value=DEFAULT_FOOTPRINT_THRESHOLDS['chgpcr_min_ce_chg_abs'], step=50,
            help="Below this, the zone's net CE OI change is too thin to trust the ChgPCR ratio — it gets "
                 "skipped instead of showing a noisy, misleadingly large number.")
        fp_chgpcr_min_pct = st.number_input(
            "...OR min % of zone OI, whichever floor is higher",
            value=DEFAULT_FOOTPRINT_THRESHOLDS['chgpcr_min_ce_chg_pct_of_oi'], step=0.1)

footprint_thresholds = {
    "iv_skew_bearish": fp_iv_skew_bearish, "iv_skew_bullish": fp_iv_skew_bullish,
    "chgpcr_bear_trap": fp_chgpcr_bear_trap, "chgpcr_bull_trap": fp_chgpcr_bull_trap,
    "vol_oi_fresh": fp_vol_oi_fresh, "vol_oi_fakeout": fp_vol_oi_fakeout,
    "trend_flat_band_pct": DEFAULT_FOOTPRINT_THRESHOLDS['trend_flat_band_pct'],
    "chgpcr_min_ce_chg_abs": fp_chgpcr_min_abs, "chgpcr_min_ce_chg_pct_of_oi": fp_chgpcr_min_pct,
}

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
    st.session_state.last_fetch = datetime.now(IST)
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
tier_report = evaluate_signal_tiers(za['pcr'], zb['ce_vol_imbalance'], zb['choi_ce'], zb['choi_pe'],
                                     zb['ce_ltp_avg'], zb['pe_ltp_avg'], DEFAULT_MASTER_THRESHOLDS)

# ==========================================
# INTRADAY CANDLES (spot OHLCV) — throttled separately from the 10s OI poll.
# This is also now the single source for VWAP (previously a second, duplicate
# call to the same Dhan endpoint just to get one scalar — consolidated here).
# ==========================================
need_candle_refresh = (
    is_open and (show_candle_chart or use_vwap) and (
        st.session_state.ohlc_df is None or st.session_state.last_candle_fetch is None
        or (datetime.now() - st.session_state.last_candle_fetch).total_seconds() >= CANDLE_FETCH_THROTTLE_SECONDS
    )
)
if need_candle_refresh:
    fetched_ohlc = fetch_intraday_ohlc(candle_interval)
    if fetched_ohlc is not None and not fetched_ohlc.empty:
        st.session_state.ohlc_df = compute_cumulative_vwap(fetched_ohlc)
        st.session_state.last_candle_fetch = datetime.now()

ohlc_df = st.session_state.ohlc_df

# ==========================================
# CONFLUENCE LAYER (new, additive — never overrides the core signal above)
# ==========================================
vwap_val = None
if use_vwap and ohlc_df is not None and not ohlc_df.empty and not ohlc_df['vwap'].isna().all():
    vwap_val = ohlc_df['vwap'].iloc[-1]
spot_vs_vwap = None
if vwap_val and spot:
    spot_vs_vwap = "Above VWAP (bullish bias)" if spot > vwap_val else "Below VWAP (bearish bias)"

# VWAP trend read (streak + touch-and-hold) — see analyze_vwap_trend() docstring
vwap_trend = None
if ohlc_df is not None and not ohlc_df.empty and not ohlc_df['vwap'].isna().all():
    vwap_trend = analyze_vwap_trend(ohlc_df, int(candle_interval), confirm_candles=vwap_confirm_candles)

if vwap_trend and vwap_trend['confirmed']:
    if st.session_state.vwap_confirmed_alert_side != vwap_trend['side']:
        st.toast(
            f"📍 VWAP trend confirmed: {vwap_trend['side'].upper()} — "
            f"{vwap_trend['streak_candles']} candles / ~{vwap_trend['streak_minutes']} min",
            icon="📍",
        )
        st.session_state.vwap_confirmed_alert_side = vwap_trend['side']
elif vwap_trend and not vwap_trend['confirmed']:
    st.session_state.vwap_confirmed_alert_side = None

mp = max_pain(df) if use_max_pain else None

atm_row = df[df['Strike'] == atm_strike]
ce_spread = spread_pct(atm_row['CE_Bid'].iloc[0], atm_row['CE_Ask'].iloc[0]) if use_liquidity and len(atm_row) else None
pe_spread = spread_pct(atm_row['PE_Bid'].iloc[0], atm_row['PE_Ask'].iloc[0]) if use_liquidity and len(atm_row) else None

try:
    dte = (datetime.strptime(st.session_state.selected_expiry, "%Y-%m-%d").date() - datetime.now(IST).date()).days
except Exception:
    dte = None

top_pcr = top_pcr_strikes(df, top_n=2)

# ==========================================
# INSTITUTIONAL FOOTPRINT — independent third read (IV Skew / ChgPCR / Vol-OI)
# ==========================================
footprint_table = compute_footprint_table(df, atm_strike, footprint_width) if show_footprint_panel else pd.DataFrame()
footprint_agg = aggregate_footprint_metrics(footprint_table, footprint_thresholds) if not footprint_table.empty else {
    'iv_skew': np.nan, 'chg_pcr': np.nan, 'vol_oi': np.nan, 'chg_pcr_reliable': False}
day_open = ohlc_df['open'].iloc[0] if (ohlc_df is not None and not ohlc_df.empty) else None
footprint_market_dir = market_direction_today(spot, day_open, footprint_thresholds['trend_flat_band_pct'])
footprint_headline, footprint_color_key, footprint_lines = institutional_footprint_signal(
    footprint_agg['iv_skew'], footprint_agg['chg_pcr'], footprint_agg['vol_oi'],
    footprint_market_dir, footprint_thresholds, chg_pcr_reliable=footprint_agg.get('chg_pcr_reliable', True)
) if show_footprint_panel and not footprint_table.empty else (None, None, [])

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
        'VWAP_Trend_Side': vwap_trend['side'] if vwap_trend else None,
        'VWAP_Trend_Streak_Candles': vwap_trend['streak_candles'] if vwap_trend else None,
        'VWAP_Trend_Confirmed': vwap_trend['confirmed'] if vwap_trend else None,
        'Footprint_IV_Skew': round(footprint_agg['iv_skew'], 2) if pd.notna(footprint_agg.get('iv_skew')) else None,
        'Footprint_ChgPCR': round(footprint_agg['chg_pcr'], 2) if pd.notna(footprint_agg.get('chg_pcr')) else None,
        'Footprint_Vol_OI': round(footprint_agg['vol_oi'], 2) if pd.notna(footprint_agg.get('vol_oi')) else None,
        'Footprint_Signal': footprint_headline,
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
# REAL-TIME CANDLESTICK CHART — Spot price action + VWAP + Max Pain trend
# ==========================================
if show_candle_chart:
    st.subheader("🕯️ Real-Time NIFTY Chart (Candlestick + VWAP + Max Pain)")
    if ohlc_df is not None and not ohlc_df.empty:
        chart_fig = go.Figure()
        chart_fig.add_trace(go.Candlestick(
            x=ohlc_df['time'], open=ohlc_df['open'], high=ohlc_df['high'],
            low=ohlc_df['low'], close=ohlc_df['close'], name='NIFTY Spot',
            increasing_line_color='#28a745', decreasing_line_color='#dc3545',
        ))

        has_true_volume = ohlc_df['volume'].sum() > 0
        if not ohlc_df['vwap'].isna().all():
            vwap_label = 'VWAP' if has_true_volume else 'Session Avg (proxy — see note below)'
            chart_fig.add_trace(go.Scatter(
                x=ohlc_df['time'], y=ohlc_df['vwap'], mode='lines', name=vwap_label,
                line=dict(color='#ffa500', width=1.6),
            ))

        # Max Pain trend, sourced from your own session log (recomputed each OI poll)
        log_df_for_chart = pd.DataFrame(st.session_state.session_log)
        if not log_df_for_chart.empty and 'MaxPain' in log_df_for_chart.columns:
            mp_series = log_df_for_chart.dropna(subset=['MaxPain'])
            if not mp_series.empty:
                mp_times = pd.to_datetime(today_str + ' ' + mp_series['Time'].astype(str)).dt.tz_localize(IST)
                chart_fig.add_trace(go.Scatter(
                    x=mp_times, y=mp_series['MaxPain'], mode='lines', name='Max Pain',
                    line=dict(color='#7b2ff7', width=1.6, dash='dot'),
                ))

        # VWAP touch-and-hold markers — candles that dipped/spiked into VWAP
        # intrabar but still closed on the same side (retest-and-continue)
        if vwap_trend and not vwap_trend['touch_hold_df'].empty:
            chart_fig.add_trace(go.Scatter(
                x=vwap_trend['touch_hold_df']['time'], y=vwap_trend['touch_hold_df']['vwap'],
                mode='markers', name='VWAP touch & hold',
                marker=dict(color='#00c2ff', size=10, symbol='circle-open', line=dict(width=2)),
            ))

        chart_fig.update_layout(
            height=480, xaxis_rangeslider_visible=False,
            legend=dict(orientation="h", y=1.08),
            margin=dict(l=10, r=10, t=10, b=10),
            yaxis_title="Price",
        )
        st.plotly_chart(chart_fig, use_container_width=True)

        if not has_true_volume:
            st.caption(
                "ℹ️ Dhan's intraday-candle feed reports 0 volume for the NIFTY *index* itself (only its "
                "constituents/futures carry traded volume), so the orange line is a cumulative simple "
                "average of typical price, not a true volume-weighted VWAP. It's a reasonable intraday "
                "trend proxy, but treat it as directional rather than an exact VWAP level."
            )
        st.caption(
            f"Candles: {candle_interval}-min · refreshed every {CANDLE_FETCH_THROTTLE_SECONDS}s while market is open "
            f"· last candle fetch: {st.session_state.last_candle_fetch.strftime('%H:%M:%S') if st.session_state.last_candle_fetch else 'N/A'}"
        )

        # ----------------------------------------
        # VWAP TREND READ — the streak/touch-and-hold pattern you track manually
        # ----------------------------------------
        st.markdown("##### 📍 VWAP Trend Read")
        if vwap_trend:
            vt1, vt2, vt3, vt4 = st.columns(4)
            side_label = "🟢 Above VWAP" if vwap_trend['side'] == 'above' else "🔴 Below VWAP"
            dist_label = (f"{vwap_trend['distance_pts']:+.1f} pts ({vwap_trend['distance_pct']:+.2f}%)"
                          if vwap_trend['distance_pct'] is not None else "—")
            vt1.metric("Current Side", side_label, dist_label)
            vt2.metric("Streak", f"{vwap_trend['streak_candles']} candles", f"~{vwap_trend['streak_minutes']} min")
            vt3.metric("VWAP Touch & Hold", f"{vwap_trend['touch_hold_count']}x this streak")
            vt4.metric("Trend Status", "✅ Confirmed" if vwap_trend['confirmed'] else "⏳ Building")

            if vwap_trend['confirmed']:
                direction_word = "upside" if vwap_trend['side'] == 'above' else "downside"
                hold_note = f", with {vwap_trend['touch_hold_count']} VWAP retest(s) held" if vwap_trend['touch_hold_count'] else ""
                st.success(
                    f"Price has **closed** on the **{vwap_trend['side']}** side of session VWAP for "
                    f"**{vwap_trend['streak_candles']} consecutive {candle_interval}-min candles "
                    f"(~{vwap_trend['streak_minutes']} min)**{hold_note}. This is the VWAP-respect pattern you "
                    f"watch for — historically this kind of hold has room to extend another **30–60 min** on the "
                    f"{direction_word} while VWAP keeps holding. Treat a candle **close** back through VWAP as the "
                    f"invalidation signal, not just a wick touch."
                )
            else:
                st.info(
                    f"Streak building: **{vwap_trend['streak_candles']}/{vwap_confirm_candles} candles** on the "
                    f"**{vwap_trend['side']}** side of VWAP — not yet confirmed as a VWAP-respecting trend."
                )
            if vwap_trend['last_break_time'] is not None:
                st.caption(f"Last VWAP side flip (candle close through VWAP): "
                           f"{vwap_trend['last_break_time'].strftime('%H:%M')}")
        else:
            st.caption("Not enough candle history yet this session to read a VWAP streak.")
    else:
        st.caption(
            "Candlestick chart unavailable — either the market is closed with no cached candle data yet this "
            "session, or Dhan's intraday-candle endpoint didn't return data (check your access token, same "
            "as the option chain fetch above)."
        )
    st.markdown("---")

# ==========================================
# SIGNAL PROGRESS PANEL
# ==========================================
st.subheader("📐 Signal Progress — what's met, what's still missing")
if sig != "wait for data confirmation":
    st.success(f"**{sig}** is currently active — all conditions for this tier are satisfied.")
elif tier_report:
    # Surface the tier with the most conditions already satisfied, so you can
    # watch a setup building through the day instead of only seeing a flip.
    closest_tier = max(tier_report.items(), key=lambda kv: kv[1]['met'] / kv[1]['total'])
    tier_name, tier_data = closest_tier
    st.info(f"Closest to firing: **{tier_name}** ({tier_data['met']}/{tier_data['total']} conditions met)")
    cols = st.columns(tier_data['total'])
    for col, (label, met, gap) in zip(cols, tier_data['conditions']):
        with col:
            icon = "✅" if met else "❌"
            gap_label = f"margin +{gap:.2f}" if met else f"short by {abs(gap):.2f}"
            st.markdown(f"{icon} **{label}**")
            st.caption(gap_label)

    with st.expander("Show all 4 tiers' full checklists"):
        for tier_name, tier_data in tier_report.items():
            st.markdown(f"**{tier_name}** — {tier_data['met']}/{tier_data['total']} met")
            for label, met, gap in tier_data['conditions']:
                icon = "✅" if met else "❌"
                gap_label = f"margin +{gap:.2f}" if met else f"short by {abs(gap):.2f}"
                st.caption(f"{icon} {label} — {gap_label}")
            st.markdown("")
else:
    st.caption("Not enough data yet to evaluate tier progress.")

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
# INSTITUTIONAL FOOTPRINT SIGNAL (new — independent of Master Signal & VWAP Trend)
# ==========================================
if show_footprint_panel:
    st.subheader("🕵️ Institutional Footprint Signal")
    if footprint_headline:
        footprint_colors = {"bullish": "#1e7e34", "bearish": "#c82333", "neutral": "#6c757d"}
        st.markdown(f"""
<div style='background-color:{footprint_colors.get(footprint_color_key, "#6c757d")};padding:18px;border-radius:10px;
margin:6px 0;'>
    <h3 style='color:white;margin:0;'>{footprint_headline}</h3>
    <p style='color:white;margin:6px 0 0 0;'>Zone: ATM ± {footprint_width} strikes &nbsp;|&nbsp;
    Today's price action: <b>{footprint_market_dir}</b></p>
</div>""", unsafe_allow_html=True)

        for line in footprint_lines:
            st.caption(f"• {line}")

        fp1, fp2, fp3 = st.columns(3)
        fp1.metric("IV Skew (CE_IV − PE_IV)", f"{footprint_agg['iv_skew']:+.2f}" if pd.notna(footprint_agg['iv_skew']) else "—")
        fp2.metric("ChgPCR (today's flow)", f"{footprint_agg['chg_pcr']:.2f}" if pd.notna(footprint_agg['chg_pcr']) else "—")
        fp3.metric("Vol/OI (conviction)", f"{footprint_agg['vol_oi']:.2f}" if pd.notna(footprint_agg['vol_oi']) else "—")

        with st.expander(f"📋 Institutional Footprint Table (ATM ± {footprint_width} strikes)"):
            fmt_table = footprint_table.copy()
            fmt_table['ATM'] = np.where(fmt_table['Strike'] == atm_strike, '⬅ ATM', '')
            display_fp_cols = ['Strike', 'ATM', 'CE_IV', 'PE_IV', 'IV_Skew', 'CE_OI_chg', 'PE_OI_chg',
                                'ChgPCR', 'CE_Volume', 'PE_Volume', 'Total_OI', 'Vol_OI']

            def _iv_skew_cell_color(val):
                if pd.isna(val):
                    return ''
                if val <= footprint_thresholds['iv_skew_bearish']:
                    return 'background-color: #f8d7da'   # red tint — Put buying
                if val >= footprint_thresholds['iv_skew_bullish']:
                    return 'background-color: #d4edda'   # green tint — Put writing
                return ''

            def _vol_oi_cell_color(val):
                if pd.isna(val):
                    return ''
                if val >= footprint_thresholds['vol_oi_fresh']:
                    return 'background-color: #cfe2ff'   # blue tint — fresh money
                if val < footprint_thresholds['vol_oi_fakeout']:
                    return 'background-color: #f8d7da'   # red tint — fakeout risk
                return ''

            # Manual CSS-based highlighting (no matplotlib dependency, unlike
            # Styler.background_gradient which isn't installed on Streamlit
            # Cloud by default). Wrapped so a styling hiccup never breaks the
            # table -- it just falls back to plain formatting.
            try:
                styled = fmt_table[display_fp_cols].style.format(precision=2)
                map_fn = styled.map if hasattr(styled, 'map') else styled.applymap
                styled = map_fn(_iv_skew_cell_color, subset=['IV_Skew'])
                map_fn2 = styled.map if hasattr(styled, 'map') else styled.applymap
                styled = map_fn2(_vol_oi_cell_color, subset=['Vol_OI'])
                st.dataframe(styled, use_container_width=True, height=360)
            except Exception:
                st.dataframe(fmt_table[display_fp_cols].style.format(precision=2),
                              use_container_width=True, height=360)

            st.caption(
                "IV_Skew = CE_IV − PE_IV · ChgPCR = today's PE_OI_chg / CE_OI_chg (today's flow, not the "
                "standing PCR) · Vol/OI = (CE_Volume+PE_Volume) / (CE_OI+PE_OI), the 'fresh money' ratio."
            )
    else:
        st.caption("Not enough option chain data yet this poll to compute the Institutional Footprint.")

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
