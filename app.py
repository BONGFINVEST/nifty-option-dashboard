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
MOBILE READABILITY FIX (this build): every cell tint in the Buildup Detection and
Institutional Footprint tables now sets an explicit BLACK font colour alongside its
background. Previously the tints set only a background, so the text kept whatever
colour it inherited from the active Streamlit theme -- dark on desktop light mode
(readable), near-white on the mobile app's dark theme (effectively invisible on a
pale green/pink/blue cell). Setting the foreground explicitly makes those columns
render identically in both themes.

DHAN TOKEN FIX (this build): the Dhan access token is now read fresh from Streamlit
Secrets on every API call instead of being cached once at startup. This means that
when you regenerate the token in the Dhan app and paste it into Streamlit Cloud
Secrets, the app picks it up automatically on its very next poll -- no redeploy,
no manual restart, no stale-token error banner hanging around.
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
"vol_oi_fresh": 13.0,        # Vol/OI >= this -> fresh institutional money, regime is "real"
"vol_oi_fakeout": 5.0,       # Vol/OI < this -> just intraday squaring off, ignore the breakout
"trend_flat_band_pct": 0.1,  # spot within +-this% of today's open counts as "sideways", not rising/falling
"chgpcr_min_ce_chg_abs": 300,       # minimum |net CE OI change| (contracts) in the zone before trusting ChgPCR
"chgpcr_min_ce_chg_pct_of_oi": 0.3, # ...OR at least this % of the zone's total OI, whichever floor is higher
}
DEFAULT_IV_LENS_THRESHOLDS = {
"lookback_minutes": 15,      # rolling window over which the two changes are measured
"iv_significant_pct": 1.5,   # |IV change| below this % (relative) counts as "no significant IV move"
"price_significant_pct": 0.10,  # |spot change| below this % counts as "flat"
"min_samples": 4,            # need at least this many logged polls in the window before reading it
"atm_iv_width": 1,           # ATM IV = mean of CE+PE IV across ATM +- N strikes (N=1 -> ATM straddle-ish)
"skew_fade_confirm": 0.0,    # weighted (CE_IV - PE_IV) below this, in the up/up quadrant, confirms the fade
"skew_width": 3,             # ATM +- N strikes for the OI-weighted skew the lens consults
"adaptive_floors": False,
"adaptive_pctile": 70,       # floor = this percentile of today's |move| per window
"adaptive_price_min": 0.02, "adaptive_price_max": 0.40,   # clamps, % of spot
"adaptive_iv_min": 0.30, "adaptive_iv_max": 6.00,         # clamps, % of IV level
}
DEFAULT_BUILDUP_THRESHOLDS = {
"price_min_pct": 2.0,   # |LTP change| below this % counts as flat -> unclassified
"oi_min_pct": 1.0,      # |OI change| below this % of previous OI counts as flat
"width": 10,            # ATM +- N strikes shown in the buildup view
}
DEFAULT_SCENARIO_THRESHOLDS = {
"choi_neutral_band": 5.0,        # |Choi_PE - Choi_CE| below this -> flow is neutral, no trigger
"level_proximity_strikes": 2,    # within N strikes of the wall counts as "at support/resistance"
"pcr_bullish": 1.0,              # PCR above this reads bullish on paper (the Scenario C tension)
}
LOT_SIZE = 65          # NIFTY F&O lot size (revised Jan-2026; was 75). Every rupee figure in
# this app scales off it — verify against the contract master each series.
TRADING_DAYS_YEAR = 252
SESSION_MINUTES = 375  # 09:15–15:30
DEFAULT_GEX_SETTINGS = {
"width": 15,            # ATM ± N strikes included in the GEX profile
"lot_size": LOT_SIZE,
"time_weight": False,   # Dhan's greeks already price in DTE; see compute_gex() docstring
"flip_near_pct": 0.15,  # spot within this % of the flip = "at the flip", regime unstable
}
DEFAULT_DECISION_CARD = {
"cutoff_hour": 15, "cutoff_minute": 0,   # all intraday longs flat by this IST time
"cutoff_warn_minutes": 30,               # start warning this long before the cutoff
"max_losses": 2,                         # stop for the day at this many losing trades
"daily_risk_pct": 1.0,                   # max % of capital at risk across the whole day
"per_trade_risk_pct": 0.5,               # ...and per individual trade
}
DEFAULT_VELOCITY_SETTINGS = {
"width": 5,             # ATM ± N strikes watched for bursts
"burst_pctile": 85,     # a poll is a "burst" above this percentile of today's own velocity
"min_burst_contracts": 15_000,   # ...and only if the absolute flow clears this, so a dead
# tape doesn't manufacture a burst out of its own quietness
"min_elapsed_s": 3.0,   # anything faster than this is a Streamlit rerun, not a new poll
"history_cap": 720,     # ~2 hours of 10s polls kept in memory for the percentile
}
DEFAULT_EM_SETTINGS = {
"straddle_sd_factor": 0.80,   # 1SD move to expiry ≈ 0.8 × ATM straddle (standard approximation)
"range_spent_high": 100.0,    # day range ≥ this % of the expected range -> move largely spent
"range_spent_low": 40.0,      # day range ≤ this % -> room left, breakouts still have runway
"drive_lens_floor": False,    # opt-in: let the straddle set the IV Lens price floor
}
DEFAULT_TRACKER_SETTINGS = {
"horizon_minutes": 15,   # forward window over which a signal is graded
"target_pts": 20,        # forward move (in the signal's own direction) that counts as a win
"min_samples": 5,        # below this a hit rate is noise, and is labelled as such
}
DEFAULT_TERM_SETTINGS = {
"throttle_seconds": 60,      # next-expiry chain is a second API call — don't run it every 10s poll
"backwardation_pts": 0.5,    # front IV exceeding next IV by this = event premium in the front
"divergence_ratio": 0.4,     # front IV moves, next expiry moves less than this fraction of it
# -> expiry-specific noise, not a vol regime change
}
DEFAULT_RISK_SETTINGS = {
"atr_period": 14,
"atr_stop_mult": 1.5,        # stop distance = this × ATR (on the chart's candle interval)
"structural_cap_mult": 2.0,  # ignore an OI-wall stop further than this × the ATR stop —
# past that the wall is a target, not a stop, and using it
# sizes the position down to nothing
"reward_multiple": 2.0,      # target = this × the stop distance
"capital": 500_000,          # one NIFTY lot is 75 × spot in notional; at ₹2L, 1% risk
# cannot fund a single lot on any realistic stop, so the
# default is set where the sizing math produces a real answer
"risk_pct": 0.5,             # % of capital risked per trade — deliberately matched to
# DEFAULT_DECISION_CARD['per_trade_risk_pct'] so the envelope's
# lot count and the card's cap agree out of the box. Raise one
# without the other and the card flags the mismatch.
"max_premium_pct": 25.0,     # cap total premium outlay at this % of capital
}
SNAPSHOT_DIR = Path("nifty_oi_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)
LOG_DIR = Path("nifty_session_logs")
LOG_DIR.mkdir(exist_ok=True)
GSHEET_SNAPSHOT_SHEET = "closing_snapshot"
GSHEET_LOG_SHEET = "session_log"
def save_chain_snapshot(df: pd.DataFrame, fetched_at: datetime, expiry: str,
spot: float = None):
    try:
        out = df.copy()
        out['_fetched_at'] = fetched_at.isoformat()
        out['_expiry'] = expiry
        out['_spot'] = float(spot) if spot else np.nan
        out.to_csv(SNAPSHOT_DIR / f"{fetched_at.strftime('%Y-%m-%d')}.csv", index=False)
    except Exception as e:
        st.sidebar.caption(f"⚠️ Chain snapshot save failed: {e}")
def append_log_row(row: dict, date_str: str):
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
def load_loss_counter(date_str: str) -> int:
    try:
        path = LOG_DIR / f"discipline_{date_str}.json"
        if path.exists():
            import json as _json
            return int(_json.loads(path.read_text()).get('losses', 0))
    except Exception:
        pass
    return 0
def save_loss_counter(date_str: str, losses: int):
    try:
        import json as _json
        (LOG_DIR / f"discipline_{date_str}.json").write_text(
            _json.dumps({'losses': int(losses), 'updated': datetime.now(IST).isoformat()}))
    except Exception as e:
        st.sidebar.caption(f"⚠️ Loss counter save failed: {e}")
def load_all_logs(max_days: int = 30) -> pd.DataFrame:
    frames = []
    try:
        for path in sorted(LOG_DIR.glob("*.csv"))[-max_days:]:
            try:
                d = pd.read_csv(path)
                if d.empty or 'Time' not in d.columns:
                    continue
                d['Date'] = path.stem
                frames.append(d)
            except Exception:
                continue
    except Exception:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
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
def save_chain_snapshot_to_gsheet(df: pd.DataFrame, fetched_at: datetime, expiry: str,
spot: float = None):
    if not gsheets_configured():
        return
    try:
        from gspread_dataframe import set_with_dataframe
        ws = get_gsheet_worksheet(GSHEET_SNAPSHOT_SHEET)
        out = df.copy()
        out['_fetched_at'] = fetched_at.isoformat()
        out['_expiry'] = expiry
        out['_spot'] = float(spot) if spot else np.nan
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
# CREDENTIALS  (DHAN TOKEN FIX)
# ==========================================
# The old code read the token ONCE at startup and cached it in DHAN_HEADERS.
# That meant a regenerated token pasted into Streamlit Cloud Secrets was
# invisible until the app was redeployed or the container restarted -- which
# is why the "token expired" banner kept hanging around.
#
# The fix: read the token fresh from st.secrets on EVERY API call. Streamlit
# re-reads secrets on each rerun, so a token pasted into Secrets takes effect
# on the very next 10-second poll. No redeploy, no restart, no stale banner.
if 'DHAN_CLIENT_ID' not in st.secrets or 'DHAN_ACCESS_TOKEN' not in st.secrets:
    st.error("❌ Dhan API credentials not found in Streamlit Secrets!")
    st.info("Please add `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` to your Streamlit Secrets.")
    st.stop()

def get_dhan_headers():
    """Build the Dhan API headers dict, reading the access token fresh from
    Streamlit Secrets on every call. This is the fix for the daily-token-
    expiry workflow: when you regenerate the token in the Dhan app and paste
    it into Streamlit Cloud Secrets, the app picks it up on its next poll
    without any redeploy or restart.
    Returns None if the credentials are missing (the caller should treat this
    as an auth failure rather than crashing)."""
    if 'DHAN_CLIENT_ID' not in st.secrets or 'DHAN_ACCESS_TOKEN' not in st.secrets:
        return None
    return {
        "client-id": st.secrets['DHAN_CLIENT_ID'],
        "access-token": st.secrets['DHAN_ACCESS_TOKEN'],
        "Accept": "application/json",
        "Content-Type": "application/json",
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
        headers = get_dhan_headers()
        if not headers:
            return None, "Dhan API credentials not configured in Streamlit Secrets."
        r = requests.post("https://api.dhan.co/v2/optionchain/expirylist", headers=headers,
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
        headers = get_dhan_headers()
        if not headers:
            return None, None, "Dhan API credentials not configured in Streamlit Secrets."
        r = requests.post("https://api.dhan.co/v2/optionchain", headers=headers,
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
            "1. Dhan app/web → **My Profile → DhanHQ Trading APIs → Generate Token**\n\n"
            "2. Copy the new token\n\n"
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
        headers = get_dhan_headers()
        if not headers:
            return None
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        payload = {
            "securityId": str(NIFTY_SCRIP), "exchangeSegment": "IDX_I", "instrument": "INDEX",
            "interval": interval, "oi": False,
            "fromDate": f"{today_str} 09:15:00", "toDate": f"{today_str} 23:59:59",
        }
        r = requests.post("https://api.dhan.co/v2/charts/intraday", headers=headers, json=payload, timeout=15)
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
def recover_spot(df: pd.DataFrame):
    """Spot for a cached / closed-market chain, in order of trustworthiness.
    1. the value persisted alongside the snapshot ('_spot')
    2. put-call parity: at the strike where |CE_LTP - PE_LTP| is smallest,
    spot ~= K + CE_LTP - PE_LTP  (rates/carry ignored -- immaterial intraday)
    Returns (spot, source). Deliberately never falls back to the median strike:
    a median strike describes the chain's WIDTH, not price, and silently
    substituting it is what corrupted every vol-surface read on cached polls.
    """
    if '_spot' in df.columns:
        v = pd.to_numeric(df['_spot'], errors='coerce').dropna()
        if len(v) and np.isfinite(v.iloc[0]) and v.iloc[0] > 0:
            return float(v.iloc[0]), 'snapshot'
    try:
        d = df.dropna(subset=['CE_LTP', 'PE_LTP']).copy()
        d = d[(d['CE_LTP'] > 0) & (d['PE_LTP'] > 0)]
        if not d.empty:
            row = d.loc[(d['CE_LTP'] - d['PE_LTP']).abs().idxmin()]
            synth = float(row['Strike'] + row['CE_LTP'] - row['PE_LTP'])
            if np.isfinite(synth) and synth > 0:
                return synth, 'parity'
    except Exception:
        pass
    return None, None
def chain_is_coherent(df: pd.DataFrame, atm: float, spot: float):
    """Guard rail on the CE/PE alignment. Returns (ok, [problems]).
    Two assertions:
    * ATM must sit within one strike interval of spot
    * the ATM put must not trade below intrinsic
    Either failure means the CE and PE legs are being read off different strikes,
    which silently corrupts IV skew, ChgPCR, the Expected Move envelope and the
    straddle breakevens. When it trips, the caller blanks those panels rather than
    publishing them -- a dark panel is safe, a confident wrong number is not.
    """
    problems = []
    if spot is None or atm is None:
        return False, ["no usable spot -- vol-surface reads suppressed"]
    if abs(atm - spot) > STRIKE_STEP:
        problems.append(f"ATM {atm:.0f} is {abs(atm - spot):.0f} pts from spot "
                        f"{spot:.0f} (max {STRIKE_STEP:.0f})")
    row = df[df['Strike'] == atm]
    if not row.empty:
        pe = row.iloc[0].get('PE_LTP')
        ce = row.iloc[0].get('CE_LTP')
        pe_intrinsic = max(0.0, atm - spot)
        ce_intrinsic = max(0.0, spot - atm)
        if pd.notna(pe) and pe < pe_intrinsic - 1.0:
            problems.append(f"ATM PE {pe:.1f} is below intrinsic {pe_intrinsic:.1f}")
        if pd.notna(ce) and ce < ce_intrinsic - 1.0:
            problems.append(f"ATM CE {ce:.1f} is below intrinsic {ce_intrinsic:.1f}")
    return (len(problems) == 0), problems
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
    zone = df[(df['Strike'] >= atm - width * STRIKE_STEP) & (df['Strike'] <= atm + width * STRIKE_STEP)]
    if zone.empty:
        return np.nan
    ivs = pd.to_numeric(pd.concat([zone['CE_IV'], zone['PE_IV']], ignore_index=True), errors='coerce')
    ivs = ivs[ivs > 0]
    return float(ivs.mean()) if len(ivs) else np.nan
def compute_lens_skew(df: pd.DataFrame, atm: float, width: int = 3):
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
    return float(np.mean(values[:edge_n])), float(np.mean(values[-edge_n:]))
def _session_move_floor(ts, values, lookback_minutes: int, pctile: float,
                        lo: float, hi: float, min_windows: int = 6):
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
    if not measured or not measured.get('ready'):
        return None
    p, v = measured['price_dir'], measured['iv_dir']
    skew_txt = f"{iv_skew:+.2f}" if pd.notna(iv_skew) else "n/a"
    notes = []
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
def evaluate_confluence_scenario(iv_lens, choi_ce, choi_pe, pcr, spot,
                                 support_strike, resistance_strike, t: dict,
                                 distribution_hard_stop: bool = False):
    if iv_lens is None:
        return None
    stance = iv_lens['stance']
    lens_bias = {'shakeout': 'bullish', 'accumulation': 'bullish',
                 'distribution': 'bearish', 'fear_bid': 'bearish'}.get(stance)
    diff = choi_pe - choi_ce
    band = t['choi_neutral_band']
    flow_bias = 'bullish' if diff > band else ('bearish' if diff < -band else 'neutral')
    flow_txt = f"Choi_PE {choi_pe:.1f}% vs Choi_CE {choi_ce:.1f}% (Δ {diff:+.1f})"
    prox = t['level_proximity_strikes'] * STRIKE_STEP
    at_support = bool(spot and support_strike and abs(spot - support_strike) <= prox)
    at_resistance = bool(spot and resistance_strike and abs(spot - resistance_strike) <= prox)
    checks, warnings = [], []
    checks.append(("IV Lens environment",
                   iv_lens['headline'].split(" — ")[0] if lens_bias else "no read",
                   lens_bias is not None))
    checks.append(("Choi flow trigger",
                   {'bullish': 'Put writers defending', 'bearish': 'Call writers attacking',
                    'neutral': 'no clear side'}[flow_bias] + f" — {flow_txt}",
                   flow_bias != 'neutral'))
    if lens_bias is None:
        scen, side, color = "WAIT", None, "#6c757d"
        headline = "⏸️ WAIT — no environment read"
        action = "No trade. The IV Lens is silent, so there's nothing to confirm."
        warnings.append("The lens is the environment half of this setup. Without it, Choi flow alone "
                        "is a trigger with nothing to trigger against.")
    elif distribution_hard_stop and stance == 'distribution':
        scen, side, color = "C", None, "#c82333"
        headline = "⛔ SCENARIO C — STAND DOWN (distribution)"
        action = "No trade. Distribution is set as an absolute stand-down."
        warnings.append("Price falling into rising IV. The hard-stop toggle is on, so this blocks shorts "
                        "as well as longs — switch it off in the sidebar to allow a flow-confirmed PE buy here.")
    elif flow_bias == 'neutral':
        scen, side, color = "WAIT", None, "#6c757d"
        headline = "⏸️ WAIT — environment set, flow not confirming"
        action = f"No trade yet. Environment is {lens_bias}, but Choi is inside the ±{band:.0f} neutral band."
        warnings.append("This is the half-setup: the environment is in place but nobody has committed yet. "
                        "Watch for Choi to separate.")
    elif lens_bias != flow_bias:
        scen, side, color = "C", None, "#c82333"
        headline = "⛔ SCENARIO C — STAY AWAY (lens vs flow conflict)"
        action = "DO NOT TRADE. The environment and the live flow are pointing opposite ways."
        warnings.append(f"IV Lens reads **{lens_bias}** while Choi flow reads **{flow_bias}**. This is the "
                        f"exact trap the scenario is built to catch — one of the two is wrong and there's no "
                        f"way to know which in advance.")
    elif lens_bias == 'bullish':
        scen, side, color = "A", "CE", "#1e7e34"
        headline = "🟢 SCENARIO A — CE BUY (long)"
        action = "Execute the CE Buy. Environment favourable, flow confirming."
    else:
        scen, side, color = "B", "PE", "#c82333"
        headline = "🔴 SCENARIO B — PE BUY (short)"
        action = "Execute the PE Buy. Environment fearful, flow confirming the breakdown."
    if scen in ("A", "B"):
        if stance == 'shakeout':
            checks.append(("At support (required for Shakeout)",
                           f"put floor {support_strike:.0f}" if support_strike else "no put floor found",
                           at_support))
            if not at_support:
                warnings.append("Shakeout is only longable **at support** — spot isn't near the put floor, "
                                "so this is a weaker version of Scenario A.")
        if stance == 'fear_bid':
            checks.append(("At resistance (required for Fear Bid)",
                           f"call wall {resistance_strike:.0f}" if resistance_strike else "no call wall found",
                           at_resistance))
            if not at_resistance:
                warnings.append("Fear Bid is only shortable **at resistance** — spot isn't near the call wall, "
                                "so this is a weaker version of Scenario B.")
            if stance == 'fear_bid' and not iv_lens['fade_confirmed']:
                warnings.append("Skew hasn't flipped negative, so the squeeze fade isn't confirmed. The short is "
                                "the earlier, riskier version of this setup.")
        pcr_agrees = (pcr > t['pcr_bullish']) if side == 'CE' else (pcr <= t['pcr_bullish'])
        checks.append(("Standing OI (PCR) agrees", f"PCR {pcr:.2f}" if pd.notna(pcr) else "n/a", bool(pcr_agrees)))
        if not pcr_agrees:
            warnings.append(f"PCR {pcr:.2f} points the other way. That's yesterday's standing OI against today's "
                            f"live IV and flow — it doesn't block the trade, but it's the Scenario C tension "
                            f"showing up, so size down.")
    met = sum(1 for _, _, ok in checks if ok)
    return {
        'scenario': scen, 'side': side, 'headline': headline, 'action': action, 'color': color,
        'lens_bias': lens_bias, 'flow_bias': flow_bias, 'checks': checks, 'warnings': warnings,
        'conviction': met, 'conviction_total': len(checks),
        'at_support': at_support, 'at_resistance': at_resistance,
        'support_strike': support_strike, 'resistance_strike': resistance_strike,
    }
def build_iv_price_chart(series_df: pd.DataFrame, window_start=None, window_end=None):
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
# BUILDUP DETECTION (new — Option Chain visual: long/short buildup,
# short covering, long unwinding, per option leg)
# ==========================================
BUILDUP_STYLES = {
    "Long Buildup":    {"label": "▲ Long Buildup",    "pattern": ""},
    "Short Buildup":   {"label": "▼ Short Buildup",   "pattern": "/"},
    "Short Covering":  {"label": "↺ Short Covering",  "pattern": "x"},
    "Long Unwinding":  {"label": "↘ Long Unwinding",  "pattern": "."},
    "Flat":            {"label": "· Flat",            "pattern": ""},
    "No data":         {"label": "— No data",         "pattern": ""},
}
BUILDUP_BIAS_COLOR = {"bullish": "#28a745", "bearish": "#dc3545", "": "#adb5bd"}
BUILDUP_TEXT_COLOR = "#000000"
BUILDUP_BIAS_TINT = {
    "bullish": f"background-color: #d4edda; color: {BUILDUP_TEXT_COLOR}; font-weight: 600",
    "bearish": f"background-color: #f8d7da; color: {BUILDUP_TEXT_COLOR}; font-weight: 600",
    "": "",
}
BUILDUP_BIAS = {
    'CE': {"Long Buildup": "bullish", "Short Buildup": "bearish",
           "Short Covering": "bullish", "Long Unwinding": "bearish"},
    'PE': {"Long Buildup": "bearish", "Short Buildup": "bullish",
           "Short Covering": "bearish", "Long Unwinding": "bullish"},
}
def classify_buildup(price_chg_pct, oi_chg_pct, t: dict) -> str:
    if price_chg_pct is None or oi_chg_pct is None or pd.isna(price_chg_pct) or pd.isna(oi_chg_pct):
        return "No data"
    if abs(price_chg_pct) < t['price_min_pct'] or abs(oi_chg_pct) < t['oi_min_pct']:
        return "Flat"
    if price_chg_pct > 0 and oi_chg_pct > 0:
        return "Long Buildup"
    if price_chg_pct < 0 and oi_chg_pct > 0:
        return "Short Buildup"
    if price_chg_pct > 0 and oi_chg_pct < 0:
        return "Short Covering"
    return "Long Unwinding"
def compute_buildup_table(df: pd.DataFrame, atm: float, width: int, t: dict) -> pd.DataFrame:
    z = df[(df['Strike'] >= atm - width * STRIKE_STEP) &
           (df['Strike'] <= atm + width * STRIKE_STEP)].copy()
    if z.empty:
        return z
    for leg in ('CE', 'PE'):
        prev_ltp = pd.to_numeric(z.get(f'{leg}_prevClose'), errors='coerce')
        prev_oi = pd.to_numeric(z.get(f'{leg}_OI_prev'), errors='coerce')
        ltp = pd.to_numeric(z[f'{leg}_LTP'], errors='coerce')
        oi_chg = pd.to_numeric(z[f'{leg}_OI_chg'], errors='coerce')
        z[f'{leg}_LTP_chg_pct'] = np.where(prev_ltp > 0, (ltp - prev_ltp) / prev_ltp * 100, np.nan)
        z[f'{leg}_OI_chg_pct'] = np.where(prev_oi > 0, oi_chg / prev_oi * 100, np.nan)
        z[f'{leg}_Buildup'] = [
            classify_buildup(p, o, t)
            for p, o in zip(z[f'{leg}_LTP_chg_pct'], z[f'{leg}_OI_chg_pct'])
        ]
        z[f'{leg}_Bias'] = [BUILDUP_BIAS[leg].get(b, "") for b in z[f'{leg}_Buildup']]
    return z.reset_index(drop=True)
def summarize_buildup(bt: pd.DataFrame, spot):
    if bt.empty:
        return None
    rows = []
    for leg in ('CE', 'PE'):
        for _, r in bt.iterrows():
            b = r[f'{leg}_Buildup']
            if b in ("Flat", "No data"):
                continue
            rows.append({'leg': leg, 'strike': r['Strike'], 'buildup': b,
                         'bias': r[f'{leg}_Bias'], 'weight': abs(r[f'{leg}_OI_chg'])})
    if not rows:
        return None
    a = pd.DataFrame(rows)
    bull = a.loc[a['bias'] == 'bullish', 'weight'].sum()
    bear = a.loc[a['bias'] == 'bearish', 'weight'].sum()
    total = bull + bear
    net_pct = ((bull - bear) / total * 100) if total else 0.0
    counts = a.groupby(['leg', 'buildup'])['weight'].sum().unstack(fill_value=0)
    top = a.loc[a['weight'].idxmax()]
    side = ("above spot" if spot and top['strike'] > spot else
            "below spot" if spot and top['strike'] < spot else "at spot")
    if net_pct > 20:
        verdict, color = "🟢 Net BULLISH buildup", "#1e7e34"
    elif net_pct < -20:
        verdict, color = "🔴 Net BEARISH buildup", "#c82333"
    else:
        verdict, color = "⚪ Mixed / two-way buildup", "#6c757d"
    matrix = {}
    for leg in ('CE', 'PE'):
        for lab in ("Long Buildup", "Short Buildup", "Short Covering", "Long Unwinding"):
            sub = a[(a['leg'] == leg) & (a['buildup'] == lab)]
            matrix[(leg, lab)] = {
                'weight': float(sub['weight'].sum()),
                'strikes': int(len(sub)),
                'bias': BUILDUP_BIAS[leg][lab],
                'top_strike': float(sub.loc[sub['weight'].idxmax(), 'strike']) if not sub.empty else None,
            }
    return {'bull_weight': bull, 'bear_weight': bear, 'net_pct': net_pct,
            'verdict': verdict, 'color': color, 'counts': counts, 'detail': a,
            'matrix': matrix,
            'top_leg': top['leg'], 'top_strike': top['strike'],
            'top_buildup': top['buildup'], 'top_weight': top['weight'], 'top_side': side}
# ==========================================
# ZONE A — PCR REGIME CLASSIFICATION (Analysis!A2:E16)
# ==========================================
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
    ce_vol_imbalance = ce_vol_pct - pe_vol_pct
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
# SIGNAL PROGRESS
# ==========================================
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
# ACTION TAG — 'Dash Board'!L3
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
# MAX PAIN
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
# MODULE 1 — GAMMA (GEX) & DELTA (DEX) EXPOSURE, DEALER GAMMA FLIP LEVEL
# ==========================================
def compute_gex(df: pd.DataFrame, spot: float, dte, settings: dict):
    if not spot or df is None or df.empty:
        return None
    width = int(settings.get('width', 15))
    lot = float(settings.get('lot_size', LOT_SIZE))
    d = df[(df['Strike'] >= spot - width * STRIKE_STEP) &
           (df['Strike'] <= spot + width * STRIKE_STEP)].copy()
    for c in ('CE_Gamma', 'PE_Gamma', 'CE_Delta', 'PE_Delta', 'CE_OI', 'PE_OI'):
        d[c] = pd.to_numeric(d.get(c), errors='coerce').fillna(0.0)
    d = d[(d['CE_OI'] > 0) | (d['PE_OI'] > 0)]
    if d.empty or (d['CE_Gamma'].abs().sum() + d['PE_Gamma'].abs().sum()) == 0:
        return None
    tw = 1.0
    if settings.get('time_weight'):
        tw = 1.0 / max(np.sqrt(max(dte if dte is not None else 1, 0.1)), 0.3)
    unit = spot * spot * 0.01 * lot / 1e7
    d['CE_GEX'] = -d['CE_Gamma'] * d['CE_OI'] * unit * tw
    d['PE_GEX'] = d['PE_Gamma'] * d['PE_OI'] * unit * tw
    d['GEX'] = d['CE_GEX'] + d['PE_GEX']
    d['DEX'] = (-d['CE_Delta'] * d['CE_OI'] - d['PE_Delta'] * d['PE_OI']) * lot / 1e5
    d = d.sort_values('Strike').reset_index(drop=True)
    d['cum_GEX'] = d['GEX'].cumsum()
    net_gex = float(d['GEX'].sum())
    net_dex = float(d['DEX'].sum())
    def _find_crossings(strikes, cumvals):
        out = []
        for i in range(1, len(cumvals)):
            a, b = cumvals[i - 1], cumvals[i]
            if (a < 0 <= b) or (a > 0 >= b):
                x = (strikes[i] if b == a else
                     strikes[i - 1] + (strikes[i] - strikes[i - 1]) * (0 - a) / (b - a))
                out.append(float(x))
        return out
    ks, cum = d['Strike'].values, d['cum_GEX'].values
    crossings = _find_crossings(ks, cum)
    flip_search_width = width
    if not crossings:
        wide = df.copy()
        for c in ('CE_Gamma', 'PE_Gamma', 'CE_OI', 'PE_OI'):
            wide[c] = pd.to_numeric(wide.get(c), errors='coerce').fillna(0.0)
        wide = wide[(wide['CE_OI'] > 0) | (wide['PE_OI'] > 0)].sort_values('Strike')
        if not wide.empty:
            w_gex = (-wide['CE_Gamma'] * wide['CE_OI'] + wide['PE_Gamma'] * wide['PE_OI']) * unit * tw
            w_cum = w_gex.cumsum().values
            crossings = _find_crossings(wide['Strike'].values, w_cum)
            if crossings:
                flip_search_width = int(len(wide) // 2)
    flip = min(crossings, key=lambda k: abs(k - spot)) if crossings else None
    dist = (spot - flip) if flip else None
    near_pct = float(settings.get('flip_near_pct', 0.15))
    at_flip = bool(flip and abs(dist) / spot * 100 <= near_pct)
    _i_near = int(np.argmin(np.abs(cum)))
    nearest_approach = float(ks[_i_near])
    nearest_approach_val = float(cum[_i_near])
    gross = float(d['GEX'].abs().sum())
    regime_margin = (net_gex / gross) if gross > 0 else 0.0
    _pit_strike = float(d.loc[d['GEX'].idxmin(), 'Strike']) if d['GEX'].min() < 0 else None
    pit_distance = (_pit_strike - spot) if _pit_strike is not None else None
    peak_pos = d.loc[d['GEX'].idxmax()] if d['GEX'].max() > 0 else None
    peak_neg = d.loc[d['GEX'].idxmin()] if d['GEX'].min() < 0 else None
    return {
        'per_strike': d[['Strike', 'CE_GEX', 'PE_GEX', 'GEX', 'cum_GEX', 'DEX']],
        'net_gex': net_gex, 'net_dex': net_dex,
        'flip_level': flip, 'flip_distance': dist, 'at_flip': at_flip,
        'flip_search_width': flip_search_width,
        'flip_beyond_band': bool(flip is None),
        'nearest_approach': nearest_approach,
        'nearest_approach_val': nearest_approach_val,
        'regime_margin': regime_margin,
        'pit_distance': pit_distance,
        'regime': 'Pinning (moves get faded)' if net_gex > 0 else 'Trending (moves get amplified)',
        'regime_key': 'pin' if net_gex > 0 else 'trend',
        'gamma_wall': float(peak_pos['Strike']) if peak_pos is not None else None,
        'gamma_wall_val': float(peak_pos['GEX']) if peak_pos is not None else None,
        'gamma_pit': float(peak_neg['Strike']) if peak_neg is not None else None,
        'gamma_pit_val': float(peak_neg['GEX']) if peak_neg is not None else None,
        'time_weighted': bool(settings.get('time_weight')),
        'width': width,
    }

def gex_breakout_discount(gex, spot) -> tuple:
    if gex is None:
        return None, None, None
    if gex['at_flip']:
        return ("⚖️ Spot is sitting ON the gamma flip", "#fd7e14",
                "Regime is unstable here — the tape can switch between fading and chasing on a "
                "20-point move. This is the worst place to size up; wait for spot to pick a side "
                "of the flip.")
    if gex['regime_key'] == 'trend':
        return ("🚀 Short gamma — breakouts have follow-through", "#1e7e34",
                "Dealers are hedging WITH the move. Wall unwinds, VWAP streaks and Scenario A/B "
                "breakouts are worth taking at full size. Stops need room: this is the regime that "
                "produces the fast extended trends, and the fake-looking overshoots are real.")
    return ("🧲 Long gamma — moves get faded", "#c82333",
            "Dealer hedging leans against the move, so rallies get sold and dips get bought back "
            "toward the high-OI strikes. Discount every breakout signal in this app today; "
            "range-fade setups toward Max Pain are what the mechanics support.")

def gex_flip_zone_pts(gex, spot, settings: dict = None):
    try:
        pct = float((settings or DEFAULT_GEX_SETTINGS).get('flip_near_pct', 0.15))
        return float(spot) * pct / 100.0
    except Exception:
        return 0.0

def build_gex_decision(gex, spot, mp, vwap_val, walls, now_ist, risk_settings,
                       card: dict, losses_today: int, breakout_signal_active: bool,
                       gex_settings: dict = None):
    minutes_to_cutoff = None
    if now_ist is not None:
        try:
            cutoff = now_ist.replace(hour=int(card.get('cutoff_hour', 15)),
                                     minute=int(card.get('cutoff_minute', 0)),
                                     second=0, microsecond=0)
            minutes_to_cutoff = (cutoff - now_ist).total_seconds() / 60.0
        except Exception:
            minutes_to_cutoff = None
    if gex is None:
        mode = {'key': 'unavailable', 'title': 'Regime unavailable — no live gamma',
                'stance': "Step 1 needs a live spot and non-zero greeks. Dhan returns zero greeks "
                          "outside market hours, so this resolves on the first live poll of the "
                          "session. Steps 2 and 3 below are still valid for preparation.",
                'instrument': '—', 'size': 'No position', 'color': '#6c757d'}
    elif gex['at_flip']:
        mode = {'key': 'flip', 'title': 'ON THE FLIP — no position',
                'stance': "Spot is inside the flip zone, where the tape can switch between fading and "
                          "chasing on a 20-point move. Wait for a break AND hold of the flip level, "
                          "then trade the regime it settles into — not the one it just left.",
                'instrument': 'Nothing until the flip breaks and holds',
                'size': 'MINIMUM SIZE once it does', 'color': '#fd7e14'}
    elif gex['net_gex'] < 0:
        mode = {'key': 'buyer', 'title': 'BUYER MODE — short gamma',
                'stance': "Dealer hedging runs with the move, so broken walls accelerate rather than "
                          "revert. Trade wall breaks and VWAP streaks in the direction of the break. "
                          "This is the regime that produces extended trends, and the overshoots that "
                          "look unsustainable are real.",
                'instrument': 'ATM / ITM calls or puts (long premium)',
                'size': 'FULL SIZE', 'color': '#1e7e34'}
    else:
        mode = {'key': 'seller', 'title': 'SELLER / FADER MODE — long gamma',
                'stance': "Dealer hedging leans against the move. Fade the walls, expect the Max Pain "
                          "magnet to hold, and treat every breakout signal elsewhere in this app as a "
                          "trap until proven otherwise.",
                'instrument': 'Defined-risk spreads (credit spreads / iron condors)',
                'size': 'Defined risk only — no naked long premium', 'color': '#c82333'}
    levels = []
    def _add(label, price, note):
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        if not np.isfinite(price):
            return
        levels.append({'Level': label, 'Price': price,
                       'Distance': (price - spot) if spot else np.nan, 'Role': note})
    if gex:
        _add("γ Flip", gex.get('flip_level'), "Regime boundary — the whole card re-reads on the other side of this")
        _add("γ Wall (max +GEX)", gex.get('gamma_wall'), "Strongest pin / magnet strike")
        _add("γ Pit (max −GEX)", gex.get('gamma_pit'), "Acceleration zone — moves speed up here")
    if walls:
        _add("Call wall (max CE OI)", walls.get('max_ce_strike'), "Resistance — writers defending. Break = upside acceleration in buyer mode")
        _add("Put wall (max PE OI)", walls.get('max_pe_strike'), "Support — put writers defending. Break = downside acceleration in buyer mode")
        _add("Max Pain", mp, "Where the option book wants price to expire")
        _add("VWAP", vwap_val, "Session mean — the streak reference")
        _add("SPOT", spot, "◀ you are here")
    ladder = (pd.DataFrame(levels).sort_values('Price', ascending=False).reset_index(drop=True)
              if levels else pd.DataFrame())
    rules = []
    if mode['key'] == 'seller' and breakout_signal_active:
        rules.append(('alert', "Long gamma + breakout signal = DISCOUNT IT",
                      "A directional breakout signal is live right now while dealers are long "
                      "gamma. This is the trap case the rule exists for — the mechanics say it gets "
                      "faded back toward the high-OI strikes."))
    elif mode['key'] == 'seller':
        rules.append(('watch', "Long gamma + breakout signal = DISCOUNT IT",
                      "In force. No breakout signal firing at the moment; if one appears, it does "
                      "not override the regime."))
    else:
        rules.append(('ok', "Long gamma + breakout signal = DISCOUNT IT", "Not applicable in this regime."))
    broke_up = bool(spot and walls and walls.get('max_ce_strike') and spot > float(walls['max_ce_strike']))
    broke_dn = bool(spot and walls and walls.get('max_pe_strike') and spot < float(walls['max_pe_strike']))
    if mode['key'] == 'buyer' and (broke_up or broke_dn):
        rules.append(('alert', "Short gamma + wall break = ACCELERATION",
                      f"Spot is {'above the call wall' if broke_up else 'below the put wall'} in a "
                      f"short-gamma regime. This is the ride-it case: dealer hedging is now feeding "
                      f"the move rather than absorbing it."))
    elif mode['key'] == 'buyer':
        rules.append(('watch', "Short gamma + wall break = ACCELERATION",
                      "Armed. Spot is still inside the walls — this triggers on the break, not in "
                      "anticipation of it."))
    else:
        rules.append(('ok', "Short gamma + wall break = ACCELERATION", "Not applicable in this regime."))
    if gex and gex['at_flip']:
        rules.append(('alert', "Stop trading the read inside the flip zone",
                      "Spot has re-entered the flip zone. The regime read this card is built on is "
                      "no longer stable — flatten or stop adding until it resolves to one side."))
    elif gex and gex.get('flip_level') and spot:
        zone = gex_flip_zone_pts(gex, spot, gex_settings)
        rules.append(('watch', "Stop trading the read inside the flip zone",
                      f"Clear of the flip by {abs(gex['flip_distance']):.0f} pts. The zone is "
                      f"±{zone:.0f} pts around {gex['flip_level']:.0f}."))
    else:
        _margin = gex.get('regime_margin') if gex else None
        _near = gex.get('nearest_approach') if gex else None
        _pitd = gex.get('pit_distance') if gex else None
        _bits = []
        if _margin is not None:
            _decisive = ("decisive" if abs(_margin) > 0.5 else
                         "moderate" if abs(_margin) > 0.2 else "MARGINAL — one large trade could flip it")
            _bits.append(f"regime margin {_margin:+.2f} ({_decisive})")
        if _near is not None and spot:
            _bits.append(f"closest approach to a flip is {_near:.0f} "
                         f"({_near - float(spot):+.0f} pts)")
        if _pitd is not None:
            _bits.append(f"acceleration pocket {_pitd:+.0f} pts away — treat it as the "
                         f"hard invalidation on any fade")
        rules.append(('ok', "Stop trading the read inside the flip zone",
                      "No zero-crossing in the chain, so the whole band is one-signed: "
                      + "; ".join(_bits) + "." if _bits else
                      "No zero-crossing in the chain — the whole band is one-signed."))
    cutoff_label = f"{int(card.get('cutoff_hour', 15)):02d}:{int(card.get('cutoff_minute', 0)):02d}"
    warn_min = float(card.get('cutoff_warn_minutes', 30))
    if minutes_to_cutoff is None:
        rules.append(('ok', f"All intraday longs closed by {cutoff_label}", "Clock unavailable."))
    elif minutes_to_cutoff <= 0:
        rules.append(('alert', f"All intraday longs closed by {cutoff_label}",
                      f"Past the cutoff by {abs(minutes_to_cutoff):.0f} min. Intraday longs should "
                      f"already be flat — theta and the closing auction are both working against "
                      f"holding now."))
    elif minutes_to_cutoff <= warn_min:
        rules.append(('watch', f"All intraday longs closed by {cutoff_label}",
                      f"{minutes_to_cutoff:.0f} minutes to the cutoff. Stop opening new intraday "
                      f"longs and start working out of what's open."))
    else:
        rules.append(('ok', f"All intraday longs closed by {cutoff_label}",
                      f"{minutes_to_cutoff:.0f} minutes until the cutoff."))
    max_losses = int(card.get('max_losses', 2))
    if losses_today >= max_losses:
        rules.append(('alert', f"Max {max_losses} losses per day",
                      f"{losses_today} logged — done for the day. Two losses in one session usually "
                      f"means the regime read is wrong, not that the next trade is due, and the "
                      f"trade taken to get even is the one that does the damage."))
    elif losses_today == max_losses - 1:
        rules.append(('watch', f"Max {max_losses} losses per day",
                      f"{losses_today} logged. One more and the session is over."))
    else:
        rules.append(('ok', f"Max {max_losses} losses per day", f"{losses_today} of {max_losses} used."))
    capital = float(risk_settings.get('capital', 0) or 0)
    daily_pct = float(card.get('daily_risk_pct', 1.0))
    trade_pct = float(card.get('per_trade_risk_pct', 0.5))
    daily_budget = capital * daily_pct / 100.0
    per_trade_cap = capital * trade_pct / 100.0
    spent = min(losses_today, max_losses) * per_trade_cap
    remaining = max(daily_budget - spent, 0.0)
    envelope_pct = float(risk_settings.get('risk_pct', 0) or 0)
    conflict_note = None
    if envelope_pct > trade_pct + 1e-9 and trade_pct > 0:
        conflict_note = (
            f"The Risk Envelope panel is sizing at **{envelope_pct:.2f}%** per trade while this card "
            f"caps a trade at **{trade_pct:.2f}%**. Its lot count is therefore about "
            f"**{envelope_pct / trade_pct:.1f}× too large** for this playbook — either halve what it "
            f"suggests, or set 'Risk per trade' to {trade_pct:.2f}% so the two panels agree.")
    risk = {
        'capital': capital, 'daily_pct': daily_pct, 'trade_pct': trade_pct,
        'daily_budget': daily_budget, 'per_trade_cap': per_trade_cap,
        'spent': spent, 'remaining': remaining,
        'trades_left': int(remaining // per_trade_cap) if per_trade_cap > 0 else 0,
        'envelope_pct': envelope_pct, 'conflict_note': conflict_note,
    }
    blocked = [r for r in rules[2:] if r[0] == 'alert']
    return {'mode': mode, 'ladder': ladder, 'rules': rules, 'risk': risk,
            'blocked': blocked, 'cutoff_label': cutoff_label,
            'minutes_to_cutoff': minutes_to_cutoff}

# ==========================================
# MODULE 2 — INTRADAY OI VELOCITY (the burst detector)
# ==========================================
def compute_oi_velocity(df: pd.DataFrame, prev_df, prev_ts, now_ts,
                        atm: float, settings: dict):
    if df is None or prev_df is None or df.empty or prev_df.empty or prev_ts is None or now_ts is None:
        return None
    try:
        elapsed = (now_ts - prev_ts).total_seconds()
    except Exception:
        return None
    if elapsed <= 0 or elapsed > 900:
        return None
    if elapsed < float(settings.get('min_elapsed_s', 3.0)):
        return None
    width = int(settings.get('width', 5))
    lo, hi = atm - width * STRIKE_STEP, atm + width * STRIKE_STEP
    cur = df.set_index('Strike')
    prv = prev_df.copy()
    prv['Strike'] = pd.to_numeric(prv['Strike'], errors='coerce')
    prv = prv.dropna(subset=['Strike']).set_index('Strike')
    common = [k for k in cur.index.intersection(prv.index) if lo <= k <= hi]
    if not common:
        return None
    def _num(frame, col):
        return pd.to_numeric(frame.loc[common, col], errors='coerce').fillna(0.0).values
    v = pd.DataFrame({
        'Strike': common,
        'dCE_OI': _num(cur, 'CE_OI') - _num(prv, 'CE_OI'),
        'dPE_OI': _num(cur, 'PE_OI') - _num(prv, 'PE_OI'),
    }).sort_values('Strike').reset_index(drop=True)
    mins = elapsed / 60.0
    v['dCE_per_min'] = v['dCE_OI'] / mins
    v['dPE_per_min'] = v['dPE_OI'] / mins
    v['Net'] = v['dPE_OI'] - v['dCE_OI']
    net_ce, net_pe = float(v['dCE_OI'].sum()), float(v['dPE_OI'].sum())
    ce_burst = v.loc[v['dCE_OI'].abs().idxmax()]
    pe_burst = v.loc[v['dPE_OI'].abs().idxmax()]
    intensity = abs(net_ce) + abs(net_pe)
    return {
        'table': v, 'elapsed_s': elapsed,
        'net_ce': net_ce, 'net_pe': net_pe,
        'net_ce_per_min': net_ce / mins, 'net_pe_per_min': net_pe / mins,
        'net_bias': net_pe - net_ce,
        'intensity': intensity, 'intensity_per_min': intensity / mins,
        'ce_burst_strike': float(ce_burst['Strike']), 'ce_burst': float(ce_burst['dCE_OI']),
        'pe_burst_strike': float(pe_burst['Strike']), 'pe_burst': float(pe_burst['dPE_OI']),
        'width': width,
    }

def classify_velocity(vel, history, settings: dict):
    if vel is None:
        return None
    pctile = float(settings.get('burst_pctile', 85))
    floor = float(settings.get('min_burst_contracts', 15_000))
    hist = [h for h in (history or []) if h is not None and np.isfinite(h)]
    threshold, rank = None, None
    if len(hist) >= 20:
        threshold = float(np.percentile(hist, pctile))
        rank = float((np.sum(np.array(hist) <= vel['intensity_per_min']) / len(hist)) * 100)
    is_burst = bool(threshold is not None
                    and vel['intensity_per_min'] >= max(threshold, floor))
    bias = vel['net_bias']
    if not is_burst:
        headline = "Normal flow"
        detail = ("Poll-to-poll OI flow is inside today's usual range — nothing is being "
                  "committed with urgency right now.")
        color, key = "#6c757d", "normal"
    elif bias > 0:
        headline = "🟢 BURST — put writing / call unwinding"
        detail = ("Size just arrived on the bullish side of the ladder: puts being written and/or "
                  "calls being bought back, inside the last poll. This is the flow that precedes a "
                  "squeeze, not the flow that follows one.")
        color, key = "#1e7e34", "bull_burst"
    else:
        headline = "🔴 BURST — call writing / put unwinding"
        detail = ("Size just arrived on the bearish side: calls being written and/or puts bought "
                  "back, inside the last poll. Writers are capping this level in real time.")
        color, key = "#c82333", "bear_burst"
    return {
        'is_burst': is_burst, 'headline': headline, 'detail': detail,
        'color': color, 'key': key, 'threshold': threshold, 'rank': rank,
        'pctile': pctile, 'floor': floor, 'samples': len(hist),
    }

# ==========================================
# MODULE 3 — ATM STRADDLE / EXPECTED MOVE
# ==========================================
def compute_expected_move(df: pd.DataFrame, atm: float, spot: float, atm_iv: float,
                          dte, ohlc_df, settings: dict):
    if df is None or df.empty or not atm:
        return None
    row = df[df['Strike'] == atm]
    if row.empty:
        return None
    ce = float(pd.to_numeric(row['CE_LTP'], errors='coerce').fillna(0).iloc[0])
    pe = float(pd.to_numeric(row['PE_LTP'], errors='coerce').fillna(0).iloc[0])
    straddle = ce + pe
    if straddle <= 0:
        return None
    ref = spot if spot else atm
    sd_factor = float(settings.get('straddle_sd_factor', 0.80))
    em_expiry = straddle * sd_factor
    em_today = None
    if atm_iv and np.isfinite(atm_iv) and atm_iv > 0 and ref:
        em_today = ref * (atm_iv / 100.0) * np.sqrt(1.0 / TRADING_DAYS_YEAR)
    if em_today is None and dte is not None:
        em_today = em_expiry / max(np.sqrt(max(dte, 1)), 1.0)
    day_high = day_low = day_range = None
    if ohlc_df is not None and not ohlc_df.empty:
        day_high, day_low = float(ohlc_df['high'].max()), float(ohlc_df['low'].min())
        day_range = day_high - day_low
    expected_range = 2 * em_today if em_today else None
    range_used = (day_range / expected_range * 100) if (day_range and expected_range) else None
    hi_th, lo_th = settings.get('range_spent_high', 100.0), settings.get('range_spent_low', 40.0)
    if range_used is None:
        verdict, vcolor = None, None
    elif range_used >= hi_th:
        verdict = ("Day's expected range is spent — a further breakout has to be paid for with "
                   "vol expansion, not just direction. Fade-and-mean-revert setups are the ones "
                   "the option market is still pricing as likely.")
        vcolor = "#c82333"
    elif range_used <= lo_th:
        verdict = ("Most of today's expected range is still unused. Breakout and trend-continuation "
                   "setups have runway, and a move that looks big on the chart is still inside what "
                   "the straddle already paid for.")
        vcolor = "#1e7e34"
    else:
        verdict = ("Price has worked through a normal share of today's expected range. No edge "
                   "either way from the range itself — read direction from the other panels.")
        vcolor = "#6c757d"
    return {
        'atm': float(atm), 'ce': ce, 'pe': pe, 'straddle': straddle,
        'em_expiry_pts': em_expiry, 'em_today_pts': em_today,
        'em_today_pct': (em_today / ref * 100) if (em_today and ref) else None,
        'upper_be': float(atm) + straddle, 'lower_be': float(atm) - straddle,
        'expected_high': (ref + em_today) if em_today else None,
        'expected_low': (ref - em_today) if em_today else None,
        'day_high': day_high, 'day_low': day_low, 'day_range': day_range,
        'expected_range': expected_range, 'range_used_pct': range_used,
        'range_left_pts': (expected_range - day_range) if (expected_range and day_range is not None) else None,
        'verdict': verdict, 'verdict_color': vcolor,
        'sd_factor': sd_factor,
    }

def straddle_implied_price_floor(em, spot, lookback_minutes: int, clamp=(0.02, 0.60)):
    if not em or not em.get('em_today_pts') or not spot:
        return None
    scaled = em['em_today_pts'] * np.sqrt(max(lookback_minutes, 1) / SESSION_MINUTES)
    pct = scaled / spot * 100
    return float(np.clip(pct, clamp[0], clamp[1]))

# ==========================================
# MODULE 4 — SIGNAL PERFORMANCE TRACKER
# ==========================================
SIGNAL_DIRECTION = {
    'Master_Signal': {
        'Strong CE Buy': 1, 'PE writers strong': 1,
        'Strong PE Buy': -1, 'CE writers strong': -1,
    },
    'Scenario': {'A': 1, 'B': -1},
    'IV_Lens_Stance': {'shakeout': 1, 'conviction': 1, 'distribution': -1, 'fear_bid': -1},
    'ZoneB_Signal': {'Buy CE': 1, 'Write PE': 1, 'Buy PE': -1, 'Write CE': -1},
    'VWAP_Trend_Side': {'above': 1, 'below': -1},
    'Hier_Signal': {'LONG': 1, 'SHORT': -1},
}

def _log_timestamps(log_df: pd.DataFrame) -> pd.Series:
    date_part = log_df['Date'] if 'Date' in log_df.columns else pd.Series(
        ['1970-01-01'] * len(log_df), index=log_df.index)
    return pd.to_datetime(date_part.astype(str) + ' ' + log_df['Time'].astype(str), errors='coerce')

def grade_signals(log_df: pd.DataFrame, settings: dict, columns=None):
    if log_df is None or log_df.empty or 'Spot' not in log_df.columns or 'Time' not in log_df.columns:
        return None
    horizon = float(settings.get('horizon_minutes', 15))
    target = float(settings.get('target_pts', 20))
    min_n = int(settings.get('min_samples', 5))
    cols = columns or [c for c in SIGNAL_DIRECTION if c in log_df.columns]
    if not cols:
        return None
    d = log_df.copy()
    d['_ts'] = _log_timestamps(d)
    d['_spot'] = pd.to_numeric(d['Spot'], errors='coerce')
    d = d.dropna(subset=['_ts', '_spot']).sort_values('_ts').reset_index(drop=True)
    if len(d) < 10:
        return None
    ts = d['_ts'].values.astype('datetime64[s]').astype(np.int64)
    px = d['_spot'].values
    day = d['Date'].values if 'Date' in d.columns else np.zeros(len(d))
    horizon_s = horizon * 60
    fwd = np.full(len(d), np.nan)
    mfe = np.full(len(d), np.nan)
    mae = np.full(len(d), np.nan)
    for i in range(len(d)):
        k = i
        while k + 1 < len(d) and ts[k + 1] - ts[i] <= horizon_s and day[k + 1] == day[i]:
            k += 1
        if k == i or ts[k] - ts[i] < horizon_s * 0.5:
            continue
        seg = px[i:k + 1]
        fwd[i] = px[k] - px[i]
        mfe[i] = seg.max() - px[i]
        mae[i] = seg.min() - px[i]
    d['_fwd'], d['_up'], d['_dn'] = fwd, mfe, mae
    graded = d.dropna(subset=['_fwd'])
    if graded.empty:
        return None
    baseline = float(graded['_fwd'].mean())
    rows = []
    for col in cols:
        mapping = SIGNAL_DIRECTION[col]
        for label, direction in mapping.items():
            sub = graded[graded[col].astype(str) == str(label)]
            if sub.empty:
                continue
            signed = sub['_fwd'] * direction
            fav = (sub['_up'] if direction > 0 else -sub['_dn'])
            adv = (sub['_dn'] if direction > 0 else -sub['_up'])
            n = len(sub)
            rows.append({
                'Source': col, 'Signal': label,
                'Dir': '▲ long' if direction > 0 else '▼ short',
                'N': n,
                'Hit %': float((signed >= target).mean() * 100),
                'Avg move': float(signed.mean()),
                'Median': float(signed.median()),
                'Avg MFE': float(fav.mean()),
                'Avg MAE': float(adv.mean()),
                'Edge vs base': float(signed.mean() - baseline * direction),
                'Reliable': n >= min_n,
            })
    if not rows:
        return None
    out = pd.DataFrame(rows).sort_values(['Reliable', 'Edge vs base'], ascending=[False, False])
    return {
        'table': out.reset_index(drop=True),
        'baseline': baseline,
        'graded_polls': int(len(graded)),
        'sessions': int(pd.Series(day).nunique()),
        'horizon': horizon, 'target': target, 'min_samples': min_n,
    }

# ==========================================
# MODULE 5 — IV TERM STRUCTURE (front vs next expiry)
# ==========================================
def compute_term_structure(front_iv, next_iv, front_dte, next_dte,
                           d_front=None, d_next=None, settings: dict = None):
    t = settings or DEFAULT_TERM_SETTINGS
    if front_iv is None or next_iv is None or not np.isfinite(front_iv) or not np.isfinite(next_iv):
        return None
    if front_iv <= 0 or next_iv <= 0:
        return None
    spread = float(next_iv - front_iv)
    back_th = float(t.get('backwardation_pts', 0.5))
    if spread <= -back_th:
        shape = "Backwardation — front expiry carries an event premium"
        shape_key, shape_color = 'backwardation', "#fd7e14"
        shape_detail = ("The front expiry is pricing more vol than the next one, which happens when "
                        "something specific is expected inside its life — an event, or simply expiry-day "
                        "gamma. Front-expiry premium decays violently once the event passes, so short-dated "
                        "long options here are paying for a decay that is about to accelerate.")
    elif spread >= back_th:
        shape = "Contango — normal upward term structure"
        shape_key, shape_color = 'contango', "#6c757d"
        shape_detail = ("The next expiry prices more vol than the front, which is the ordinary shape. "
                        "Nothing to read from the term structure itself today.")
    else:
        shape = "Flat term structure"
        shape_key, shape_color = 'flat', "#6c757d"
        shape_detail = "Front and next expiry are pricing near-identical vol. No calendar signal."
    divergence, div_note = None, None
    ratio_th = float(t.get('divergence_ratio', 0.4))
    if d_front is not None and d_next is not None and abs(d_front) > 0.1:
        ratio = abs(d_next) / abs(d_front)
        same_sign = (d_front * d_next) > 0
        if ratio < ratio_th or not same_sign:
            divergence = 'front_only'
            div_note = (f"Front expiry IV moved {d_front:+.2f} while the next expiry moved only "
                        f"{d_next:+.2f}. The vol surface has not moved — this is expiry-specific, so "
                        f"treat the IV Lens's read as noise rather than a regime change.")
        else:
            divergence = 'confirmed'
            div_note = (f"Both expiries moved together ({d_front:+.2f} front, {d_next:+.2f} next). "
                        f"This is a genuine shift in the vol surface, so the IV Lens's quadrant read "
                        f"carries its full weight.")
    return {
        'front_iv': float(front_iv), 'next_iv': float(next_iv), 'spread': spread,
        'front_dte': front_dte, 'next_dte': next_dte,
        'shape': shape, 'shape_key': shape_key, 'shape_color': shape_color,
        'shape_detail': shape_detail,
        'd_front': d_front, 'd_next': d_next,
        'divergence': divergence, 'divergence_note': div_note,
    }

def measure_term_iv_change(log_records, date_str: str, minutes: float):
    if not log_records:
        return None, None
    try:
        d = pd.DataFrame(log_records)
        if 'Term_Front_IV' not in d.columns or 'Term_Next_IV' not in d.columns:
            return None, None
        d['_ts'] = pd.to_datetime(date_str + ' ' + d['Time'].astype(str), errors='coerce')
        for c in ('Term_Front_IV', 'Term_Next_IV'):
            d[c] = pd.to_numeric(d[c], errors='coerce')
        d = d.dropna(subset=['_ts', 'Term_Front_IV', 'Term_Next_IV'])
        if len(d) < 3:
            return None, None
        cutoff = d['_ts'].iloc[-1] - pd.Timedelta(minutes=minutes)
        w = d[d['_ts'] >= cutoff]
        if len(w) < 3:
            return None, None
        n = max(1, min(3, len(w) // 3))
        f = float(w['Term_Front_IV'].iloc[-n:].mean() - w['Term_Front_IV'].iloc[:n].mean())
        x = float(w['Term_Next_IV'].iloc[-n:].mean() - w['Term_Next_IV'].iloc[:n].mean())
        return f, x
    except Exception:
        return None, None

# ==========================================
# MODULE 6 — RISK ENVELOPE (ATR, stops, targets, position size)
# ==========================================
def compute_atr(ohlc_df: pd.DataFrame, period: int = 14):
    if ohlc_df is None or ohlc_df.empty or len(ohlc_df) < 2:
        return None
    d = ohlc_df.copy()
    prev_close = d['close'].shift(1)
    tr = pd.concat([
        d['high'] - d['low'],
        (d['high'] - prev_close).abs(),
        (d['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    n = min(int(period), len(tr.dropna()))
    if n < 2:
        return None
    atr = float(tr.dropna().tail(n).mean())
    return atr if np.isfinite(atr) and atr > 0 else None

def build_risk_envelope(spot, atr, em, direction: int, df: pd.DataFrame, atm: float,
                        walls, settings: dict, interval_label: str = ""):
    if not spot or not atr or direction == 0:
        return None
    mult = float(settings.get('atr_stop_mult', 1.5))
    rr = float(settings.get('reward_multiple', 2.0))
    capital = float(settings.get('capital', 200_000))
    risk_pct = float(settings.get('risk_pct', 1.0))
    lot = float(settings.get('lot_size', LOT_SIZE))
    max_prem_pct = float(settings.get('max_premium_pct', 25.0))
    atr_stop_dist = atr * mult
    structural, struct_label = None, None
    if walls:
        if direction > 0 and walls.get('max_pe_strike'):
            structural = spot - (float(walls['max_pe_strike']) - STRIKE_STEP * 0.5)
            struct_label = f"below the put floor at {walls['max_pe_strike']:.0f}"
        elif direction < 0 and walls.get('max_ce_strike'):
            structural = (float(walls['max_ce_strike']) + STRIKE_STEP * 0.5) - spot
            struct_label = f"above the call wall at {walls['max_ce_strike']:.0f}"
        if structural is not None and structural <= 0:
            structural = None
    cap = atr_stop_dist * float(settings.get('structural_cap_mult', 2.0))
    structural_ignored = bool(structural and structural > cap)
    if structural_ignored:
        structural = None
    stop_dist = max(atr_stop_dist, structural) if structural else atr_stop_dist
    stop_src = ("structural" if (structural and structural >= atr_stop_dist) else "ATR")
    target_dist = stop_dist * rr
    entry = float(spot)
    stop_px = entry - stop_dist * direction
    target_px = entry + target_dist * direction
    row = df[df['Strike'] == atm]
    leg = 'CE' if direction > 0 else 'PE'
    premium = delta = None
    if not row.empty:
        premium = float(pd.to_numeric(row[f'{leg}_LTP'], errors='coerce').fillna(0).iloc[0])
        delta = abs(float(pd.to_numeric(row[f'{leg}_Delta'], errors='coerce').fillna(0).iloc[0]))
    if not premium or premium <= 0:
        return None
    delta_assumed = False
    if not delta or delta <= 0:
        delta, delta_assumed = 0.5, True
    risk_amount = capital * risk_pct / 100.0
    prem_risk_per_lot = stop_dist * delta * lot
    prem_gain_per_lot = target_dist * delta * lot
    cost_per_lot = premium * lot
    lots_by_risk = int(risk_amount // prem_risk_per_lot) if prem_risk_per_lot > 0 else 0
    lots_by_capital = int((capital * max_prem_pct / 100.0) // cost_per_lot) if cost_per_lot > 0 else 0
    lots = max(0, min(lots_by_risk, lots_by_capital))
    binding = ("risk budget" if lots_by_risk <= lots_by_capital else "premium cap")
    warnings = []
    if lots == 0:
        warnings.append(
            f"Zero lots at these settings — one lot would risk ₹{prem_risk_per_lot:,.0f} against a "
            f"₹{risk_amount:,.0f} budget. Either the stop is too wide for the account or the position "
            f"has to be expressed in a spread rather than a naked option.")
    if em and em.get('range_left_pts') is not None and target_dist > max(em['range_left_pts'], 0):
        warnings.append(
            f"The {rr:.1f}R target needs {target_dist:.0f} points but only about "
            f"{max(em['range_left_pts'], 0):.0f} points of today's expected range are left unspent. "
            f"Either take the trade with a reduced target or accept it as a multi-session hold.")
    if em and em.get('em_today_pts') and stop_dist > em['em_today_pts']:
        warnings.append(
            f"The stop ({stop_dist:.0f} pts) is wider than the full 1SD session move "
            f"({em['em_today_pts']:.0f} pts) — it will rarely be hit, but each loss costs a full day's "
            f"expected range. That is a position-size decision, not a stop-placement one.")
    return {
        'direction': direction, 'side': 'LONG (CE)' if direction > 0 else 'SHORT (PE)',
        'leg': leg, 'entry': entry, 'stop': stop_px, 'target': target_px,
        'stop_dist': stop_dist, 'target_dist': target_dist,
        'atr': atr, 'atr_stop_dist': atr_stop_dist, 'atr_mult': mult, 'interval': interval_label,
        'structural': structural, 'structural_label': struct_label, 'stop_source': stop_src,
        'structural_ignored': structural_ignored, 'structural_cap': cap,
        'rr': rr, 'premium': premium, 'delta': delta, 'delta_assumed': delta_assumed,
        'prem_risk_per_lot': prem_risk_per_lot, 'prem_gain_per_lot': prem_gain_per_lot,
        'cost_per_lot': cost_per_lot, 'lots': lots, 'lots_by_risk': lots_by_risk,
        'lots_by_capital': lots_by_capital, 'binding': binding,
        'risk_amount': risk_amount, 'deployed': lots * cost_per_lot,
        'risk_at_stop': lots * prem_risk_per_lot, 'gain_at_target': lots * prem_gain_per_lot,
        'warnings': warnings,
    }

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
    floor = df['CE_OI'].max() * min_ce_oi_frac
    valid = df[(df['CE_OI'] >= floor) & df['PCR'].notna()]
    return valid.nlargest(top_n, 'PCR')[['Strike', 'PCR']].reset_index(drop=True)

# ==========================================
# INSTITUTIONAL FOOTPRINT (new — a third, independent read alongside the
# Master Signal and the VWAP Trend Read. Never feeds back into either.)
# ==========================================
def compute_footprint_table(df: pd.DataFrame, atm: float, width: int) -> pd.DataFrame:
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
    min_pct_floor = (t or {}).get('chgpcr_min_ce_chg_pct_of_oi', 0.3)
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
    if not spot or not day_open:
        return "unknown"
    change_pct = (spot - day_open) / day_open * 100
    if change_pct > flat_band_pct:
        return "rising"
    if change_pct < -flat_band_pct:
        return "falling"
    return "sideways"

def institutional_footprint_signal(iv_skew, chg_pcr, vol_oi, market_direction, t: dict, chg_pcr_reliable: bool = True):
    lines = []
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
    fl = footprint_legs(iv_skew, chg_pcr, vol_oi, market_direction, t, chg_pcr_reliable)
    fp_dir, fp_state, n_agree, n_total = footprint_direction(
        fl, t.get('_hierarchy', HIERARCHY_DEFAULTS)['footprint_min_legs'])
    if fp_state == 'qualified':
        headline_bias = "Bullish" if fp_dir > 0 else "Bearish"
    else:
        headline_bias = "Neutral"
    if fp_state == 'split':
        lines.append(f"⚠️ Legs disagree ({n_total} directional, none in agreement) — the "
                     f"Footprint asserts NO direction. Rank 3 yields to Buildup.")
    else:
        lines.append(f"⚠️ Only {n_total} directional leg — the Footprint needs 2 in "
                     f"agreement before it asserts a direction. Vol/OI measures whether "
                     f"the flow is real, not which way it points, so it does not vote.")
    if headline_bias == "Bullish":
        headline, color_key = "🟢 Institutional Footprint: BULLISH", "bullish"
    elif headline_bias == "Bearish":
        headline, color_key = "🔴 Institutional Footprint: BEARISH", "bearish"
    else:
        headline, color_key = "⚪ Institutional Footprint: NO QUALIFIED READ", "neutral"
    if conviction == "Fakeout risk" and headline_bias != "Neutral":
        headline += " (low conviction — Vol/OI thin)"
    return headline, color_key, lines, fl

# ============================================================================
# DIRECTIONAL HIERARCHY
# ============================================================================
HIERARCHY_DEFAULTS = {
    "footprint_min_legs": 2,
    "chgpcr_bullish": 1.15,
    "chgpcr_bearish": 0.85,
    "buildup_min_net_pct": 20.0,
    "buildup_min_strikes": 4,
    "chgpcr_alone_max_rank": 4,
}

def footprint_legs(iv_skew, chg_pcr, vol_oi, market_direction, t, chg_pcr_reliable=True):
    legs, notes = {}, {}
    if pd.isna(iv_skew):
        legs['iv_skew'], notes['iv_skew'] = 0, "unavailable"
    elif iv_skew <= t['iv_skew_bearish']:
        legs['iv_skew'], notes['iv_skew'] = -1, f"{iv_skew:+.2f} <= {t['iv_skew_bearish']}"
    elif iv_skew >= t['iv_skew_bullish']:
        legs['iv_skew'], notes['iv_skew'] = 1, f"{iv_skew:+.2f} >= {t['iv_skew_bullish']}"
    else:
        legs['iv_skew'], notes['iv_skew'] = 0, f"{iv_skew:+.2f} inside the dead band"
    h = t.get('_hierarchy', HIERARCHY_DEFAULTS)
    if not chg_pcr_reliable or pd.isna(chg_pcr):
        legs['chg_pcr'], notes['chg_pcr'] = 0, "unreliable / unavailable"
    else:
        trap = None
        if market_direction == "falling" and chg_pcr > t['chgpcr_bear_trap']:
            trap = 1
        elif market_direction == "rising" and chg_pcr < t['chgpcr_bull_trap']:
            trap = -1
        if trap is not None:
            legs['chg_pcr'] = trap
            notes['chg_pcr'] = f"{chg_pcr:.2f} trap vs {market_direction} price"
        elif chg_pcr >= h['chgpcr_bullish']:
            legs['chg_pcr'], notes['chg_pcr'] = 1, f"{chg_pcr:.2f} put-side flow"
        elif chg_pcr <= h['chgpcr_bearish']:
            legs['chg_pcr'], notes['chg_pcr'] = -1, f"{chg_pcr:.2f} call-side flow"
        else:
            legs['chg_pcr'], notes['chg_pcr'] = 0, f"{chg_pcr:.2f} inside the dead band"
    conviction = ("unknown" if pd.isna(vol_oi) else
                  "confirmed" if vol_oi >= t['vol_oi_fresh'] else
                  "fakeout" if vol_oi < t['vol_oi_fakeout'] else "moderate")
    return {'legs': legs, 'notes': notes, 'conviction': conviction}

def footprint_direction(fp_legs, min_legs=2):
    votes = [v for v in fp_legs['legs'].values() if v != 0]
    n = len(votes)
    if n < min_legs:
        return 0, 'insufficient', n, n
    if all(v == votes[0] for v in votes):
        return votes[0], 'qualified', n, n
    return 0, 'split', 0, n

def buildup_quality(bsum, h=None):
    h = h or HIERARCHY_DEFAULTS
    if bsum is None:
        return {'ok': False, 'dir': 0, 'reason': "no strike cleared the buildup thresholds"}
    net = float(bsum.get('net_pct', 0.0))
    detail = bsum.get('detail')
    n_legs = int(len(detail)) if detail is not None else 0
    bull, bear = float(bsum.get('bull_weight', 0)), float(bsum.get('bear_weight', 0))
    saturated = (bull == 0) or (bear == 0)
    if n_legs < h['buildup_min_strikes']:
        return {'ok': False, 'dir': 0, 'n_legs': n_legs, 'saturated': saturated,
                'reason': f"only {n_legs} strike-leg(s) classified; needs {h['buildup_min_strikes']}"}
    if abs(net) < h['buildup_min_net_pct']:
        return {'ok': False, 'dir': 0, 'n_legs': n_legs, 'saturated': saturated,
                'reason': f"net bias {net:+.0f}% is inside the +/-{h['buildup_min_net_pct']:.0f}% dead band"}
    d = 1 if net > 0 else -1
    return {'ok': True, 'dir': d, 'n_legs': n_legs, 'saturated': saturated, 'net_pct': net,
            'reason': (f"net {net:+.0f}% across {n_legs} strike-legs"
                       + (" -- SATURATED: one side contributed zero weight, so treat the "
                          "magnitude as unreadable even though the sign is usable."
                          if saturated else ""))}

def resolve_direction(gex, bsum, fp_legs, chg_pcr, chg_pcr_reliable, t, h=None):
    h = h or HIERARCHY_DEFAULTS
    t = dict(t); t['_hierarchy'] = h
    if gex is None:
        gate = {'ok': False, 'mode': None, 'why': "no live gamma this poll -- regime unknown",
                'size': 'No position'}
    elif gex.get('at_flip'):
        gate = {'ok': False, 'mode': 'flip', 'size': 'No position',
                'why': f"spot is inside the flip zone ({gex['flip_level']:.0f}); regime is undefined"}
    elif gex.get('net_gex', 0) < 0:
        gate = {'ok': True, 'mode': 'follow', 'size': 'Full size',
                'why': "short gamma -- dealer hedging runs WITH the move, so trade the direction below"}
    else:
        gate = {'ok': True, 'mode': 'fade', 'size': 'Defined risk only',
                'why': "long gamma -- dealer hedging leans AGAINST the move, so FADE the direction below"}
    bq = buildup_quality(bsum, h)
    fp_dir, fp_state, fp_agree, fp_total = footprint_direction(fp_legs, h['footprint_min_legs'])
    raw_pcr_dir = 0
    if chg_pcr_reliable and not pd.isna(chg_pcr):
        raw_pcr_dir = (1 if chg_pcr >= h['chgpcr_bullish'] else
                       -1 if chg_pcr <= h['chgpcr_bearish'] else 0)
    if bq['ok']:
        src, rank, direction = 'Buildup', 2, bq['dir']
        why = f"Buildup on full inputs -- {bq['reason']}"
        size_cap = 1.0
    elif fp_state == 'qualified':
        src, rank, direction = 'Footprint', 3, fp_dir
        why = (f"Buildup did not qualify ({bq['reason']}); Footprint has "
               f"{fp_agree}/{fp_total} legs agreeing")
        size_cap = 0.75
    elif raw_pcr_dir != 0:
        src, rank, direction = 'ChgPCR alone', 4, raw_pcr_dir
        why = (f"neither Buildup nor Footprint qualified; ChgPCR {chg_pcr:.2f} is the only "
               f"directional input left")
        size_cap = 0.25
    else:
        src, rank, direction = None, None, 0
        why = "no source in the hierarchy qualified -- direction is genuinely unknown"
        size_cap = 0.0
    label = {1: "BULLISH", -1: "BEARISH", 0: "NO DIRECTION"}[direction]
    contested = None
    if bq['ok'] and fp_state == 'qualified' and bq['dir'] != fp_dir:
        contested = ("Buildup and a fully-qualified Footprint disagree. Both cleared their own "
                     "bars, so this is a real split -- take the Buildup per the hierarchy, at "
                     "half the size the gate would otherwise allow.")
        size_cap = min(size_cap, 0.5)
    trade_dir = 0
    if gate['ok'] and direction != 0:
        trade_dir = direction if gate['mode'] == 'follow' else -direction
    return {
        'gate': gate, 'source': src, 'rank': rank,
        'direction': direction, 'label': label, 'trade_dir': trade_dir,
        'why': why, 'contested': contested, 'size_cap': size_cap,
        'buildup': bq, 'footprint': {'dir': fp_dir, 'state': fp_state,
                                      'agree': fp_agree, 'total': fp_total,
                                      'legs': fp_legs['legs'], 'notes': fp_legs['notes'],
                                      'conviction': fp_legs['conviction']},
        'chg_pcr_dir': raw_pcr_dir,
        'tradeable': bool(gate['ok'] and trade_dir != 0),
    }

# ============================================================================
# STRUCTURE-AWARE RISK MODEL
# ============================================================================
DEFAULT_TRADE_RISK = {
    "daily_budget_pct": 1.0,
    "sizing_basis": "stop",
    "per_trade_share": {"debit_spread": 0.50, "credit_spread": 0.50, "neutral_credit": 0.70},
    "stop_mult": {"credit": 1.0, "debit": 0.5},
    "tail_cap_mult": 2.5,
    "min_credit_ratio": 0.18,
    "max_walk_strikes": 4,
    "rank_multiplier": {2: 1.0, 3: 0.75, 4: 0.25},
}

def trade_risk_caps(trade_risk: dict, capital: float, family: str, rank=None):
    tr = {**DEFAULT_TRADE_RISK, **(trade_risk or {})}
    daily = float(capital) * float(tr['daily_budget_pct']) / 100.0
    share = tr['per_trade_share'].get(family, 0.5)
    cap = daily * float(share)
    if rank is not None:
        cap *= float(tr['rank_multiplier'].get(rank, 1.0))
    tail = cap * float(tr['tail_cap_mult'])
    return cap, tail, daily

def size_position(premium_pts, max_loss_pts, lot, per_trade_cap, tail_cap,
                  is_credit: bool, trade_risk: dict = None):
    tr = {**DEFAULT_TRADE_RISK, **(trade_risk or {})}
    if premium_pts is None or max_loss_pts is None or max_loss_pts <= 0:
        return 0, None, None, "unpriceable"
    if tr.get('sizing_basis') == 'max_loss':
        per_lot = max_loss_pts * lot
        lots = int(per_trade_cap // per_lot) if per_lot > 0 else 0
        return max(lots, 0), per_lot, per_lot, "max_loss basis"
    mult = tr['stop_mult']['credit' if is_credit else 'debit']
    stop_pts = min(abs(premium_pts) * mult, max_loss_pts)
    stop_per_lot = stop_pts * lot
    max_per_lot = max_loss_pts * lot
    if stop_per_lot <= 0:
        return 0, None, None, "no stop distance"
    by_stop = int(per_trade_cap // stop_per_lot)
    by_tail = int(tail_cap // max_per_lot) if max_per_lot > 0 else 0
    lots = max(min(by_stop, by_tail), 0)
    binding = ("stop cap" if by_stop <= by_tail else "tail cap")
    return lots, stop_per_lot, max_per_lot, binding

# ============================================================================
# TRADE CONSTRUCTOR — structure selection and concrete strikes
# ============================================================================
STRUCTURE_RULES = {
    ('trend', True):  'debit_spread',
    ('trend', False): 'no_trade',
    ('pin',   True):  'credit_spread',
    ('pin',   False): 'neutral_credit',
    ('flip',  True):  'no_trade',
    ('flip',  False): 'no_trade',
}

def _snap(x, step, mode='near'):
    if x is None:
        return None
    if mode == 'up':
        return float(np.ceil(x / step) * step)
    if mode == 'down':
        return float(np.floor(x / step) * step)
    return float(round(x / step) * step)

def _ltp(chain, strike, side):
    try:
        row = chain[chain['Strike'] == strike]
        if row.empty:
            return None
        v = pd.to_numeric(row.iloc[0].get(f'{side}_LTP'), errors='coerce')
        return float(v) if pd.notna(v) and v > 0 else None
    except Exception:
        return None

def _net_premium(chain, legs):
    total, complete = 0.0, True
    for act, strike, side in legs:
        p = _ltp(chain, strike, side)
        if p is None:
            complete = False
            continue
        total += p if act == 'SELL' else -p
    return total, complete

def _fit_credit_spread(chain, short_k, side, spot, floor_k, lot, step,
                       per_trade_cap, tail_cap, trade_risk,
                       widths=(50, 100, 150, 200, 250, 300)):
    tr = {**DEFAULT_TRADE_RISK, **(trade_risk or {})}
    min_ratio = float(tr['min_credit_ratio'])
    best = (None, None, None, None, 0, None, None, None, 0)
    for walk in range(int(tr['max_walk_strikes']) + 1):
        k = short_k - walk * step if side == 'CE' else short_k + walk * step
        if side == 'CE' and floor_k is not None and k < floor_k:
            break
        if side == 'PE' and floor_k is not None and k > floor_k:
            break
        for w in widths:
            long_k = k + w if side == 'CE' else k - w
            credit, complete = _net_premium(chain, [('SELL', k, side), ('BUY', long_k, side)])
            if not complete or credit <= 0:
                continue
            if credit < min_ratio * w:
                continue
            risk_pts = w - credit
            if risk_pts <= 0:
                continue
            lots, stop_rs, max_rs, binding = size_position(
                credit, risk_pts, lot, per_trade_cap, tail_cap, True, tr)
            if lots >= 1:
                best = (float(w), float(k), float(long_k), float(credit),
                        lots, stop_rs, max_rs, binding, walk)
        if best[0] is not None:
            break
    return best

def _fit_neutral(chain, short_ce, short_pe, lot, step, per_trade_cap, tail_cap,
                 trade_risk, put_ratio=1.0,
                 widths=(50, 100, 150, 200, 250, 300, 400, 500)):
    tr = {**DEFAULT_TRADE_RISK, **(trade_risk or {})}
    best = (None, None, None, 0, None, None, None, None)
    for w in widths:
        pw = max(step, round(w * put_ratio / step) * step)
        legs = [('SELL', short_ce, 'CE'), ('BUY', short_ce + w, 'CE'),
                ('SELL', short_pe, 'PE'), ('BUY', short_pe - pw, 'PE')]
        credit, complete = _net_premium(chain, legs)
        if not complete or credit <= 0:
            continue
        risk_pts = max(w, pw) - credit
        if risk_pts <= 0:
            continue
        lots, stop_rs, max_rs, binding = size_position(
            credit, risk_pts, lot, per_trade_cap, tail_cap, True, tr)
        if lots >= 1:
            best = (float(w), float(pw), float(credit), lots, stop_rs, max_rs, binding, legs)
    return best

def _fit_debit_spread(chain, long_k, side, target_k, lot, step,
                      per_trade_cap, tail_cap, trade_risk):
    tr = {**DEFAULT_TRADE_RISK, **(trade_risk or {})}
    span = abs(target_k - long_k)
    for w in [x for x in np.arange(span, step, -step) if x >= step * 2]:
        sk = long_k + w if side == 'CE' else long_k - w
        net, complete = _net_premium(chain, [('BUY', long_k, side), ('SELL', sk, side)])
        if not complete:
            continue
        debit = -net
        if debit <= 0:
            continue
        lots, stop_rs, max_rs, binding = size_position(
            debit, debit, lot, per_trade_cap, tail_cap, False, tr)
        if lots >= 1:
            return (float(w), float(sk), float(debit), lots, stop_rs, max_rs, binding)
    return (None, None, None, 0, None, None, None)

def build_trade(hierarchy, gex, expected_move, spot, atm_strike, mp, dte,
                chain, risk_settings, lot=None, step=None, trade_risk=None):
    lot = float(lot or LOT_SIZE)
    step = float(step or STRIKE_STEP)
    tr = {**DEFAULT_TRADE_RISK, **(trade_risk or {})}
    out = {'legs': [], 'notes': [], 'structure': None, 'family': None,
           'lots': 0, 'max_loss': None, 'stop_loss': None, 'net_premium': None,
           'premium_complete': False, 'invalidation': None, 'target': None,
           'blocked': None, 'binding': None, 'per_trade_cap': None, 'tail_cap': None}
    if spot is None or chain is None or chain.empty:
        out['blocked'] = "No spot or no chain — nothing to construct."
        return out
    if gex is None:
        out['blocked'] = "No live gamma. The regime picks the structure, so nothing is built without it."
        return out
    regime = 'flip' if gex.get('at_flip') else gex.get('regime_key')
    has_dir = bool(hierarchy and hierarchy.get('tradeable'))
    family = STRUCTURE_RULES.get((regime, has_dir), 'no_trade')
    out['family'] = family
    if family == 'no_trade':
        out['blocked'] = (
            "Spot is on the gamma flip. The regime can invert on a 20-point move, so neither "
            "the buy structure nor the sell structure is safe."
            if regime == 'flip' else
            "Short gamma with no qualified direction. Dealer hedging amplifies whatever move "
            "comes and the hierarchy cannot say which way — this is the one cell where premium "
            "selling AND premium buying are both wrong.")
        return out
    capital = float(risk_settings.get('capital', 500000))
    rank = hierarchy.get('rank') if hierarchy else None
    per_trade_cap, tail_cap, daily = trade_risk_caps(tr, capital, family, rank)
    out['per_trade_cap'], out['tail_cap'] = per_trade_cap, tail_cap
    wall = gex.get('gamma_wall')
    pit = gex.get('gamma_pit')
    em_hi = expected_move.get('expected_high') if expected_move else None
    em_lo = expected_move.get('expected_low') if expected_move else None
    trade_dir = hierarchy['trade_dir'] if has_dir else 0
    def _above_pit(c):
        if pit is not None and c is not None and spot < pit <= c + step:
            return _snap(pit + step, step, 'up')
        return c
    def _below_pit(c):
        if pit is not None and c is not None and c - step <= pit < spot:
            return _snap(pit - step, step, 'down')
        return c
    if family == 'credit_spread':
        if trade_dir < 0:
            anchor = max([v for v in (em_hi, wall if (wall and wall > spot) else None,
                                      spot + step * 2) if v is not None])
            short_k = _above_pit(_snap(anchor, step, 'up'))
            floor_k = _above_pit(_snap(em_hi or spot + step * 2, step, 'up'))
            side, name, dir_txt = 'CE', 'Bear Call Spread', "short"
        else:
            anchor = min([v for v in (em_lo, wall if (wall and wall < spot) else None,
                                      spot - step * 2) if v is not None])
            short_k = _below_pit(_snap(anchor, step, 'down'))
            floor_k = _below_pit(_snap(em_lo or spot - step * 2, step, 'down'))
            side, name, dir_txt = 'PE', 'Bull Put Spread', "long"
        w, sk, lk, credit, lots, stop_rs, max_rs, binding, walked = _fit_credit_spread(
            chain, short_k, side, spot, floor_k, lot, step, per_trade_cap, tail_cap, tr)
        if w is None:
            out['blocked'] = (
                f"No {name} clears the caps. Short leg starts at {short_k:.0f}; walking it in to "
                f"{floor_k:.0f} (the 1SD / acceleration floor) still does not produce a credit of at "
                f"least {tr['min_credit_ratio']:.0%} of the width. Per-trade cap ₹{per_trade_cap:,.0f}, "
                f"tail cap ₹{tail_cap:,.0f}. This is a thin-premium no-trade, not a sizing problem — "
                f"buying credit by moving inside the 1SD band buys it by taking the risk the band "
                f"exists to avoid.")
            return out
        out['legs'] = [('SELL', sk, side), ('BUY', lk, side)]
        out['structure'], out['lots'] = name, lots
        out['net_premium'], out['premium_complete'] = credit, True
        out['stop_loss'], out['max_loss'] = stop_rs * lots, max_rs * lots
        out['binding'], out['invalidation'] = binding, sk
        out['target'] = mp if mp else _snap(spot, step)
        out['notes'].append(
            f"Long gamma fades the {hierarchy['label'].lower()} read, so the trade is {dir_txt}. "
            f"Short leg {sk:.0f}, width {w:.0f}, credit {credit:.1f} pts "
            f"({credit / w:.0%} of width)."
            + (f" Short leg walked {walked} strike(s) toward spot to reach a payable credit; "
               f"{floor_k:.0f} was the floor." if walked else ""))
    elif family == 'debit_spread':
        long_k = _snap(atm_strike, step)
        if trade_dir > 0:
            tgt = min([v for v in (em_hi, wall if (wall and wall > spot) else None)
                       if v is not None], default=spot + step * 4)
            target_k, side, name = max(_snap(tgt, step, 'up'), long_k + step * 4), 'CE', 'Bull Call Spread'
        else:
            tgt = max([v for v in (em_lo, wall if (wall and wall < spot) else None)
                       if v is not None], default=spot - step * 4)
            target_k, side, name = min(_snap(tgt, step, 'down'), long_k - step * 4), 'PE', 'Bear Put Spread'
        w, sk, debit, lots, stop_rs, max_rs, binding = _fit_debit_spread(
            chain, long_k, side, target_k, lot, step, per_trade_cap, tail_cap, tr)
        if w is None:
            out['blocked'] = (f"Even the narrowest {name} from {long_k:.0f} breaches the caps "
                              f"(per-trade ₹{per_trade_cap:,.0f}, tail ₹{tail_cap:,.0f}).")
            return out
        out['legs'] = [('BUY', long_k, side), ('SELL', sk, side)]
        out['structure'], out['lots'] = name, lots
        out['net_premium'], out['premium_complete'] = -debit, True
        out['stop_loss'], out['max_loss'] = stop_rs * lots, max_rs * lots
        out['binding'] = binding
        out['invalidation'] = _snap(spot - step * 2 if trade_dir > 0 else spot + step * 2, step)
        out['target'] = sk
        out['notes'].append(
            f"Short gamma gives the move follow-through, so premium is bought rather than sold. "
            f"Long leg at the money; short leg {sk:.0f} finances it at the level the move is "
            f"expected to reach. Debit {debit:.1f} pts; sized on a "
            f"{tr['stop_mult']['debit']:.0%} cut, not on total loss.")
    elif family == 'neutral_credit':
        expiry_day = (dte is not None and dte <= 1)
        if expiry_day:
            k = _snap(atm_strike, step)
            short_ce, short_pe, name, put_ratio = k, k, 'Iron Fly (broken wing)', 0.6
        else:
            short_ce = _above_pit(_snap(max(em_hi or spot + step * 3, spot + step * 3), step, 'up'))
            short_pe = _below_pit(_snap(min(em_lo or spot - step * 3, spot - step * 3), step, 'down'))
            name, put_ratio = 'Iron Condor', 1.0
        cw, pw, credit, lots, stop_rs, max_rs, binding, legs = _fit_neutral(
            chain, short_ce, short_pe, lot, step, per_trade_cap, tail_cap, tr, put_ratio)
        if cw is None:
            out['blocked'] = (f"No wing width for a {name} clears the caps (per-trade "
                              f"₹{per_trade_cap:,.0f}, tail ₹{tail_cap:,.0f}) at lot {lot:.0f}.")
            return out
        out['legs'], out['structure'], out['lots'] = legs, name, lots
        out['net_premium'], out['premium_complete'] = credit, True
        out['stop_loss'], out['max_loss'] = stop_rs * lots, max_rs * lots
        out['binding'], out['invalidation'] = binding, (short_ce, short_pe)
        out['target'] = mp if mp else _snap(spot, step)
        out['notes'].append(
            f"Long gamma with no qualified direction is the one regime whose mechanics support "
            f"selling premium on both sides. Credit {credit:.1f} pts; call wing {cw:.0f}, put wing "
            f"{pw:.0f} — the put wing is tighter because downside moves are the sharper ones.")
        if out['lots']:
            out['notes'].append(
                f"Sized on the STOP (₹{out['stop_loss']:,.0f}) against a ₹{per_trade_cap:,.0f} per-trade "
                f"cap; theoretical max loss ₹{out['max_loss']:,.0f} against a ₹{tail_cap:,.0f} tail cap. "
                f"Binding constraint: {out['binding']}.")
    if out['lots'] and hierarchy and hierarchy.get('size_cap', 1.0) < 1.0:
        per_lot = (out['max_loss'] / out['lots']) if out['max_loss'] else None
        capped = max(int(out['lots'] * hierarchy['size_cap']), 0)
        if capped != out['lots']:
            out['notes'].append(
                f"Rank-{hierarchy['rank']} size cap ({hierarchy['size_cap']:.0%}): "
                f"{out['lots']} → {capped} lot(s).")
        out['lots'] = capped
        out['max_loss'] = (per_lot * capped) if (per_lot is not None) else out['max_loss']
    if out['lots'] == 0 and out['legs'] and not out['blocked']:
        out['blocked'] = (f"Structure resolves to {out['structure']} at these strikes, but the "
                          f"sizing comes to zero lots. Shown for reference — do not send it.")
    return out

# ==========================================
# READ CONFLICT — the highest-probability no-trade condition
# ==========================================
def detect_read_conflict(footprint_color_key, footprint_agg, bsum,
                         footprint_market_dir=None, footprint_state=None):
    if not footprint_color_key or bsum is None:
        return None
    if footprint_state is not None and footprint_state != 'qualified':
        return None
    fp_dir = {'bullish': 1, 'bearish': -1}.get(footprint_color_key, 0)
    net_pct = float(bsum.get('net_pct', 0.0))
    bu_dir = 1 if net_pct > 20 else (-1 if net_pct < -20 else 0)
    fp_label = {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[fp_dir]
    bu_label = {1: "BULLISH", -1: "BEARISH", 0: "MIXED"}[bu_dir]
    fp_weak = footprint_market_dir in (None, 'unknown')
    chg_pcr = footprint_agg.get('chg_pcr') if footprint_agg else None
    chg_pcr_agrees_with_buildup = None
    if chg_pcr is not None and np.isfinite(chg_pcr) and bu_dir != 0:
        chg_pcr_dir = 1 if chg_pcr > 1.0 else -1
        chg_pcr_agrees_with_buildup = (chg_pcr_dir == bu_dir)
    if fp_dir != 0 and bu_dir != 0 and fp_dir != bu_dir:
        state, color = 'conflict', "#c82333"
        headline = f"⚠️ READ CONFLICT — Footprint {fp_label} vs Buildup {bu_label}"
        detail = (
            "Two independent reads of the same tape are pointing opposite ways. Footprint is built "
            "from the vol surface and the flow ratio; Buildup is built from price-vs-OI at every "
            "strike. They share no inputs, so this is not one indicator contradicting itself — it is "
            "positioning and flow genuinely out of alignment.")
        action = ("Historically your highest-probability no-trade condition. If you take anything "
                  "here, take it at reduced size with a wider stop, and require a live OI Velocity "
                  "burst on your side as the tiebreak — standing OI has already proven it cannot "
                  "settle the question.")
    elif fp_dir != 0 and fp_dir == bu_dir:
        state, color = 'aligned', "#1e7e34"
        headline = f"✅ READS ALIGNED — Footprint and Buildup both {fp_label}"
        detail = ("Two independent reads agree. Because they share no inputs, agreement is real "
                  "corroboration rather than the same number counted twice.")
        action = ("This is the environment for full size, subject to the gamma regime and the IV "
                  "Lens gate below.")
    else:
        state, color = 'inconclusive', "#6c757d"
        headline = f"◽ READS INCONCLUSIVE — Footprint {fp_label}, Buildup {bu_label}"
        detail = ("At least one of the two reads has no opinion. That is not a conflict — it is a "
                  "read declining to call a direction, which usually means the thresholds haven't "
                  "been cleared rather than that the tape is balanced.")
        action = "Treat direction as unconfirmed and lean on the gates below rather than on these two."
    return {
        'state': state, 'headline': headline, 'detail': detail, 'action': action,
        'color': color, 'fp_dir': fp_dir, 'bu_dir': bu_dir,
        'fp_label': fp_label, 'bu_label': bu_label,
        'net_pct': net_pct, 'fp_weak': fp_weak,
        'chg_pcr': chg_pcr, 'chg_pcr_agrees_with_buildup': chg_pcr_agrees_with_buildup,
        'iv_skew': footprint_agg.get('iv_skew') if footprint_agg else None,
        'vol_oi': footprint_agg.get('vol_oi') if footprint_agg else None,
        'top_commitment': (f"{bsum.get('top_leg')} {bsum.get('top_buildup')} at "
                           f"{bsum.get('top_strike'):.0f}") if bsum.get('top_strike') else None,
    }

# ==========================================
# SESSION STATE INIT
# ==========================================
for key, default in [
    ('previous_df', None), ('last_fetch', None), ('expiry_list', None),
    ('expiry_list_fetched_at', None), ('selected_expiry', None),
    ('baseline_source', None), ('last_gsheet_write', None),
    ('session_log', []), ('vwap_session_start', None), ('token_status', None),
    ('ohlc_df', None), ('last_candle_fetch', None), ('vwap_confirmed_alert_side', None),
    ('iv_lens_alert_stance', None), ('scenario_alert', None),
    ('poll_prev_df', None), ('poll_prev_ts', None),
    ('oi_velocity_hist', []), ('velocity_burst_alert', None),
    ('next_chain_df', None), ('next_chain_fetch_at', None), ('next_chain_expiry', None),
    ('gex_regime_alert', None),
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
# UI — READ CONFLICT FLAG (very top: the no-trade check comes before the signal)
# ==========================================
if read_conflict:
    rc = read_conflict
    if rc['state'] == 'conflict':
        st.markdown(f"""
<div style='background-color:{rc['color']};padding:20px;border-radius:10px;margin:4px 0 10px 0;
border:3px solid #7d1420;'>
<h3 style='color:white;margin:0;'>{rc['headline']}</h3>
<p style='color:white;margin:10px 0 0 0;'>{rc['detail']}</p>
<p style='color:white;margin:10px 0 0 0;'><b>{rc['action']}</b></p>
</div>""", unsafe_allow_html=True)
    else:
        st.markdown(f"""
<div style='background-color:{rc['color']};padding:14px 18px;border-radius:10px;margin:4px 0 10px 0;'>
<h4 style='color:white;margin:0;'>{rc['headline']}</h4>
<p style='color:white;margin:6px 0 0 0;'>{rc['detail']}</p>
</div>""", unsafe_allow_html=True)
    rcc1, rcc2, rcc3, rcc4 = st.columns(4)
    rcc1.metric("Footprint read", rc['fp_label'],
                f"IV skew {rc['iv_skew']:+.2f}" if rc['iv_skew'] is not None and np.isfinite(rc['iv_skew']) else "")
    rcc2.metric("Buildup read", rc['bu_label'], f"net bias {rc['net_pct']:+.0f}%")
    rcc3.metric("ChgPCR", f"{rc['chg_pcr']:.2f}" if rc['chg_pcr'] is not None and np.isfinite(rc['chg_pcr']) else "—",
                ("agrees with Buildup" if rc['chg_pcr_agrees_with_buildup']
                 else "agrees with Footprint") if rc['chg_pcr_agrees_with_buildup'] is not None else "")
    rcc4.metric("Vol/OI conviction", f"{rc['vol_oi']:.2f}" if rc['vol_oi'] is not None and np.isfinite(rc['vol_oi']) else "—",
                "flow is real either way")
    if rc['state'] == 'conflict':
        _notes = []
        if rc['chg_pcr_agrees_with_buildup']:
            _notes.append(
                f"**The conflict is less even than it looks.** ChgPCR ({rc['chg_pcr']:.2f}) is itself a "
                f"Footprint component, and on this poll it sides with the Buildup rather than with the "
                f"Footprint's own headline — which leaves the Footprint read resting mostly on IV skew.")
        if rc['fp_weak']:
            _notes.append(
                "**The Footprint read is running on one leg.** With today's price direction unknown, its "
                "ChgPCR trap logic is switched off entirely, so the headline is driven by IV skew alone. "
                "That is its weakest configuration and it should not be weighted equally against a "
                "Buildup read that has full inputs.")
        if rc['top_commitment']:
            _notes.append(f"**Heaviest single commitment:** {rc['top_commitment']}.")
        if rc['vol_oi'] is not None and np.isfinite(rc['vol_oi']) and rc['vol_oi'] > 5:
            _notes.append(
                f"**Vol/OI at {rc['vol_oi']:.2f} does not break the tie** — it says the flow is genuine, "
                f"not which direction it favours. High conviction on a conflicted read means both sides "
                f"are committing real money, which is what makes the session dangerous rather than "
                f"what resolves it.")
        for n in _notes:
            st.caption(n)
    with st.expander("Why a disagreement is more useful than either read alone"):
        st.markdown(
            "These two reads share **no inputs**:\n\n"
            "| | Footprint | Buildup |\n"
            "|---|---|---|\n"
            "| Built from | IV skew, ChgPCR, Vol/OI | price vs previous close, OI vs previous OI |\n"
            "| Measures | the vol surface and the flow ratio | strike-by-strike commitment |\n"
            "| Window | today's flow | whole session, day-over-day |\n\n"
            "So when they agree it is genuine corroboration rather than one number counted twice — "
            "and when they disagree, neither is malfunctioning. Positioning and flow are actually "
            "pointing different ways.\n\n"
            "The reason this is a stand-aside rather than a puzzle to solve: on these sessions the "
            "market has not decided either, and price tends to run stops in both directions before "
            "it picks. Sizing down does not help much when the stop is what gets hit; not trading "
            "does.\n\n"
            "**Two known false-conflict cases** worth checking before trusting the flag. On a "
            "vol-crush day, calls get labelled Short Buildup purely from IV decay rather than "
            "direction, which manufactures a bearish Buildup read out of nothing. And on a stale or "
            "weekend snapshot the Buildup is describing the *last* trading session, not now — the "
            "Footprint is closer to live, so the two are measuring different days."
        )
    st.markdown("---")

# ==========================================
# UI — MASTER SIGNAL BANNER
# ==========================================
signal_colors = {
    "Strong CE Buy": "#1e7e34", "PE writers strong": "#28a745",
    "Strong PE Buy": "#c82333", "CE writers strong": "#dc3545",
    "wait for data confirmation": "#6c757d",
}

# ==========================================
# UI — CONFLUENCE SCENARIO CARD (top of app: the decision)
# ==========================================
if show_scenario_card:
    if scenario is None:
        st.markdown("""
<div style='background-color:#6c757d;padding:20px;border-radius:10px;margin:10px 0;'>
<h3 style='color:white;margin:0;'>🎯 Confluence Scenario — waiting for data</h3>
<p style='color:white;margin:6px 0 0 0;'>The IV Lens needs a few minutes of logged Spot + ATM IV
before the environment half of the setup exists.</p>
</div>""", unsafe_allow_html=True)
    else:
        side_txt = f" &nbsp;|&nbsp; Side: <b>{scenario['side']}</b>" if scenario['side'] else ""
        st.markdown(f"""
<div style='background-color:{scenario['color']};padding:22px;border-radius:10px;margin:10px 0;'>
<h2 style='color:white;margin:0;'>{scenario['headline']}</h2>
<p style='color:white;margin:10px 0 0 0;font-size:1.05em;'>{scenario['action']}</p>
<p style='color:white;margin:8px 0 0 0;'>Environment: <b>{scenario['lens_bias'] or 'no read'}</b>
&nbsp;|&nbsp; Flow: <b>{scenario['flow_bias']}</b>{side_txt}
&nbsp;|&nbsp; Confirmations: <b>{scenario['conviction']}/{scenario['conviction_total']}</b></p>
</div>""", unsafe_allow_html=True)
        sc_cols = st.columns(len(scenario['checks']))
        for col, (label, detail, ok) in zip(sc_cols, scenario['checks']):
            with col:
                st.markdown(f"{'✅' if ok else '❌'} **{label}**")
                st.caption(detail)
        for w in scenario['warnings']:
            if scenario['scenario'] == 'C':
                st.error(w)
            elif scenario['scenario'] == 'WAIT':
                st.info(w)
            else:
                st.warning(w)
        with st.expander("How this card decides"):
            st.markdown(
                "| Environment (IV Lens) | Flow (Choi) | Verdict |\n"
                "|---|---|---|\n"
                "| Shakeout / Conviction | Choi_PE > Choi_CE | 🟢 **A — CE Buy** |\n"
                "| Distribution / Fear Bid | Choi_CE > Choi_PE | 🔴 **B — PE Buy** |\n"
                "| Either | flow points the **other** way | ⛔ **C — Stay Away** |\n"
                "| Either | flow inside the neutral band | ⏸️ **WAIT** |\n"
                "| No lens read | anything | ⏸️ **WAIT** |\n"
            )
            st.caption(
                f"Scenarios B and C both key off a bearish environment, so what separates them is whether "
                f"today's flow **confirms** it (B, trade) or **contradicts** it (C, stand aside). PCR is "
                f"standing OI carried from yesterday, so when it disagrees it downgrades conviction rather "
                f"than blocking — a stale number shouldn't veto a live one. Shakeout additionally wants spot "
                f"near the put floor and Fear Bid near the call wall (within "
                f"{scenario_thresholds['level_proximity_strikes']} strikes); missing that is flagged as a "
                f"weaker setup, not a block, because the walls move intraday. "
                + ("Distribution is currently set as an absolute stand-down, so Scenario B can only fire on "
                   "Fear Bid." if distribution_hard_stop else
                   "Distribution currently permits a flow-confirmed Scenario B short; the sidebar toggle "
                   "restores the absolute stand-down.")
            )
    st.markdown("---")

# ------------------------------------------------------------------
# MASTER SIGNAL BANNER — raw read on top, ARBITRATED direction underneath.
# ------------------------------------------------------------------
if hierarchy['tradeable']:
    _net_side = "LONG" if hierarchy['trade_dir'] > 0 else "SHORT"
    _net_mode = hierarchy['gate']['mode'].upper()
    _net_html = (f"<div style='background-color:rgba(0,0,0,0.28);border-radius:8px;"
                 f"padding:10px 14px;margin:12px 0 0 0;'>"
                 f"<span style='color:white;font-size:0.85em;opacity:0.85;'>"
                 f"AFTER HIERARCHY — this is the executable direction</span><br>"
                 f"<b style='color:white;font-size:1.25em;'>{_net_side}</b>"
                 f"<span style='color:white;'> &nbsp;·&nbsp; {_net_mode} the "
                 f"{hierarchy['label'].lower()} read from "
                 f"{hierarchy['source']} (rank {hierarchy['rank']}) "
                 f"&nbsp;·&nbsp; size cap {hierarchy['size_cap']:.0%}</span></div>")
elif hierarchy['source'] is None:
    _net_html = ("<div style='background-color:rgba(0,0,0,0.28);border-radius:8px;"
                 "padding:10px 14px;margin:12px 0 0 0;'>"
                 "<span style='color:white;font-size:0.85em;opacity:0.85;'>"
                 "AFTER HIERARCHY</span><br>"
                 "<b style='color:white;font-size:1.15em;'>NO POSITION</b>"
                 "<span style='color:white;'> &nbsp;·&nbsp; no source in the hierarchy "
                 "qualified, so direction is unknown</span></div>")
else:
    _gate_why = hierarchy['gate']['why']
    _net_html = (f"<div style='background-color:rgba(0,0,0,0.28);border-radius:8px;"
                 f"padding:10px 14px;margin:12px 0 0 0;'>"
                 f"<span style='color:white;font-size:0.85em;opacity:0.85;'>"
                 f"AFTER HIERARCHY</span><br>"
                 f"<b style='color:white;font-size:1.15em;'>NO POSITION</b>"
                 f"<span style='color:white;'> &nbsp;·&nbsp; rank 1 gate closed — "
                 f"{_gate_why}</span></div>")

st.markdown(f"""
<div style='background-color:{signal_colors.get(sig, "#6c757d")};padding:24px;border-radius:10px;
text-align:center;margin:10px 0;'>
<h2 style='color:white;margin:0;'>{sig}</h2>
<p style='color:white;margin:6px 0 0 0;font-size:0.9em;opacity:0.9;'>RAW READ (pre-hierarchy,
no gamma-regime awareness)</p>
<p style='color:white;margin:6px 0 0 0;'>Action: <b>{action or "—"}</b> &nbsp;|&nbsp;
Zone B raw signal: <b>{zb['signal']}</b> &nbsp;|&nbsp; PCR regime: <b>{za['classification']}</b></p>
{_net_html}
</div>""", unsafe_allow_html=True)

# --- IV LENS GATE STRIP (directly under the Master Signal) ---
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
# REAL-TIME CANDLESTICK CHART
# ==========================================
if show_candle_chart:
    st.subheader("1️⃣ 🕯️ Real-Time NIFTY Chart (Candlestick + VWAP + Max Pain)")
    if ohlc_df is not None and not ohlc_df.empty:
        oi_profile = build_oi_profile(df, atm_strike, int(oi_profile_width)) if show_oi_profile else None
        chart_fig = go.Figure()
        profile_max_x = 0.0
        if oi_profile:
            b = oi_profile['band']
            block = STRIKE_STEP * oi_bar_thickness
            if oi_profile_mode.startswith("Combined"):
                chart_fig.add_trace(go.Bar(
                    y=b['Strike'], x=b['Total_OI'], orientation='h', name='Total OI (CE+PE)',
                    marker_color='#8e7cc3', opacity=0.9, width=block, xaxis='x2',
                    hovertemplate='Strike %{y:.0f}<br>Total OI %{x:,.0f}<extra></extra>'))
                profile_max_x = oi_profile['max_total_oi']
            else:
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
        log_df_for_chart = pd.DataFrame(st.session_state.session_log)
        if not log_df_for_chart.empty and 'MaxPain' in log_df_for_chart.columns:
            mp_series = log_df_for_chart.dropna(subset=['MaxPain'])
            if not mp_series.empty:
                mp_times = pd.to_datetime(today_str + ' ' + mp_series['Time'].astype(str)).dt.tz_localize(IST)
                chart_fig.add_trace(go.Scatter(
                    x=mp_times, y=mp_series['MaxPain'], mode='lines', name='Max Pain',
                    line=dict(color='#7b2ff7', width=1.6, dash='dot'),
                ))
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
        if oi_profile and profile_max_x > 0:
            chart_fig.update_layout(xaxis2=dict(
                overlaying='x', side='top', range=[profile_max_x / oi_profile_frac, 0],
                showgrid=False, showticklabels=False, zeroline=False, fixedrange=True))
            t0, t1 = ohlc_df['time'].iloc[0], ohlc_df['time'].iloc[-1]
            step = pd.Timedelta(minutes=int(candle_interval))
            pad = max((t1 - t0) * (oi_profile_frac / (1 - oi_profile_frac)), step * 3)
            chart_fig.update_layout(xaxis=dict(range=[t0 - step, t1 + pad]))
        if show_oi_levels:
            def _level_line(y, color, text, position, yshift=0):
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
                _level_line(mp, '#e8a33d', f"Max Pain {mp:.0f}", 'bottom left', yshift=-20)
        if fit_to_price:
            pad_y = int(oi_pad_strikes) * STRIKE_STEP
            chart_fig.update_yaxes(range=[ohlc_df['low'].min() - pad_y, ohlc_df['high'].max() + pad_y])
        st.plotly_chart(chart_fig, use_container_width=True)
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
# GAMMA EXPOSURE PANEL
# ==========================================
if show_gex_panel:
    st.subheader("2️⃣ ⚡ Gamma Exposure (GEX) — regime: which kind of setup fits today")
    if gex is None:
        st.caption(
            "GEX unavailable this poll — needs a live spot price and non-zero gammas in the chain. "
            "Dhan returns zero greeks outside market hours, so this panel stays blank on a cached "
            "snapshot."
        )
    else:
        st.markdown(f"""
<div style='background-color:{gex_color};padding:18px;border-radius:10px;margin:6px 0;'>
<h4 style='color:white;margin:0;'>{gex_verdict}</h4>
<p style='color:white;margin:8px 0 0 0;'>{gex_detail}</p>
</div>""", unsafe_allow_html=True)
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("Net GEX (₹cr δ / 1% move)", f"{gex['net_gex']:+,.1f}",
                  "long gamma" if gex['net_gex'] > 0 else "short gamma")
        if gex['flip_level']:
            _flip_val = f"{gex['flip_level']:.0f}"
            _flip_note = f"spot {gex['flip_distance']:+.0f} pts"
            if gex.get('flip_search_width', 0) > gex.get('width', 0):
                _flip_note += " (found outside the display band)"
            else:
                _flip_val = f"~{gex['nearest_approach']:.0f}"
                _flip_note = (f"no crossing — closest approach "
                              f"{gex['nearest_approach'] - float(spot):+.0f} pts")
        g2.metric("Gamma Flip Level", _flip_val, _flip_note)
        g3.metric("Net DEX (lakh δ)", f"{gex['net_dex']:+,.1f}",
                  "dealers short index" if gex['net_dex'] < 0 else "dealers long index")
        g4.metric("Regime", "Pin" if gex['regime_key'] == 'pin' else "Trend",
                  "⚠️ unstable — at the flip" if gex['at_flip'] else gex['regime'])
        ps = gex['per_strike']
        gfig = make_subplots(specs=[[{"secondary_y": True}]])
        gfig.add_trace(go.Bar(
            x=ps['Strike'], y=ps['GEX'], name="Net GEX per strike",
            marker_color=np.where(ps['GEX'] >= 0, '#28a745', '#dc3545'),
            hovertemplate="Strike %{x:.0f}<br>GEX %{y:+,.2f} ₹cr/1%<extra></extra>"),
            secondary_y=False)
        gfig.add_trace(go.Scatter(
            x=ps['Strike'], y=ps['cum_GEX'], name="Cumulative GEX", mode='lines',
            line=dict(color='#0d6efd', width=2, dash='dot'),
            hovertemplate="Strike %{x:.0f}<br>cumulative %{y:+,.2f}<extra></extra>"),
            secondary_y=True)
        gfig.add_hline(y=0, line_color="gray", line_width=1)
        if spot:
            gfig.add_vline(x=spot, line_dash="dash", line_color="#6c757d",
                           annotation_text=" spot ", annotation_position="top")
        if gex['flip_level']:
            gfig.add_vline(x=gex['flip_level'], line_dash="solid", line_color="#fd7e14",
                           line_width=2, annotation_text=" γ flip ", annotation_position="top left")
        gfig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10),
                           legend=dict(orientation="h", y=1.15), xaxis_title="Strike",
                           bargap=0.15)
        gfig.update_yaxes(title_text="GEX per strike", secondary_y=False)
        gfig.update_yaxes(title_text="Cumulative", secondary_y=True, showgrid=False)
        st.plotly_chart(gfig, use_container_width=True)
    st.markdown("---")

# ==========================================
# TRADE CONSTRUCTOR
# ==========================================
st.markdown("#### 🎯 Trade Constructor — structure, strikes and size")
if trade['blocked']:
    st.warning(f"**No order.** {trade['blocked']}")
    for _n in trade['notes']:
        st.caption(_n)
else:
    _prem_lbl = "CREDIT" if trade['net_premium'] > 0 else "DEBIT"
    _prem_rs = abs(trade['net_premium']) * LOT_SIZE * trade['lots']
    st.markdown(f"""
<div style='background-color:#0d6efd;padding:18px;border-radius:10px;margin:6px 0;'>
<h3 style='color:white;margin:0;'>{trade['structure']} &nbsp;·&nbsp; {trade['lots']} lot(s)</h3>
<p style='color:white;margin:8px 0 0 0;'>{_prem_lbl} <b>{abs(trade['net_premium']):.1f} pts</b>
(₹{_prem_rs:,.0f}) &nbsp;|&nbsp; Stop <b>₹{trade['stop_loss']:,.0f}</b>
&nbsp;|&nbsp; Max loss <b>₹{trade['max_loss']:,.0f}</b>
&nbsp;|&nbsp; Invalidation <b>{trade['invalidation']}</b></p>
</div>""", unsafe_allow_html=True)
    _leg_rows = [{'Action': a, 'Strike': f"{k:.0f}", 'Type': sd,
                  'LTP': (lambda v: f"{v:.2f}" if v else "—")(_ltp(df, k, sd)),
                  'Qty': f"{int(trade['lots'] * LOT_SIZE)}"}
                 for a, k, sd in trade['legs']]
    st.dataframe(pd.DataFrame(_leg_rows), use_container_width=True, hide_index=True)
    st.caption("Execution order matters: **buy the long legs first**, then sell the shorts. "
               "Reversed, the account is briefly naked short and margin can spike enough to "
               "reject the second leg. Unwind in the opposite order.")
    for _n in trade['notes']:
        st.caption(_n)
    if decision and decision['blocked']:
        _why = " · ".join(r[1] for r in decision['blocked'])
        st.error(f"⛔ The Decision Card below is in STAND DOWN — {_why}. This order is shown "
                 f"for preparation only. Do not send it.")
    st.markdown("---")

# ==========================================
# DECISION CARD
# ==========================================
if decision:
    st.markdown("#### 📋 GEX Decision Card")
    _rule_icon = {'alert': '🔴', 'watch': '🟡', 'ok': '🟢'}
    if decision['blocked']:
        reasons = " · ".join(r[1] for r in decision['blocked'])
        st.error(f"⛔ **STAND DOWN — {reasons}.** The mode below describes the regime, but a "
                 f"discipline rule is breached. Regime tells you which trade; discipline tells "
                 f"you whether to take one at all, and it wins.")
    m = decision['mode']
    st.markdown(f"""
<div style='background-color:{m['color']};padding:20px;border-radius:10px;margin:6px 0;
{"opacity:0.55;" if decision['blocked'] else ""}'>
<p style='color:white;margin:0;font-size:0.85em;letter-spacing:1px;'>STEP 1 — NET GEX SIGN</p>
<h3 style='color:white;margin:4px 0 0 0;'>{m['title']}</h3>
<p style='color:white;margin:10px 0 0 0;'>{m['stance']}</p>
<p style='color:white;margin:10px 0 0 0;'>Instrument: <b>{m['instrument']}</b>
&nbsp;|&nbsp; Size: <b>{m['size']}</b></p>
</div>""", unsafe_allow_html=True)
    st.markdown("**STEP 2 — Levels**")
    if decision['ladder'].empty:
        st.caption("No levels available on this poll.")
    else:
        _lad = decision['ladder'].copy()
        _lad['Price'] = _lad['Price'].map(lambda v: f"{v:,.0f}")
        _lad['Distance'] = _lad['Distance'].map(
            lambda v: "—" if pd.isna(v) else f"{v:+,.0f}")
        st.dataframe(_lad, use_container_width=True, hide_index=True,
                     height=min(360, 60 + 35 * len(_lad)))
    st.markdown("**STEP 3 — Rules**")
    for status, title, detail in decision['rules']:
        st.markdown(f"{_rule_icon[status]} **{title}**")
        st.caption(detail)
    st.markdown("**STEP 4 — Risk**")
    rk = decision['risk']
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Daily risk budget", f"₹{rk['daily_budget']:,.0f}",
              f"{rk['daily_pct']:.2f}% of ₹{rk['capital']:,.0f}")
    k2.metric("Max loss per trade", f"₹{rk['per_trade_cap']:,.0f}",
              f"{rk['trade_pct']:.2f}% of capital")
    k3.metric("Budget used", f"₹{rk['spent']:,.0f}",
              f"{losses_today} loss(es) logged")
    k4.metric("Trades left today", f"{rk['trades_left']}",
              f"₹{rk['remaining']:,.0f} remaining")
    if rk['conflict_note']:
        st.warning(f"⚠️ **Sizing conflict.** {rk['conflict_note']}")
    lc1, lc2, lc3 = st.columns([1, 1, 2])
    with lc1:
        if st.button("➕ Log a loss", use_container_width=True):
            save_loss_counter(today_str, losses_today + 1)
            st.rerun()
    with lc2:
        if st.button("↺ Reset counter", use_container_width=True):
            save_loss_counter(today_str, 0)
            st.rerun()
    with lc3:
        st.caption(
            "The counter is written to a small daily file, so it survives reruns, the 10-second "
            "poll and a browser refresh — which is exactly when it would otherwise get "
            "conveniently forgotten."
        )
    st.markdown("---")

# ==========================================
# BUILDUP DETECTION VIEW
# ==========================================
if show_buildup:
    st.subheader("3️⃣ 🔥 Buildup Detection — direction")
    if bsum is None:
        st.caption(
            "No strike in the band cleared the classification thresholds yet — usually means previous-close "
            "or previous-OI values haven't populated (common right after open, or on a stale snapshot). "
            "Lower the minimums in the sidebar if you want smaller moves classified."
        )
    else:
        st.markdown(f"""
<div style='background-color:{bsum['color']};padding:16px;border-radius:10px;margin:6px 0;'>
<h4 style='color:white;margin:0;'>{bsum['verdict']}</h4>
<p style='color:white;margin:6px 0 0 0;'>Net bias <b>{bsum['net_pct']:+.0f}%</b> (OI-change weighted)
&nbsp;|&nbsp; Heaviest commitment: <b>{bsum['top_leg']} {bsum['top_buildup']}</b> at
<b>{bsum['top_strike']:.0f}</b> ({bsum['top_side']}, {bsum['top_weight']:,.0f} contracts)</p>
</div>""", unsafe_allow_html=True)
        bc1, bc2, bc3 = st.columns(3)
        bc1.metric("Bullish buildup weight", f"{bsum['bull_weight']:,.0f}")
        bc2.metric("Bearish buildup weight", f"{bsum['bear_weight']:,.0f}")
        bc3.metric("Net", f"{bsum['net_pct']:+.0f}%",
                   "OI-change weighted, not a strike count")
        bt = buildup_table
        legs = {"Both legs": ('CE', 'PE'), "CE only": ('CE',), "PE only": ('PE',)}[buildup_view]
        bfig = go.Figure()
        for leg in legs:
            sign = 1 if leg == 'CE' else -1
            for label in ["Long Buildup", "Short Buildup", "Short Covering", "Long Unwinding", "Flat"]:
                sl = bt[bt[f'{leg}_Buildup'] == label]
                bias = BUILDUP_BIAS[leg].get(label, "")
                bias_txt = f" — {bias}" if bias else ""
                pattern = dict(shape=BUILDUP_STYLES[label]['pattern'],
                               fillmode='overlay',
                               fgcolor="rgba(255,255,255,0.55)", size=7, solidity=0.30)
                bfig.add_trace(go.Bar(
                    x=sl['Strike'], y=sl[f'{leg}_OI_chg'] * sign,
                    name=f"{leg} {label}{bias_txt}",
                    marker=dict(color=BUILDUP_BIAS_COLOR[bias], pattern=pattern,
                                line=dict(width=0.5, color='rgba(0,0,0,0.25)')),
                    opacity=0.5 if label == "Flat" else 0.95,
                    hovertemplate=(f"{leg} %{{x:.0f}}<br>{label}{bias_txt}"
                                   "<br>ΔOI %{customdata[0]:,.0f} (%{customdata[1]:+.1f}%)"
                                   "<br>ΔLTP %{customdata[2]:+.1f}%<extra></extra>"),
                    customdata=(np.stack([sl[f'{leg}_OI_chg'], sl[f'{leg}_OI_chg_pct'].fillna(0),
                                          sl[f'{leg}_LTP_chg_pct'].fillna(0)], axis=-1)
                                if not sl.empty else np.empty((0, 3))),
                ))
        if spot:
            bfig.add_vline(x=spot, line_dash="dash", line_color="#6c757d", line_width=1.4,
                           annotation_text=" spot ", annotation_position="top")
            bfig.add_hline(y=0, line_color="gray", line_width=1)
        bfig.update_layout(
            barmode='relative', height=430, margin=dict(l=10, r=10, t=10, b=10),
            legend=dict(orientation="h", y=1.12), xaxis_title="Strike",
            yaxis_title="ΔOI  (CE plotted up · PE plotted down)")
        st.plotly_chart(bfig, use_container_width=True)
        show_cols = ['Strike', 'CE_LTP_chg_pct', 'CE_OI_chg', 'CE_Buildup', 'CE_Bias',
                     'PE_Buildup', 'PE_Bias', 'PE_OI_chg', 'PE_LTP_chg_pct']
        disp = bt[show_cols].copy()
        disp['ATM'] = np.where(disp['Strike'] == atm_strike, '⬅', '')
        disp = disp[['Strike', 'ATM'] + [c for c in show_cols if c != 'Strike']]
        for c in ('CE_Buildup', 'PE_Buildup'):
            disp[c] = disp[c].map(lambda b: BUILDUP_STYLES.get(b, {}).get('label', b))
        def _bias_cell(val):
            return ACTIVE_BUILDUP_TINT.get(val, '')
        def _buildup_cell_by_bias(col):
            leg = 'CE' if col.name.startswith('CE') else 'PE'
            out = []
            for v in col:
                raw = next((k for k, s in BUILDUP_STYLES.items() if s['label'] == v), None)
                out.append(ACTIVE_BUILDUP_TINT.get(BUILDUP_BIAS[leg].get(raw, ''), ''))
            return out
        try:
            sty = disp.style.format({'CE_LTP_chg_pct': '{:+.1f}%', 'PE_LTP_chg_pct': '{:+.1f}%',
                                     'CE_OI_chg': '{:+,.0f}', 'PE_OI_chg': '{:+,.0f}',
                                     'Strike': '{:.0f}'}, na_rep='—')
            sty = sty.apply(_buildup_cell_by_bias, subset=['CE_Buildup', 'PE_Buildup'])
            mf = sty.map if hasattr(sty, 'map') else sty.applymap
            sty = mf(_bias_cell, subset=['CE_Bias', 'PE_Bias'])
            st.dataframe(sty, use_container_width=True, height=420)
        except Exception:
            st.dataframe(disp, use_container_width=True, height=420)
    st.markdown("---")

# ==========================================
# IV LENS PANEL
# ==========================================
if show_iv_lens:
    st.subheader("4️⃣ 🔬 IV Lens — the gate")
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
        st.plotly_chart(
            build_iv_price_chart(iv_measured['series'],
                                 iv_measured['window_start'], iv_measured['window_end']),
            use_container_width=True,
        )
    st.markdown("---")

# ==========================================
# OI VELOCITY PANEL
# ==========================================
if show_velocity_panel:
    st.subheader("5️⃣ 💥 Intraday OI Velocity — is anyone acting right now?")
    if oi_vel is None or vel_class is None:
        st.caption(
            "Velocity needs two consecutive live polls to diff. It appears a few seconds after the "
            "first live fetch of the session, stays blank while the market is closed (there is no "
            "second poll to difference against), and is skipped on the rerun that follows a sidebar "
            "change — that rerun refetches milliseconds after the last poll, which is not a real "
            "interval to measure a rate over."
        )
    else:
        st.markdown(f"""
<div style='background-color:{vel_class['color']};padding:16px;border-radius:10px;margin:6px 0;'>
<h4 style='color:white;margin:0;'>{vel_class['headline']}</h4>
<p style='color:white;margin:6px 0 0 0;'>{vel_class['detail']}</p>
</div>""", unsafe_allow_html=True)
        v1, v2, v3, v4 = st.columns(4)
        v1.metric("CE OI flow", f"{oi_vel['net_ce']:+,.0f}",
                  f"{oi_vel['net_ce_per_min']:+,.0f}/min")
        v2.metric("PE OI flow", f"{oi_vel['net_pe']:+,.0f}",
                  f"{oi_vel['net_pe_per_min']:+,.0f}/min")
        v3.metric("Net bias (PE − CE)", f"{oi_vel['net_bias']:+,.0f}",
                  "put writing leads" if oi_vel['net_bias'] > 0 else "call writing leads")
        v4.metric("Flow intensity", f"{oi_vel['intensity_per_min']:,.0f}/min",
                  (f"{vel_class['rank']:.0f}th pctile of today" if vel_class['rank'] is not None
                   else f"calibrating ({vel_class['samples']}/20 polls)"))
        vfig = go.Figure()
        vfig.add_trace(go.Bar(x=oi_vel['table']['Strike'], y=oi_vel['table']['dCE_OI'],
                              name="ΔCE OI (this interval)", marker_color='#dc3545'))
        vfig.add_trace(go.Bar(x=oi_vel['table']['Strike'], y=oi_vel['table']['dPE_OI'],
                              name="ΔPE OI (this interval)", marker_color='#28a745'))
        vfig.add_hline(y=0, line_color="gray", line_width=1)
        if spot:
            vfig.add_vline(x=spot, line_dash="dash", line_color="#6c757d",
                           annotation_text=" spot ", annotation_position="top")
        vfig.update_layout(barmode='group', height=320, margin=dict(l=10, r=10, t=30, b=10),
                           legend=dict(orientation="h", y=1.18), xaxis_title="Strike",
                           yaxis_title="ΔOI since last poll")
        st.plotly_chart(vfig, use_container_width=True)
    st.markdown("---")

# ==========================================
# EXPECTED MOVE PANEL
# ==========================================
if show_em_panel:
    st.subheader("6️⃣ 🎯 Expected Move — is there room left today?")
    if expected_move is None:
        st.caption("Expected move needs a priced ATM straddle — unavailable on this poll.")
    else:
        em = expected_move
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("ATM straddle", f"{em['straddle']:.1f}",
                  f"{em['ce']:.1f} CE + {em['pe']:.1f} PE")
        e2.metric("1SD move — today", f"±{em['em_today_pts']:.0f}" if em['em_today_pts'] else "—",
                  f"±{em['em_today_pct']:.2f}% of spot" if em['em_today_pct'] else "")
        e3.metric("1SD move — to expiry", f"±{em['em_expiry_pts']:.0f}",
                  f"{em['sd_factor']:.2f} × straddle")
        e4.metric("Range used today",
                  f"{em['range_used_pct']:.0f}%" if em['range_used_pct'] is not None else "—",
                  f"{em['day_range']:.0f} pts of ±{em['em_today_pts']:.0f}" if (em['day_range'] and em['em_today_pts']) else "")
        if em['verdict']:
            st.markdown(f"""
<div style='background-color:{em['verdict_color']};padding:14px 18px;border-radius:10px;margin:6px 0;'>
<p style='color:white;margin:0;'>{em['verdict']}</p>
</div>""", unsafe_allow_html=True)
    st.markdown("---")

# ==========================================
# RISK ENVELOPE PANEL
# ==========================================
if show_risk_panel:
    st.subheader("7️⃣ 🛡️ Risk Envelope — stop, target and size")
    if atr_val is None or not spot:
        st.caption(
            "The risk envelope needs live spot and at least a few candles for ATR. It stays blank "
            "while the market is closed or before the candle feed has populated."
        )
    else:
        r1, r2, r3 = st.columns(3)
        r1.metric(f"ATR ({risk_settings['atr_period']} × {candle_interval}m)", f"{atr_val:.1f} pts")
        r2.metric("Risk budget / trade", f"₹{risk_settings['capital'] * risk_settings['risk_pct'] / 100:,.0f}",
                  f"{risk_settings['risk_pct']:.1f}% of ₹{risk_settings['capital']:,.0f}")
        r3.metric("Active direction",
                  risk_active['side'] if risk_active else "none",
                  ("from Scenario " + scenario['scenario']) if (scenario and scenario.get('scenario') in ('A', 'B'))
                  else (f"from {risk_direction_source}" if risk_direction else "no directional signal"))
        def _render_envelope(env, is_active):
            if env is None:
                st.caption("Envelope unavailable — no priced ATM option on this leg.")
                return
            badge = "🟢 ACTIVE" if is_active else "⚪ reference"
            st.markdown(f"**{env['side']}** — {badge}")
            st.markdown(
                f"| | Level | Distance |\n"
                f"|---|---|---|\n"
                f"| Entry | `{env['entry']:.0f}` | — |\n"
                f"| Stop | `{env['stop']:.0f}` | {env['stop_dist']:.0f} pts ({env['stop_source']}) |\n"
                f"| Target | `{env['target']:.0f}` | {env['target_dist']:.0f} pts ({env['rr']:.1f}R) |\n"
            )
            st.markdown(
                f"**Size: {env['lots']} lot(s)** of the {env['leg']} {atm_strike:.0f} "
                f"@ ₹{env['premium']:.1f} (δ {env['delta']:.2f})"
            )
            for w in env['warnings']:
                st.warning(w)
        rc1, rc2 = st.columns(2)
        with rc1:
            _render_envelope(risk_long, risk_direction > 0)
        with rc2:
            _render_envelope(risk_short, risk_direction < 0)
        if risk_direction == 0:
            st.info(
                "No directional signal is active, so neither envelope is live. They are shown so the "
                "levels are already on screen when one fires — deciding where the stop goes after "
                "entering is how a planned 1% loss becomes a 3% one."
            )
    st.markdown("---")

# ==========================================
# EXECUTION CHECKPOINT
# ==========================================
st.subheader("8️⃣ 🚦 Execution Checkpoint — the last look before acting")
_gates = []
_g = hierarchy['gate']
if not _g['ok']:
    _gates.append(("⛔", "Gamma regime (rank 1)", _g['why'][:1].upper() + _g['why'][1:] + "."))
else:
    _gates.append(("✅", f"Gamma regime (rank 1) — {_g['mode'].upper()}",
                   _g['why'][:1].upper() + _g['why'][1:] + "."))
if hierarchy['source'] is None:
    _gates.append(("⛔", "Direction source",
                   hierarchy['why'][:1].upper() + hierarchy['why'][1:] + "."))
else:
    _icon = {2: "✅", 3: "⚠️", 4: "⚠️"}[hierarchy['rank']]
    _gates.append((_icon, f"Direction — {hierarchy['source']} (rank {hierarchy['rank']})",
                   f"{hierarchy['label']} · {hierarchy['why']}. "
                   f"Size cap {hierarchy['size_cap']:.0%}."))
if hierarchy['tradeable']:
    _side = "LONG" if hierarchy['trade_dir'] > 0 else "SHORT"
    _gates.append(("✅", "Net trade direction",
                   f"{_g['mode'].upper()} the {hierarchy['label'].lower()} read → go {_side}. "
                   f"Instrument per Step 1 of the GEX card."))
else:
    _gates.append(("⛔", "Net trade direction",
                   "Gate closed or no qualified direction — no position."))
if hierarchy['contested']:
    _gates.append(("⚠️", "Contested read", hierarchy['contested']))
if hierarchy['buildup'].get('saturated') and hierarchy['buildup'].get('ok'):
    _gates.append(("⚠️", "Buildup saturated",
                   "One side contributed zero weight, so the net-bias magnitude is "
                   "unreadable. Trade the sign, ignore the strength."))
if not iv_lens:
    _gates.append(("—", "IV Lens gate", "No lens read yet — needs its lookback window to fill."))
elif iv_lens.get('veto'):
    _gates.append(("⛔", "IV Lens gate", f"VETO — {iv_lens['stance']}."))
else:
    _gates.append(("✅", "IV Lens gate", f"Open — {iv_lens['stance']}."))
if vel_class is None:
    _gates.append(("—", "Live flow", "No poll-to-poll delta available."))
elif vel_class['is_burst']:
    _gates.append(("✅", "Live flow", f"{vel_class['headline']}."))
else:
    _gates.append(("⚠️", "Live flow",
                   "Normal flow — nothing is being committed with urgency right now."))
if not expected_move or expected_move.get('range_used_pct') is None:
    _gates.append(("—", "Room left today", "No day range yet."))
elif expected_move['range_used_pct'] >= em_settings['range_spent_high']:
    _gates.append(("⚠️", "Room left today",
                   f"{expected_move['range_used_pct']:.0f}% of the expected range already spent."))
else:
    _gates.append(("✅", "Room left today",
                   f"{expected_move['range_used_pct']:.0f}% of the expected range used; "
                   f"~{max(expected_move['range_left_pts'], 0):.0f} pts unspent."))
if risk_active is None:
    _gates.append(("—", "Position sizing",
                   "No directional signal active, so no envelope is live."))
elif risk_active['lots'] == 0:
    _gates.append(("⛔", "Position sizing",
                   "Zero lots at current settings — the stop is too wide for the risk budget."))
else:
    _gates.append(("✅", "Position sizing",
                   f"{risk_active['lots']} lot(s) {risk_active['leg']}, stop {risk_active['stop']:.0f}, "
                   f"target {risk_active['target']:.0f}, risk ₹{risk_active['risk_at_stop']:,.0f}."))
if decision and decision['blocked']:
    _gates.append(("⛔", "Discipline",
                   " · ".join(r[1] for r in decision['blocked']) + "."))
elif decision:
    _gates.append(("✅", "Discipline", "No rule breached — cutoff and loss limit both clear."))
if risk_size_cap_note:
    _cap_icon = "⛔" if (risk_active and risk_active.get('lots') == 0) else "⚠️"
    _gates.append((_cap_icon, "Position sizing capped", risk_size_cap_note))
_blockers = [g for g in _gates if g[0] == "⛔"]
_cautions = [g for g in _gates if g[0] == "⚠️"]
if _blockers:
    st.markdown(f"""
<div style='background-color:#c82333;padding:20px;border-radius:10px;margin:6px 0;'>
<h3 style='color:white;margin:0;'>⛔ DO NOT EXECUTE — {len(_blockers)} hard blocker(s)</h3>
<p style='color:white;margin:8px 0 0 0;'>{' · '.join(g[1] for g in _blockers)}</p>
</div>""", unsafe_allow_html=True)
elif _cautions:
    st.markdown(f"""
<div style='background-color:#fd7e14;padding:18px;border-radius:10px;margin:6px 0;'>
<h4 style='color:white;margin:0;'>⚠️ PROCEED WITH REDUCED SIZE — {len(_cautions)} caution(s)</h4>
<p style='color:white;margin:8px 0 0 0;'>{' · '.join(g[1] for g in _cautions)}</p>
</div>""", unsafe_allow_html=True)
else:
    st.markdown("""
<div style='background-color:#1e7e34;padding:18px;border-radius:10px;margin:6px 0;'>
<h4 style='color:white;margin:0;'>✅ ALL GATES CLEAR</h4>
<p style='color:white;margin:8px 0 0 0;'>Every check above is satisfied. This is the
configuration the whole workflow exists to identify — and it should be rare.</p>
</div>""", unsafe_allow_html=True)
for icon, label, detail in _gates:
    st.markdown(f"{icon} **{label}** — {detail}")
st.markdown("---")

# ==========================================
# IV TERM STRUCTURE PANEL
# ==========================================
if show_term_panel:
    st.subheader("📐 IV Term Structure (front vs next expiry)")
    if term is None:
        st.caption(
            "Term structure needs a usable ATM IV on both expiries. It stays blank while the market "
            "is closed, on the final expiry in the list (no next expiry to compare against), or if the "
            "second chain fetch failed — that call fails soft so it can never take the dashboard down."
        )
    else:
        t1, t2, t3 = st.columns(3)
        t1.metric(f"Front expiry IV ({dte}d)", f"{term['front_iv']:.2f}",
                  f"{term['d_front']:+.2f} over {iv_lens_thresholds['lookback_minutes']}m" if term['d_front'] is not None else "")
        t2.metric(f"Next expiry IV ({term['next_dte']}d)" if term['next_dte'] is not None else "Next expiry IV",
                  f"{term['next_iv']:.2f}",
                  f"{term['d_next']:+.2f} over {iv_lens_thresholds['lookback_minutes']}m" if term['d_next'] is not None else "")
        t3.metric("Spread (next − front)", f"{term['spread']:+.2f}", term['shape'].split(" — ")[0])
        st.markdown(f"""
<div style='background-color:{term['shape_color']};padding:14px 18px;border-radius:10px;margin:6px 0;'>
<h4 style='color:white;margin:0;'>{term['shape']}</h4>
<p style='color:white;margin:6px 0 0 0;'>{term['shape_detail']}</p>
</div>""", unsafe_allow_html=True)
        if term['divergence'] == 'front_only':
            st.warning(f"⚠️ **Front-expiry noise.** {term['divergence_note']}")
        elif term['divergence'] == 'confirmed':
            st.success(f"✅ **Vol surface confirms.** {term['divergence_note']}")
        else:
            st.caption(
                "No usable IV change to compare yet — the divergence test needs a few minutes of "
                "logged front and next IV. It appears once the log has both columns populated across "
                "the lens window."
            )
    st.markdown("---")

# ==========================================
# INSTITUTIONAL FOOTPRINT SIGNAL
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
                    return FOOTPRINT_TINTS['bearish']
                if val >= footprint_thresholds['iv_skew_bullish']:
                    return FOOTPRINT_TINTS['bullish']
                return ''
            def _vol_oi_cell_color(val):
                if pd.isna(val):
                    return ''
                if val >= footprint_thresholds['vol_oi_fresh']:
                    return FOOTPRINT_TINTS['fresh']
                if val < footprint_thresholds['vol_oi_fakeout']:
                    return FOOTPRINT_TINTS['fakeout']
                return ''
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
    else:
        st.caption("Not enough option chain data yet this poll to compute the Institutional Footprint.")
    st.markdown("---")

# ==========================================
# SIGNAL PROGRESS PANEL
# ==========================================
st.subheader("📐 Signal Progress — what's met, what's still missing")
if sig != "wait for data confirmation":
    st.success(f"**{sig}** is currently active — all conditions for this tier are satisfied.")
elif tier_report:
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
st.markdown("---")

# ==========================================
# HIGHEST-PCR STRIKES
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
# FULL CHAIN TABLE
# ==========================================
st.subheader("📋 Option Chain (ATM ± 10)")
band = df[(df['Strike'] >= atm_strike - 10 * STRIKE_STEP) & (df['Strike'] <= atm_strike + 10 * STRIKE_STEP)]
display_cols = ['CE_Delta', 'CE_IV', 'CE_Volume', 'CE_OI_chg', 'CE_OI', 'CE_LTP',
                'Strike', 'PCR',
                'PE_LTP', 'PE_OI', 'PE_OI_chg', 'PE_Volume', 'PE_IV', 'PE_Delta']
st.dataframe(band[display_cols].style.format(precision=2), use_container_width=True, height=420)

# ==========================================
# SIGNAL PERFORMANCE TRACKER
# ==========================================
if show_tracker_panel:
    st.subheader("📊 Signal Performance Tracker")
    _tracker_log = load_all_logs() if track_all_sessions else pd.DataFrame()
    if _tracker_log.empty:
        _tracker_log = pd.DataFrame(st.session_state.session_log)
    if not _tracker_log.empty:
        _tracker_log = _tracker_log.copy()
        _tracker_log['Date'] = today_str
        grades = grade_signals(_tracker_log, tracker_settings)
        if grades is None:
            st.caption(
                "Not enough logged history to grade anything yet. Each signal needs "
                f"{tracker_settings['horizon_minutes']} minutes of polls *after* it fired before its result "
                "exists, so this panel fills in gradually through the session and gets genuinely useful "
                "after a few days of logs."
            )
        else:
            gt = grades['table']
            st.caption(
                f"Graded **{grades['graded_polls']:,}** polls across **{grades['sessions']}** session(s). "
                f"A 'win' is a **{grades['target']:.0f}+ point** move in the signal's own direction within "
                f"**{grades['horizon']:.0f} minutes**. Baseline drift over the same window across all polls: "
                f"**{grades['baseline']:+.1f} pts** — that is the number every signal has to beat to be "
                f"worth anything at all."
            )
            disp = gt.copy()
            disp['Hit %'] = disp['Hit %'].map(lambda v: f"{v:.0f}%")
            for c in ('Avg move', 'Median', 'Avg MFE', 'Avg MAE', 'Edge vs base'):
                disp[c] = disp[c].map(lambda v: f"{v:+.1f}")
            disp['Signal'] = np.where(gt['Reliable'], disp['Signal'], disp['Signal'] + "  ⚠️")
            st.dataframe(disp.drop(columns=['Reliable']), use_container_width=True, hide_index=True,
                         height=min(420, 60 + 35 * len(disp)))
            st.download_button(
                "Download the grading table (CSV)", gt.to_csv(index=False),
                file_name=f"nifty_signal_grades_{today_str}.csv", mime="text/csv")
    st.markdown("---")

# ==========================================
# SESSION LOG
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
