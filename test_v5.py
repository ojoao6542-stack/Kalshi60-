import base64
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import engine as e
import strategy as s


class PaperTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.object(e,"PATH",tmp.name+"/v5.sqlite3"))
        self.enterContext(patch.object(e,"DAILY",60))
        e.init()
        self.now = time.time()
        self.market = dict(ticker="KXBTC15M-TEST",floor_strike=98,
                           yes_ask_dollars="0.5000", no_ask_dollars="0.5100",
                           yes_bid_size_fp="500",yes_ask_size_fp="500")
        with e.db(True) as c:
            c.executemany("INSERT INTO prices VALUES(?,?)",[(self.now-i*10,100+(i%2)*0.02) for i in range(1,40)])

    def chart(self, now):
        end=int(now//300)*300
        bars=[[end-(36-i)*300,0,0,100+i,101+i,1] for i in range(36)]
        return s.chart_forecast(bars,end)

    def book(self, market=None):
        e.record(1, market or self.market, self.now+600,100,self.now,self.chart(self.now))

    def test_lifecycle_and_restart(self):
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(lambda _:self.book(),range(3)))
        opened = app.snapshot()
        self.assertEqual(len(opened["recent"]),1)
        risk = opened["recent"][0]["risk"]
        self.assertLessEqual(risk,20)
        self.assertAlmostEqual(opened["cash"],500-risk)
        self.assertEqual(app.control("reset",1)[0],409)
        app.control("pause",1)
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(lambda _:e.settle(1,1,"YES",1,self.now),range(3)))
        e.init()
        closed = app.snapshot()
        self.assertEqual(closed["seen"],1)
        self.assertEqual(closed["filtered"]["wins"],1)
        self.assertAlmostEqual(closed["cash"],500+closed["recent"][0]["pnl"])
        self.assertEqual(app.control("reset",1)[0],200)
        self.assertEqual(app.snapshot()["cash"],500)
        self.assertEqual(app.control("reset",1)[0],409)
        with e.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM observations WHERE account=1").fetchone()[0],1)

    def test_daily_allowance_and_liquidity(self):
        with patch.object(e,"DAILY",25):
            self.book()
            e.settle(1,1,"NO",0,self.now)
            self.book(dict(self.market,ticker="KXBTC15M-B"))
            self.assertEqual(len(app.snapshot()["recent"]),1)
            self.assertIn("allowance",app.snapshot()["status"])
        self.assertIsNone(s.entry(.95,{"YES":(.50,1),"NO":(.51,100)},20))
        for cap in (20,30,40,50):
            for p in (.01,.125,.2999,.56,.8):
                o=s.entry(.99,{"YES":(p,10000),"NO":(1-p,10000)},cap)
                self.assertIsNotNone(o)
                self.assertLessEqual(o["paid"],cap)
                self.assertGreater(s.cost(p,o["n"]+1)[0],cap)

    def test_five_slots_and_independent_settlement(self):
        with patch.object(e,"DAILY",250):
            markets=[dict(self.market,ticker=f"KXBTC15M-{i}") for i in range(6)]
            with ThreadPoolExecutor(6) as pool:
                list(pool.map(self.book,markets))
            state=app.snapshot()
            self.assertEqual(state["open_count"],5)
            self.assertAlmostEqual(state["cash"]+state["reserved"],500)
            with e.db() as c:
                skipped=c.execute("SELECT id FROM observations WHERE actual IS NULL").fetchone()[0]
                first=c.execute("SELECT id FROM observations WHERE actual IS NOT NULL ORDER BY id DESC").fetchone()[0]
            e.settle(1,skipped,"YES",1,self.now)
            self.assertEqual(app.snapshot()["open_count"],5)
            e.settle(1,first,"NO",0,self.now)
            self.assertEqual(app.snapshot()["open_count"],4)
            self.book(dict(self.market,ticker="KXBTC15M-NEW"))
            self.assertEqual(app.snapshot()["open_count"],5)

    def test_daily_cap_counts_unsettled_positions(self):
        with patch.object(e,"DAILY",250), e.db(True) as c:
            model=e.initial()
            model["cap"]=50
            c.execute("UPDATE v5_accounts SET model=?,cash=420 WHERE id=1",(json.dumps(model),))
            # Prior losses today leave $170 of room before opening new positions.
            c.execute("INSERT INTO observations(account,ticker,result,settled,pnl) VALUES(1,'prior','NO',?,-80)",
                      (self.now,))
        with patch.object(e,"DAILY",250):
            for i in range(6):
                self.book(dict(self.market,ticker=f"KXBTC15M-{i}"))
            state=app.snapshot()
            self.assertEqual(state["open_count"],4)
            self.assertLessEqual(state["reserved"],170)
            self.assertGreater(state["reserved"],150)

    def test_scan_visits_all_eligible_markets(self):
        iso=lambda t: datetime.fromtimestamp(t,timezone.utc).isoformat()
        markets=[dict(self.market,ticker=f"KXBTC15M-{i}",open_time=iso(self.now-400),
                      close_time=iso(self.now+500+i)) for i in range(5)]
        def feed(url):
            if url==e.COINBASE:
                return {"price":"100","time":iso(time.time())}
            if "/candles?" in url:
                end=int(time.time()//300)*300
                return [[end-(36-i)*300,0,0,100+i,101+i,1] for i in range(36)]
            if "/markets?" in url:
                return {"markets":markets}
            return {"market":{"result":"yes","status":"finalized"}}
        with patch.object(e,"DAILY",250), patch.object(e,"get",side_effect=feed):
            e.cycle()
            self.assertEqual(app.snapshot()["open_count"],5)
            with e.db(True) as c:
                c.execute("UPDATE observations SET close=?",(self.now-1,))
            app.control("pause",1)
            e.cycle()
            self.assertEqual(app.snapshot()["open_count"],0)
            self.assertEqual(app.snapshot()["all"]["count"],5)

    def test_later_entry_charged_and_learned_once(self):
        close = self.now + 600
        def scan(seconds, probability):
            with patch.object(e,"signal",return_value=(probability,[1,0,0,0,0])):
                e.record(1,self.market,close,100,close-seconds,self.chart(close-seconds))
        scan(600,.5)
        scan(400,.9)
        self.assertEqual(app.snapshot()["open_count"],0)
        scan(290,.9)
        opened=app.snapshot()
        self.assertEqual(opened["open_count"],1)
        scan(110,.98)
        self.assertEqual(app.snapshot()["cash"],opened["cash"])
        with e.db() as c:
            row=c.execute("SELECT * FROM observations").fetchone()
            self.assertEqual(row["opened"],close-290)
            snap=json.loads(row["snap"])
            self.assertEqual(snap["base"],.5)
            self.assertEqual(snap["base_order"]["confidence"],.9)
        e.settle(1,row["id"],"YES",1,close)
        self.assertEqual(app.snapshot()["seen"],1)

    def test_chart_forecast_and_order_gate(self):
        end=int(self.now//300)*300
        bars=[[end-(36-i)*300,0,0,150-i,149-i,1] for i in range(36)]
        falling=s.chart_forecast(list(reversed(bars))+[[end,0,0,1,10000,1]], end)
        self.assertEqual(falling["direction"],"DOWN")
        self.assertIsNone(s.chart_forecast(bars[1:],end))
        self.assertEqual(s.chart_forecast([[b[0],0,0,100,100,1] for b in bars],end)["direction"],"MIXED")
        for i,chart in enumerate((None,dict(falling,end=end-300),falling)):
            e.record(1,dict(self.market,ticker=f"KXBTC15M-WAIT-{i}"),self.now+500,100,self.now,chart)
        self.assertEqual(app.snapshot()["cash"],500)
        e.record(1,dict(self.market,ticker="KXBTC15M-DOWN",floor_strike=102),self.now+500,100,self.now,falling)
        self.assertEqual(app.snapshot()["recent"][0]["side"],"NO")

    def test_forward_windows(self):
        # A deliberately biased stream exercises retraining, promotion and risk
        # increases. It is not a market performance test.
        with patch.object(e,"signal",return_value=(.5,[1,0,0,0,0])):
            for i in range(190):
                self.book(dict(self.market,ticker=f"KXBTC15M-{i}"))
                y=int(i<150)
                e.settle(1,i+1,"YES" if y else "NO",y,self.now)
                if i in (29,68,69,109,149,189):
                    state=app.snapshot()
                    self.assertEqual(state["cap"],{29:20,68:20,69:30,109:40,149:50,189:20}[i])
                    if i==68:
                        self.assertIsNone(state["review"])
                    if i==69:
                        self.assertTrue(state["review"]["promoted"])
                if i==29:
                    with e.db() as c:
                        frozen=json.loads(e.account(c)["model"])["frozen"]
                if i==68:
                    with e.db() as c:
                        model=json.loads(e.account(c)["model"])
                        self.assertEqual(frozen,model["frozen"])
                        self.assertNotEqual(frozen,model["online"])
        self.assertFalse(app.snapshot()["review"]["promoted"])

    def test_public_feed_cycle(self):
        def iso(t):
            return datetime.fromtimestamp(t,timezone.utc).isoformat()
        market=dict(self.market,open_time=iso(self.now-400),close_time=iso(self.now+500),strike_type="greater_or_equal")
        def fixture(url):
            if url==e.COINBASE:
                return {"price":"100","time":iso(time.time())}
            if "/candles?" in url:
                end=int(time.time()//300)*300
                return [[end-(36-i)*300,0,0,100+i,101+i,1] for i in range(36)]
            if "/markets?" in url:
                return {"markets":[market]}
            return {"market":{"result":"no","status":"finalized"}}
        with patch.object(e,"get",side_effect=fixture):
            e.cycle()
            before=app.snapshot()
            self.assertEqual(before["recent"][0]["result"],"OPEN")
            with e.db(True) as c:
                c.execute("UPDATE observations SET close=?",(self.now-2,))
            app.control("pause",1)
            e.cycle()
        after=app.snapshot()
        self.assertEqual(after["all"]["count"],1)
        self.assertEqual(after["cash"],before["cash"])

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
        for endpoint,expected in (("/",b"60% filter experiment"),("/state",b'"cash": 500')):
            client.request("GET",endpoint,headers={"Cookie":cookie})
            page=client.getresponse()
            self.assertEqual(page.status,200)
            self.assertIn(expected,page.read())
        client.request("GET","/state",headers={"Cookie":cookie+"0"})
        page=client.getresponse()
        self.assertEqual(page.status,401)
        page.read()
        headers={"Authorization":"Basic "+base64.b64encode(b"joao:unit-test-secret").decode()}
        for path,marker in (("/","Bitcoin paper trader"),("/state",'"cash": 500'),("/trades.csv","contracts")):
            with urlopen(Request(url+path,headers=headers)) as response:
                self.assertIn(marker,response.read().decode())
        h=dict(headers,**{"X-V5-Control":"1"})
        with urlopen(Request(url+"/control",headers=h,data=b'{"action":"pause","account":1}')) as response:
            self.assertEqual(response.status,200)
        self.assertFalse(app.snapshot()["enabled"])


if __name__=="__main__":
    unittest.main()
