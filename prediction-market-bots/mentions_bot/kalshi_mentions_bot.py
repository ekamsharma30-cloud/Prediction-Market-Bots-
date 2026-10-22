#!/usr/bin/env python3
"""
Kalshi mention-market PAPER-trading bot. Never places a real order.

STRATEGY - mention long-shot maker
  Kalshi lists "mention" markets ("Will <speaker> say <word> during <event>?"). Many words trade
  as cheap long shots. On each event day at 8:00 AM ET the bot takes one snapshot of the event and,
  for every word whose best YES ask is 2c-30c, rests a paper order that SELLS YES at that ask
  (= a NO bid at 1 - ask), joining the BACK of the queue at that price. It holds to settlement.
  Rationale: across prediction markets, cheap long shots tend to win less often than their price
  implies (favourite-longshot bias), and makers (resting orders) out-earn takers.

HOW FILLS ARE DECIDED (no assumptions - everything comes from Kalshi's real public data):
  * At placement the bot reads the live order book and records how many contracts are already
    resting at our price. We are behind all of them (price-time priority).
  * Every real trade printed afterwards is read from the public trade tape, with its size:
      - trades at our price that hit our side eat the queue ahead of us first; only the
        leftover contracts fill us (partial fills are tracked);
      - a trade that prints THROUGH our price means our whole level was cleared -> full fill.
  * The book is re-read regularly. Growth at our price = new orders behind us; shrinkage not
    explained by trades = cancels, split pro-rata between ahead and behind (QUEUE model).
    QUEUE-SAFE assumes every cancel was behind us (worst case).
  * Unfilled contracts are cancelled when the market closes. P&L uses only filled contracts,
    Kalshi's real settlement, and the series' own fee schedule.
  Four fill models are reported side by side: QUEUE (realistic), QUEUE-SAFE, STRICT
  (trade-through only) and TOUCH (front of queue, optimistic).

RISK CONTROLS
  * Paper account with a starting bankroll; quarter-Kelly sizing on current equity, max 5% per order.
  * Resting orders reserve cash like Kalshi does; signals are skipped if cash is tied up.
  * All words of ONE event (one speech/call) may reserve at most 20% of equity, cheapest words first.
  * "Event does not qualify" (cancellation) markets are skipped.

Usage:
    python3 kalshi_mentions_bot.py selftest       # connectivity + live parsing check
    python3 kalshi_mentions_bot.py run            # run forever (Ctrl-C to stop; safe to restart)
    python3 kalshi_mentions_bot.py report [--csv]
    dashboard: open http://127.0.0.1:8765 while `run` is going (or run `dashboard` on its own)

Only public endpoints; no API key, no login. Standard library only, Python 3.8+.
Educational paper-trading tool. Not financial advice.
"""
import argparse
import csv
import json
import math
import os
import random
import re
import sqlite3
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # very old Python: EDT is in effect until Nov 1 2026
    ET = timezone(timedelta(hours=-4))


BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "paper_mentions.db")
LOG_PATH = os.path.join(HERE, "bot.log")
UA = "kalshi-mentions-paper-bot/1.0"

# ---------------- settings (edit here) ----------------
START_BANK = 500.0         # starting paper bankroll ($)
KELLY_MULT = 0.25          # quarter Kelly
MAX_FRAC = 0.05            # never commit more than 5% of equity to one order
MAX_QTY = 1000             # hard cap on contracts per order
EDGE = {"mentions": 0.041} # edge per contract ($) assumed for Kelly sizing (from the backtest; held fixed)
MAX_EVENT_FRAC = 0.20      # all orders on ONE event may reserve at most 20% of equity
MENTION_SKIP_LABELS = ("does not qualify",)   # "Event does not qualify" = cancellation market, not a word
MENTION_BUCKETS = [(0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.30)]   # YES-price buckets for calibration
MENTION_YES_ASK_MIN = 0.02
MENTION_YES_ASK_MAX = 0.30
MENTION_ENTRY_HOUR_ET = 8  # each event is snapshotted once, first poll at/after this hour on event day
MENTION_ALL_SERIES = True  # every mention series (False = only MENTION_CORE)
MENTION_POLL_SEC = 300     # core series
MENTION_SCAN_SEC = 1800    # full scan of all open mention events
MENTION_MAINT_SEC = 60     # re-read book + tape for working orders
MAKER_FEE_RATE = 0.0175    # only applied if the series fee_type mentions maker fees

MENTION_CORE = ["KXTRUMPMENTION", "KXTRUMPMENTIONB"]
MENTION_WEAK_WORDS = ("VANCE", "FIGHT", "PERSON")   # were negative in the backtest (reported separately)
EPS = 5e-4

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


# ---------------- utilities ----------------
def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


_ctx = None


def ssl_ctx():
    global _ctx
    if _ctx is None:
        try:
            import certifi  # optional
            _ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx = ssl.create_default_context()
    return _ctx


_last_call = [0.0]


class SSLProblem(Exception):
    pass


