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

And directly below the chart, the IV LENS implements your trade-gating read of implied
volatility against price:

    Price DOWN + IV DOWN -> Shakeout            -> longable
    Price DOWN + IV UP   -> Distribution        -> stand down, however good the OI looks
    Price UP   + IV UP   -> Fear bid / squeeze  -> never chase; negative skew confirms the fade
    Price UP   + IV DOWN -> Conviction          -> controlled accumulation (the smart-money grind)

The lens is a GATE, not another opinion: the distribution quadrant vetoes the OI Master
Signal outright, and the squeeze quadrant blocks chasing. A compact gate strip sits under
the Master Signal banner (toggleable) with the full detail below the chart.

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

# OI PROFILE overlay -- horizontal per-strike OI bars pinned to the right edge of the
# candle chart, on the SAME price axis as the candles, so an OI wall lines up visually
# with the price level it sits at. Read like a volume profile, but of open interest.
OI_PROFILE_WIDTH = 8          # ATM +- N strikes included in the profile
OI_PROFILE_FRAC = 0.25        # widest bar occupies this fraction of the chart width
OI_PROFILE_PAD_STRIKES = 3    # headroom (in strikes) above/below the day's range when fitting Y to price

# Institutional Footprint settings -- a THIRD, fully independent read (IV Skew,
# ChgPCR momentum, Vol/OI conviction) layered alongside the Master Signal and the
# VWAP Trend Read above. Never feeds into either of those; purely additive.
FOOTPRINT_WIDTH = 5                                    # ATM +- N strikes for the footprint zone/table
DEFAULT_FOOTPRINT_THRESHOLDS = {
    "iv_skew_bearish": -2.0,     # CE_IV - PE_IV <= this -> aggressive Put buying -> look for breakdown
    "iv_skew_bullish": 2.0,      # CE_IV - PE_IV >= this -> Put writers running -> look for short-covering rally
    "chgpcr_bear_trap": 1.5,     # ChgPCR spikes above this while price is FALLING -> Bear Trap (dip being bought)
    "chgpcr_bull_trap": 0.5,     # ChgPCR collapses below this while price is RISING -> Bull Trap (rally being sold into)
    # CALIBRATION NOTE (from the 14-Aug-2026 live session, 635 polls): the original
    # 0.6 / 0.2 thresholds were an order of magnitude below the Vol/OI this feed
    # actually produces. Observed range was 0.47 to 21.9, median 8.3 -- so "fresh
    # money confirmed" fired on 99.7% of polls and "fakeout risk" never fired once.
    # The conviction tag was pinned to REAL all day and carried no information.
    # These are today's 75th/25th percentiles, so the tag now actually discriminates.
    # One day is thin calibration -- the panel shows where the live value sits in
    # today's own distribution so you can retune these with a week of evidence.
    "vol_oi_fresh": 13.0,        # Vol/OI >= this -> fresh institutional money, regime is "real"
    "vol_oi_fakeout": 5.0,       # Vol/OI < this -> just intraday squaring off, ignore the breakout
    "trend_flat_band_pct": 0.1,  # spot within +-this% of today's open counts as "sideways", not rising/falling
    "chgpcr_min_ce_chg_abs": 300,       # minimum |net CE OI change| (contracts) in the zone before trusting ChgPCR
    "chgpcr_min_ce_chg_pct_of_oi": 0.3, # ...OR at least this % of the zone's total OI, whichever floor is higher
}

