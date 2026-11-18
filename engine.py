import json
import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from strategy import BLOCK, MIN_TRAIN, assess, chart_forecast, chart_side, entry, gains, probability, signal, update

API = "https://external-api.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
PATH = os.getenv("V5_DB_PATH", "kalshi_v5.sqlite3")
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
                active_name="V4 baseline", last_review=None)


def init():
    Path(PATH).parent.mkdir(parents=True, exist_ok=True)
    with db(True) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "v5_accounts" not in tables:
            raise ValueError("Set V5_DB_PATH to a separate V5 database file.")
        c.execute("""CREATE TABLE IF NOT EXISTS v5_accounts (
            id INTEGER PRIMARY KEY, cash REAL DEFAULT 500, enabled INTEGER DEFAULT 1,
            model TEXT, reason TEXT DEFAULT 'Starting worker', heartbeat REAL, btc REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, account INTEGER, ticker TEXT, opened REAL, close REAL,
            snap TEXT, actual TEXT, result TEXT, settled REAL, pnl REAL, epoch INTEGER,
            UNIQUE(account,ticker))""")
        c.execute("CREATE TABLE IF NOT EXISTS chart_cache(id INTEGER PRIMARY KEY, end INTEGER, forecast TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS prices(ts REAL PRIMARY KEY, price REAL)")
        c.execute("INSERT OR IGNORE INTO v5_accounts(id,model) VALUES(1,?)", (json.dumps(initial()),))


def account(c):
    return c.execute("SELECT * FROM v5_accounts ORDER BY id DESC LIMIT 1").fetchone()


def daily(c, aid, now):
    midnight = now - now % 86400
    return c.execute("SELECT COALESCE(SUM(pnl),0) FROM observations WHERE account=? AND settled>=?",
                     (aid, midnight)).fetchone()[0]


def note(c, aid, msg):
    c.execute("UPDATE v5_accounts SET reason=? WHERE id=?", (msg, aid))


def get(url):
    with urlopen(Request(url, headers={"User-Agent":"KalshiPaperBot/5.0","Accept":"application/json"}), timeout=7) as response:
        return json.load(response)


