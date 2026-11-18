from config import BOT_ID
from decimal import Decimal, ROUND_CEILING
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, urlopen

from strategy import BLOCK, MIN_TRAIN, assess, entry, probability, chart_forecast, chart_side, chart_signal, update

API = "https://external-api.kalshi.com/trade-api/v2"
PATH = os.getenv("PAPER_DB_PATH", BOT_ID + ".sqlite3")
DAILY = float(os.getenv("DAILY_LOSS_LIMIT", "250"))
MAX_POSITIONS = 5
if not math.isfinite(DAILY) or DAILY <= 0:
    raise ValueError("DAILY_LOSS_LIMIT must be positive and finite")


@contextmanager
def db(write=False):
    c = sqlite3.connect(PATH, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        if write:
            c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    finally:
        c.close()


def initial():
    return dict(seen=0, online=[0.0]*5, active=None, frozen=None, epoch=0, cap=20,
                family="candles_v1", active_name="60% chart forecast", last_review=None)


def init():
    Path(PATH).parent.mkdir(parents=True, exist_ok=True)
    with db(True) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "bot_identity" not in tables:
            raise ValueError("Use a separate database for this bot")
        c.execute("CREATE TABLE IF NOT EXISTS bot_identity(name TEXT PRIMARY KEY)")
        identity = c.execute("SELECT name FROM bot_identity").fetchone()
        if identity and identity[0] != BOT_ID:
            raise ValueError("Database belongs to another strategy")
        c.execute("INSERT OR IGNORE INTO bot_identity VALUES(?)", (BOT_ID,))
        c.execute("""CREATE TABLE IF NOT EXISTS v5_accounts (
            id INTEGER PRIMARY KEY, cash REAL DEFAULT 500, enabled INTEGER DEFAULT 1,
            model TEXT, reason TEXT DEFAULT 'Starting worker', heartbeat REAL, btc REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, account INTEGER, ticker TEXT, opened REAL, close REAL,
            snap TEXT, actual TEXT, result TEXT, settled REAL, pnl REAL, epoch INTEGER,
            UNIQUE(account,ticker))""")
        c.execute("""CREATE TABLE IF NOT EXISTS ticks(ticker TEXT, ts REAL, close REAL, target REAL,
                     yes REAL, no REAL, mid REAL, PRIMARY KEY(ticker,ts))""")
        c.execute("CREATE TABLE IF NOT EXISTS chart_cache(id INTEGER PRIMARY KEY, end INTEGER, forecast TEXT)")
        c.execute("DROP TRIGGER IF EXISTS start_cadence")
        c.execute("INSERT OR IGNORE INTO v5_accounts(id,model) VALUES(1,?)", (json.dumps(initial()),))
        current = account(c)
        if json.loads(current["model"]).get("family") != "candles_v1":
            c.execute("UPDATE v5_accounts SET model=? WHERE id=?", (json.dumps(initial()), current["id"]))


def account(c):
    return c.execute("SELECT * FROM v5_accounts ORDER BY id DESC LIMIT 1").fetchone()


def daily(c, aid, now):
    midnight = now - now % 86400
    return c.execute("SELECT COALESCE(SUM(pnl),0) FROM observations WHERE account=? AND settled>=?",
                     (aid, midnight)).fetchone()[0]


def note(c, aid, msg):
    c.execute("UPDATE v5_accounts SET reason=? WHERE id=?", (msg, aid))


class RateLimited(Exception):
    def __init__(self, url, retry_after):
        self.provider = urlsplit(url).hostname
        self.minimum_wait = 0
        if retry_after:
            try:
                self.minimum_wait = max(0, int(retry_after))
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(retry_after).timestamp()
                    self.minimum_wait = max(0, math.ceil(deadline - time.time()))
                except (ValueError, TypeError, OverflowError):
                    pass
        super().__init__(self.provider)


def get(url):
    request = Request(url, headers={"User-Agent":"KalshiPaperBot/5.0","Accept":"application/json"})
    try:
        with urlopen(request, timeout=7) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code != 429:
            raise
        exc.close()
        raise RateLimited(url, exc.headers.get("Retry-After")) from exc


def stamp(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def market_feed(now):
    markets = get(API + "/markets?" + urlencode(dict(series_ticker="KXBTC15M",status="open",limit=100)))["markets"]
    valid = []
    for m in markets:
        if not m["ticker"].startswith("KXBTC15M-") or m.get("strike_type") not in ("greater", "greater_or_equal", None):
            continue
        close = stamp(m["close_time"])
        if m.get("floor_strike") and 0 < close - now <= 900 and stamp(m["open_time"]) <= now:
            valid.append((close, m))
    return sorted(valid, key=lambda item: item[0])


def quotes(m):
    try:
        y, n = float(m["yes_ask_dollars"]), float(m["no_ask_dollars"])
        yd, nd = float(m["yes_ask_size_fp"]), float(m["yes_bid_size_fp"])
        if not (0 < y < 1 and 0 < n < 1 and 0.99999 <= y+n <= 1.10001):
            return None
        if not all(math.isfinite(d) and d >= 0 for d in (yd,nd)):
            return None
        return {"YES":(y,int(yd)), "NO":(n,int(nd))}
    except (KeyError, ValueError, TypeError):
        return None


def complete_quotes(m):
    if quotes(m) is not None:
        return m
    data = get(API + "/markets/" + quote(m["ticker"], safe="") + "/orderbook?depth=1")
    try:
        book = data["orderbook_fp"]
        yes_bid, yes_size = book["yes_dollars"][0]
        no_bid, no_size = book["no_dollars"][0]
        return dict(m, yes_ask_dollars=round(1-float(no_bid), 6),
                    no_ask_dollars=round(1-float(yes_bid), 6),
                    yes_bid_size_fp=yes_size, yes_ask_size_fp=no_size)
    except (IndexError, KeyError, TypeError, ValueError):
        return m


def pending(c, aid):
    return c.execute("SELECT * FROM observations WHERE account=? AND result IS NULL ORDER BY id LIMIT 1",(aid,)).fetchone()


def open_risk(c, aid):
    row = c.execute("""SELECT COUNT(*), COALESCE(SUM(json_extract(actual,'$.paid')),0)
                       FROM observations WHERE account=? AND result IS NULL AND actual IS NOT NULL""",
                    (aid,)).fetchone()
    return row[0], row[1]


def load_chart(now):
    end = int(now // 300) * 300
    with db() as c:
        row = c.execute("SELECT forecast FROM chart_cache WHERE id=1 AND end=?", (end,)).fetchone()
    if row:
        cached = json.loads(row[0])
        if cached.pop("prices", None) and "last_close" in cached:
            return cached
    url = "https://api.exchange.coinbase.com/products/BTC-USD" + "/candles?" + urlencode(dict(
        granularity=300, start=datetime.fromtimestamp(end-10800,timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end,timezone.utc).isoformat()))
    chart = chart_forecast(get(url), end)
    if chart:
        with db(True) as c:
            c.execute("INSERT OR REPLACE INTO chart_cache VALUES(1,?,?)",(end,json.dumps(chart)))
        chart.pop("prices", None)
    return chart


def record(aid, m, close, now, chart):
    ticker = m["ticker"]
    q = quotes(m)
    remaining = close - now
    with db(True) as c:
        a = account(c)
        if a["id"] != aid or not a["enabled"] or not 0 < remaining <= 900:
            return
        if q:
            mid = (1 + q["YES"][0] - q["NO"][0]) / 2
            c.execute("INSERT OR REPLACE INTO ticks VALUES(?,?,?,?,?,?,?)",
                      (ticker,now,close,float(m["floor_strike"]),q["YES"][0],q["NO"][0],mid))
        c.execute("DELETE FROM ticks WHERE close<?",(now-900,))
        if remaining <= 60:
            return note(c,aid,"Final minute · entries closed")
        prior = c.execute("SELECT actual,result FROM observations WHERE account=? AND ticker=?",(aid,ticker)).fetchone()
        if prior and (prior["actual"] or prior["result"]):
            return note(c,aid,"Decision recorded for this contract")
        model = json.loads(a["model"])
        current = chart and chart["end"] == int(now//300)*300
        strike = float(m["floor_strike"])
        prediction = None
        if not current:
            reason = "Waiting for a complete three-hour BTC chart"
        elif not q:
            reason = "Waiting for a valid Kalshi quote"
        elif not math.isfinite(strike) or strike <= 0:
            reason = "Waiting for a valid Kalshi target"
        else:
            spot = chart["last_close"]
            horizon = remaining + now - chart["end"]
            side = chart_side(chart, spot, strike, horizon)
            reason = "Waiting for a chart trend that agrees with the contract target"
            if side:
                prediction = chart_signal(chart, spot, strike, horizon)
        snap = dict(strategy=BOT_ID, family=model["family"], quotes=q, seconds=remaining)
        actual = None
        if prediction:
            base, x = prediction
            snap.update(base=base, x=x)
            for label, weights in (("base", None), ("active", model["active"]), ("rival", model["frozen"])):
                prob = probability(base, weights, x)
                snap[label] = prob
                snap[label+"_order"] = entry(prob,q,20,allowed=(side,))
            count, reserved = open_risk(c, aid)
            available = min(50, a["cash"], DAILY + daily(c,aid,now) - reserved)
            if count >= MAX_POSITIONS or available < 20:
                reason = "Waiting for available paper risk budget"
            else:
                price = q[side][0]
                unit = Decimal(str(price))
                min_contracts = (Decimal(20) / unit).to_integral_value(rounding=ROUND_CEILING)
                target = max(model["cap"], float(min_contracts * unit))
                actual = entry(snap["active"],q,min(target,available),allowed=(side,),minimum=20)
                reason = "Opened paper position" if actual else "Entry thresholds or available depth insufficient"
        snap["reason"] = reason
        encoded = json.dumps(actual) if actual else None
        c.execute("""INSERT INTO observations(account,ticker,opened,close,snap,actual,epoch)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(account,ticker) DO UPDATE SET opened=excluded.opened,
                       snap=excluded.snap,actual=excluded.actual,epoch=excluded.epoch
                     WHERE excluded.actual IS NOT NULL OR (json_extract(observations.snap,'$.x') IS NULL AND json_extract(excluded.snap,'$.x') IS NOT NULL)""",(aid,m["ticker"],now,close,json.dumps(snap),encoded,model["epoch"]))
        if actual:
            c.execute("UPDATE v5_accounts SET cash=cash-? WHERE id=?", (actual["paid"],aid))
        note(c, aid, reason)


def learn(c, aid, model, snap, yes):
    model["online"] = update(model["online"],model["seen"],snap["base"],snap["x"],yes)
    model["seen"] += 1
    if model["epoch"]:
        raw = c.execute("""SELECT snap,result FROM observations WHERE account=? AND epoch=?
                           AND json_extract(snap,'$.family')='candles_v1' AND json_extract(snap,'$.x') IS NOT NULL AND result IN ('YES','NO') ORDER BY id""",(aid,model["epoch"])).fetchall()
        if len(raw) < BLOCK:
            return
        rows = [dict(json.loads(r["snap"]), yes=int(r["result"]=="YES")) for r in raw]
        rival = assess(rows,"rival","active")
        incumbent = assess(rows,"active","base")
        rival_base = assess(rows,"rival","base")
        promoted = rival["passed"] and rival_base["passed"]
        if promoted:
            model["active"] = model["frozen"]
            model["active_name"] = f"Validated model {model['epoch']}"
        validated = promoted or incumbent["passed"]
        model["cap"] = min(50, model["cap"] + 10) if validated else 20
        model["last_review"] = dict(round=model["epoch"], promoted=promoted,
                                    rival=rival, active_vs_baseline=incumbent, rival_vs_baseline=rival_base)
    if model["seen"] >= MIN_TRAIN:
        model["frozen"] = model["online"][:]
        model["epoch"] += 1


def settle(aid, oid, result, value, now):
    with db(True) as c:
        a = account(c)
        r = c.execute("SELECT * FROM observations WHERE id=? AND account=? AND result IS NULL",(oid,aid)).fetchone()
        if a["id"] != aid or not r:
            return
        order = json.loads(r["actual"]) if r["actual"] else None
        payout = 0
        if order:
            payout = order["n"] * (value if order["side"]=="YES" else 1-value)
        pnl = round(payout-order["paid"],6) if order else None
        c.execute("UPDATE observations SET settled=?,result=?,pnl=? WHERE id=?",(now,result,pnl,oid))
        model = json.loads(a["model"])
        snap = json.loads(r["snap"])
        if result in ("YES","NO") and snap.get("family") == model["family"] and "x" in snap:
            learn(c,aid,model,snap,int(result=="YES"))
        c.execute("UPDATE v5_accounts SET cash=cash+?,model=? WHERE id=?", (payout,json.dumps(model),aid))


def cycle():
    now = time.time()
    with db(True) as c:
        a = account(c)
        aid = a["id"]
        c.execute("UPDATE v5_accounts SET heartbeat=? WHERE id=?",(now,aid))
        due = c.execute("""SELECT id,ticker FROM observations WHERE account=? AND result IS NULL
                           AND close<=? ORDER BY close,id""",(aid,now)).fetchall()
    for r in due:
        try:
            m = get(API+"/markets/"+quote(r["ticker"],safe=""))["market"]
            result = str(m.get("result","")).upper()
            if m.get("status") in ("settled","finalized") and not m.get("is_provisional",False):
                if result in ("YES","NO"):
                    settle(aid,r["id"],result,int(result=="YES"),now)
                elif m.get("settlement_value_dollars") is not None:
                    value = float(m["settlement_value_dollars"])
                    if 0 <= value <= 1:
                        settle(aid,r["id"],"SCALAR",value,now)
        except (OSError, ValueError, KeyError) as exc:
            logging.warning("Settlement for %s will retry: %s",r["ticker"],exc)
    with db() as c:
        if not account(c)["enabled"]:
            return
    markets = market_feed(time.time())
    if markets:
        chart = load_chart(time.time())
        for close,m in markets:
            m = complete_quotes(m)
            record(aid,m,close,time.time(),chart)
    else:
        with db(True) as c:
            note(c,aid,"Waiting for an open BTC market")


def worker(stop):
    backoff = 30
    while not stop.is_set():
        try:
            cycle()
            backoff = 30
        except RateLimited as exc:
            delay = max(backoff, exc.minimum_wait)
            backoff = min(backoff * 2, 300)
            deadline = time.monotonic() + delay
            while not stop.is_set():
                remaining = math.ceil(deadline - time.monotonic())
                if remaining <= 0:
                    break
                with db(True) as c:
                    c.execute("UPDATE v5_accounts SET heartbeat=?,reason=? WHERE id=?",
                              (time.time(), f"{exc.provider} rate limit. Next scan in {remaining}s.",
                               account(c)["id"]))
                stop.wait(min(10, remaining))
            continue
        except Exception as exc:
            logging.exception("Scan failed")
            with db(True) as c:
                note(c,account(c)["id"],f"Scan will retry: {type(exc).__name__}: {str(exc)[:120]}")
        stop.wait(10)