def http_get(path, params=None, tries=4):
    """GET a public endpoint. Returns parsed JSON, or None on 404 / repeated failure."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        gap = time.time() - _last_call[0]
        if gap < 0.07:  # <= ~14 requests/sec, under the public limit
            time.sleep(0.07 - gap)
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20, context=ssl_ctx()) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(2 * (i + 1))
                continue
            log("HTTP %s on %s" % (e.code, url))
            return None
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise SSLProblem(str(e))
            time.sleep(2 * (i + 1))
        except (OSError, ValueError):
            time.sleep(2 * (i + 1))
    return None


def get_all(path, params, key, max_pages=20):
    out, cursor = [], None
    for _ in range(max_pages):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        d = http_get(path, p)
        if not d:
            break
        out += d.get(key) or []
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


_ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


def parse_ts(s):
    """ISO time -> float epoch seconds, keeping sub-second precision."""
    if not s:
        return None
    m = _ts_re.match(str(s).strip())
    if not m:
        return None
    try:
        tz = m.group(3) or "Z"
        dt = datetime.fromisoformat(m.group(1) + ("+00:00" if tz == "Z" else tz))
        return dt.timestamp() + (float(m.group(2)) if m.group(2) else 0.0)
    except Exception:
        return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def px(obj, name):
    """Price in dollars from either the *_dollars string field or the legacy cents field."""
    v = fnum(obj.get(name + "_dollars"))
    if v is not None:
        return v
    v = obj.get(name)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 100.0
    return None


def trade_count(tr):
    c = fnum(tr.get("count_fp"))
    if c is None:
        c = fnum(tr.get("count"))
    return c or 0.0


def event_date(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return datetime(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))).date()
    except ValueError:
        return None


def mention_group(series):
    s = (series or "").upper()
    if s in MENTION_CORE:
        return "trump"
    if any(w in s for w in MENTION_WEAK_WORDS):
        return "weak"
    if "EARN" in s:
        return "earnings"
    return "other"


# ---------------- order book ----------------
def book_levels(d, side):
    """{price: size} of resting BIDS for `side` ('yes' or 'no') from an orderbook response."""
    out = {}
    if not d:
        return out
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    lv = ob.get(side + "_dollars")
    if lv:
        for p, q in lv:
            p, q = fnum(p), fnum(q)
            if p is not None and q is not None:
                out[round(p, 4)] = out.get(round(p, 4), 0.0) + q
        return out
    for p, q in ob.get(side) or []:
        if isinstance(p, (int, float)) and q is not None:
            out[round(p / 100.0, 4)] = out.get(round(p / 100.0, 4), 0.0) + float(q)
    return out


def size_at(levels, price):
    for p, q in levels.items():
        if abs(p - price) < EPS:
            return q
    return 0.0


def get_book(ticker):
    return http_get("/markets/%s/orderbook" % ticker)


# ---------------- database ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY,
  strategy TEXT, grp TEXT, series TEXT, event TEXT, ticker TEXT, label TEXT,
  side TEXT, limit_px REAL, qty REAL, fee_rate REAL,
  placed_ts REAL, close_ts REAL, yes_bid REAL, yes_ask REAL, entry_note TEXT,
  q_ahead0 REAL, q_ahead REAL, q_behind REAL DEFAULT 0,  -- realistic queue: ahead of / behind us
  qc_ahead REAL, fill_qc REAL DEFAULT 0, fill_qc_ts REAL,   -- worst-case queue model
  fill_q REAL DEFAULT 0,              -- QUEUE model filled contracts
  fill_q_ts REAL, fill_q_full_ts REAL,
  strict_ts REAL,                     -- first trade THROUGH our price
  touch_ts REAL,                      -- first trade at/through our price on our side
  vol_at REAL DEFAULT 0,              -- contracts traded at our price on our side after placement
  last_trade_ts REAL, last_ids TEXT, book_checks INTEGER DEFAULT 0,
  status TEXT,                        -- working | done (market closed, awaiting result) | settled | void
  result TEXT, settled_ts REAL,
  pnl_queue REAL, pnl_safe REAL, pnl_strict REAL, pnl_touch REAL,   -- dollars, net of fees
  UNIQUE(strategy, ticker));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def kv_get(conn, k, default=None):
    r = conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def kv_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))
    conn.commit()


def exists(conn, strategy, ticker):
    return conn.execute("SELECT 1 FROM orders WHERE strategy=? AND ticker=?",
                        (strategy, ticker)).fetchone() is not None


def bank_state(conn, strategy="mentions"):
    """Paper account for one strategy, mirroring how Kalshi holds cash:
    resting orders reserve price x size; filled contracts stay locked until settlement."""
    realized = conn.execute("SELECT COALESCE(SUM(pnl_queue),0) FROM orders WHERE strategy=? AND status='settled'",
                            (strategy,)).fetchone()[0]
    reserved = 0.0
    for r in conn.execute("SELECT status, limit_px, qty, fill_q FROM orders WHERE strategy=? AND status IN "
                          "('working','done')", (strategy,)):
        reserved += r["limit_px"] * (r["qty"] if r["status"] == "working" else (r["fill_q"] or 0))
    withdrawn = kv_get(conn, "withdrawn_" + strategy, 0.0)
    equity = START_BANK + realized - withdrawn
    return dict(equity=equity, reserved=reserved, cash=equity - reserved, withdrawn=withdrawn, realized=realized)


def event_reserved(conn, strategy, event):
    tot = 0.0
    for r in conn.execute("SELECT status, limit_px, qty, fill_q FROM orders WHERE strategy=? AND event=? AND status IN "
                          "('working','done')", (strategy, event)):
        tot += r["limit_px"] * (r["qty"] if r["status"] == "working" else (r["fill_q"] or 0))
    return tot


def size_order(conn, strategy, L, event=None):
    """Quarter-Kelly on current equity (compounding), capped per order, per event, and by free cash."""
    b = bank_state(conn, strategy)
    if b["equity"] <= 0 or L <= 0 or L >= 1:
        return 0, b
    kelly = EDGE[strategy] / (1 - L)            # full Kelly fraction for a binary bought at L
    frac = min(MAX_FRAC, KELLY_MULT * kelly)
    budget = min(b["equity"] * frac, b["cash"])
    if event:
        budget = min(budget, b["equity"] * MAX_EVENT_FRAC - event_reserved(conn, strategy, event))
    return max(0, min(MAX_QTY, int(budget / L))), b


def place(conn, **o):
    qty, b = size_order(conn, o["strategy"], o["limit_px"], o.get("event"))
    if qty < 1:
        if o.get("event") and event_reserved(conn, o["strategy"], o["event"]) >= b["equity"] * MAX_EVENT_FRAC - o["limit_px"]:
            kv_set(conn, "evcap_" + o["strategy"], kv_get(conn, "evcap_" + o["strategy"], 0) + 1)
            log("EVENT CAP    %s: event already uses %d%% of equity - word skipped" % (o["ticker"], 100 * MAX_EVENT_FRAC))
            return False
        k = "skiplog_" + o["strategy"]
        if time.time() - _last_check.get(k, 0) > 600:
            _last_check[k] = time.time()
            log("SKIPPED      %-8s %s: not enough free cash (equity $%.2f, reserved $%.2f)" % (
                o["strategy"], o["ticker"], b["equity"], b["reserved"]))
        kv_set(conn, "skipped_" + o["strategy"], kv_get(conn, "skipped_" + o["strategy"], 0) + 1)
        return False
    o["qty"] = qty
    o.setdefault("q_ahead", o.get("q_ahead0"))
    o.setdefault("qc_ahead", o.get("q_ahead0"))
    cols = ",".join(o.keys())
    q = ",".join("?" * len(o))
    try:
        conn.execute("INSERT INTO orders(%s,status,last_trade_ts,last_ids) VALUES(%s,'working',?,'[]')"
                     % (cols, q), tuple(o.values()) + (o["placed_ts"],))
        conn.commit()
        q0 = o.get("q_ahead0") or 0
        log("PAPER ORDER  %-8s %-44s bid %-3s @ %.3f x%g  queue ahead %g (we'd be %.0f%% of level)  %s" % (
            o["strategy"], o["ticker"], o["side"].upper(), o["limit_px"], o["qty"], q0,
            100.0 * o["qty"] / (q0 + o["qty"]), o.get("label") or ""))
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------- series info ----------------
_fee_cache = {}


def fee_rate(series):
    """Maker fee rate for a series, from Kalshi's own series metadata."""
    if series in _fee_cache:
        return _fee_cache[series]
    d = http_get("/series/" + series)
    s = (d or {}).get("series") or {}
    ft = (s.get("fee_type") or "").lower()
    mult = fnum(s.get("fee_multiplier")) or 1.0
    rate = MAKER_FEE_RATE * mult if "maker" in ft else 0.0
    if d is not None:
        _fee_cache[series] = rate
    return rate


