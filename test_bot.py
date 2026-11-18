import json
import os
import tempfile
import time
import threading
import base64
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import engine as e
import strategy as s
import app
import config


class PaperTest(unittest.TestCase):

    def test_rolling_cadence_with_restart_and_risk_limits(self):
        start = self.now
        self.market.update(yes_ask_dollars=".51", no_ask_dollars=".51",
                           yes_bid_size_fp="200", yes_ask_size_fp="200")

        def record(t, ticker):
            market = dict(self.market, ticker=ticker)
            e.record(1, market, t + 500, t, None)

        with e.db(True) as c:
            c.execute("UPDATE cadence SET started=? WHERE account=1", (start,))
        record(start + 1799, "first")
        self.assertEqual(app.snapshot()["open_count"], 0)
        e.init()
        record(start + 1800, "first")
        record(start + 1801, "first")
        record(start + 3599, "second")
        with e.db(True) as c:
            rows = c.execute("SELECT actual FROM observations WHERE actual IS NOT NULL").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertTrue(json.loads(rows[0][0])["timed"])
            cash = e.account(c)["cash"]
            self.assertAlmostEqual(500 - cash, json.loads(rows[0][0])["paid"])
            c.execute("UPDATE v5_accounts SET cash=19")
        record(start + 3600, "second")
        with e.db(True) as c:
            self.assertEqual(e.account(c)["cash"], 19)
            c.execute("UPDATE v5_accounts SET cash=?", (cash,))
        record(start + 3601, "second")
        with e.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM observations WHERE actual IS NOT NULL").fetchone()[0], 2)

    def bars(self, now):
        end = int(now//300)*300
        return [[end-(36-i)*300,0,0,64000+i,64001+i,0] for i in range(36)]

    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(e,"PATH",temp+"/test.sqlite3"))
        e.init()
        self.now = time.time()
        self.market = dict(ticker="KXBTC15M-TEST",floor_strike=64000)
        self.close = self.now + 295

    def test_gross_edge_boundary_both_sides(self):
        for side,yes in [("YES",.60),("NO",.40)]:
            q = {"YES":(.53,500),"NO":(.53,500)}
            q[side] = (.48,500)
            o = s.entry(yes,q,50,allowed=(side,),minimum=20)
            self.assertEqual(o["fee"],0)
            self.assertAlmostEqual(o["paid"],o["n"]*.48)
            self.assertLessEqual(o["paid"],50)
            self.assertGreaterEqual(o["paid"],20)
            q[side] = (.481,500)
            self.assertIsNone(s.entry(yes,q,50,allowed=(side,),minimum=20))
        q = {"YES":(.40,100),"NO":(.61,100)}
        self.assertEqual(s.entry(.59,q,40) is None,not config.FIVE_MINUTES)
        self.assertIsNone(s.entry(.85,q,19,minimum=20))

    def test_cycle_settlement_and_dashboard(self):
        now = self.now
        ask = .48
        def iso(t):
            return datetime.fromtimestamp(t,timezone.utc).isoformat()
        def feed(url):
            if "/candles?" in url:
                return self.bars(now)
            self.assertTrue(url.startswith(e.API+"/markets"))
            if "/markets?" in url:
                return {"markets":[dict(self.market,open_time=iso(self.close-900),close_time=iso(self.close))]}
            if "/orderbook?" in url:
                return {"orderbook_fp":{"yes_dollars":[[str(ask-.02),"100"]],"no_dollars":[[str(1-ask),"100"]]}}
            return {"market":{"status":"finalized","result":"yes"}}
        with patch.object(e,"get",side_effect=feed), patch("time.time",side_effect=lambda:now):
            for elapsed,ask in [(30,.33),(20,.38),(10,.43),(0,.48)]:
                now = self.now-elapsed
                e.cycle()
            state = app.snapshot()
            self.assertEqual(state["chart"]["ticker"],self.market["ticker"])
            self.assertEqual(len(state["chart"]["points"]),4)
            self.assertEqual(state["open_count"],1)
            order = state["recent"][0]
            self.assertEqual(order["side"],"YES")
            self.assertAlmostEqual(state["cash"],500-order["contracts"]*order["price"])
            e.cycle()
            self.assertEqual(app.snapshot()["cash"],state["cash"])
            now=self.close+1
            app.control("pause",1)
            e.cycle()
            settled=app.snapshot()
            self.assertIsNone(settled["chart"])
            self.assertEqual(settled["seen"],1)
            self.assertAlmostEqual(settled["cash"],500+order["contracts"]*(1-order["price"]))
            e.cycle()
            self.assertEqual(app.snapshot()["cash"],settled["cash"])

    def test_time_window(self):
        q=dict(self.market,yes_ask_dollars=.48,no_ask_dollars=.54,yes_ask_size_fp=100,yes_bid_size_fp=100)
        chart=s.chart_forecast(self.bars(self.now),int(self.now//300)*300)
        with patch.object(e,"chart_signal",return_value=(.65,[1,0,0,0,0])):
            e.record(1,q,self.now+301,self.now,chart)
            self.assertEqual(app.snapshot()["open_count"],0 if config.FIVE_MINUTES else 1)
            e.record(1,dict(q,ticker="KXBTC15M-LATE"),self.now+60,self.now,chart)
            self.assertEqual(app.snapshot()["open_count"],0 if config.FIVE_MINUTES else 1)
            e.record(1,q,self.now+270,self.now,chart)
            self.assertEqual(app.snapshot()["open_count"],1)

    def test_existing_account_upgrades_once(self):
        with e.db(True) as c:
            legacy = e.initial()
            legacy.pop("family")
            legacy.update(seen=90, cap=50, active_name="Kalshi quote baseline")
            c.execute("UPDATE v5_accounts SET cash=321, model=?", (json.dumps(legacy),))
        e.init()
        state = app.snapshot()
        self.assertEqual((state["cash"],state["seen"],state["target"]), (321,0,20))
        self.assertEqual(state["active"], "60% chart forecast")
        with e.db(True) as c:
            c.execute("UPDATE v5_accounts SET model=json_set(model,'$.seen',2)")
        e.init()
        self.assertEqual(app.snapshot()["seen"], 2)

    def test_v4_label_with_chart_family_migrates(self):
        model = dict(e.initial(), active_name="V4 baseline", active=[1.0]*5, seen=98)
        with e.db(True) as c:
            c.execute("UPDATE v5_accounts SET cash=345,enabled=0,model=?", (json.dumps(model),))
        e.init()
        state = app.snapshot()
        self.assertEqual((state["cash"],state["seen"],state["active"]), (345,0,"60% chart forecast"))
        self.assertFalse(state["enabled"])
        self.assertIn(b"BTC candle model", app.HTML)
        with e.db() as c:
            self.assertIsNone(json.loads(e.account(c)["model"])["active"])

    def test_isolated_database(self):
        with patch.object(e,"BOT_ID","other"):
            with self.assertRaises(ValueError):
                e.init()

    def test_dashboard_http(self):
        self.enterContext(patch.dict(os.environ,DASHBOARD_PASSWORD="unit-test-secret"))
        srv=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        thread=threading.Thread(target=srv.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url="http://127.0.0.1:"+str(srv.server_port)
        with self.assertRaises(HTTPError) as exc:
            urlopen(url+"/state")
        self.assertEqual(exc.exception.code,401)
        import http.client
        client = http.client.HTTPConnection(*srv.server_address)
        self.addCleanup(client.close)
        client.request("GET","/")
        page=client.getresponse()
        self.assertEqual(page.status,200)
        self.assertIn(b'action="/login"',page.read())
        client.request("POST","/login","password=wrong")
        page=client.getresponse()
        self.assertEqual(page.status,401)
        self.assertIn(b'role=alert',page.read())
        client.request("POST","/login","password=unit-test-secret")
        page=client.getresponse()
        self.assertEqual(page.status,303)
        self.assertEqual(page.getheader("Location"),"/")
        cookie=page.getheader("Set-Cookie").split(";")[0]
        page.read()
        for endpoint,expected in (("/",b"Entry rule"),("/state",b'"cash": 500')):
            client.request("GET",endpoint,headers={"Cookie":cookie})
            page=client.getresponse()
            self.assertEqual(page.status,200)
            self.assertIn(expected,page.read())
        client.request("GET","/state",headers={"Cookie":cookie+"0"})
        page=client.getresponse()
        self.assertEqual(page.status,401)
        page.read()
        headers={"Authorization":"Basic "+base64.b64encode(b"joao:unit-test-secret").decode()}
        for path,marker in (("/","Kalshi YES price"),("/state",'"cash": 500'),("/trades.csv","contracts")):
            with urlopen(Request(url+path,headers=headers)) as response:
                self.assertIn(marker,response.read().decode())
        h=dict(headers,**{"X-V5-Control":"1"})
        with urlopen(Request(url+"/control",headers=h,data=b'{"action":"pause","account":1}')) as response:
            self.assertEqual(response.status,200)
        self.assertFalse(app.snapshot()["enabled"])


if __name__=="__main__":
    unittest.main()