def load_chart(now):
    end = int(now // 300) * 300
    with db() as c:
        row = c.execute("SELECT forecast FROM chart_cache WHERE id=1 AND end=?", (end,)).fetchone()
    if row:
        return json.loads(row[0])
    url = COINBASE.rsplit("/",1)[0] + "/candles?" + urlencode(dict(
        granularity=300, start=datetime.fromtimestamp(end-10800,timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end,timezone.utc).isoformat()))
    chart = chart_forecast(get(url), end)
    if chart:
        with db(True) as c:
            c.execute("INSERT OR REPLACE INTO chart_cache VALUES(1,?,?)",(end,json.dumps(chart)))
    return chart


def stamp(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def market_feed(now):
    markets = get(API + "/markets?" + urlencode(dict(series_ticker="KXBTC15M",status="open",limit=100)))["markets"]
    valid = []
    for m in markets:
        if not m["ticker"].startswith("KXBTC15M-") or m.get("strike_type") not in ("greater", "greater_or_equal", None):
            continue
        close = stamp(m["close_time"])
        if m.get("floor_strike") and 75 <= close - now <= 720 and stamp(m["open_time"]) <= now:
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


def pending(c, aid):
    return c.execute("SELECT * FROM observations WHERE account=? AND result IS NULL ORDER BY id LIMIT 1",(aid,)).fetchone()


def open_risk(c, aid):
    row = c.execute("""SELECT COUNT(*), COALESCE(SUM(json_extract(actual,'$.paid')),0)
                       FROM observations WHERE account=? AND result IS NULL AND actual IS NOT NULL""",
                    (aid,)).fetchone()
    return row[0], row[1]


def record(aid, m, close, spot, now, chart):
    with db(True) as c:
        a = account(c)
        if a["id"] != aid:
            return
        row = c.execute("SELECT id,snap,actual,result FROM observations WHERE account=? AND ticker=?",
                        (aid,m["ticker"])).fetchone()
        remaining = close - now
        stage = 2 if remaining <= 120 else 1 if remaining <= 300 else 0
        prior = json.loads(row["snap"]) if row else None
        if row and (row["result"] is not None or stage <= prior.get("stage", 2)):
            return
        if not a["enabled"]:
            return note(c, aid, "Paused")
        q = quotes(m)
        if not q:
            return note(c, aid, "Waiting for valid quotes and a spread of 10¢ or less")
        samples = c.execute("SELECT ts,price FROM (SELECT * FROM prices ORDER BY ts DESC LIMIT 180) ORDER BY ts").fetchall()
        strike = float(m["floor_strike"])
        if not math.isfinite(strike) or strike <= 0 or close-now < 75:
            return note(c, aid, "Waiting for a valid strike and entry window")
        if not chart or chart["end"] != int(now//300)*300:
            return note(c, aid, "Waiting for three complete hours of current BTC candles")
        side = chart_side(chart,spot,strike,remaining)
        allowed = (side,) if side else ()
        s = signal(samples, spot, strike, close-now, q["YES"][0], q["NO"][0])
        if not s:
            return note(c, aid, "Collecting a fresh BTC price history")
        base, x = s
        model = json.loads(a["model"])
        snap = prior if row else dict(x=x, spot=spot, strike=strike, quotes=q,
                                      weights={"base":None,"active":model["active"],"rival":model["frozen"]})
        snap["stage"] = stage
        snap["last_chart"] = chart
        for label, weights in snap["weights"].items():
            p = probability(base,weights,x)
            if not row:
                snap[label] = p
            if not snap.get(label+"_order"):
                snap[label+"_order"] = entry(p,q,20,allowed)
        actual = None
        count, reserved = open_risk(c, aid)
        room = min(a["cash"], DAILY + daily(c, aid, now) - reserved, model["cap"])
        if row and row["actual"]:
            reason = "Tracking open paper position"
        elif count >= MAX_POSITIONS:
            reason = "All five paper position slots are occupied"
        elif room < 20:
            reason = "Paper cash or remaining daily loss allowance is below $20"
        else:
            actual = entry(probability(base,model["active"],x),q,room,allowed)
            reason = "Opened paper position" if actual else "Observed market; edge or quoted size did not qualify"
            if actual:
                actual["chart"] = chart
            elif not side:
                reason = "Chart has no qualifying direction for this strike"
        if not actual and not (row and row["actual"]) and stage < 2:
            reason += "; next check at " + ("5" if stage == 0 else "2") + " minutes remaining"
        encoded = json.dumps(actual) if actual else None
        if row:
            c.execute("""UPDATE observations SET snap=?,actual=COALESCE(actual,?),
                         opened=CASE WHEN actual IS NULL AND ? IS NOT NULL THEN ? ELSE opened END
                         WHERE id=?""",(json.dumps(snap),encoded,encoded,now,row["id"]))
        else:
            c.execute("""INSERT INTO observations(account,ticker,opened,close,snap,actual,epoch)
                         VALUES(?,?,?,?,?,?,?)""",(aid,m["ticker"],now,close,json.dumps(snap),encoded,model["epoch"]))
        if actual:
            c.execute("UPDATE v5_accounts SET cash=cash-? WHERE id=?", (actual["paid"],aid))
        note(c, aid, reason)


def learn(c, aid, model, snap, yes):
    model["online"] = update(model["online"],model["seen"],snap["base"],snap["x"],yes)
    model["seen"] += 1
    if model["epoch"]:
        raw = c.execute("""SELECT snap,result FROM observations WHERE account=? AND epoch=?
                           AND result IN ('YES','NO') ORDER BY id""",(aid,model["epoch"])).fetchall()
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
        if result in ("YES","NO"):
            learn(c,aid,model,json.loads(r["snap"]),int(result=="YES"))
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
    tick = get(COINBASE)
    spot = float(tick["price"])
    now = time.time()
    if not math.isfinite(spot) or spot <= 0 or not -5 <= now-stamp(tick["time"]) <= 45:
        raise ValueError("Waiting for a fresh BTC quote")
    with db(True) as c:
        if account(c)["id"] != aid:
            return
        c.execute("INSERT OR IGNORE INTO prices VALUES(?,?)",(stamp(tick["time"]),spot))
        c.execute("DELETE FROM prices WHERE ts<?",(now-3600,))
        c.execute("UPDATE v5_accounts SET btc=? WHERE id=?",(spot,aid))
        if not account(c)["enabled"]:
            return note(c,aid,"Paused")
    chart = load_chart(now)
    markets = market_feed(time.time())
    if markets:
        for close,m in markets:
            record(aid,m,close,spot,time.time(),chart)
    else:
        with db(True) as c:
            note(c,aid,"Waiting for a market with 75 seconds to 12 minutes remaining")


def worker(stop):
    while not stop.is_set():
        try:
            cycle()
        except Exception as exc:
            logging.exception("Scan failed")
            with db(True) as c:
                note(c,account(c)["id"],f"Scan will retry: {type(exc).__name__}: {str(exc)[:120]}")
        stop.wait(10)