def fee_dollars(rate, contracts, price):
    if rate <= 0 or contracts <= 0:
        return 0.0
    return math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9) / 100.0


def discover(conn, now, force=False):
    last = kv_get(conn, "discover_ts", 0)
    if not force and now - last < 6 * 3600 and kv_get(conn, "mention_series"):
        return kv_get(conn, "mention_series")
    mention = set(MENTION_CORE)
    d = http_get("/series", {"category": "Mentions"})
    for s in (d or {}).get("series") or []:
        t = (s.get("ticker") or "").upper()
        if t:
            mention.add(t)
    mention = sorted(mention)
    kv_set(conn, "mention_series", mention)
    kv_set(conn, "discover_ts", now)
    log("series: %d mention series" % len(mention))
    return mention



# ---------------- strategy: mention long-shot maker ----------------
def mention_candidate(m):
    if (m.get("status") or "active") not in ("active", "open"):
        return False
    ya = px(m, "yes_ask")
    return ya is not None and MENTION_YES_ASK_MIN - EPS <= ya <= MENTION_YES_ASK_MAX + EPS


def mention_confirm(book):
    """Selling YES at the best ask == joining the best NO bid. -> (L_no, size) or None."""
    lv = book_levels(book, "no")
    if not lv:
        return None
    best = max(lv)
    ya = round(1 - best, 4)
    if MENTION_YES_ASK_MIN - EPS <= ya <= MENTION_YES_ASK_MAX + EPS:
        return best, lv[best]
    return None