# IV LENS settings -- the trade gate that sits below the candlestick chart (and, if
# enabled, as a compact strip directly under the Master Signal banner).
#
# MEASUREMENT: over a rolling window, the change in spot is compared against the change
# in ATM implied volatility, both taken from the SAME logged polls.
#
# RULESET:
#     Price DOWN + IV DOWN -> Shakeout            -> longable
#     Price DOWN + IV UP   -> Distribution        -> stand down (vetoes the OI Master Signal)
#     Price UP   + IV UP   -> Fear bid / squeeze  -> never chase; skew flipping negative confirms the fade
#     Price UP   + IV DOWN -> Conviction          -> controlled accumulation
#
# All four up/down quadrants are covered, so unlike the earlier four-rule panel this
# replaced, there is no unmapped state to toggle. When either leg is FLAT the lens
# stays silent rather than inventing a verdict -- flat is not one of the four quadrants.
DEFAULT_IV_LENS_THRESHOLDS = {
    "lookback_minutes": 15,      # rolling window over which the two changes are measured
    "iv_significant_pct": 1.5,   # |IV change| below this % (relative) counts as "no significant IV move"
    "price_significant_pct": 0.10,  # |spot change| below this % counts as "flat"
    "min_samples": 4,            # need at least this many logged polls in the window before reading it
    "atm_iv_width": 1,           # ATM IV = mean of CE+PE IV across ATM +- N strikes (N=1 -> ATM straddle-ish)
    "skew_fade_confirm": 0.0,    # weighted (CE_IV - PE_IV) below this, in the up/up quadrant, confirms the fade
    "skew_width": 3,             # ATM +- N strikes for the OI-weighted skew the lens consults
    # ADAPTIVE FLOORS (opt-in, default off). A fixed % floor is calibrated to one
    # volatility regime and silently changes meaning when the regime changes. On the
    # 14-Aug session the 0.10% price floor was ~24 pts over 15 min, while the index's
    # ENTIRE day range was 96 pts -- so price read "flat" on 606 of 623 polls and the
    # lens was silent 97% of the day. Switched on, the floors are instead set to a
    # percentile of the session's OWN realized moves over the same lookback, so the
    # same setting behaves sensibly on a quiet day and a trending one.
    "adaptive_floors": False,
    "adaptive_pctile": 70,       # floor = this percentile of today's |move| per window
    "adaptive_price_min": 0.02, "adaptive_price_max": 0.40,   # clamps, % of spot
    "adaptive_iv_min": 0.30, "adaptive_iv_max": 6.00,         # clamps, % of IV level
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
    replacement for manually pasting Dash Board!A3:N3 into a new row).

    Schema-change safe: if today's file was started by an older build of this app
    (i.e. before the ATM_IV / IV_Lens_Stance columns existed), a blind append
    would silently shift every value one column to the left. So the header is
    checked first -- same columns in a different order are just reordered, and a
    genuinely different column set triggers a one-off rewrite with the union of
    columns (old rows get blanks in the new fields). After that single rewrite,
    appends go back to being cheap."""
    try:
        path = LOG_DIR / f"{date_str}.csv"
        row_df = pd.DataFrame([row])
        if path.exists():
            existing_cols = list(pd.read_csv(path, nrows=0).columns)
            if set(existing_cols) == set(row.keys()):
                row_df[existing_cols].to_csv(path, mode='a', header=False, index=False)
            else:
                old = pd.read_csv(path)
                pd.concat([old, row_df], ignore_index=True).to_csv(path, index=False)
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
    """Same schema-safety concern as append_log_row(): an existing sheet started by
    an older build won't have the new columns, so values are written positionally
    against the sheet's actual header, and any genuinely new keys are appended to
    the header row first (old rows simply stay blank in those columns)."""
    if not gsheets_configured():
        return
    try:
        ws = get_gsheet_worksheet(GSHEET_LOG_SHEET)
        existing = ws.get_all_values()
        if not existing:
            header = list(row.keys())
            ws.append_row(header)
        else:
            header = existing[0]
            missing = [k for k in row.keys() if k not in header]
            if missing:
                header = header + missing
                try:
                    ws.update(values=[header], range_name='A1')
                except TypeError:   # older gspread signature: update(range_name, values)
                    ws.update('A1', [header])
        ws.append_row([str(row.get(c, "")) for c in header])
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


def build_oi_profile(df: pd.DataFrame, atm: float, width: int):
    """Per-strike OI slice for the chart's right-edge profile, plus the two levels
    that actually matter for a breakout read: the strike carrying the most CE OI
    (the resistance wall / where call writers are defending) and the most PE OI
    (the support floor).

    Today's OI *change* at each of those strikes is carried along too, because the
    standing OI alone can't tell you whether a wall is being defended or abandoned
    -- and that distinction is the whole difference between a real breakout and a
    false one."""
    band = df[(df['Strike'] >= atm - width * STRIKE_STEP) &
              (df['Strike'] <= atm + width * STRIKE_STEP)].copy()
    if band.empty:
        return None
    band['Total_OI'] = band['CE_OI'] + band['PE_OI']

    def _peak(col, chg_col):
        if band[col].max() <= 0:
            return None, None, None
        row = band.loc[band[col].idxmax()]
        return float(row['Strike']), float(row[col]), float(row[chg_col])

    ce_strike, ce_oi, ce_chg = _peak('CE_OI', 'CE_OI_chg')
    pe_strike, pe_oi, pe_chg = _peak('PE_OI', 'PE_OI_chg')

    return {
        'band': band,
        'max_ce_strike': ce_strike, 'max_ce_oi': ce_oi, 'max_ce_chg': ce_chg,
        'max_pe_strike': pe_strike, 'max_pe_oi': pe_oi, 'max_pe_chg': pe_chg,
        'max_side_oi': float(max(band['CE_OI'].max(), band['PE_OI'].max())),
        'max_total_oi': float(band['Total_OI'].max()),
    }


# ==========================================
# IV LENS — trade gate (replaces the earlier four-rule IV vs Price panel)
# ==========================================
def compute_atm_iv(df: pd.DataFrame, atm: float, width: int = 1):
    """Single 'the market's IV right now' scalar: the mean of CE and PE implied
    vol across ATM +- width strikes. Zero/blank IVs are dropped rather than
    averaged in, because Dhan returns 0 for strikes it has no IV for and
    including those would drag the average down and manufacture a fake
    'IV falling' reading. Width 1 keeps it close to the ATM straddle, which is
    the cleanest proxy for at-the-money vol and the least contaminated by wing
    skew moving around."""
    zone = df[(df['Strike'] >= atm - width * STRIKE_STEP) & (df['Strike'] <= atm + width * STRIKE_STEP)]
    if zone.empty:
        return np.nan
    ivs = pd.to_numeric(pd.concat([zone['CE_IV'], zone['PE_IV']], ignore_index=True), errors='coerce')
    ivs = ivs[ivs > 0]
    return float(ivs.mean()) if len(ivs) else np.nan


def compute_lens_skew(df: pd.DataFrame, atm: float, width: int = 3):
    """OI-weighted CE_IV - PE_IV across ATM +- width strikes, used by the lens
    only in the price-up + IV-up quadrant to confirm a squeeze fade.

    Computed standalone rather than reusing the Institutional Footprint's skew,
    so the lens keeps working with the Footprint panel switched off and with its
    own band width. Strikes with a zero/blank IV on either leg are dropped
    instead of being treated as 0 vol, which would manufacture a large fake
    negative skew and falsely 'confirm' a fade."""
    zone = df[(df['Strike'] >= atm - width * STRIKE_STEP) &
              (df['Strike'] <= atm + width * STRIKE_STEP)].copy()
    if zone.empty:
        return np.nan
    ce = pd.to_numeric(zone['CE_IV'], errors='coerce')
    pe = pd.to_numeric(zone['PE_IV'], errors='coerce')
    w = pd.to_numeric(zone['CE_OI'] + zone['PE_OI'], errors='coerce')
    skew = ce - pe
    mask = skew.notna() & (ce > 0) & (pe > 0) & w.notna() & (w > 0)
    if not mask.any():
        return np.nan
    return float((skew[mask] * w[mask]).sum() / w[mask].sum())


def _edge_means(values: np.ndarray, edge_n: int):
    """Start/end levels of a window, averaged over a few samples at each end so
    one jumpy 10-second poll can't flip the whole read."""
    return float(np.mean(values[:edge_n])), float(np.mean(values[-edge_n:]))


