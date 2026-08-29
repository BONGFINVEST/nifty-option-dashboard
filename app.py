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

# BUILDUP DETECTION settings -- the classic price-vs-OI quadrant read, applied
# per option leg in the Option Chain table:
#
#     Price UP   + OI UP    -> Long Buildup     (fresh buyers)
#     Price DOWN + OI UP    -> Short Buildup    (fresh writers)
#     Price UP   + OI DOWN  -> Short Covering   (writers buying back)
#     Price DOWN + OI DOWN  -> Long Unwinding   (buyers exiting)
#
# Both legs of the comparison are measured over the SAME interval -- option LTP
# vs its previous close, OI vs its previous OI -- so this is a whole-session
# read, not an intraday-fresh one. It won't flip quickly through the day, which
# is correct for this indicator but worth knowing before watching it tick.
DEFAULT_BUILDUP_THRESHOLDS = {
    "price_min_pct": 2.0,   # |LTP change| below this % counts as flat -> unclassified
    "oi_min_pct": 1.0,      # |OI change| below this % of previous OI counts as flat
    "width": 10,            # ATM +- N strikes shown in the buildup view
}


# Lens (environment) with Choi flow (trigger) and PCR (standing OI) into one of:
#   A  Perfect CE Buy    -- lens bullish + flow bullish
#   B  Perfect PE Buy    -- lens bearish + flow bearish
#   C  Stay Away         -- lens and flow disagree (or a hard stand-down fires)
#   WAIT                 -- no environment read, or flow not confirming either way
DEFAULT_SCENARIO_THRESHOLDS = {
    "choi_neutral_band": 5.0,        # |Choi_PE - Choi_CE| below this -> flow is neutral, no trigger
    "level_proximity_strikes": 2,    # within N strikes of the wall counts as "at support/resistance"
    "pcr_bullish": 1.0,              # PCR above this reads bullish on paper (the Scenario C tension)
}


# ==========================================
# INSTITUTIONAL LAYER (new) — six modules that consume data the app already
# fetches but never used, plus the two things every desk has and no retail
# dashboard does: a signal grader and a risk envelope.
#
#   1. GEX / DEX      -- dealer gamma + delta exposure, gamma flip level.
#                        Answers "is today a pin day or a trend day", which is
#                        the question that decides whether a breakout signal
#                        from any other panel is worth acting on.
#   2. OI velocity    -- poll-to-poll ΔOI. The existing OI change is cumulative
#                        since open and cannot distinguish "steady all morning"
#                        from "just detonated in the last 20 seconds".
#   3. Expected move  -- ATM straddle / IV implied range. The market's own
#                        definition of "significant" for today, which the IV
#                        Lens currently has to guess at with a fixed % floor.
#   4. Signal grader  -- forward-return scoring of every signal this app has
#                        ever fired, from the session logs it already writes.
#   5. Term structure -- front vs next expiry ATM IV. Separates a real vol
#                        regime change from front-expiry-only noise, which the
#                        single-expiry IV Lens cannot see.
#   6. Risk envelope  -- ATR, stop/target levels, and premium-based position
#                        sizing. Signal without a risk envelope is not a trade.
# ==========================================
LOT_SIZE = 75          # NIFTY F&O lot size — retire this constant when the exchange changes it again
TRADING_DAYS_YEAR = 252
SESSION_MINUTES = 375  # 09:15–15:30

DEFAULT_GEX_SETTINGS = {
    "width": 15,            # ATM ± N strikes included in the GEX profile
    "lot_size": LOT_SIZE,
    "time_weight": False,   # Dhan's greeks already price in DTE; see compute_gex() docstring
    "flip_near_pct": 0.15,  # spot within this % of the flip = "at the flip", regime unstable
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
    "risk_pct": 1.0,             # % of capital risked per trade
    "max_premium_pct": 25.0,     # cap total premium outlay at this % of capital
}


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


def load_all_logs(max_days: int = 30) -> pd.DataFrame:
    """Every session log on disk, concatenated, with a Date column taken from the
    filename. The signal tracker needs more than one day before a hit rate means
    anything, and each day is already sitting in LOG_DIR as its own CSV.

    Files are read defensively: a log written by an older build has fewer columns,
    and one truncated by a crashed session may have a broken final row. Either is
    skipped rather than being allowed to kill the panel."""
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


def evaluate_confluence_scenario(iv_lens, choi_ce, choi_pe, pcr, spot,
                                 support_strike, resistance_strike, t: dict,
                                 distribution_hard_stop: bool = False):
    """Combines the IV Lens (environment) with Choi flow (trigger) and PCR
    (standing OI) into a single A / B / C / WAIT verdict.

    The organising idea is AGREEMENT vs CONTRADICTION, not the lens state alone:
    Scenario B and Scenario C both fire on a bearish lens, so the thing that
    separates them has to be whether today's flow confirms the environment or
    fights it. Flow confirming -> take the trade (B). Flow contradicting ->
    stand aside (C). PCR is standing OI from yesterday's positions, so when it
    disagrees with both the lens and today's flow it downgrades conviction
    rather than blocking -- otherwise a stale number would veto a live one.

    `distribution_hard_stop` restores the stricter original rule (price down +
    IV up = stand down unconditionally, so B can only fire on Fear Bid).

    Level context: Shakeout is only 'longable at support' and Fear Bid only
    'shortable at resistance', per the spec. Those are reported as conviction
    flags rather than hard gates, because the OI walls move intraday and a
    hard gate on a shifting level would silently suppress valid setups.

    Returns None when there's no lens object at all."""
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

    # --- resolve ---------------------------------------------------------
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

    # --- conviction qualifiers (never flip the verdict, only grade it) ----
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
# BUILDUP DETECTION (new — Option Chain visual: long/short buildup,
# short covering, long unwinding, per option leg)
# ==========================================
# Raw quadrant label -> (glyph label, bar pattern). Note the glyphs are
# deliberately DIRECTION-NEUTRAL (▲▼↺↘ describe the position action, not a
# market view) and the tints live in BUILDUP_BIAS_TINT instead. Colouring by
# buildup type was actively misleading: PE Short Buildup is put writing, which
# is bullish support, but it rendered in the same red as CE Short Buildup.
# Throughout this section colour now means DIRECTION FOR NIFTY, and the type
# is carried by the glyph, the bar pattern and the label text.
BUILDUP_STYLES = {
    "Long Buildup":    {"label": "▲ Long Buildup",    "pattern": ""},
    "Short Buildup":   {"label": "▼ Short Buildup",   "pattern": "/"},
    "Short Covering":  {"label": "↺ Short Covering",  "pattern": "x"},
    "Long Unwinding":  {"label": "↘ Long Unwinding",  "pattern": "."},
    "Flat":            {"label": "· Flat",            "pattern": ""},
    "No data":         {"label": "— No data",         "pattern": ""},
}

# The only colour axis in this section: what it means for the underlying.
BUILDUP_BIAS_COLOR = {"bullish": "#28a745", "bearish": "#dc3545", "": "#adb5bd"}

# MOBILE FIX: these tints MUST set an explicit foreground colour, not just a
# background. A background-only rule leaves the text at whatever colour the
# active Streamlit theme inherits -- dark on desktop light mode (readable on
# #d4edda / #f8d7da), near-white on the mobile app's dark theme, which renders
# CE_Buildup / PE_Buildup / CE_Bias / PE_Bias as white-on-pastel and effectively
# invisible. Pinning colour to black makes those four columns read identically
# in light and dark mode. Rows with no bias ("Flat" / "No data") deliberately
# get NO rule at all, so they keep the theme's own text colour and stay legible
# on a dark background rather than turning into black-on-black.
BUILDUP_TEXT_COLOR = "#000000"
BUILDUP_BIAS_TINT = {
    "bullish": f"background-color: #d4edda; color: {BUILDUP_TEXT_COLOR}; font-weight: 600",
    "bearish": f"background-color: #f8d7da; color: {BUILDUP_TEXT_COLOR}; font-weight: 600",
    "": "",
}

# leg -> raw buildup -> bias for the UNDERLYING (not for the option itself)
BUILDUP_BIAS = {
    'CE': {"Long Buildup": "bullish", "Short Buildup": "bearish",
           "Short Covering": "bullish", "Long Unwinding": "bearish"},
    'PE': {"Long Buildup": "bearish", "Short Buildup": "bullish",
           "Short Covering": "bearish", "Long Unwinding": "bullish"},
}