def mention_word(m):
    return str((m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or m.get("subtitle") or "")


def snapshot_event(conn, now, series, et, markets, late_h):
    strat = "mentions"
    n = 0
    # cheapest (most overpriced long-shot) words first, so the per-event cap keeps the best ones
    for m in sorted(markets or [], key=lambda m: px(m, "yes_ask") if px(m, "yes_ask") is not None else 9):
        t = m.get("ticker")
        if not t or exists(conn, strat, t) or not mention_candidate(m):
            continue
        if any(w in (mention_word(m) + " " + str(m.get("title") or "")).lower() for w in MENTION_SKIP_LABELS):
            continue
        t0 = time.time()
        conf = mention_confirm(get_book(t))
        if not conf:
            continue
        L, qsize = conf
        word = mention_word(m)
        n += place(conn, strategy=strat, grp=mention_group(series), series=series, event=et,
                   ticker=t, label=str(word)[:40], side="no", limit_px=L, qty=0,
                   fee_rate=fee_rate(series), placed_ts=t0, close_ts=parse_ts(m.get("close_time")),
                   yes_bid=px(m, "yes_bid"), yes_ask=px(m, "yes_ask"),
                   entry_note="hours_after_8am=%.2f" % late_h, q_ahead0=qsize)
    return n


def mention_tick(conn, now, mention_series, full_scan):
    et_now = datetime.fromtimestamp(now, ET)
    today = et_now.date()
    if et_now.hour < MENTION_ENTRY_HOUR_ET:
        return 0
    key = "mention_done_%s" % today.isoformat()
    done = set(kv_get(conn, key, []))
    late_h = et_now.hour + et_now.minute / 60.0 - MENTION_ENTRY_HOUR_ET
    todo = []  # (series, event_ticker, markets or None)
    for s in MENTION_CORE:
        for ev in get_all("/events", {"series_ticker": s, "status": "open",
                                      "with_nested_markets": "true", "limit": 200}, "events", 3):
            todo.append((s, ev.get("event_ticker"), ev.get("markets")))
    if full_scan and MENTION_ALL_SERIES:
        ms = set(mention_series)
        for ev in get_all("/events", {"status": "open", "limit": 200}, "events", 150):
            s = (ev.get("series_ticker") or "").upper()
            if s in ms and s not in MENTION_CORE:
                todo.append((s, ev.get("event_ticker"), None))
    n = 0
    for s, et, markets in todo:
        if not et or et in done or event_date(et) != today:
            continue
        if markets is None:
            d = http_get("/events/" + et, {"with_nested_markets": "true"}) or {}
            markets = (d.get("event") or {}).get("markets") or d.get("markets") or []
        done.add(et)  # one snapshot per event, same as the backtest
        n += snapshot_event(conn, now, s, et, markets, late_h)
    kv_set(conn, key, sorted(done))
    return n



# ---------------- fill engine ----------------
# Two queue estimates run side by side for every order:
#   QUEUE      - realistic. We track the contracts ahead of us AND behind us. Growth of the level is
#                new orders joining behind us; shrinkage not explained by trades is cancels, split
#                pro-rata between ahead and behind (the standard queue-simulation assumption).
#   QUEUE-SAFE - worst case. Cancels only move us up when the level shrinks below our queue.
def _consume(st, cnt, qty, ts, has_behind):
    """A seller/buyer of `cnt` contracts hits our price level (price-time priority)."""
    eat = min(st["ahead"], cnt)
    st["ahead"] -= eat
    rem = cnt - eat
    add = min(rem, qty - st["fill"])
    if add > 0:
        st["fill"] += add
        st["ts"] = st["ts"] or ts
        if st["fill"] >= qty - 1e-9:
            st["fill"], st["full"] = qty, ts
    rem -= max(add, 0)
    if has_behind and rem > 0:
        st["behind"] = max(0.0, st["behind"] - rem)


def _fill_all(st, qty, ts):
    if st["fill"] < qty:
        st["fill"], st["ts"], st["full"] = qty, st["ts"] or ts, ts
    st["ahead"] = 0.0


def apply_trades(o, trades, upto=None):
    """Advance fill state through new trades (any order), only those with time <= upto.
    Pure: mutates and returns o. Queue states live in o['P'] (realistic) and o['C'] (safe)."""
    side, L, qty = o["side"], o["limit_px"], o["qty"]
    our_yes_px = L if side == "yes" else 1 - L   # our resting price in YES terms
    seen_ts, seen_ids = o["last_trade_ts"], set(o["last_ids"])
    rows = []
    for tr in trades:
        ts = parse_ts(tr.get("created_time"))
        p = px(tr, "yes_price")
        if ts is None or p is None or ts <= o["placed_ts"] or (upto is not None and ts > upto):
            continue
        tid = tr.get("trade_id") or "%s|%s|%s" % (tr.get("created_time"), p, tr.get("count_fp"))
        if ts < seen_ts or (ts == seen_ts and tid in seen_ids):
            continue
        rows.append((ts, tid, p, (tr.get("taker_side") or "").lower(), trade_count(tr)))
    rows.sort(key=lambda r: r[0])
    for ts, tid, p, taker, cnt in rows:
        if side == "yes":   # we bid YES at L; sellers of YES (taker 'no') hit us
            through = p < L - EPS
            at_ours = abs(p - L) < EPS and taker == "no"
            crossed = abs(p - L) < EPS and taker == "yes"   # an ask rested at our price => our level was empty
        else:               # we bid NO at L == offer YES at 1-L; buyers of YES (taker 'yes') hit us
            through = p > our_yes_px + EPS
            at_ours = abs(p - our_yes_px) < EPS and taker == "yes"
            crossed = abs(p - our_yes_px) < EPS and taker == "no"
        if through and o["strict_ts"] is None:
            o["strict_ts"] = ts
        if (through or at_ours or crossed) and o["touch_ts"] is None:
            o["touch_ts"] = ts
        if at_ours:
            o["vol_at"] += cnt
        for key, hb in (("P", True), ("C", False)):
            st = o[key]
            if through or crossed:
                _fill_all(st, qty, ts)
            elif at_ours:
                _consume(st, cnt, qty, ts, hb)
        if ts > seen_ts:
            seen_ts, seen_ids = ts, set()
        seen_ids.add(tid)
    o["last_trade_ts"], o["last_ids"] = seen_ts, sorted(seen_ids)
    return o


def apply_book(o, book):
    """Reconcile the queue with the resting size now at our price (which never includes us: paper)."""
    if book is None:
        return o
    S = size_at(book_levels(book, o["side"]), o["limit_px"])
    c = o["C"]
    c["ahead"] = min(c["ahead"], S)
    p = o["P"]
    exp = p["ahead"] + p["behind"]
    d = S - exp
    if d > 0:                       # new orders joined the level -> they are behind us
        p["behind"] += d
    elif d < 0 and exp > 0:         # cancels -> pro-rata between ahead and behind
        cut = min(-d, exp)
        p["ahead"] -= cut * p["ahead"] / exp
        p["behind"] -= cut * p["behind"] / exp
    p["ahead"], p["behind"] = max(0.0, p["ahead"]), max(0.0, p["behind"])
    o["book_checks"] = (o.get("book_checks") or 0) + 1
    return o


def load_state(row):
    o = dict(row)
    o["last_ids"] = json.loads(row["last_ids"] or "[]")
    o["vol_at"] = row["vol_at"] or 0.0
    q0 = row["q_ahead0"] or 0.0
    o["P"] = {"ahead": q0 if row["q_ahead"] is None else row["q_ahead"], "behind": row["q_behind"] or 0.0,
              "fill": row["fill_q"] or 0.0, "ts": row["fill_q_ts"], "full": row["fill_q_full_ts"]}
    o["C"] = {"ahead": q0 if row["qc_ahead"] is None else row["qc_ahead"], "behind": 0.0,
              "fill": row["fill_qc"] or 0.0, "ts": row["fill_qc_ts"], "full": None}
    return o


def save_state(conn, o):
    P, C = o["P"], o["C"]
    conn.execute("UPDATE orders SET q_ahead=?, q_behind=?, fill_q=?, fill_q_ts=?, fill_q_full_ts=?,"
                 " qc_ahead=?, fill_qc=?, fill_qc_ts=?, strict_ts=?, touch_ts=?, vol_at=?, last_trade_ts=?,"
                 " last_ids=?, book_checks=? WHERE id=?",
                 (P["ahead"], P["behind"], P["fill"], P["ts"], P["full"], C["ahead"], C["fill"], C["ts"],
                  o["strict_ts"], o["touch_ts"], o["vol_at"], o["last_trade_ts"], json.dumps(o["last_ids"]),
                  o.get("book_checks") or 0, o["id"]))
    conn.commit()


def update_order(conn, row, final=False):
    o = load_state(row)
    before = o["P"]["fill"]
    # Book first, then the tape: trades up to the snapshot time are applied BEFORE reconciling the
    # book, so a trade is never mistaken for a cancel. Later trades wait for the next round.
    tb = time.time()
    book = None if final or o["P"]["fill"] >= o["qty"] else get_book(row["ticker"])
    since = int(o["last_trade_ts"] or o["placed_ts"])
    trades = get_all("/markets/trades", {"ticker": row["ticker"], "min_ts": since, "limit": 1000},
                     "trades", 50)
    if book is not None:
        apply_trades(o, trades, upto=tb)
        apply_book(o, book)
    else:
        apply_trades(o, trades)
    if o["P"]["fill"] > before + 1e-9:
        log("PAPER FILL   %-8s %-44s %s @ %.3f  +%g (now %g/%g)" % (
            row["strategy"], row["ticker"], row["side"].upper(), row["limit_px"],
            o["P"]["fill"] - before, o["P"]["fill"], o["qty"]))
    save_state(conn, o)


def pnl(side, L, contracts, result, rate):
    if contracts <= 0:
        return 0.0
    per = (1 - L) if result == side else -L
    return round(per * contracts - fee_dollars(rate, contracts, L), 4)


def settle_order(conn, row, m, now):
    result = (m.get("result") or "").lower()
    status = (m.get("status") or "").lower()
    closed = status in ("closed", "settled", "finalized", "determined") or \
        (row["close_ts"] and now > (parse_ts(m.get("close_time")) or row["close_ts"]))
    if row["status"] == "working" and (closed or result):
        update_order(conn, row, final=True)   # last look at every print up to the close
        conn.execute("UPDATE orders SET status='done' WHERE id=?", (row["id"],))
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
    if result in ("yes", "no"):
        L, q, rate = row["limit_px"], row["qty"], row["fee_rate"] or 0.0
        pq = pnl(row["side"], L, row["fill_q"] or 0, result, rate)
        pc = pnl(row["side"], L, row["fill_qc"] or 0, result, rate)
        ps = pnl(row["side"], L, q if row["strict_ts"] else 0, result, rate)
        pt = pnl(row["side"], L, q if row["touch_ts"] else 0, result, rate)
        conn.execute("UPDATE orders SET status='settled', result=?, settled_ts=?, pnl_queue=?, pnl_safe=?,"
                     " pnl_strict=?, pnl_touch=? WHERE id=?", (result, now, pq, pc, ps, pt, row["id"]))
        conn.commit()
        if (row["fill_q"] or 0) > 0:
            log("SETTLED      %-44s result=%s  filled %g/%g  $%+.2f" % (
                row["ticker"], result.upper(), row["fill_q"], q, pq))
    elif result in ("void", "scalar"):
        conn.execute("UPDATE orders SET status='void', settled_ts=? WHERE id=?", (now, row["id"]))
        conn.commit()


_last_check = {}


def maintain(conn, now):
    # 1) advance fills on working orders
    for r in conn.execute("SELECT * FROM orders WHERE status='working'").fetchall():
        if now - _last_check.get(r["id"], 0) < MENTION_MAINT_SEC - 1:
            continue
        _last_check[r["id"]] = now
        if r["close_ts"] and now > r["close_ts"]:
            continue  # handled by settlement below
        update_order(conn, r)
    # 2) close out / settle, one market-listing request per event (every 5 min)
    pending = conn.execute("SELECT * FROM orders WHERE status IN ('working','done')").fetchall()
    by_event = {}
    for r in pending:
        by_event.setdefault(r["event"], []).append(r)
    for ev, rows in by_event.items():
        k = "ev:" + ev
        if now - _last_check.get(k, 0) < 300:
            continue
        _last_check[k] = now
        mk = {m.get("ticker"): m for m in get_all("/markets", {"event_ticker": ev, "limit": 200}, "markets", 3)}
        for r in rows:
            m = mk.get(r["ticker"])
            if m is None:
                m = ((http_get("/markets/" + r["ticker"]) or {}).get("market"))
            if m:
                settle_order(conn, r, m, now)


# ---------------- report ----------------
MODELS = [("QUEUE      (realistic - decide on this)", "pnl_queue", "q"),
          ("QUEUE-SAFE (worst case: every cancel was behind us)", "pnl_safe", "c"),
          ("STRICT (conservative: trade-through only)", "pnl_strict", "s"),
          ("TOUCH  (optimistic: front of queue)", "pnl_touch", "t")]


def filled_contracts(r, kind):
    if kind == "q":
        return r["fill_q"] or 0.0
    if kind == "c":
        return r["fill_qc"] or 0.0
    if kind == "s":
        return r["qty"] if r["strict_ts"] else 0.0
    return r["qty"] if r["touch_ts"] else 0.0


def boot_ci(clusters, n=2000, seed=7):
    """Cluster (per event) bootstrap of cents per filled contract. clusters: [(dollars, contracts)]"""
    clusters = [c for c in clusters if c[1] > 0]
    if len(clusters) < 3:
        return (float("nan"), float("nan"))
    rnd = random.Random(seed)
    means = []
    for _ in range(n):
        d = c = 0.0
        for _ in range(len(clusters)):
            x = clusters[rnd.randrange(len(clusters))]
            d += x[0]
            c += x[1]
        means.append(100 * d / c)
    means.sort()
    return means[int(0.05 * n)], means[int(0.95 * n) - 1]


def summarize(rows, pf, kind):
    done = [r for r in rows if r[pf] is not None]
    posted_c = sum(r["qty"] for r in rows)
    filled_c = sum(filled_contracts(r, kind) for r in rows)
    fdone = [r for r in done if filled_contracts(r, kind) > 0]
    fc = sum(filled_contracts(r, kind) for r in fdone)
    dollars = sum(r[pf] for r in done)
    cl = {}
    for r in fdone:
        a = cl.setdefault(r["event"], [0.0, 0.0])
        a[0] += r[pf]
        a[1] += filled_contracts(r, kind)
    lo, hi = boot_ci([tuple(v) for v in cl.values()])
    wins = sum(1 for r in fdone if r[pf] > 0)
    return dict(orders=len(rows), posted_c=posted_c, filled_c=filled_c, fills=len(fdone), wins=wins,
                avg=(100 * dollars / fc) if fc else float("nan"), lo=lo, hi=hi, dollars=dollars)


def coverage(conn, first, now):
    gaps = kv_get(conn, "gaps", [])
    down = sum(max(0, min(b, now) - max(a, first)) for a, b in gaps)
    return 100.0 * (1 - down / max(now - first, 1)), gaps


def _equity_dd(rows):
    eq, peak, dd = START_BANK, START_BANK, 0.0
    for r in sorted((r for r in rows if r["pnl_queue"] is not None and r["settled_ts"]), key=lambda r: r["settled_ts"]):
        eq += r["pnl_queue"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 0)
    return dd


def report(write_csv=False):
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY placed_ts").fetchall()
    if not rows:
        print("No paper orders yet. Mention orders are placed at 8 AM ET on event days.")
        return
    now = time.time()
    first = min(kv_get(conn, "start_ts", rows[0]["placed_ts"]), rows[0]["placed_ts"])
    days = max((now - first) / 86400.0, 1.0)
    up, gaps = coverage(conn, first, now)
    print("=" * 104)
    print("KALSHI MENTIONS PAPER BOT   %s -> %s   (%.1f days, bot was up %.0f%% of the time)" % (
        datetime.fromtimestamp(first).strftime("%b %d %H:%M"), datetime.now().strftime("%b %d %H:%M"),
        (now - first) / 86400.0, up))
    print("Every fill below comes from real Kalshi trades after the order was placed, behind the real")
    print("queue at that price. $ are net of Kalshi fees for each series.")
    print("=" * 104)
    groups = [("ALL", list(rows))] + [("  " + g, [r for r in rows if r["grp"] == g]) for g in sorted(set(r["grp"] for r in rows))]
    for title, pf, kind in MODELS:
        print("\n" + title)
        print("%-20s %6s %8s %8s %6s %6s %8s %17s %10s %9s" % (
            "group", "orders", "posted", "filled", "fill%", "win%", "c/contr", "90% CI (c)", "total $", "$ / day"))
        for name, gr in groups:
            s = summarize(gr, pf, kind)
            fr = 100.0 * s["filled_c"] / s["posted_c"] if s["posted_c"] else 0
            wr = 100.0 * s["wins"] / s["fills"] if s["fills"] else float("nan")
            print("%-20s %6d %8.0f %8.0f %5.0f%% %5.0f%% %+8.2f   [%+6.2f,%+6.2f] %+10.2f %+9.2f" % (
                name, s["orders"], s["posted_c"], s["filled_c"], fr, wr, s["avg"], s["lo"], s["hi"],
                s["dollars"], s["dollars"] / days))
    part = sum(1 for r in rows if 0 < (r["fill_q"] or 0) < r["qty"])
    full = sum(1 for r in rows if (r["fill_q"] or 0) >= r["qty"])
    waits = sorted((r["fill_q_ts"] - r["placed_ts"]) / 60 for r in rows if r["fill_q_ts"])
    q0 = sorted(r["q_ahead0"] or 0 for r in rows)
    print("\nQUEUE DETAIL: full fills %d, partial %d, none %d | median queue ahead at entry %.0f | median wait to first fill %.1f min" % (
        full, part, len(rows) - full - part, q0[len(q0) // 2], waits[len(waits) // 2] if waits else float("nan")))
    b = bank_state(conn)
    print("\nPAPER ACCOUNT: start $%.0f, equity $%.2f (%+.1f%%), free cash $%.2f, max drawdown %.1f%%, skipped %d, event-capped %d" % (
        START_BANK, b["equity"], 100 * (b["equity"] / START_BANK - 1), b["cash"], 100 * _equity_dd(rows),
        kv_get(conn, "skipped_mentions", 0), kv_get(conn, "evcap_mentions", 0)))
    print("\nCALIBRATION (filled + settled words): do cheap words get said LESS often than their price says?")
    print("  %-10s %6s %10s %10s %10s" % ("YES price", "words", "price says", "was said", "P&L $"))
    for lo, hi in MENTION_BUCKETS:
        bk = [r for r in rows if r["pnl_queue"] is not None and (r["fill_q"] or 0) > 0 and lo - EPS <= 1 - r["limit_px"] < hi - EPS]
        if bk:
            print("  %2.0f-%2.0f\u00a2    %6d %9.1f%% %9.1f%% %+10.2f" % (lo * 100, hi * 100, len(bk), 100 * sum(1 - r["limit_px"] for r in bk) / len(bk),
                                                              100 * sum(1 for r in bk if r["result"] == "yes") / len(bk), sum(r["pnl_queue"] for r in bk)))
    print("  (edge = 'was said' BELOW 'price says'. Needs dozens of words per row before it means much.)")
    worst = sorted((r["pnl_queue"], r["ticker"]) for r in rows if r["pnl_queue"])[:5]
    if worst:
        print("\nWORST 5: " + "; ".join("%s $%+.2f" % (t, v) for v, t in worst))
    big = [g for g in gaps if g[1] - g[0] > 600]
    if big:
        print("\nBOT DOWNTIME > 10 min: " + "; ".join("%s (%.0f min)" % (datetime.fromtimestamp(a).strftime("%b %d %H:%M"), (b2 - a) / 60) for a, b2 in big[-8:]))
    print("\nOpen paper orders: %d" % sum(1 for r in rows if r["status"] in ("working", "done")))
    print("\nHow to read it: decide on the QUEUE rows; an edge is only 'proven' once the 90% CI is above 0. Not financial advice.")
    if write_csv:
        path = os.path.join(HERE, "orders.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(rows[0].keys())
            for r in rows:
                w.writerow(list(r))
        print("\nwrote " + path)



# ---------------- live dashboard feed (/api/mentions) ----------------
DASH_PORT = 8765


def _num(v):
    return None if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else round(v, 4)


def mentions_state(conn):
    now = time.time()
    rows = conn.execute("SELECT * FROM orders ORDER BY placed_ts").fetchall()
    b = bank_state(conn)
    start_ts = kv_get(conn, "start_ts", rows[0]["placed_ts"] if rows else now)
    done = sorted((r for r in rows if r["pnl_queue"] is not None and r["settled_ts"]), key=lambda r: r["settled_ts"])
    filled = [r for r in done if (r["fill_q"] or 0) > 0]
    fc = sum(r["fill_q"] for r in filled)
    pnl_tot = sum(r["pnl_queue"] for r in filled)
    eq, curve = START_BANK, [[start_ts, START_BANK]]
    for r in done:
        eq += r["pnl_queue"]
        if len(curve) > 1 and r["settled_ts"] - curve[-1][0] < 10:
            curve[-1] = [curve[-1][0], round(eq, 2)]          # same settlement batch -> one step
        else:
            curve.append([r["settled_ts"], round(eq, 2)])
    curve.append([now, round(b["equity"], 2)])
    peak, dd, maxdd = START_BANK, [], 0.0
    for _, v in curve:
        peak = max(peak, v)
        d = (peak - v) / peak if peak > 0 else 0
        maxdd = max(maxdd, d)
        dd.append(round(d, 4))
    daily = {}
    for r in filled:
        k = datetime.fromtimestamp(r["settled_ts"], ET).strftime("%Y-%m-%d")
        a = daily.setdefault(k, [0.0, 0, 0])
        a[0] += r["pnl_queue"]; a[1] += 1; a[2] += r["pnl_queue"] > 0
    today = datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d")
    groups = []
    for g in sorted(set(r["grp"] for r in rows)):
        gr = [r for r in filled if r["grp"] == g]
        g_fc = sum(r["fill_q"] for r in gr)
        groups.append({"g": g, "n": len(gr), "win": (sum(r["pnl_queue"] > 0 for r in gr) / len(gr)) if gr else None,
                       "cpc": (100 * sum(r["pnl_queue"] for r in gr) / g_fc) if g_fc else None,
                       "pnl": round(sum(r["pnl_queue"] for r in gr), 2)})
    calib = []
    for lo, hi in MENTION_BUCKETS:
        bk = [r for r in filled if lo - EPS <= 1 - r["limit_px"] < hi - EPS]
        calib.append({"b": "%d-%d" % (round(lo * 100), round(hi * 100)), "n": len(bk),
                      "implied": (sum(1 - r["limit_px"] for r in bk) / len(bk)) if bk else None,
                      "said": (sum(1 for r in bk if r["result"] == "yes") / len(bk)) if bk else None,
                      "pnl": round(sum(r["pnl_queue"] for r in bk), 2)})
    open_rows = [r for r in rows if r["status"] in ("working", "done")]
    events = {}
    for r in open_rows:
        e = events.setdefault(r["event"], {"event": r["event"], "series": r["series"], "grp": r["grp"], "bets": []})
        e["bets"].append({"word": r["label"], "px": r["limit_px"], "qty": r["qty"], "fill": r["fill_q"] or 0,
                          "ahead": round(r["q_ahead"] or 0), "status": r["status"]})
    trades = [{"t": max(r["placed_ts"] or 0, r["settled_ts"] or 0), "event": r["event"], "word": r["label"],
               "px": r["limit_px"], "qty": r["qty"], "fill": r["fill_q"] or 0, "status": r["status"],
               "result": r["result"], "pnl": r["pnl_queue"]}
              for r in sorted(rows, key=lambda r: max(r["placed_ts"] or 0, r["settled_ts"] or 0), reverse=True)[:40]]
    cl = {}
    for r in filled:
        a = cl.setdefault(r["event"], [0.0, 0.0])
        a[0] += r["pnl_queue"]; a[1] += r["fill_q"]
    lo, hi = boot_ci([tuple(v) for v in cl.values()], n=600)
    return {"now": now, "start_ts": start_ts, "bot_ok": now - kv_get(conn, "loop_ts", 0) < 90,
            "next_entry": next_entry_time(now),
            "kpi": {"equity": round(b["equity"], 2), "start": START_BANK, "net": round(b["equity"] - START_BANK, 2),
                    "cash": round(b["cash"], 2), "reserved": round(b["reserved"], 2),
                    "today": round(daily.get(today, [0])[0], 2), "fills": len(filled), "posted": len(rows),
                    "win": (sum(1 for r in filled if r["pnl_queue"] > 0) / len(filled)) if filled else None,
                    "cpc": 100 * pnl_tot / fc if fc else None, "ci": [_num(lo), _num(hi)],
                    "maxdd": round(maxdd, 4), "curdd": dd[-1] if dd else 0,
                    "events": len(set(r["event"] for r in filled)), "open": len(open_rows),
                    "fill_rate": (sum(r["fill_q"] or 0 for r in rows) / sum(r["qty"] for r in rows)) if rows else None,
                    "days": (now - start_ts) / 86400.0},
            "curve": curve, "dd": dd,
            "daily": [{"d": k, "pnl": round(v[0], 2), "n": v[1], "win": v[2] / v[1] if v[1] else None} for k, v in sorted(daily.items())][-35:],
            "groups": groups, "calib": calib, "open_events": list(events.values()), "trades": trades}


def start_dashboard(port=DASH_PORT):
    import http.server
    import threading
    page_path = os.path.join(HERE, "dashboard.html")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path.startswith("/api/mentions"):
                    c = sqlite3.connect(DB_PATH, timeout=30)
                    c.row_factory = sqlite3.Row
                    try:
                        body = json.dumps(mentions_state(c), allow_nan=False).encode()
                    finally:
                        c.close()
                    self._send(200, body, "application/json")
                elif self.path in ("/", "/index.html"):
                    with open(page_path, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain")

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        log("dashboard: port %d busy (another copy running?) - dashboard not started here" % port)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("DASHBOARD    open http://127.0.0.1:%d in your browser" % port)
    return srv


# ---------------- main ----------------
def next_entry_time(now):
    """Next 8:00:01 AM ET, so mention entries happen on the dot."""
    d = datetime.fromtimestamp(now, ET)
    t = d.replace(hour=MENTION_ENTRY_HOUR_ET, minute=0, second=1, microsecond=0)
    if t.timestamp() <= now:
        t = (d + timedelta(days=1)).replace(hour=MENTION_ENTRY_HOUR_ET, minute=0, second=1, microsecond=0)
    return t.timestamp()


def ssl_help(e):
    log("SSL certificate error: %s" % e)
    print("\nYour Python can't verify HTTPS certificates. Fix (one time), then rerun:\n"
          "  macOS python.org installs: open \"/Applications/Python 3.*/Install Certificates.command\"\n"
          "  or:  python3 -m pip install --user certifi\n")


def selftest():
    conn = db()
    try:
        mention = discover(conn, time.time(), force=True)
    except SSLProblem as e:
        ssl_help(e)
        return 1
    ok = http_get("/exchange/status")
    print("exchange status:", ok)
    print("mention series found:", len(mention))
    for s in MENTION_CORE:
        evs = get_all("/events", {"series_ticker": s, "status": "open", "with_nested_markets": "true",
                                  "limit": 50}, "events", 1)
        for ev in evs[:4]:
            mk = ev.get("markets") or []
            print("  %s  event date %s  markets %d  fee rate %s" % (
                ev.get("event_ticker"), event_date(ev.get("event_ticker")), len(mk), fee_rate(s)))
            for m in mk[:1]:
                bk = book_levels(get_book(m["ticker"]), "no")
                print("    e.g. %s  '%s'  best NO bid %s (%s resting)" % (
                    m["ticker"], mention_word(m), max(bk) if bk else None, bk.get(max(bk)) if bk else None))
    print("OK - connectivity looks fine." if ok else "Could not reach Kalshi.")
    return 0


def run():
    conn = db()
    if kv_get(conn, "start_ts") is None:
        kv_set(conn, "start_ts", time.time())
    log("mentions paper bot started (no real orders). db=%s" % DB_PATH)
    start_dashboard()
    last_beat = 0
    nxt = {"m": 0, "scan": 0, "hb": 0}
    last_loop = time.time()
    while True:
        now = time.time()
        if now - last_loop > 120:  # computer slept / network down: record the gap honestly
            gaps = kv_get(conn, "gaps", [])
            gaps.append([last_loop, now])
            kv_set(conn, "gaps", gaps[-500:])
            log("resumed after %.0f min gap" % ((now - last_loop) / 60))
        last_loop = now
        try:
            mention = discover(conn, now)
            if now >= nxt["m"]:
                full = now >= nxt["scan"]
                mention_tick(conn, now, mention, full)
                nxt["m"] = min(now + MENTION_POLL_SEC, next_entry_time(now))
                if full:
                    nxt["scan"] = min(now + MENTION_SCAN_SEC, next_entry_time(now))
            maintain(conn, time.time())
            if now >= nxt["hb"]:
                c = dict(conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
                log("heartbeat  orders=%s  equity=$%.2f" % (c, bank_state(conn)["equity"]))
                nxt["hb"] = now + 1800
        except KeyboardInterrupt:
            raise
        except SSLProblem as e:
            ssl_help(e)
            return 1
        except Exception:
            log("error (will retry): " + traceback.format_exc().strip().splitlines()[-1])
            time.sleep(10)
        last_loop = time.time()
        if last_loop - last_beat > 15:
            kv_set(conn, "loop_ts", last_loop)  # lets the dashboard show LIVE vs BOT OFFLINE
            last_beat = last_loop
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser(description="Kalshi mention-market paper-trading bot (no real orders).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run")
    rp = sub.add_parser("report")
    rp.add_argument("--csv", action="store_true")
    sub.add_parser("selftest")
    sub.add_parser("dashboard")
    a = ap.parse_args()
    if a.cmd == "run":
        try:
            return run()
        except KeyboardInterrupt:
            log("stopped by user")
    elif a.cmd == "report":
        report(a.csv)
    elif a.cmd == "dashboard":
        if start_dashboard():
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    elif a.cmd == "selftest":
        return selftest()
    else:
        ap.print_help()


if __name__ == "__main__":
    sys.exit(main() or 0)