def _session_move_floor(ts, values, lookback_minutes: int, pctile: float,
                        lo: float, hi: float, min_windows: int = 6):
    """Significance floor derived from the session's own realized moves rather
    than a fixed constant.

    The series is resampled into NON-OVERLAPPING buckets the same length as the
    lookback window, and the floor is set to a percentile of those buckets'
    absolute % changes. Non-overlapping matters: overlapping windows share most
    of their samples, so their moves are heavily autocorrelated and a percentile
    taken over them would be far too tight.

    Returns (floor, n_windows), or (None, n) while there aren't enough completed
    buckets yet to estimate anything -- the caller falls back to the fixed floor
    during that warm-up rather than guessing from two data points."""
    try:
        s = pd.Series(np.asarray(values, dtype=float), index=pd.to_datetime(ts))
        res = s.resample(f'{int(lookback_minutes)}min').last().dropna()
        moves = res.pct_change().dropna().abs() * 100
        if len(moves) < min_windows:
            return None, len(moves)
        return float(np.clip(np.percentile(moves, pctile), lo, hi)), len(moves)
    except Exception:
        return None, 0


def measure_price_iv_window(log_records, date_str: str, t: dict):
    """Measures, without interpreting: the change in spot and the change in ATM
    IV over the rolling window, and which direction each of those counts as.

    Both series come from THIS APP'S session log (spot + ATM IV recorded on the
    same poll), deliberately -- not from the candle feed for price and the chain
    for IV. Mixing two clocks would compare a price change measured over one
    interval against an IV change measured over a slightly different one, which
    is exactly the kind of small misalignment that flips a borderline quadrant
    call for no real reason -- and with a veto hanging off that call, a spurious
    flip is expensive.

    'Significant' is relative, not absolute: IV is judged as a % change of the
    IV level itself (so 0.3 vol points means something different at 9 IV than at
    22 IV), and price as a % of spot. Both floors are tunable in the sidebar.

    Returns None when there's no usable history, or a dict with ready=False
    while the window is still filling up."""
    if not log_records:
        return None
    hist = pd.DataFrame(log_records)
    if not {'Time', 'Spot', 'ATM_IV'}.issubset(hist.columns):
        return None

    hist = hist[['Time', 'Spot', 'ATM_IV']].copy()
    hist['Spot'] = pd.to_numeric(hist['Spot'], errors='coerce')
    hist['ATM_IV'] = pd.to_numeric(hist['ATM_IV'], errors='coerce')
    hist = hist.dropna(subset=['Spot', 'ATM_IV'])
    hist = hist[(hist['ATM_IV'] > 0) & (hist['Spot'] > 0)]
    if hist.empty:
        return None

    hist['ts'] = pd.to_datetime(date_str + ' ' + hist['Time'].astype(str), errors='coerce')
    hist = hist.dropna(subset=['ts']).sort_values('ts').reset_index(drop=True)
    if hist.empty:
        return None

    end_ts = hist['ts'].iloc[-1]
    window = hist[hist['ts'] >= end_ts - timedelta(minutes=t['lookback_minutes'])]
    samples = len(window)
    span_minutes = ((window['ts'].iloc[-1] - window['ts'].iloc[0]).total_seconds() / 60) if samples > 1 else 0.0

    if samples < t['min_samples']:
        return {'ready': False, 'samples': samples, 'needed': int(t['min_samples']),
                'span_minutes': span_minutes, 'series': hist,
                'window_start': window['ts'].iloc[0] if samples else None, 'window_end': end_ts}

    edge = max(1, min(3, samples // 4))
    p_start, p_end = _edge_means(window['Spot'].values, edge)
    iv_start, iv_end = _edge_means(window['ATM_IV'].values, edge)

    price_chg_pts = p_end - p_start
    price_chg_pct = (price_chg_pts / p_start * 100) if p_start else np.nan
    iv_chg_pts = iv_end - iv_start
    iv_chg_pct = (iv_chg_pts / iv_start * 100) if iv_start else np.nan

    p_th, iv_th = t['price_significant_pct'], t['iv_significant_pct']
    floor_source, floor_windows = 'fixed', 0
    if t.get('adaptive_floors'):
        pf, floor_windows = _session_move_floor(
            hist['ts'], hist['Spot'], t['lookback_minutes'], t['adaptive_pctile'],
            t['adaptive_price_min'], t['adaptive_price_max'])
        vf, _ = _session_move_floor(
            hist['ts'], hist['ATM_IV'], t['lookback_minutes'], t['adaptive_pctile'],
            t['adaptive_iv_min'], t['adaptive_iv_max'])
        # Both floors switch together or neither does, so the two legs are always
        # judged on the same basis -- a mixed pair would make the quadrant depend
        # on which series happened to have enough history.
        if pf is not None and vf is not None:
            p_th, iv_th, floor_source = pf, vf, 'adaptive'
        else:
            floor_source = 'fixed (adaptive warming up)'

    price_dir = 'rising' if price_chg_pct > p_th else ('falling' if price_chg_pct < -p_th else 'flat')
    iv_dir = 'rising' if iv_chg_pct > iv_th else ('falling' if iv_chg_pct < -iv_th else 'flat')

    return {
        'ready': True,
        'price_dir': price_dir, 'iv_dir': iv_dir,
        'price_floor': p_th, 'iv_floor': iv_th,
        'floor_source': floor_source, 'floor_windows': floor_windows,
        'price_chg_pct': price_chg_pct, 'price_chg_pts': price_chg_pts,
        'iv_chg_pct': iv_chg_pct, 'iv_chg_pts': iv_chg_pts,
        'price_start': p_start, 'price_end': p_end,
        'iv_start': iv_start, 'iv_end': iv_end,
        'samples': samples, 'span_minutes': span_minutes, 'edge_n': edge,
        'series': hist, 'window_start': window['ts'].iloc[0], 'window_end': end_ts,
    }


def apply_iv_lens(measured, iv_skew, t: dict):
    """Maps the measured quadrant onto your lens ruleset.

      Price DOWN + IV DOWN -> Shakeout      -> longable
      Price DOWN + IV UP   -> Distribution  -> stand down, however good the OI looks
      Price UP   + IV UP   -> Fear bid      -> never chase; negative skew confirms the fade
      Price UP   + IV DOWN -> Conviction    -> controlled accumulation

    Kept separate from measure_price_iv_window() on purpose: that function only
    describes what happened, this one is the only place a verdict is asserted,
    so retuning the rules never touches the measurement.

    When either leg is FLAT the lens returns a no-read rather than guessing --
    flat isn't one of the four quadrants, and a gate that vetoes trades should
    stay silent instead of improvising. Returns None until the window is ready."""
    if not measured or not measured.get('ready'):
        return None

    p, v = measured['price_dir'], measured['iv_dir']
    skew_txt = f"{iv_skew:+.2f}" if pd.notna(iv_skew) else "n/a"
    notes = []

    # Which leg (if any) is holding the lens silent, and by how much. Without this
    # a silent lens is indistinguishable from a broken one -- on the 14-Aug session
    # it read "NO LENS READ" on 97% of polls and there was no way to see from the
    # panel that price was simply 16 points short of the floor.
    p_floor = measured.get('price_floor', t['price_significant_pct'])
    iv_floor = measured.get('iv_floor', t['iv_significant_pct'])
    blockers = []
    if p == 'flat':
        short_pct = p_floor - abs(measured['price_chg_pct'])
        short_pts = short_pct / 100 * measured['price_end'] if measured.get('price_end') else None
        pts_txt = f" (~{short_pts:.0f} pts)" if short_pts is not None else ""
        blockers.append(("price", f"moved {measured['price_chg_pct']:+.3f}% over the window, floor is "
                                  f"±{p_floor:.3f}% — short by {short_pct:.3f}%{pts_txt}"))
    if v == 'flat':
        short_iv = iv_floor - abs(measured['iv_chg_pct'])
        blockers.append(("IV", f"moved {measured['iv_chg_pct']:+.2f}% over the window, floor is "
                               f"±{iv_floor:.2f}% — short by {short_iv:.2f}%"))

    if p == 'falling' and v == 'falling':
        stance, headline, color = 'shakeout', "🟢 SHAKEOUT — longable", "#1e7e34"
        action = "Longs permitted into weakness"
        direction, veto, chase_block = 'bullish', False, False
        notes.append("Price coming off while vol bleeds — nobody is paying up for protection on the way down. "
                     "That's positioning being flushed, not risk being repriced. Dips here are the buyable kind.")
    elif p == 'falling' and v == 'rising':
        stance, headline, color = 'distribution', "⛔ DISTRIBUTION — stand down", "#c82333"
        action = "No new positions — the OI read does not apply"
        direction, veto, chase_block = 'bearish', True, False
        notes.append("Price down with vol bid is genuine repricing, not a flush: protection is being paid for "
                     "into the decline. Stand down regardless of how constructive the OI/Master Signal looks.")
    elif p == 'rising' and v == 'rising':
        stance, headline, color = 'fear_bid', "🟠 FEAR BID / SQUEEZE — do not chase", "#d97706"
        action = "No chasing — wait for the fade or a pullback"
        direction, veto, chase_block = 'bearish', False, True
        notes.append("Price and vol rising together is a squeeze / fear bid, not accumulation — the move is "
                     "being paid for in premium. Never chase strength in this quadrant.")
        if pd.isna(iv_skew):
            notes.append("Skew unavailable this poll, so the fade confirmation can't be checked.")
        elif iv_skew < t['skew_fade_confirm']:
            notes.append(f"Skew {skew_txt} has flipped negative (CE_IV below PE_IV) — **fade confirmed**: calls "
                         "are being sold into the rip while puts stay bid.")
        else:
            notes.append(f"Skew {skew_txt} has not flipped negative yet — the squeeze may still have legs, so "
                         "the fade is not confirmed. Still no chasing either way.")
    elif p == 'rising' and v == 'falling':
        stance, headline, color = 'accumulation', "🟢 CONVICTION — controlled accumulation", "#1e7e34"
        action = "Trend longs — the smart-money grind"
        direction, veto, chase_block = 'bullish', False, False
        notes.append("Price grinding up while vol bleeds out: size is being absorbed without anyone paying up "
                     "for protection. This is the accumulation regime, not a chase.")
    else:
        stance, headline, color = 'no_read', "⚪ NO LENS READ", "#6c757d"
        action = "Lens is silent"
        direction, veto, chase_block = 'neutral', False, False
        notes.append(f"Price is {p} and IV is {v}. The lens is defined only for the four up/down quadrants, so "
                     "no verdict is being asserted.")
        for lbl, gap in blockers:
            notes.append(f"Blocked by {lbl}: {gap}")

    fade_confirmed = bool(stance == 'fear_bid' and pd.notna(iv_skew) and iv_skew < t['skew_fade_confirm'])

    return {
        'stance': stance, 'headline': headline, 'action': action, 'color': color,
        'direction': direction, 'veto': veto, 'chase_block': chase_block,
        'fade_confirmed': fade_confirmed, 'notes': notes, 'blockers': blockers,
        'price_dir': p, 'iv_dir': v, 'iv_skew': iv_skew,
    }


def build_iv_price_chart(series_df: pd.DataFrame, window_start=None, window_end=None):
    """Dual-axis session view of spot (left) against ATM IV (right) — the visual
    behind the lens verdict, so you can see whether the two lines are converging
    or diverging rather than trusting a single label."""
    f = make_subplots(specs=[[{"secondary_y": True}]])
    f.add_trace(go.Scatter(x=series_df['ts'], y=series_df['Spot'], name='Spot',
                           line=dict(color='#0d6efd', width=1.7)), secondary_y=False)
    f.add_trace(go.Scatter(x=series_df['ts'], y=series_df['ATM_IV'], name='ATM IV',
                           line=dict(color='#ffa500', width=1.7)), secondary_y=True)
    if window_start is not None and window_end is not None and window_start != window_end:
        f.add_vrect(x0=window_start, x1=window_end, fillcolor="#6c757d", opacity=0.12,
                    line_width=0, annotation_text="lens window", annotation_position="top left")
    f.update_yaxes(title_text="Spot", secondary_y=False)
    f.update_yaxes(title_text="ATM IV (%)", secondary_y=True)
    f.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                    legend=dict(orientation="h", y=1.16))
    return f


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
    ('iv_lens_alert_stance', None),
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

        st.markdown("**OI profile overlay**")
        show_oi_profile = st.checkbox("Show OI bars on the right edge", value=True)
        oi_profile_mode = st.radio(
            "Bars", ["CE vs PE (split)", "Combined total OI"], index=0,
            help="Split shows the call wall and put floor separately (better for a breakout read). "
                 "Combined shows where total OI is concentrated (better for spotting pin levels).")
        oi_profile_width = st.number_input(
            "OI profile band (ATM ± N strikes)", min_value=1, max_value=30, value=OI_PROFILE_WIDTH)
        oi_profile_frac = st.slider(
            "Profile width (% of chart)", min_value=10, max_value=45, value=int(OI_PROFILE_FRAC * 100),
            help="The time axis is padded by the same amount on the right, so the bars sit over empty "
                 "space instead of covering the most recent candles.") / 100
        fit_to_price = st.checkbox(
            "Fit Y-axis to price action", value=True,
            help="Off, the axis stretches to cover every strike in the band and squashes the candles flat.")
        oi_pad_strikes = st.number_input(
            "...with N strikes of headroom", min_value=0, max_value=20, value=OI_PROFILE_PAD_STRIKES)
        show_oi_levels = st.checkbox("Draw support / resistance / max-pain level lines", value=True)
        oi_bar_thickness = st.slider(
            "OI bar block thickness (% of strike gap)", min_value=20, max_value=95, value=50,
            help="Height of the CE+PE block at each strike, as a share of the 50-point strike spacing.") / 100

    st.markdown("---")
    with st.expander("🔬 IV Lens (trade gate)", expanded=True):
        show_iv_lens = st.checkbox("Show IV Lens panel", value=True)
        lens_at_top = st.checkbox(
            "Pin the lens gate under the Master Signal", value=True,
            help="The lens can veto the OI signal, so it's worth having it where you make the decision.")
        lens_enforce_gate = st.checkbox(
            "Enforce the veto / no-chase warnings on the Master Signal", value=True,
            help="Off, the lens still reports its stance but stops flagging conflicts with the OI signal.")

        st.markdown("**Measurement window**")
        iv_price_lookback = st.number_input(
            "Lookback window (minutes)", min_value=1, max_value=180,
            value=DEFAULT_IV_LENS_THRESHOLDS['lookback_minutes'],
            help="Both the price change and the IV change are measured across this rolling window, "
                 "using your own logged polls.")
        price_sig_pct = st.number_input(
            "Price move is 'significant' at ± (%)", value=DEFAULT_IV_LENS_THRESHOLDS['price_significant_pct'],
            step=0.05, format="%.2f",
            help="Below this, price counts as flat and the lens stays silent rather than picking a quadrant.")
        iv_sig_pct = st.number_input(
            "IV move is 'significant' at ± (%)", value=DEFAULT_IV_LENS_THRESHOLDS['iv_significant_pct'], step=0.25,
            help="Relative change in ATM IV. Below this, IV counts as flat and the lens stays silent.")
        iv_price_atm_width = st.number_input(
            "ATM IV band (ATM ± N strikes)", min_value=0, max_value=5,
            value=DEFAULT_IV_LENS_THRESHOLDS['atm_iv_width'])
        iv_price_min_samples = st.number_input(
            "Minimum logged polls in window", min_value=2, max_value=60,
            value=DEFAULT_IV_LENS_THRESHOLDS['min_samples'])

        adaptive_floors = st.checkbox(
            "Auto-calibrate floors to today's volatility", value=False,
            help="Instead of the fixed % floors above, set them to a percentile of the session's own "
                 "realized moves over the same window length. Keeps one setting workable across quiet "
                 "and trending days. The fixed floors are still used while it warms up.")
        adaptive_pctile = st.slider(
            "...at which percentile of today's moves", min_value=40, max_value=90,
            value=DEFAULT_IV_LENS_THRESHOLDS['adaptive_pctile'],
            help="Higher = stricter = the lens speaks less often. ~70 gave roughly 9% of polls on the "
                 "14-Aug session, versus 3% with the fixed 0.10% floor.")

        st.markdown("**Squeeze fade confirmation**")
        lens_skew_fade = st.number_input(
            "Skew below this confirms the fade", value=DEFAULT_IV_LENS_THRESHOLDS['skew_fade_confirm'], step=0.5,
            help="Only consulted in the price-up + IV-up quadrant. Skew = OI-weighted CE_IV − PE_IV.")
        lens_skew_width = st.number_input(
            "Lens skew band (ATM ± N strikes)", min_value=1, max_value=15,
            value=DEFAULT_IV_LENS_THRESHOLDS['skew_width'])

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

iv_lens_thresholds = {
    "lookback_minutes": iv_price_lookback, "iv_significant_pct": iv_sig_pct,
    "price_significant_pct": price_sig_pct, "min_samples": iv_price_min_samples,
    "atm_iv_width": iv_price_atm_width,
    "skew_fade_confirm": lens_skew_fade, "skew_width": int(lens_skew_width),
    "adaptive_floors": adaptive_floors, "adaptive_pctile": adaptive_pctile,
    "adaptive_price_min": DEFAULT_IV_LENS_THRESHOLDS['adaptive_price_min'],
    "adaptive_price_max": DEFAULT_IV_LENS_THRESHOLDS['adaptive_price_max'],
    "adaptive_iv_min": DEFAULT_IV_LENS_THRESHOLDS['adaptive_iv_min'],
    "adaptive_iv_max": DEFAULT_IV_LENS_THRESHOLDS['adaptive_iv_max'],
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

# ==========================================
# IV LENS — trade gate (spot change vs ATM IV change, + skew for fade confirmation)
# ==========================================
# Computed BEFORE the log append below, so this poll's own (Spot, ATM_IV) pair is
# part of the window and the resulting stance can be written into the same log row
# rather than lagging one poll behind.
atm_iv = compute_atm_iv(df, atm_strike, int(iv_price_atm_width))
lens_skew = compute_lens_skew(df, atm_strike, iv_lens_thresholds['skew_width'])
current_iv_sample = [{
    'Time': (st.session_state.last_fetch or now_ist).strftime('%H:%M:%S'),
    'Spot': spot, 'ATM_IV': atm_iv,
}] if is_open else []
iv_measured = measure_price_iv_window(
    list(st.session_state.session_log) + current_iv_sample, today_str, iv_lens_thresholds)
iv_lens = apply_iv_lens(iv_measured, lens_skew, iv_lens_thresholds)

# One-shot toast when the lens stance flips (not on every 10s poll)
if iv_lens and iv_lens['stance'] != 'no_read':
    if st.session_state.iv_lens_alert_stance != iv_lens['stance']:
        st.toast(f"🔬 IV Lens: {iv_lens['headline']}", icon="🔬")
        st.session_state.iv_lens_alert_stance = iv_lens['stance']
else:
    st.session_state.iv_lens_alert_stance = None

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
        # --- IV Lens (appended at the end so older logs stay column-aligned) ---
        'ATM_IV': round(atm_iv, 2) if pd.notna(atm_iv) else None,
        'IV_Lens_Stance': iv_lens['stance'] if iv_lens else None,
        'IV_Lens_Headline': iv_lens['headline'] if iv_lens else None,
        'IV_Lens_Skew': round(lens_skew, 2) if pd.notna(lens_skew) else None,
        'IV_Lens_Veto': iv_lens['veto'] if iv_lens else None,
        'IV_Lens_dPrice_pct': round(iv_measured['price_chg_pct'], 3) if (iv_measured and iv_measured.get('ready')) else None,
        'IV_Lens_dIV_pct': round(iv_measured['iv_chg_pct'], 2) if (iv_measured and iv_measured.get('ready')) else None,
        'IV_Lens_Price_Floor': round(iv_measured['price_floor'], 3) if (iv_measured and iv_measured.get('ready')) else None,
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

# --- IV LENS GATE STRIP (directly under the Master Signal, where the decision happens) ---
if show_iv_lens and lens_at_top:
    if iv_lens:
        lens_skew_txt = f"{iv_lens['iv_skew']:+.2f}" if pd.notna(iv_lens['iv_skew']) else "n/a"
        st.markdown(f"""
<div style='background-color:{iv_lens['color']};padding:14px 18px;border-radius:10px;margin:0 0 10px 0;'>
    <h4 style='color:white;margin:0;'>🔬 IV Lens — {iv_lens['headline']}</h4>
    <p style='color:white;margin:6px 0 0 0;'>{iv_lens['action']} &nbsp;|&nbsp;
    Price <b>{iv_lens['price_dir']}</b> · IV <b>{iv_lens['iv_dir']}</b> · Skew <b>{lens_skew_txt}</b>
    {"· fade confirmed" if iv_lens['fade_confirmed'] else ""}</p>
</div>""", unsafe_allow_html=True)

        if lens_enforce_gate:
            if iv_lens['veto'] and sig != "wait for data confirmation":
                st.error(
                    f"⛔ **Lens veto.** The OI Master Signal reads **{sig}**, but price is falling into rising IV "
                    f"— that's distribution, not a dip. Stand down regardless of how good the OI looks."
                )
            elif iv_lens['veto']:
                st.error("⛔ **Lens veto.** Distribution quadrant — no new positions while this holds.")
            elif iv_lens['chase_block'] and sig in ("Strong CE Buy", "PE writers strong"):
                fade_note = (" Skew has flipped negative, which confirms the fade."
                             if iv_lens['fade_confirmed'] else "")
                st.warning(
                    f"🟠 The OI Master Signal reads **{sig}**, but this is the squeeze quadrant — take the entry "
                    f"on a pullback, not by chasing strength.{fade_note}"
                )
    else:
        st.caption("🔬 IV Lens — waiting on enough logged Spot + ATM IV history to read the window.")

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
        oi_profile = build_oi_profile(df, atm_strike, int(oi_profile_width)) if show_oi_profile else None

        chart_fig = go.Figure()

        # --- OI profile bars, plotted on a reversed overlay x-axis (x2) so they grow
        # leftward from the right edge, on the same price (y) axis as the candles.
        # Added FIRST so the candlesticks render on top of them.
        profile_max_x = 0.0
        if oi_profile:
            b = oi_profile['band']
            block = STRIKE_STEP * oi_bar_thickness      # total block height per strike
            if oi_profile_mode.startswith("Combined"):
                chart_fig.add_trace(go.Bar(
                    y=b['Strike'], x=b['Total_OI'], orientation='h', name='Total OI (CE+PE)',
                    marker_color='#8e7cc3', opacity=0.9, width=block, xaxis='x2',
                    hovertemplate='Strike %{y:.0f}<br>Total OI %{x:,.0f}<extra></extra>'))
                profile_max_x = oi_profile['max_total_oi']
            else:
                # One block per strike: CE (red) sitting directly on top of PE (green),
                # edges touching at the strike itself. Explicit y-offsets rather than
                # barmode='group', which behaves unpredictably alongside candlesticks.
                bar_h = block / 2
                chart_fig.add_trace(go.Bar(
                    y=b['Strike'] + bar_h / 2, x=b['CE_OI'], orientation='h', name='CE OI (resistance)',
                    marker_color='#f2827f', opacity=0.9, width=bar_h, xaxis='x2',
                    hovertemplate='Strike %{y:.0f}<br>CE OI %{x:,.0f}<extra></extra>'))
                chart_fig.add_trace(go.Bar(
                    y=b['Strike'] - bar_h / 2, x=b['PE_OI'], orientation='h', name='PE OI (support)',
                    marker_color='#5cbfa6', opacity=0.9, width=bar_h, xaxis='x2',
                    hovertemplate='Strike %{y:.0f}<br>PE OI %{x:,.0f}<extra></extra>'))
                profile_max_x = oi_profile['max_side_oi']

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
            barmode='overlay',
        )

        # --- OI profile axis + framing ---------------------------------------
        if oi_profile and profile_max_x > 0:
            # Reversed range: x=0 lands on the right edge, so bars based at 0 grow
            # leftward and the widest one spans exactly `oi_profile_frac` of the chart.
            chart_fig.update_layout(xaxis2=dict(
                overlaying='x', side='top', range=[profile_max_x / oi_profile_frac, 0],
                showgrid=False, showticklabels=False, zeroline=False, fixedrange=True))

            # Pad the time axis on the right by the same fraction, so the bars sit over
            # empty space rather than hiding the most recent candles. Set via update_layout
            # and NOT update_xaxes -- the latter applies to every x-axis and would overwrite
            # the OI overlay axis above with this datetime range.
            t0, t1 = ohlc_df['time'].iloc[0], ohlc_df['time'].iloc[-1]
            step = pd.Timedelta(minutes=int(candle_interval))
            pad = max((t1 - t0) * (oi_profile_frac / (1 - oi_profile_frac)), step * 3)
            chart_fig.update_layout(xaxis=dict(range=[t0 - step, t1 + pad]))

            if show_oi_levels:
                def _level_line(y, color, text, position, yshift=0):
                    """Dotted level line with a filled tag on the left, matching the
                    OI-profile overlay style."""
                    chart_fig.add_hline(
                        y=y, line_dash='dot', line_color=color, line_width=1.4,
                        annotation_text=f" {text} ", annotation_position=position,
                        annotation_bgcolor=color, annotation_font_color='white',
                        annotation_font_size=11, annotation_yshift=yshift)

                if oi_profile['max_ce_strike']:
                    _level_line(oi_profile['max_ce_strike'], '#e05252',
                                f"Resistance strike {oi_profile['max_ce_strike']:.0f}", 'top left')
                if oi_profile['max_pe_strike']:
                    _level_line(oi_profile['max_pe_strike'], '#3aa17e',
                                f"Support strike {oi_profile['max_pe_strike']:.0f}", 'bottom left')
                if mp is not None:
                    # Nudged down so the tag stays readable on the days when Max Pain
                    # lands on the same strike as the call wall or put floor.
                    _level_line(mp, '#e8a33d', f"Max Pain {mp:.0f}", 'bottom left', yshift=-20)

        if fit_to_price:
            # Without this the Y axis stretches to cover every strike in the profile band
            # and flattens the candles into a ribbon. Bars outside the range just clip.
            pad_y = int(oi_pad_strikes) * STRIKE_STEP
            chart_fig.update_yaxes(range=[ohlc_df['low'].min() - pad_y, ohlc_df['high'].max() + pad_y])

        st.plotly_chart(chart_fig, use_container_width=True)

        # --- OI wall readout: level, size, and whether it's being defended -----
        if oi_profile:
            def _wall_note(strike, oi, chg, side):
                if strike is None:
                    return None
                verb = "building" if chg > 0 else ("unwinding" if chg < 0 else "flat")
                return (f"**{side} {strike:.0f}** — OI {oi:,.0f} ({chg:+,.0f} today, {verb})")

            notes = [n for n in (
                _wall_note(oi_profile['max_ce_strike'], oi_profile['max_ce_oi'], oi_profile['max_ce_chg'],
                           "🔴 Call wall"),
                _wall_note(oi_profile['max_pe_strike'], oi_profile['max_pe_oi'], oi_profile['max_pe_chg'],
                           "🟢 Put floor"),
            ) if n]
            if notes:
                st.markdown(" &nbsp;·&nbsp; ".join(notes))
            if fit_to_price:
                lo = ohlc_df['low'].min() - int(oi_pad_strikes) * STRIKE_STEP
                hi = ohlc_df['high'].max() + int(oi_pad_strikes) * STRIKE_STEP
                hidden = oi_profile['band'][(oi_profile['band']['Strike'] < lo) |
                                            (oi_profile['band']['Strike'] > hi)]
                if not hidden.empty:
                    st.caption(
                        f"ℹ️ {len(hidden)} strike(s) in the profile band sit outside the visible price range "
                        f"and are clipped — raise the headroom setting to bring them into view."
                    )
            st.caption(
                "Reading the walls: a candle **closing** through the call wall while CE OI at that strike is "
                "**unwinding** is writers covering — the breakout has something behind it. Price poking through "
                "while CE OI keeps **building** is writers defending, which is the classic false breakout. "
                "Mirror it at the put floor for downside breaks. The bars are today's standing OI, so check the "
                "±change above, not the bar height, for who's winning right now."
            )

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
# IV LENS PANEL — sits directly below the NIFTY chart
# ==========================================
if show_iv_lens:
    st.subheader("🔬 IV Lens (trade gate)")

    if iv_measured is None:
        st.caption(
            "No usable Spot + ATM IV history logged yet today. The lens builds its two series from your own "
            "polls, so it needs the app running during market hours for a couple of minutes before it can read "
            "anything. (Logs written by an older build of this app won't have the ATM_IV column — those days "
            "will stay blank here.)"
        )
    elif not iv_measured.get('ready'):
        st.info(
            f"Building the read — **{iv_measured['samples']}/{iv_measured['needed']} polls** collected in the "
            f"last {iv_price_lookback} minutes (~{iv_measured['span_minutes']:.1f} min of history so far)."
        )
        if len(iv_measured['series']) >= 2:
            st.plotly_chart(build_iv_price_chart(iv_measured['series']), use_container_width=True)
    else:
        arrow = {'rising': '↑', 'falling': '↓', 'flat': '→'}
        lens_skew_txt = f"{iv_lens['iv_skew']:+.2f}" if pd.notna(iv_lens['iv_skew']) else "n/a"
        st.markdown(f"""
<div style='background-color:{iv_lens['color']};padding:18px;border-radius:10px;margin:6px 0;'>
    <h3 style='color:white;margin:0;'>{iv_lens['headline']}</h3>
    <p style='color:white;margin:8px 0 0 0;'><b>{iv_lens['action']}</b></p>
    <p style='color:white;margin:6px 0 0 0;'>
    Price <b>{iv_measured['price_dir']} {arrow[iv_measured['price_dir']]}</b>
    ({iv_measured['price_chg_pct']:+.2f}%, {iv_measured['price_chg_pts']:+.0f} pts)
    &nbsp;|&nbsp;
    IV <b>{iv_measured['iv_dir']} {arrow[iv_measured['iv_dir']]}</b>
    ({iv_measured['iv_chg_pct']:+.2f}%, {iv_measured['iv_chg_pts']:+.2f} vol pts)
    &nbsp;|&nbsp; Skew <b>{lens_skew_txt}</b>
    &nbsp;|&nbsp; window ~{iv_measured['span_minutes']:.0f} min</p>
</div>""", unsafe_allow_html=True)

        for line in iv_lens['notes']:
            st.caption(f"• {line}")

        if iv_lens['veto']:
            st.error(
                "⛔ **Stand down.** This is the distribution quadrant — the lens overrides the OI read here by "
                "design. No new positions while it holds, however constructive the Master Signal looks."
            )
        elif iv_lens['chase_block']:
            st.warning(
                "🟠 **Do not chase.** Price and IV rising together is a squeeze, not accumulation. "
                + ("Skew has flipped negative — the fade is confirmed."
                   if iv_lens['fade_confirmed'] else
                   "Skew hasn't flipped negative yet, so the fade isn't confirmed — but still no chasing.")
            )

        ip1, ip2, ip3, ip4 = st.columns(4)
        ip1.metric("Spot", f"{iv_measured['price_end']:.0f}",
                   f"{iv_measured['price_chg_pct']:+.2f}% over window")
        ip2.metric("ATM IV", f"{iv_measured['iv_end']:.2f}",
                   f"{iv_measured['iv_chg_pct']:+.2f}% over window")
        ip3.metric(f"Skew (ATM ± {iv_lens_thresholds['skew_width']})", lens_skew_txt,
                   "fade confirmed" if iv_lens['fade_confirmed'] else "")
        ip4.metric("Samples in window", f"{iv_measured['samples']}",
                   f"~{iv_measured['span_minutes']:.0f} min")

        # --- Distance to the floors: the difference between "silent" and "broken" ---
        p_floor, iv_floor = iv_measured['price_floor'], iv_measured['iv_floor']
        p_pct_of_floor = min(abs(iv_measured['price_chg_pct']) / p_floor, 1.0) if p_floor else 0
        iv_pct_of_floor = min(abs(iv_measured['iv_chg_pct']) / iv_floor, 1.0) if iv_floor else 0
        floor_pts = p_floor / 100 * iv_measured['price_end']

        fl1, fl2 = st.columns(2)
        with fl1:
            st.caption(f"**Price leg** — {abs(iv_measured['price_chg_pct']):.3f}% of the ±{p_floor:.3f}% "
                       f"floor (±{floor_pts:.0f} pts) {'✅' if p_pct_of_floor >= 1 else '⏳'}")
            st.progress(p_pct_of_floor)
        with fl2:
            st.caption(f"**IV leg** — {abs(iv_measured['iv_chg_pct']):.2f}% of the ±{iv_floor:.2f}% "
                       f"floor {'✅' if iv_pct_of_floor >= 1 else '⏳'}")
            st.progress(iv_pct_of_floor)

        if iv_measured['floor_source'] == 'adaptive':
            st.caption(f"Floors auto-calibrated to the {adaptive_pctile}th percentile of today's own "
                       f"{iv_price_lookback}-min moves ({iv_measured['floor_windows']} completed windows so far).")
        elif iv_measured['floor_source'].startswith('fixed ('):
            st.caption("Auto-calibration is on but still warming up — needs ~6 completed windows. "
                       "Using the fixed floors until then.")

        st.plotly_chart(
            build_iv_price_chart(iv_measured['series'],
                                 iv_measured['window_start'], iv_measured['window_end']),
            use_container_width=True,
        )

        with st.expander("The lens ruleset"):
            st.markdown(
                "| Price | IV | Read | Action |\n|---|---|---|---|\n"
                "| ↓ Falling | ↓ Falling | Shakeout | 🟢 Longable — positioning flushed, not risk repriced |\n"
                "| ↓ Falling | ↑ Rising | Distribution | ⛔ Stand down — **overrides the OI read** |\n"
                "| ↑ Rising | ↑ Rising | Fear bid / squeeze | 🟠 Never chase; negative skew confirms the fade |\n"
                "| ↑ Rising | ↓ Falling | Conviction | 🟢 Controlled accumulation — the smart-money grind |\n"
            )
            st.caption(
                f"ATM IV = mean of CE and PE implied vol across ATM ± {int(iv_price_atm_width)} strike(s), "
                f"zero/blank IVs excluded. Both changes are measured from the start to the end of the "
                f"{iv_price_lookback}-minute window, each end averaged over {iv_measured['edge_n']} sample(s) "
                f"so a single jumpy poll can't flip the verdict. 'Significant' is relative: IV as a % of the IV "
                f"level, price as a % of spot — below those floors the leg counts as flat and the lens stays "
                f"silent rather than picking a quadrant. Spot and IV both come from the same poll, so the two "
                f"changes span exactly the same interval. Skew is the OI-weighted CE_IV − PE_IV across ATM ± "
                f"{iv_lens_thresholds['skew_width']} strikes and is only consulted in the price-up + IV-up "
                f"quadrant. The lens does not feed into the Master Signal, the VWAP Trend Read or the "
                f"Institutional Footprint — it gates them."
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
# INSTITUTIONAL FOOTPRINT SIGNAL (independent of Master Signal, VWAP Trend & IV Lens)
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

        # Threshold sanity check against the session's own distribution. A threshold
        # that every poll clears (or none does) produces a constant tag that looks
        # like a signal but carries no information -- which is exactly what the
        # original 0.6 / 0.2 Vol/OI defaults did on the 14-Aug session.
        _hist = pd.DataFrame(st.session_state.session_log)
        if not _hist.empty and 'Footprint_Vol_OI' in _hist.columns:
            _v = pd.to_numeric(_hist['Footprint_Vol_OI'], errors='coerce').dropna()
            if len(_v) >= 20:
                fresh_hit = (_v >= footprint_thresholds['vol_oi_fresh']).mean() * 100
                fake_hit = (_v < footprint_thresholds['vol_oi_fakeout']).mean() * 100
                warn = " ⚠️ this threshold isn't discriminating — retune it" if (
                    fresh_hit > 95 or fresh_hit < 5) else ""
                st.caption(
                    f"Calibration check ({len(_v)} polls today): Vol/OI ranged {_v.min():.1f}–{_v.max():.1f} "
                    f"(median {_v.median():.1f}). Your 'fresh' threshold fired on {fresh_hit:.0f}% of polls, "
                    f"'fakeout' on {fake_hit:.0f}%.{warn}"
                )

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