def classify_buildup(price_chg_pct, oi_chg_pct, t: dict) -> str:
    """The four-quadrant label, with a neutral band on both axes.

    Anything inside the band is 'Flat' rather than being forced into a
    quadrant -- near-zero moves would otherwise flip label on noise and
    make the whole column look busy when nothing is happening.

    Returns 'No data' when the previous close or previous OI is missing,
    which Dhan does return as 0 for illiquid strikes. Treating a 0 previous
    close as a 100% price rise would paint fake Long Buildup across the
    wings, so those are excluded rather than guessed at."""
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
    """Per-strike buildup for both legs across ATM +- width strikes."""
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
    """Rolls the per-strike labels into a directional tally.

    Strikes are weighted by the SIZE of the OI change, not counted equally:
    one strike where 6M contracts were written matters more than four wing
    strikes that moved a few thousand each, and an unweighted count would
    let the thin wings outvote the money."""
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

    # Where the biggest single commitment sits, and on which side of spot.
    top = a.loc[a['weight'].idxmax()]
    side = ("above spot" if spot and top['strike'] > spot else
            "below spot" if spot and top['strike'] < spot else "at spot")

    if net_pct > 20:
        verdict, color = "🟢 Net BULLISH buildup", "#1e7e34"
    elif net_pct < -20:
        verdict, color = "🔴 Net BEARISH buildup", "#c82333"
    else:
        verdict, color = "⚪ Mixed / two-way buildup", "#6c757d"

    # Complete 2x4 matrix, zeros included. Absent categories previously just
    # disappeared, which reads as a broken feature rather than as "no CE Long
    # Buildup happened today" -- a real and fairly common state, e.g. when both
    # legs are bleeding to IV crush and nothing anywhere is being bought.
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
# MODULE 1 — GAMMA (GEX) & DELTA (DEX) EXPOSURE, DEALER GAMMA FLIP LEVEL
# ==========================================
# The app already pulls greeks.gamma and greeks.delta on every 10-second poll and
# throws both away. They answer the one question none of the other panels do:
# whether today's tape amplifies moves or fades them.
#
# Convention (the standard dealer-positioning proxy): dealers are net SHORT the
# options retail and funds buy, so they are short customer calls and short
# customer puts.
#
#   Short call gamma -> dealers must SELL into rallies and BUY into dips to stay
#                       hedged  -> stabilising -> counted NEGATIVE here.
#   Short put gamma  -> dealers must BUY dips and SELL rallies
#                       -> also stabilising  -> counted POSITIVE here.
#
# Net GEX > 0 : long-gamma / PIN regime. Dealer hedging leans against the move.
#               Ranges hold, breakouts fail back toward Max Pain and the high-OI
#               strikes. Fade extremes; discount breakout signals from every
#               other panel in this app.
# Net GEX < 0 : short-gamma / TREND regime. Dealer hedging goes WITH the move and
#               feeds it. This is the regime where the wall-unwind reads, the VWAP
#               streak and a Scenario A/B breakout actually carry through.
#
# The flip level is where the cumulative profile crosses zero — the price at which
# the market changes character. It is the single most useful intraday level this
# app can compute, and it is free from data already on hand.
def compute_gex(df: pd.DataFrame, spot: float, dte, settings: dict):
    """Per-strike and aggregate gamma/delta exposure, plus the gamma flip level.

    Units: GEX is expressed as ₹ crore of dealer delta bought or sold per 1%
    move in the index -- gamma x OI x lot x spot^2 x 1% -- which is the form the
    number is quoted in on a desk. The absolute scale depends on the lot size and
    the OI the feed reports, so treat the SIGN and the FLIP LEVEL as the signal
    and the magnitude as a same-day relative measure, not a cross-day constant.

    TIME WEIGHTING is off by default and deliberately so. Dhan returns a live
    gamma per contract that already embeds time-to-expiry -- 0DTE gammas come
    back an order of magnitude larger than 30DTE gammas on their own. Dividing
    by sqrt(dte) on top of that double-counts the expiry effect and inflates
    expiry-day numbers into meaninglessness. The switch exists because some
    feeds do return an un-normalised gamma; leave it off with Dhan."""
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

    # ₹ crore of delta per 1% index move
    unit = spot * spot * 0.01 * lot / 1e7
    d['CE_GEX'] = -d['CE_Gamma'] * d['CE_OI'] * unit * tw
    d['PE_GEX'] = d['PE_Gamma'] * d['PE_OI'] * unit * tw
    d['GEX'] = d['CE_GEX'] + d['PE_GEX']

    # Dealer net delta: short customer calls (-CE delta) and short customer puts
    # (-PE delta; PE delta is itself negative, so this contributes positively).
    # In lakhs of index-delta units. Large negative = dealers are short the index
    # and must buy it back on strength, which is the mechanical short-squeeze fuel.
    d['DEX'] = (-d['CE_Delta'] * d['CE_OI'] - d['PE_Delta'] * d['PE_OI']) * lot / 1e5

    d = d.sort_values('Strike').reset_index(drop=True)
    d['cum_GEX'] = d['GEX'].cumsum()

    net_gex = float(d['GEX'].sum())
    net_dex = float(d['DEX'].sum())

    # --- Gamma flip: zero-crossing of the cumulative profile, linearly
    # interpolated between the bracketing strikes rather than snapped to one of
    # them, since the true level usually sits inside a 50-point gap. Where the
    # profile crosses more than once, the crossing nearest spot is the one that
    # governs the current regime.
    ks, cum = d['Strike'].values, d['cum_GEX'].values
    crossings = []
    for i in range(1, len(cum)):
        a, b = cum[i - 1], cum[i]
        if (a < 0 <= b) or (a > 0 >= b):
            x = ks[i] if b == a else ks[i - 1] + (ks[i] - ks[i - 1]) * (0 - a) / (b - a)
            crossings.append(float(x))
    flip = min(crossings, key=lambda k: abs(k - spot)) if crossings else None

    dist = (spot - flip) if flip else None
    near_pct = float(settings.get('flip_near_pct', 0.15))
    at_flip = bool(flip and abs(dist) / spot * 100 <= near_pct)

    # Largest single-strike gamma concentrations — where hedging flow clusters.
    peak_pos = d.loc[d['GEX'].idxmax()] if d['GEX'].max() > 0 else None
    peak_neg = d.loc[d['GEX'].idxmin()] if d['GEX'].min() < 0 else None

    return {
        'per_strike': d[['Strike', 'CE_GEX', 'PE_GEX', 'GEX', 'cum_GEX', 'DEX']],
        'net_gex': net_gex, 'net_dex': net_dex,
        'flip_level': flip, 'flip_distance': dist, 'at_flip': at_flip,
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
    """Translates the regime into the one instruction it implies for every other
    panel in this app: are breakout-type reads tradeable right now?

    Returns (verdict, colour, detail). Deliberately advisory -- it is never wired
    into the Master Signal or the Scenario card, exactly like the VWAP and
    Footprint reads, so nothing about the existing decision logic changes."""
    if gex is None:
        return None, None, None
    if gex['at_flip']:
        return ("⚖️ Spot is sitting ON the gamma flip",
                "#fd7e14",
                "Regime is unstable here — the tape can switch between fading and chasing on a "
                "20-point move. This is the worst place to size up; wait for spot to pick a side "
                "of the flip.")
    if gex['regime_key'] == 'trend':
        return ("🚀 Short gamma — breakouts have follow-through",
                "#1e7e34",
                "Dealers are hedging WITH the move. Wall unwinds, VWAP streaks and Scenario A/B "
                "breakouts are worth taking at full size. Stops need room: this is the regime that "
                "produces the fast extended trends, and the fake-looking overshoots are real.")
    return ("🧲 Long gamma — moves get faded",
            "#c82333",
            "Dealer hedging leans against the move, so rallies get sold and dips get bought back "
            "toward the high-OI strikes. Discount every breakout signal in this app today; "
            "range-fade setups toward Max Pain are what the mechanics support.")


# ==========================================
# MODULE 2 — INTRADAY OI VELOCITY (the burst detector)
# ==========================================
# Every OI-change number in this app is `oi - previous_oi`: cumulative since the
# open. That column cannot tell you whether 400k contracts of put writing arrived
# steadily over four hours or landed in the last twenty seconds -- and those are
# completely different pieces of information. The first is background; the second
# is somebody with size acting right now.
#
# The app already holds consecutive chain snapshots in session state. Diffing them
# is nearly free and turns a static level into a rate.
def compute_oi_velocity(df: pd.DataFrame, prev_df, prev_ts, now_ts,
                        atm: float, settings: dict):
    """Poll-to-poll ΔOI across the ATM band, normalised to contracts per minute.

    Returns the net flow on each leg over the interval, the single biggest strike
    burst on each side, and the elapsed seconds -- because a 10-second poll and a
    90-second gap (after a rerun, or a slow API) produce very different raw deltas
    and comparing them unnormalised would be meaningless.

    Strikes are intersected between the two snapshots, so a chain that gains or
    loses strikes between polls degrades to the common set instead of erroring."""
    if df is None or prev_df is None or df.empty or prev_df.empty or prev_ts is None or now_ts is None:
        return None
    try:
        elapsed = (now_ts - prev_ts).total_seconds()
    except Exception:
        return None
    if elapsed <= 0 or elapsed > 900:      # >15 min gap: not a poll-to-poll delta, it's a session gap
        return None
    # Streamlit reruns the whole script on ANY widget change, which triggers a fresh
    # fetch and produces a second "poll" milliseconds after the first. Extrapolating a
    # 0.2-second delta to a per-minute rate multiplies it by 300 and puts a garbage
    # number into the percentile history, which then suppresses every genuine burst
    # for the rest of the session. Anything faster than the poll interval is a rerun,
    # not new information.
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
    v['Net'] = v['dPE_OI'] - v['dCE_OI']     # >0 = put writing / call unwinding = bullish-leaning flow

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
    """Grades this poll's flow against the session's OWN distribution, not a fixed
    contract count. An absolute threshold is unusable here: 50k contracts a minute
    is a firehose on a sleepy Tuesday and background noise on expiry day. The
    percentile self-calibrates; the absolute floor stops a dead tape from
    manufacturing a 'burst' purely out of its own quietness."""
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
# Every "significant" threshold in this app is a hard-coded percentage: the IV
# Lens's 0.10% price floor, the buildup module's 2%, the footprint's flat band.
# Each was calibrated on one volatility regime and quietly changes meaning when
# the regime changes -- which is exactly what the code comment on the adaptive
# floors already admits happened on 14-Aug.
#
# The straddle removes the guesswork. The ATM call plus the ATM put is the price
# the market itself is charging for the move it expects, and it reprices every
# tick. Use it as the yardstick and "significant" is defined by today's market
# rather than by last month's calibration.
def compute_expected_move(df: pd.DataFrame, atm: float, spot: float, atm_iv: float,
                          dte, ohlc_df, settings: dict):
    """ATM straddle, the implied 1SD move to expiry and for the single session
    ahead, breakevens, and how much of today's expected range price has already
    spent.

    Two horizons are returned because they answer different questions. The
    straddle covers expiry (which on a 4-day-out weekly is not today), so the
    one-session number is derived from ATM IV instead:
        1SD_today = spot x IV x sqrt(1/252)
    On expiry day the two converge, which is a useful sanity check on the feed."""
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
    # Fall back to the straddle scaled down by the remaining sessions when the
    # feed gives no usable IV, rather than dropping the panel entirely.
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
    """Converts the day's expected move into the IV Lens's own units: the % of spot
    that a *typical* move over the lens's lookback window should be.

    Moves scale with the square root of time, so a 15-minute slice of a 375-minute
    session is sqrt(15/375) ~ 0.2 of the session's 1SD. Anything smaller than that
    is genuinely noise and the lens is right to stay silent; anything larger is
    genuinely a move, on today's terms rather than on a fixed 0.10%."""
    if not em or not em.get('em_today_pts') or not spot:
        return None
    scaled = em['em_today_pts'] * np.sqrt(max(lookback_minutes, 1) / SESSION_MINUTES)
    pct = scaled / spot * 100
    return float(np.clip(pct, clamp[0], clamp[1]))


# ==========================================
# MODULE 4 — SIGNAL PERFORMANCE TRACKER
# ==========================================
# The app fires signals all day and has never once been asked whether they work.
# Every poll is already logged with a timestamp and a spot price, which means the
# forward return after every signal this app has ever produced is sitting in
# LOG_DIR waiting to be measured. No edge is provable without this, and a signal
# that grades out at 45% over a hundred samples is worth knowing about before it
# costs money rather than after.
SIGNAL_DIRECTION = {
    'Master_Signal': {
        'Strong CE Buy': 1, 'PE writers strong': 1,
        'Strong PE Buy': -1, 'CE writers strong': -1,
    },
    'Scenario': {'A': 1, 'B': -1},
    'IV_Lens_Stance': {'shakeout': 1, 'conviction': 1, 'distribution': -1, 'fear_bid': -1},
    'ZoneB_Signal': {'Buy CE': 1, 'Write PE': 1, 'Buy PE': -1, 'Write CE': -1},
    'VWAP_Trend_Side': {'above': 1, 'below': -1},
}


def _log_timestamps(log_df: pd.DataFrame) -> pd.Series:
    """Session logs store Time as HH:MM:SS and (for multi-day loads) Date as the
    filename stem. Combined into a real timestamp so forward windows can't wrap
    across midnight or across two different sessions."""
    date_part = log_df['Date'] if 'Date' in log_df.columns else pd.Series(
        ['1970-01-01'] * len(log_df), index=log_df.index)
    return pd.to_datetime(date_part.astype(str) + ' ' + log_df['Time'].astype(str), errors='coerce')


def grade_signals(log_df: pd.DataFrame, settings: dict, columns=None):
    """Forward-return scoring of every logged signal.

    For each poll, spot is compared against spot at the end of a fixed forward
    window, with the best and worst excursions inside that window captured too --
    because a signal that eventually pays but first goes 40 points against you is
    not the same trade as one that never draws down, and the closing number alone
    hides the difference.

    Rows too close to the end of a session are dropped rather than graded against
    a truncated window, which would systematically understate every signal fired
    in the last N minutes of the day."""
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
    mfe = np.full(len(d), np.nan)   # best excursion, signed by direction below
    mae = np.full(len(d), np.nan)
    for i in range(len(d)):
        # advance to the last sample inside the window and on the same session
        k = i
        while k + 1 < len(d) and ts[k + 1] - ts[i] <= horizon_s and day[k + 1] == day[i]:
            k += 1
        if k == i or ts[k] - ts[i] < horizon_s * 0.5:
            continue        # window never filled: end of session, or a data gap
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
            # MFE positive, MAE negative, both from the trade's point of view — so a
            # row reading "+14 / -9" is immediately legible as "went 14 my way, but
            # drew down 9 first" without having to remember a sign convention.
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
# The IV Lens reads a single expiry, which makes it blind to the most common false
# positive in the whole panel: the front weekly's IV collapsing into its own expiry
# while the actual volatility surface hasn't moved at all. The lens sees "IV DOWN"
# and pairs it with price to call Shakeout or Conviction, when nothing has happened
# except the calendar.
#
# Comparing the front expiry against the next one separates the two. If both fall,
# vol is genuinely being sold and the lens reading is real. If only the front falls,
# it is expiry mechanics and the lens should be discounted.
def compute_term_structure(front_iv, next_iv, front_dte, next_dte,
                           d_front=None, d_next=None, settings: dict = None):
    """Front vs next expiry ATM IV: the level spread (contango/backwardation) and,
    more usefully intraday, whether a move in front IV is being confirmed by the
    next expiry or is front-only noise."""
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

    # --- Intraday divergence: the part that actually gates the lens ---
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
    """Change in the two logged term-structure IVs across the same rolling window
    the IV Lens uses, so the divergence test and the lens are measured over an
    identical interval. Reuses the session log rather than keeping a second
    in-memory series that would reset on every Streamlit rerun."""
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
# Everything above this line is opinion. This is the part that makes an opinion a
# trade: how far the instrument moves on a normal bar, where the position is wrong,
# and how many lots that permits given a fixed fraction of capital at risk.
#
# Sizing is done in PREMIUM terms via the ATM delta, not in index points, because
# the account is buying options -- a 40-point index stop is not a 40-point premium
# loss, and treating them as equal is how a "1% risk" position turns into a 4% loss.
def compute_atr(ohlc_df: pd.DataFrame, period: int = 14):
    """Wilder-style True Range averaged over N candles of whatever interval the
    chart is set to. Returns points, so a 5-minute ATR and a 15-minute ATR are not
    interchangeable -- the panel labels which one is in use."""
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
    """Stop, target and lot count for one direction.

    The stop is the WIDER of the ATR multiple and the structural level (below the
    put floor for a long, above the call wall for a short), because a stop placed
    inside the wall that everyone else is defending is a stop that gets taken out
    by the very flow the trade is betting on.

    Targets are sanity-checked against the day's remaining expected move: a 2R
    target that sits beyond what the straddle prices for the whole session is not
    a target, it is a wish, and the panel says so."""
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
        structural = None       # wall is already behind price; ATR stop stands alone

    # A wall far enough away stops being a stop and becomes a target. Without this
    # cap, a put floor 300 points below spot produces a 300-point stop, which sizes
    # the position down to zero lots and quietly makes the whole panel useless.
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

    # --- premium leg: which option, what does it cost, what is its delta ---
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
        delta, delta_assumed = 0.5, True   # ATM fallback, flagged so it isn't read as measured

    risk_amount = capital * risk_pct / 100.0
    prem_risk_per_lot = stop_dist * delta * lot          # ₹ lost per lot if the index stop hits
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
    ('iv_lens_alert_stance', None), ('scenario_alert', None),
    # --- institutional layer ---
    # poll_prev_df is deliberately SEPARATE from previous_df. previous_df is the
    # closed-market fallback snapshot and gets overwritten with the current chain
    # on every fetch, so by the time any panel reads it, it is the current poll —
    # useless for a poll-to-poll diff. These two keys hold the genuinely previous
    # poll and its timestamp.
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
    with st.expander("🔥 Buildup Detection (option chain)", expanded=True):
        show_buildup = st.checkbox("Show buildup columns & chart in the chain", value=True)
        buildup_width = st.number_input(
            "Chain band (ATM ± N strikes)", min_value=3, max_value=25,
            value=DEFAULT_BUILDUP_THRESHOLDS['width'])
        buildup_price_min = st.number_input(
            "Min |LTP change| to classify (%)", value=DEFAULT_BUILDUP_THRESHOLDS['price_min_pct'], step=0.5,
            help="Below this the strike is left unclassified rather than forced into a quadrant.")
        buildup_oi_min = st.number_input(
            "Min |OI change| to classify (% of prev OI)", value=DEFAULT_BUILDUP_THRESHOLDS['oi_min_pct'], step=0.5)
        buildup_view = st.radio(
            "Chart", ["Both legs", "CE only", "PE only"], index=0, horizontal=True)
        buildup_high_contrast = st.checkbox(
            "High-contrast table text (mobile)", value=True,
            help="Forces black, semi-bold text on the tinted Buildup/Bias cells. Leave this on if you read "
                 "the app on the mobile app or in dark mode — without it those cells inherit the theme's "
                 "near-white font and vanish against the pale green/pink tint.")

    st.markdown("---")
    with st.expander("🎯 Confluence Scenario (top card)", expanded=True):
        show_scenario_card = st.checkbox("Show the A/B/C decision card at the top", value=True)
        scen_choi_band = st.number_input(
            "Choi neutral band (±%)", value=DEFAULT_SCENARIO_THRESHOLDS['choi_neutral_band'], step=1.0,
            help="|Choi_PE − Choi_CE| inside this band counts as no trigger, so the card waits instead of "
                 "acting on a coin-flip flow reading.")
        scen_level_prox = st.number_input(
            "'At support/resistance' means within N strikes", min_value=1, max_value=8,
            value=DEFAULT_SCENARIO_THRESHOLDS['level_proximity_strikes'])
        scen_wall_width = st.number_input(
            "Wall search band (ATM ± N strikes)", min_value=2, max_value=20, value=OI_PROFILE_WIDTH,
            help="Where to look for the max-CE-OI call wall and max-PE-OI put floor.")
        distribution_hard_stop = st.checkbox(
            "Distribution is an absolute stand-down (blocks Scenario B too)", value=False,
            help="Off (default): a Distribution environment with confirming bearish flow fires Scenario B. "
                 "On: restores your original rule — price down + IV up means no trade at all, in either "
                 "direction, however good the flow looks.")

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

    # ======================================================================
    # INSTITUTIONAL LAYER — the six new modules
    # ======================================================================
    st.markdown("---")
    with st.expander("⚡ Gamma Exposure (GEX / DEX)", expanded=True):
        show_gex_panel = st.checkbox("Show dealer gamma & flip level", value=True)
        gex_width = st.number_input(
            "GEX band (ATM ± N strikes)", min_value=5, max_value=40,
            value=DEFAULT_GEX_SETTINGS['width'],
            help="Wide enough to capture the whole hedging book. Too narrow and the flip level "
                 "jumps around as spot walks between strikes.")
        gex_lot_size = st.number_input(
            "Lot size (contract multiplier)", min_value=1, max_value=1000, value=LOT_SIZE,
            help="NIFTY is 75 at time of writing. Change this the day the exchange revises it — "
                 "every GEX and position-sizing number below scales off it.")
        gex_flip_near = st.number_input(
            "'At the flip' means within ± (%) of spot", value=DEFAULT_GEX_SETTINGS['flip_near_pct'],
            step=0.05, format="%.2f",
            help="Inside this band the regime is unstable and neither the pin nor the trend read "
                 "is trustworthy.")
        gex_time_weight = st.checkbox(
            "Extra sqrt(DTE) time weighting", value=DEFAULT_GEX_SETTINGS['time_weight'],
            help="Leave OFF with Dhan. Its greeks already embed time-to-expiry, so weighting again "
                 "double-counts the expiry effect and makes expiry-day numbers meaningless.")

    st.markdown("---")
    with st.expander("💥 OI Velocity (burst detector)", expanded=True):
        show_velocity_panel = st.checkbox("Show poll-to-poll OI velocity", value=True)
        vel_width = st.number_input(
            "Velocity band (ATM ± N strikes)", min_value=1, max_value=15,
            value=DEFAULT_VELOCITY_SETTINGS['width'])
        vel_pctile = st.slider(
            "Burst = above this percentile of today's flow", min_value=60, max_value=99,
            value=DEFAULT_VELOCITY_SETTINGS['burst_pctile'],
            help="Self-calibrating: an absolute contract threshold is a firehose on a quiet day "
                 "and background noise on expiry.")
        vel_min_contracts = st.number_input(
            "...and at least this many contracts/min", min_value=0, max_value=500_000,
            value=DEFAULT_VELOCITY_SETTINGS['min_burst_contracts'], step=1_000,
            help="Absolute floor so a dead tape can't manufacture a 'burst' out of its own quietness.")

    st.markdown("---")
    with st.expander("🎯 Expected Move (ATM straddle)", expanded=True):
        show_em_panel = st.checkbox("Show straddle / expected move", value=True)
        em_sd_factor = st.number_input(
            "1SD to expiry = straddle ×", value=DEFAULT_EM_SETTINGS['straddle_sd_factor'],
            step=0.05, format="%.2f",
            help="The standard approximation is 0.80. Raise it if you want a more conservative "
                 "(wider) expected range.")
        em_range_high = st.number_input(
            "Range 'spent' at (% of expected)", value=DEFAULT_EM_SETTINGS['range_spent_high'], step=5.0)
        em_range_low = st.number_input(
            "Range 'unused' below (% of expected)", value=DEFAULT_EM_SETTINGS['range_spent_low'], step=5.0)
        em_drive_lens = st.checkbox(
            "Let the straddle set the IV Lens price floor", value=DEFAULT_EM_SETTINGS['drive_lens_floor'],
            help="Replaces the fixed % floor (and the adaptive-percentile one) with the straddle's "
                 "own implied move over the lens lookback, scaled by sqrt(time). This is the "
                 "market's definition of 'significant' for today rather than last month's "
                 "calibration. Overrides both other floor settings while it's on.")

    st.markdown("---")
    with st.expander("📊 Signal Performance Tracker", expanded=True):
        show_tracker_panel = st.checkbox("Show signal grading", value=True)
        track_horizon = st.number_input(
            "Grade forward over (minutes)", min_value=1, max_value=180,
            value=DEFAULT_TRACKER_SETTINGS['horizon_minutes'],
            help="How long after a signal fires its result is measured. Match this to how long you "
                 "actually hold — grading a 15-minute scalp over 60 minutes tells you nothing "
                 "about the scalp.")
        track_target = st.number_input(
            "A 'win' is a move of at least (points)", min_value=1, max_value=500,
            value=DEFAULT_TRACKER_SETTINGS['target_pts'],
            help="In the signal's own direction. Set this to your real target, not a token move — "
                 "hit rate against a 5-point target is close to a coin flip by construction.")
        track_min_samples = st.number_input(
            "Flag a hit rate as unreliable below N samples", min_value=2, max_value=200,
            value=DEFAULT_TRACKER_SETTINGS['min_samples'])
        track_all_sessions = st.checkbox(
            "Include prior sessions from disk", value=True,
            help="One day is not a sample. Reads every CSV in the session-log folder.")

    st.markdown("---")
    with st.expander("📐 IV Term Structure (front vs next expiry)", expanded=True):
        show_term_panel = st.checkbox("Show term structure", value=True,
                                       help="Costs one extra API call per throttle interval.")
        term_throttle = st.number_input(
            "Next-expiry refresh every (seconds)", min_value=30, max_value=600,
            value=DEFAULT_TERM_SETTINGS['throttle_seconds'], step=15,
            help="This is a second full option-chain fetch, so it is deliberately not run on every "
                 "10-second poll.")
        term_backwardation = st.number_input(
            "Backwardation flagged at (IV pts)", value=DEFAULT_TERM_SETTINGS['backwardation_pts'],
            step=0.1, format="%.2f")
        term_div_ratio = st.number_input(
            "Next-expiry must move ≥ this fraction of front", value=DEFAULT_TERM_SETTINGS['divergence_ratio'],
            step=0.05, format="%.2f",
            help="Below this the IV move is front-expiry-only — expiry mechanics, not a vol regime "
                 "change — and the IV Lens read gets discounted.")

    st.markdown("---")
    with st.expander("🛡️ Risk Envelope (ATR / stops / sizing)", expanded=True):
        show_risk_panel = st.checkbox("Show risk & position sizing", value=True)
        risk_capital = st.number_input(
            "Trading capital (₹)", min_value=10_000, max_value=100_000_000,
            value=DEFAULT_RISK_SETTINGS['capital'], step=10_000)
        risk_pct_per_trade = st.number_input(
            "Risk per trade (% of capital)", min_value=0.1, max_value=10.0,
            value=DEFAULT_RISK_SETTINGS['risk_pct'], step=0.1, format="%.1f")
        risk_atr_period = st.number_input(
            "ATR period (candles)", min_value=2, max_value=100,
            value=DEFAULT_RISK_SETTINGS['atr_period'],
            help="Measured on the chart's candle interval, so a 14-period ATR on 5-min candles is "
                 "a 70-minute measure.")
        risk_atr_mult = st.number_input(
            "Stop = ATR ×", value=DEFAULT_RISK_SETTINGS['atr_stop_mult'], step=0.1, format="%.1f")
        risk_struct_cap = st.number_input(
            "Ignore an OI-wall stop beyond N × the ATR stop",
            value=DEFAULT_RISK_SETTINGS['structural_cap_mult'], step=0.5, format="%.1f",
            help="The stop normally widens to sit just beyond the defended wall. Past this multiple "
                 "the wall is too far to be a stop at all — it's a target — and using it would size "
                 "the position down to zero.")
        risk_rr = st.number_input(
            "Target = stop ×", value=DEFAULT_RISK_SETTINGS['reward_multiple'], step=0.5, format="%.1f")
        risk_max_premium = st.number_input(
            "Max premium outlay (% of capital)", min_value=1.0, max_value=100.0,
            value=DEFAULT_RISK_SETTINGS['max_premium_pct'], step=5.0,
            help="A second, independent cap. The risk budget sizes for the stop; this stops a "
                 "cheap-option trade from turning into an oversized premium bet.")

# MOBILE FIX (part 2): the same background-only problem applies to the tinted
# IV_Skew / Vol_OI cells in the Footprint table, so those tints are built here
# with an explicit black foreground too, driven by the same toggle.
_TINT_FG = f"; color: {BUILDUP_TEXT_COLOR}; font-weight: 600" if buildup_high_contrast else ""
FOOTPRINT_TINTS = {
    'bearish': f"background-color: #f8d7da{_TINT_FG}",   # red tint — Put buying
    'bullish': f"background-color: #d4edda{_TINT_FG}",   # green tint — Put writing
    'fresh':   f"background-color: #cfe2ff{_TINT_FG}",   # blue tint — fresh money
    'fakeout': f"background-color: #f8d7da{_TINT_FG}",   # red tint — fakeout risk
}

# Buildup tints follow the same toggle, so switching high-contrast off returns
# to the original background-only behaviour rather than being baked in.
ACTIVE_BUILDUP_TINT = BUILDUP_BIAS_TINT if buildup_high_contrast else {
    "bullish": "background-color: #d4edda", "bearish": "background-color: #f8d7da", "": "",
}

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

buildup_thresholds = {
    "price_min_pct": buildup_price_min, "oi_min_pct": buildup_oi_min, "width": int(buildup_width),
}

scenario_thresholds = {
    "choi_neutral_band": scen_choi_band,
    "level_proximity_strikes": int(scen_level_prox),
    "pcr_bullish": DEFAULT_SCENARIO_THRESHOLDS['pcr_bullish'],
}

pcr_thresholds = {"overbought": overbought_th, "bullish": bullish_th, "bearish": bearish_th}
signal_thresholds = {"strong": strong_diff_th, "mild": mild_diff_th}

# --- institutional layer settings ---
gex_settings = {
    "width": int(gex_width), "lot_size": int(gex_lot_size),
    "time_weight": gex_time_weight, "flip_near_pct": gex_flip_near,
}
velocity_settings = {
    "width": int(vel_width), "burst_pctile": int(vel_pctile),
    "min_burst_contracts": float(vel_min_contracts),
    "min_elapsed_s": DEFAULT_VELOCITY_SETTINGS['min_elapsed_s'],
    "history_cap": DEFAULT_VELOCITY_SETTINGS['history_cap'],
}
em_settings = {
    "straddle_sd_factor": em_sd_factor,
    "range_spent_high": em_range_high, "range_spent_low": em_range_low,
    "drive_lens_floor": em_drive_lens,
}
tracker_settings = {
    "horizon_minutes": int(track_horizon), "target_pts": float(track_target),
    "min_samples": int(track_min_samples),
}
term_settings = {
    "throttle_seconds": int(term_throttle), "backwardation_pts": term_backwardation,
    "divergence_ratio": term_div_ratio,
}
risk_settings = {
    "atr_period": int(risk_atr_period), "atr_stop_mult": risk_atr_mult,
    "structural_cap_mult": risk_struct_cap,
    "reward_multiple": risk_rr, "capital": float(risk_capital),
    "risk_pct": risk_pct_per_trade, "max_premium_pct": risk_max_premium,
    "lot_size": int(gex_lot_size),
}

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

    # --- OI VELOCITY: hand the OUTGOING chain to the velocity module before it is
    # overwritten. previous_df is reassigned on the next line, so anything reading
    # it later in this script sees the current poll, not the previous one. These
    # two lines are the entire reason poll-to-poll ΔOI is computable at all.
    _prev_poll_df = st.session_state.previous_df
    _prev_poll_ts = st.session_state.last_fetch
    if _prev_poll_df is not None and st.session_state.baseline_source == 'live' and _prev_poll_ts is not None:
        st.session_state.poll_prev_df = _prev_poll_df
        st.session_state.poll_prev_ts = _prev_poll_ts

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
# IV TERM STRUCTURE — next-expiry chain (throttled; second API call)
# ==========================================
# Fetched on its own clock rather than on the 10-second OI poll, for the same
# reason the candle endpoint is throttled: this is a full second option chain and
# ATM IV on the next expiry does not move fast enough to justify 360 calls an hour.
next_expiry = None
if show_term_panel and st.session_state.expiry_list:
    _later = [e for e in st.session_state.expiry_list if e > st.session_state.selected_expiry]
    next_expiry = _later[0] if _later else None

if show_term_panel and is_open and next_expiry:
    _stale = (
        st.session_state.next_chain_df is None
        or st.session_state.next_chain_expiry != next_expiry
        or st.session_state.next_chain_fetch_at is None
        or (datetime.now() - st.session_state.next_chain_fetch_at).total_seconds() >= term_settings['throttle_seconds']
    )
    if _stale:
        _n_spot, _n_df, _n_err = fetch_option_chain(next_expiry)
        # Fails soft on purpose: a failed second-expiry call must never take down
        # the whole dashboard, so the panel just reports it as unavailable.
        if _n_err is None and _n_df is not None and not _n_df.empty:
            st.session_state.next_chain_df = _n_df
            st.session_state.next_chain_expiry = next_expiry
            st.session_state.next_chain_fetch_at = datetime.now()

next_chain_df = st.session_state.next_chain_df if st.session_state.next_chain_expiry == next_expiry else None

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
# MODULE 1 — GAMMA EXPOSURE (regime: pin vs trend)
# ==========================================
gex = compute_gex(df, spot, dte, gex_settings) if (spot and show_gex_panel) else None
gex_verdict, gex_color, gex_detail = gex_breakout_discount(gex, spot)

# One-shot toast when the regime flips sides, not on every 10s poll.
if gex:
    _regime_state = 'at_flip' if gex['at_flip'] else gex['regime_key']
    if st.session_state.gex_regime_alert != _regime_state:
        st.toast(f"⚡ Gamma regime: {gex['regime']}", icon="⚡")
        st.session_state.gex_regime_alert = _regime_state
else:
    st.session_state.gex_regime_alert = None

# ==========================================
# MODULE 2 — INTRADAY OI VELOCITY (burst detector)
# ==========================================
oi_vel = compute_oi_velocity(
    df, st.session_state.poll_prev_df, st.session_state.poll_prev_ts,
    st.session_state.last_fetch, atm_strike, velocity_settings
) if (show_velocity_panel and is_open) else None

if oi_vel:
    hist = list(st.session_state.oi_velocity_hist)
    hist.append(float(oi_vel['intensity_per_min']))
    st.session_state.oi_velocity_hist = hist[-velocity_settings['history_cap']:]
vel_class = classify_velocity(oi_vel, st.session_state.oi_velocity_hist, velocity_settings)

# Burst alerts fire on the transition into a burst, so a sustained flurry toasts
# once rather than every ten seconds for two minutes.
if vel_class and vel_class['is_burst']:
    if st.session_state.velocity_burst_alert != vel_class['key']:
        st.toast(f"💥 {vel_class['headline']}", icon="💥")
        st.session_state.velocity_burst_alert = vel_class['key']
elif not (vel_class and vel_class['is_burst']):
    st.session_state.velocity_burst_alert = None

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

# ==========================================
# MODULE 3 — ATM STRADDLE / EXPECTED MOVE
# ==========================================
# Computed here, ahead of the lens measurement, because it can supply the lens's
# price floor. This is the fix for the calibration problem the adaptive-floors
# comment already documents: instead of a fixed 0.10% or a percentile of the
# session's own moves, "significant" becomes what the straddle says it is.
expected_move = compute_expected_move(df, atm_strike, spot, atm_iv, dte, ohlc_df, em_settings)

lens_floor_source = 'adaptive percentile' if iv_lens_thresholds['adaptive_floors'] else 'fixed %'
if em_settings['drive_lens_floor'] and expected_move:
    _straddle_floor = straddle_implied_price_floor(
        expected_move, spot, iv_lens_thresholds['lookback_minutes'])
    if _straddle_floor:
        iv_lens_thresholds = dict(iv_lens_thresholds)
        iv_lens_thresholds['price_significant_pct'] = _straddle_floor
        iv_lens_thresholds['adaptive_floors'] = False   # the straddle floor supersedes both others
        lens_floor_source = 'straddle-implied'

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

# ==========================================
# MODULE 5 — IV TERM STRUCTURE (gates the lens's IV leg)
# ==========================================
# The front expiry's ATM IV is the number the lens already reads. Pairing it with
# the next expiry's is what separates a real vol move from the front weekly simply
# decaying into its own expiry — the single biggest false positive the lens can
# produce, and one it structurally cannot see on its own.
next_atm_iv = compute_atm_iv(next_chain_df, atm_strike, int(iv_price_atm_width)) \
    if next_chain_df is not None else None
try:
    next_dte = (datetime.strptime(next_expiry, "%Y-%m-%d").date() - datetime.now(IST).date()).days \
        if next_expiry else None
except Exception:
    next_dte = None

_d_front, _d_next = measure_term_iv_change(
    list(st.session_state.session_log), today_str, iv_lens_thresholds['lookback_minutes'])
term = compute_term_structure(atm_iv, next_atm_iv, dte, next_dte,
                              _d_front, _d_next, term_settings) if show_term_panel else None

# ==========================================
# CONFLUENCE SCENARIO — IV Lens (environment) + Choi (trigger) + PCR (standing OI)
# ==========================================
# Walls are computed independently of the chart's OI-profile overlay so the card
# keeps working with the candlestick panel switched off.
_scen_walls = build_oi_profile(df, atm_strike, int(scen_wall_width))
scenario = evaluate_confluence_scenario(
    iv_lens, zb['choi_ce'], zb['choi_pe'], za['pcr'], spot,
    _scen_walls['max_pe_strike'] if _scen_walls else None,
    _scen_walls['max_ce_strike'] if _scen_walls else None,
    scenario_thresholds, distribution_hard_stop=distribution_hard_stop,
)

if scenario and scenario['scenario'] in ('A', 'B'):
    if st.session_state.scenario_alert != scenario['scenario']:
        st.toast(f"🎯 {scenario['headline']}", icon="🎯")
        st.session_state.scenario_alert = scenario['scenario']
elif scenario is None or scenario['scenario'] not in ('A', 'B'):
    st.session_state.scenario_alert = None

# ==========================================
# MODULE 6 — RISK ENVELOPE (ATR / stops / targets / sizing)
# ==========================================
# The active direction is taken from the Scenario card when it has one, and from
# the Master Signal otherwise. Both envelopes are still computed either way, so the
# panel can show the trade you're not taking alongside the one you are — the cost
# of the wrong side is usually the more informative number.
atr_val = compute_atr(ohlc_df, risk_settings['atr_period']) if show_risk_panel else None

risk_direction = 0
if scenario and scenario.get('scenario') == 'A':
    risk_direction = 1
elif scenario and scenario.get('scenario') == 'B':
    risk_direction = -1
elif sig in ("Strong CE Buy", "PE writers strong"):
    risk_direction = 1
elif sig in ("Strong PE Buy", "CE writers strong"):
    risk_direction = -1

risk_long = build_risk_envelope(spot, atr_val, expected_move, 1, df, atm_strike,
                                _scen_walls, risk_settings, f"{candle_interval}m") if atr_val else None
risk_short = build_risk_envelope(spot, atr_val, expected_move, -1, df, atm_strike,
                                 _scen_walls, risk_settings, f"{candle_interval}m") if atr_val else None
risk_active = risk_long if risk_direction > 0 else (risk_short if risk_direction < 0 else None)

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
        # --- Confluence Scenario ---
        'Scenario': scenario['scenario'] if scenario else None,
        'Scenario_Side': scenario['side'] if scenario else None,
        'Scenario_Conviction': f"{scenario['conviction']}/{scenario['conviction_total']}" if scenario else None,
        # --- Institutional layer (appended at the end so older logs stay column-aligned;
        #     append_log_row() rewrites the header once when it sees the new keys) ---
        'Net_GEX': round(gex['net_gex'], 2) if gex else None,
        'Net_DEX': round(gex['net_dex'], 2) if gex else None,
        'Gamma_Flip': round(gex['flip_level'], 1) if (gex and gex['flip_level']) else None,
        'Gamma_Regime': (('at_flip' if gex['at_flip'] else gex['regime_key']) if gex else None),
        'OI_Vel_Net_CE': round(oi_vel['net_ce'], 0) if oi_vel else None,
        'OI_Vel_Net_PE': round(oi_vel['net_pe'], 0) if oi_vel else None,
        'OI_Vel_Intensity_min': round(oi_vel['intensity_per_min'], 0) if oi_vel else None,
        'OI_Vel_Burst': vel_class['key'] if (vel_class and vel_class['is_burst']) else None,
        'ATM_Straddle': round(expected_move['straddle'], 2) if expected_move else None,
        'EM_Today_Pts': round(expected_move['em_today_pts'], 1) if (expected_move and expected_move['em_today_pts']) else None,
        'EM_Range_Used_Pct': round(expected_move['range_used_pct'], 1) if (expected_move and expected_move['range_used_pct'] is not None) else None,
        'Term_Front_IV': round(atm_iv, 2) if pd.notna(atm_iv) else None,
        'Term_Next_IV': round(next_atm_iv, 2) if (next_atm_iv is not None and pd.notna(next_atm_iv)) else None,
        'Term_Spread': round(term['spread'], 2) if term else None,
        'Term_Divergence': term['divergence'] if term else None,
        'ATR': round(atr_val, 1) if atr_val else None,
        'Risk_Stop_Dist': round(risk_active['stop_dist'], 1) if risk_active else None,
        'Risk_Lots': risk_active['lots'] if risk_active else None,
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
                "| Environment (IV Lens) | Flow (Choi) | Verdict |\n|---|---|---|\n"
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
# GAMMA EXPOSURE PANEL — the regime question every other panel assumes an answer to
# ==========================================
if show_gex_panel:
    st.subheader("⚡ Gamma Exposure (GEX) — dealer positioning & regime")
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
        g2.metric("Gamma Flip Level",
                  f"{gex['flip_level']:.0f}" if gex['flip_level'] else "—",
                  (f"spot {gex['flip_distance']:+.0f} pts" if gex['flip_level'] else "no zero-crossing in band"))
        g3.metric("Net DEX (lakh δ)", f"{gex['net_dex']:+,.1f}",
                  "dealers short index" if gex['net_dex'] < 0 else "dealers long index")
        g4.metric("Regime", "Pin" if gex['regime_key'] == 'pin' else "Trend",
                  "⚠️ unstable — at the flip" if gex['at_flip'] else gex['regime'])

        if gex['flip_level'] and spot:
            side = "above" if spot > gex['flip_level'] else "below"
            mp_txt = f"Max Pain {mp:.0f}" if mp else "the high-OI strikes"
            st.caption(
                f"Spot is **{side}** the gamma flip at **{gex['flip_level']:.0f}**. Below the flip "
                f"(short gamma) dealers chase moves, so the wall-unwind and VWAP-streak reads in this app "
                f"carry real follow-through. Above it, moves get faded back toward {mp_txt} — discount "
                f"breakout signals and treat the walls as magnets rather than barriers."
            )

        # --- per-strike profile: where the hedging flow actually sits ---
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

        gw1, gw2 = st.columns(2)
        with gw1:
            if gex['gamma_wall']:
                st.metric("Largest positive-gamma strike", f"{gex['gamma_wall']:.0f}",
                          f"{gex['gamma_wall_val']:+,.1f} — strongest pin/magnet")
        with gw2:
            if gex['gamma_pit']:
                st.metric("Largest negative-gamma strike", f"{gex['gamma_pit']:.0f}",
                          f"{gex['gamma_pit_val']:+,.1f} — acceleration zone")

        with st.expander("How GEX is computed and what it does not tell you"):
            st.markdown(
                "**Convention.** Dealers are assumed net short the options that customers buy — short "
                "customer calls and short customer puts. Hedging a short call means selling into "
                "rallies and buying dips; hedging a short put means buying dips and selling rallies. "
                "Both are stabilising, so call gamma is signed negative here and put gamma positive, "
                "and the net tells you whether hedging flow leans with the move or against it.\n\n"
                "| Net GEX | Regime | What it means for this app's other panels |\n|---|---|---|\n"
                "| **> 0** | Long gamma / pin | Dealer hedging fades moves. Breakouts fail back toward "
                "Max Pain and the OI walls. **Discount** Scenario A/B breakouts, VWAP streaks and "
                "wall-unwind reads. |\n"
                "| **< 0** | Short gamma / trend | Dealer hedging feeds the move. Those same signals "
                "carry through, ranges break, and stops need genuine room. |\n"
                "| **near flip** | Unstable | Character can switch on a 20-point move. Worst place to "
                "size up. |\n"
            )
            st.caption(
                f"Units are ₹ crore of dealer delta bought or sold per 1% index move: "
                f"gamma × OI × {gex_settings['lot_size']} × spot² × 1%. Band is ATM ± "
                f"{gex['width']} strikes."
                + (" Extra sqrt(DTE) weighting is ON." if gex['time_weighted'] else "")
                + "\n\n**Three honest caveats.** (1) The short-dealer assumption is a convention, not "
                "an observation — Dhan does not publish who is on which side, and on days when "
                "institutions are net buyers of downside the sign is wrong. (2) The magnitude depends "
                "on lot size and reported OI, so compare it against today's own range, not against a "
                "number you remember from last month. (3) The flip level is interpolated from a "
                "cumulative profile across strikes, which is the standard proxy but not a full "
                "reprice of the book at each hypothetical spot — treat it as a zone of roughly a "
                "strike's width, not a line to the point."
            )
    st.markdown("---")

# ==========================================
# OI VELOCITY PANEL — cumulative OI change turned into a rate
# ==========================================
if show_velocity_panel:
    st.subheader("💥 Intraday OI Velocity (burst detector)")
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

        st.caption(
            f"Measured across the last **{oi_vel['elapsed_s']:.0f}s** between polls, ATM ± "
            f"{oi_vel['width']} strikes. Biggest single-strike moves this interval: "
            f"**CE {oi_vel['ce_burst']:+,.0f}** at {oi_vel['ce_burst_strike']:.0f}, "
            f"**PE {oi_vel['pe_burst']:+,.0f}** at {oi_vel['pe_burst_strike']:.0f}."
        )

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

        with st.expander("Why this is different from the OI change everywhere else in this app"):
            st.markdown(
                "Every other OI-change number here is `oi − previous_oi`: **cumulative since the open**. "
                "That column cannot distinguish 400k contracts of put writing that arrived steadily over "
                "four hours from the same 400k that landed in the last twenty seconds — and those are "
                "opposite pieces of information. The first is background positioning; the second is "
                "someone with size acting *now*.\n\n"
                "This panel differences consecutive chain snapshots instead, so it reads the **rate**. "
                "A burst here that agrees with the Master Signal is that signal being acted on in real "
                "time; a burst that contradicts it is the more important of the two, because standing OI "
                "reflects yesterday and this reflects the last ten seconds."
            )
            st.caption(
                f"'Burst' = flow intensity above the {vel_class['pctile']:.0f}th percentile of today's own "
                f"polls **and** above the {vel_class['floor']:,.0f}/min absolute floor. Both conditions are "
                f"needed: the percentile alone would call the quietest hour of a dead day a burst, since "
                f"something is always in the top 15% of a distribution. "
                + (f"Today's threshold is {vel_class['threshold']:,.0f}/min across {vel_class['samples']} polls."
                   if vel_class['threshold'] else
                   "Still calibrating — needs 20 polls before the percentile means anything.")
            )
    st.markdown("---")

# ==========================================
# EXPECTED MOVE PANEL — the market's own definition of "significant"
# ==========================================
if show_em_panel:
    st.subheader("🎯 Expected Move (ATM straddle)")
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

        lvl1, lvl2 = st.columns(2)
        with lvl1:
            st.markdown("**Today's 1SD envelope**")
            if em['expected_high'] and em['expected_low']:
                st.caption(f"Upper `{em['expected_high']:.0f}` · Lower `{em['expected_low']:.0f}` — "
                           f"roughly a 68% chance the session closes inside this band. A target beyond it "
                           f"is a bet against the option market's own pricing, which is fine, but it should "
                           f"be a deliberate one.")
        with lvl2:
            st.markdown("**Straddle breakevens (to expiry)**")
            st.caption(f"Upper `{em['upper_be']:.0f}` · Lower `{em['lower_be']:.0f}` — beyond these a "
                       f"long straddle bought at ATM makes money. Inside them, premium sellers do. "
                       f"They are also where a directional option buyer stops fighting theta.")

        if em_settings['drive_lens_floor']:
            st.info(
                f"🔗 The IV Lens is currently taking its price floor from this straddle: "
                f"**±{iv_lens_thresholds['price_significant_pct']:.3f}%** over its "
                f"{iv_lens_thresholds['lookback_minutes']}-minute window "
                f"(today's 1SD scaled by √(window ÷ session)). That replaces both the fixed floor and "
                f"the adaptive-percentile one while it's switched on."
            )
        else:
            _suggest = straddle_implied_price_floor(em, spot, iv_lens_thresholds['lookback_minutes'])
            if _suggest:
                st.caption(
                    f"For reference, the straddle implies a typical "
                    f"{iv_lens_thresholds['lookback_minutes']}-minute move of about "
                    f"**±{_suggest:.3f}%** of spot, against the lens's current "
                    f"{lens_floor_source} floor of ±{iv_lens_thresholds['price_significant_pct']:.3f}%. "
                    f"If those are far apart, the lens is either silent all day or firing on noise — "
                    f"the sidebar toggle hands the floor to the straddle."
                )

        with st.expander("What the straddle is actually telling you"):
            st.markdown(
                "The ATM call plus the ATM put is what the market charges for the move it expects. It "
                "reprices every tick, which makes it the only threshold in this dashboard that is "
                "calibrated to *today* rather than to whichever regime happened to be running when a "
                "constant was hard-coded.\n\n"
                "Two horizons, because they answer different questions:\n"
                "- **To expiry** — `0.8 × straddle`. On a weekly with days left, this covers several "
                "sessions, not this one.\n"
                "- **Today** — `spot × IV × √(1/252)`, the one-session 1SD. This is the number to "
                "compare an intraday target against.\n\n"
                "The two converge on expiry day, which doubles as a sanity check on the feed: if they "
                "are far apart with DTE at zero, the IV or the straddle price is stale."
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
# IV TERM STRUCTURE PANEL — front vs next expiry (gates the lens's IV leg)
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

        if next_expiry:
            _age = ((datetime.now() - st.session_state.next_chain_fetch_at).total_seconds()
                    if st.session_state.next_chain_fetch_at else None)
            st.caption(
                f"Next expiry: **{next_expiry}**, refreshed every {term_settings['throttle_seconds']}s"
                + (f" (last fetch {_age:.0f}s ago)." if _age is not None else ".")
            )

        with st.expander("Why single-expiry IV is a blind spot"):
            st.markdown(
                "The IV Lens reads one expiry, which leaves it open to the most common false positive "
                "it can produce: **the front weekly's IV collapsing into its own expiry while the actual "
                "volatility surface has not moved at all.** The lens sees IV DOWN, pairs it with price, "
                "and calls Shakeout or Conviction — when nothing has happened except the calendar.\n\n"
                "| Front IV | Next IV | Read |\n|---|---|---|\n"
                "| ↓ | ↓ | Vol genuinely being sold. Lens reading is **real**. |\n"
                "| ↓ | flat | Expiry decay only. **Discount** the lens's IV leg. |\n"
                "| ↑ | ↑ | Genuine fear bid across the surface. Lens carries full weight. |\n"
                "| ↑ | flat | Event premium in the front expiry alone. |\n\n"
                "**Backwardation** (front above next) means the market expects something inside the "
                "front expiry's life. It is normal on expiry day from gamma alone, and meaningful on any "
                "other day. That premium decays violently once the event passes, which is why long "
                "short-dated options into a known event so often lose money even when the direction "
                "was right."
            )
            st.caption(
                f"Both IVs use the same ATM ± {int(iv_price_atm_width)} strike band and the same "
                f"{iv_lens_thresholds['lookback_minutes']}-minute change window as the lens, so the "
                f"comparison is like-for-like. Divergence fires when the next expiry moves less than "
                f"{term_settings['divergence_ratio']:.2f}× the front's move, or moves the opposite way."
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
# RISK ENVELOPE PANEL — signal + risk envelope is what makes it a trade
# ==========================================
if show_risk_panel:
    st.subheader("🛡️ Risk Envelope (ATR · stops · targets · position size)")
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
                  else ("from Master Signal" if risk_direction else "no directional signal"))

        def _render_envelope(env, is_active):
            if env is None:
                st.caption("Envelope unavailable — no priced ATM option on this leg.")
                return
            badge = "🟢 ACTIVE" if is_active else "⚪ reference"
            st.markdown(f"**{env['side']}** — {badge}")
            st.markdown(
                f"| | Level | Distance |\n|---|---|---|\n"
                f"| Entry | `{env['entry']:.0f}` | — |\n"
                f"| Stop | `{env['stop']:.0f}` | {env['stop_dist']:.0f} pts ({env['stop_source']}) |\n"
                f"| Target | `{env['target']:.0f}` | {env['target_dist']:.0f} pts ({env['rr']:.1f}R) |\n"
            )
            st.markdown(
                f"**Size: {env['lots']} lot(s)** of the {env['leg']} {atm_strike:.0f} "
                f"@ ₹{env['premium']:.1f} (δ {env['delta']:.2f})"
            )
            st.caption(
                f"Premium outlay ₹{env['deployed']:,.0f} · risk at stop ₹{env['risk_at_stop']:,.0f} · "
                f"gain at target ₹{env['gain_at_target']:,.0f}. One lot risks "
                f"₹{env['prem_risk_per_lot']:,.0f}. Binding constraint: **{env['binding']}** "
                f"({env['lots_by_risk']} by risk vs {env['lots_by_capital']} by premium cap)."
                + (f" Stop set {env['structural_label']}."
                   if env['stop_source'] == 'structural' and env['structural_label'] else
                   f" ATR stop ({env['atr_mult']:.1f} × {env['atr']:.0f} = {env['atr_stop_dist']:.0f} pts) governs.")
                + (f" The OI wall is further than {env['structural_cap']:.0f} pts away, so it's a target "
                   f"here rather than a stop." if env['structural_ignored'] else "")
                + (" ⚠️ ATM delta came back empty on this leg, so 0.50 is assumed — the lot count is an "
                   "estimate, not a measurement." if env['delta_assumed'] else "")
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

        with st.expander("How the stop, the target and the size are derived"):
            st.markdown(
                "**Stop** is the wider of two candidates:\n"
                f"- **Volatility**: {risk_settings['atr_stop_mult']:.1f} × ATR on the chart's "
                f"{candle_interval}-minute candles.\n"
                "- **Structural**: just beyond the OI wall on the relevant side — below the put floor "
                "for a long, above the call wall for a short.\n\n"
                "The wider one wins, because a stop placed *inside* the wall everybody else is "
                "defending gets taken out by exactly the flow the trade is betting on. A stop that is "
                "too tight is not a smaller loss; it is the same loss taken more often.\n\n"
                "**Size is computed in premium, not index points.** A 40-point index stop is not a "
                "40-point premium loss — the option only moves by its delta. Treating them as equal is "
                "the single most common way a nominal '1% risk' becomes a 4% one:\n\n"
                "`risk per lot = stop distance × delta × lot size`\n\n"
                "`lots = risk budget ÷ risk per lot`, then capped independently by the maximum premium "
                "outlay, so a cheap far-dated option can't turn into an oversized bet just because each "
                "lot is individually small."
            )
            st.caption(
                "Two things this deliberately does not model. **Theta**: on a 0–1 DTE option the premium "
                "decays whether or not the index moves, so the real loss at a stop hit later in the day "
                "is worse than the delta arithmetic here suggests. **Gamma**: delta is not constant — a "
                "move toward the strike raises it and away lowers it, so the true loss curve is convex "
                "and this linear estimate is the optimistic edge of it. Both errors point the same "
                "direction, which is why the sizing here should be treated as a ceiling rather than a "
                "recommendation."
            )
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

            # MOBILE FIX: these return FOOTPRINT_TINTS entries, which pair each
            # background with an explicit black foreground. Returning a bare
            # background-color left the text at the theme's inherited colour --
            # invisible in the mobile app's dark mode.
            def _iv_skew_cell_color(val):
                if pd.isna(val):
                    return ''
                if val <= footprint_thresholds['iv_skew_bearish']:
                    return FOOTPRINT_TINTS['bearish']   # Put buying
                if val >= footprint_thresholds['iv_skew_bullish']:
                    return FOOTPRINT_TINTS['bullish']   # Put writing
                return ''

            def _vol_oi_cell_color(val):
                if pd.isna(val):
                    return ''
                if val >= footprint_thresholds['vol_oi_fresh']:
                    return FOOTPRINT_TINTS['fresh']     # fresh money
                if val < footprint_thresholds['vol_oi_fakeout']:
                    return FOOTPRINT_TINTS['fakeout']   # fakeout risk
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

# ==========================================
# BUILDUP DETECTION VIEW (new — additive; the plain chain above is unchanged)
# ==========================================
if show_buildup:
    st.markdown("### 🔥 Buildup Detection")
    buildup_table = compute_buildup_table(df, atm_strike, int(buildup_width), buildup_thresholds)
    bsum = summarize_buildup(buildup_table, spot) if not buildup_table.empty else None

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

        # --- Complete 2x4 breakdown, zeros shown explicitly -------------------
        st.markdown("**All four buildup types, both legs** — a dash here means that "
                    "combination genuinely didn't occur in the band, not that it's missing.")
        grid_rows = []
        for lab in ("Long Buildup", "Short Buildup", "Short Covering", "Long Unwinding"):
            # Column names are FIXED at 'CE'/'PE'. Naming them by bias instead
            # silently split the grid into four columns, because bias flips
            # between rows for the same leg (CE Long is bullish, CE Short is
            # bearish), so each row produced different column keys and pandas
            # unioned them with None-filled gaps.
            row = {'Buildup type': BUILDUP_STYLES[lab]['label']}
            for leg in ('CE', 'PE'):
                m = bsum['matrix'][(leg, lab)]
                mark = "🟢" if m['bias'] == 'bullish' else "🔴"
                row[f'{leg} leg'] = (
                    f"{mark} {m['bias']} · —" if m['strikes'] == 0
                    else f"{mark} {m['bias']} · {m['weight']:,.0f} · "
                         f"{m['strikes']} strike(s) · top {m['top_strike']:.0f}")
            grid_rows.append(row)
        st.dataframe(pd.DataFrame(grid_rows, columns=['Buildup type', 'CE leg', 'PE leg']),
                     use_container_width=True, hide_index=True)

        absent = [f"{leg} {lab}" for (leg, lab), m in bsum['matrix'].items() if m['strikes'] == 0]
        if absent:
            st.caption(
                f"Not present in this band right now: {', '.join(absent)}. That's common — e.g. on a day "
                f"where both legs are bleeding to IV crush or theta, nothing is being *bought* anywhere, so "
                f"neither leg shows Long Buildup and you only see writing and unwinding."
            )

        # --- Per-strike buildup chart: OI change, coloured by buildup type -----
        bt = buildup_table
        legs = {"Both legs": ('CE', 'PE'), "CE only": ('CE',), "PE only": ('PE',)}[buildup_view]
        bfig = go.Figure()
        for leg in legs:
            # CE plotted upward, PE downward, so the two sides read as a
            # diverging profile against the strike ladder instead of overlapping.
            sign = 1 if leg == 'CE' else -1
            for label in ["Long Buildup", "Short Buildup", "Short Covering", "Long Unwinding", "Flat"]:
                sl = bt[bt[f'{leg}_Buildup'] == label]
                bias = BUILDUP_BIAS[leg].get(label, "")
                bias_txt = f" — {bias}" if bias else ""
                # Empty categories are still added, so the legend always shows all
                # four types per leg. Dropping them silently made a genuinely absent
                # category look like a missing feature.
                pattern = dict(shape=BUILDUP_STYLES[label]['pattern'],
                               # fillmode MUST be 'overlay'. The Plotly default is
                               # 'replace', under which marker.color becomes the
                               # PATTERN colour rather than the fill -- with a white
                               # fgcolor that renders the bars white on transparent,
                               # i.e. completely invisible.
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
        st.caption("CE bars point up, PE bars down, so each strike shows both legs without overlapping. "
                   "**Colour = direction for NIFTY** (green bullish, red bearish), bar height = today's OI "
                   "change, fill pattern = buildup type: solid ▲ Long Buildup, diagonal ▼ Short Buildup, "
                   "cross ↺ Short Covering, dotted ↘ Long Unwinding. So a large red bar pointing down is "
                   "put *buying*, while a large green bar pointing down is put *writing* — both are PE, "
                   "opposite messages.")

        # --- Colour-coded per-strike table ------------------------------------
        show_cols = ['Strike', 'CE_LTP_chg_pct', 'CE_OI_chg', 'CE_Buildup', 'CE_Bias',
                     'PE_Buildup', 'PE_Bias', 'PE_OI_chg', 'PE_LTP_chg_pct']
        disp = bt[show_cols].copy()
        disp['ATM'] = np.where(disp['Strike'] == atm_strike, '⬅', '')
        disp = disp[['Strike', 'ATM'] + [c for c in show_cols if c != 'Strike']]
        for c in ('CE_Buildup', 'PE_Buildup'):
            disp[c] = disp[c].map(lambda b: BUILDUP_STYLES.get(b, {}).get('label', b))

        # MOBILE FIX: ACTIVE_BUILDUP_TINT carries "color: #000000" alongside the
        # background, so CE_Bias / PE_Bias / CE_Buildup / PE_Buildup stay black-on-
        # pastel in the mobile app's dark theme instead of inheriting near-white
        # text. Untinted cells (Flat / No data) are left alone so they keep the
        # theme's own readable colour.
        def _bias_cell(val):
            return ACTIVE_BUILDUP_TINT.get(val, '')

        def _buildup_cell_by_bias(col):
            """Tint the Buildup cell using its OWN leg's bias, so the label and
            the colour on a single row can never disagree — a PE Short Buildup
            row is green because put writing is bullish, even though 'Short'
            reads bearish at a glance."""
            leg = 'CE' if col.name.startswith('CE') else 'PE'
            out = []
            for v in col:
                raw = next((k for k, s in BUILDUP_STYLES.items() if s['label'] == v), None)
                out.append(ACTIVE_BUILDUP_TINT.get(BUILDUP_BIAS[leg].get(raw, ''), ''))
            return out

        # Same manual-CSS approach as the Footprint table (no matplotlib on
        # Streamlit Cloud), wrapped so a styling hiccup degrades to a plain table.
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

        if buildup_high_contrast:
            st.caption(
                "ℹ️ Tinted cells are forced to black text so they stay readable in the mobile app's dark "
                "theme — without that they inherit a near-white font and disappear against the pale tint. "
                "Turn off *High-contrast table text* in the sidebar to revert to theme-default text."
            )

        with st.expander("How to read this"):
            st.markdown(
                "| Option price | Open interest | Label | On a CALL | On a PUT |\n|---|---|---|---|---|\n"
                "| ↑ Up | ↑ Up | ▲ **Long Buildup** | 🟢 Bullish — call buying | 🔴 Bearish — put buying |\n"
                "| ↓ Down | ↑ Up | ▼ **Short Buildup** | 🔴 Bearish — call writing | 🟢 Bullish — put writing |\n"
                "| ↑ Up | ↓ Down | ↺ **Short Covering** | 🟢 Bullish — call writers buying back | 🔴 Bearish — put writers buying back |\n"
                "| ↓ Down | ↓ Down | ↘ **Long Unwinding** | 🔴 Bearish — call buyers exiting | 🟢 Bullish — put buyers exiting |\n\n"
                "**The same label flips meaning between the legs**, which is why colour here always means "
                "direction for NIFTY rather than buildup type. Short Buildup on a call is writers capping "
                "upside; Short Buildup on a put is writers defending a floor. Identical label, opposite "
                "message — so the bars are green or red by what it does to the index, and the glyph and "
                "fill pattern tell you which of the four types produced it."
            )
            st.caption(
                f"Both legs are measured over the same interval — option LTP vs its previous close, OI vs "
                f"its previous OI — so this is a whole-session read and won't flip quickly intraday. "
                f"Strikes below ±{buildup_price_min:.1f}% price or ±{buildup_oi_min:.1f}% OI change are left "
                f"unclassified. One caveat worth holding: option prices move on IV and theta as well as "
                f"direction, so a call can lose value on a flat-to-up day purely from vol collapse and get "
                f"labelled Short Buildup. Cross-check against the Footprint and the walls before acting on a "
                f"single strike — the aggregate net bias above is far more robust than any one row."
            )

st.markdown("---")

# ==========================================
# SIGNAL PERFORMANCE TRACKER — the panel that makes the rest falsifiable
# ==========================================
if show_tracker_panel:
    st.subheader("📊 Signal Performance Tracker")

    # Prefer the on-disk logs when multi-session grading is on, but fall back to the
    # in-memory session log — on a fresh Streamlit Cloud deploy the log folder is
    # empty and grading only today's polls is better than grading nothing.
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

        thin = gt[~gt['Reliable']]
        if not thin.empty:
            st.caption(
                f"⚠️ marks a signal with fewer than {grades['min_samples']} samples. Those rows are "
                f"shown for completeness, not for decisions — a 100% hit rate on three occurrences is "
                f"not evidence of anything."
            )

        reliable = gt[gt['Reliable']]
        if not reliable.empty:
            best = reliable.iloc[0]
            worst = reliable.iloc[-1]
            b1, b2 = st.columns(2)
            with b1:
                st.success(
                    f"**Best-graded signal: {best['Signal']}** ({best['Source']}) — "
                    f"{best['Hit %']:.0f}% hit rate over {int(best['N'])} samples, "
                    f"average {best['Avg move']:+.1f} pts, edge {best['Edge vs base']:+.1f} vs baseline."
                )
            with b2:
                if worst['Edge vs base'] < 0:
                    st.error(
                        f"**Worst-graded signal: {worst['Signal']}** ({worst['Source']}) — "
                        f"{worst['Hit %']:.0f}% hit rate over {int(worst['N'])} samples, "
                        f"average {worst['Avg move']:+.1f} pts, edge {worst['Edge vs base']:+.1f}. "
                        f"On this evidence it is worse than doing nothing."
                    )
                else:
                    st.info(
                        f"Every reliable signal currently grades positive against the baseline. "
                        f"Weakest is **{worst['Signal']}** at {worst['Edge vs base']:+.1f} pts of edge."
                    )

        st.download_button(
            "Download the grading table (CSV)", gt.to_csv(index=False),
            file_name=f"nifty_signal_grades_{today_str}.csv", mime="text/csv")

        with st.expander("How to read this, and how to lie to yourself with it"):
            st.markdown(
                "**Columns.**\n"
                "- `Hit %` — share of occurrences that reached the target within the window, in the "
                "signal's own direction.\n"
                "- `Avg move` / `Median` — forward move signed by the signal's direction, so a bearish "
                "signal followed by a fall shows as positive. The median matters when one outlier "
                "session carries the mean.\n"
                "- `Avg MFE` / `Avg MAE` — the best and worst excursions *inside* the window. A signal "
                "that ends +15 but first goes −40 is not tradeable at the stop sizes in the risk panel, "
                "and the closing number alone hides that completely.\n"
                "- `Edge vs base` — average move minus the baseline drift over the same window. This is "
                "the only column that matters. On a strongly trending day every bullish signal in the "
                "app grades well, and none of them added anything.\n\n"
                "**Four ways this will mislead you.**\n"
                "1. **Overlapping samples.** Polls are 10 seconds apart, so a signal that stays on for "
                "an hour contributes ~360 highly correlated rows. Read `N` as duration, not as "
                "independent trades.\n"
                "2. **No costs.** Spread, slippage and theta are not modelled. A 20-point edge on a "
                "position with a 3-point spread is a 17-point edge at best.\n"
                "3. **Spot, not premium.** Forward moves are index points. What the option actually did "
                "depends on delta and on what IV did over the same window.\n"
                "4. **Selection.** Retuning a threshold until its signal grades well is curve-fitting "
                "against a few sessions of your own logs. The honest use is to catch signals that are "
                "clearly *not* working, which is a much lower bar for evidence than confirming one is."
            )
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
