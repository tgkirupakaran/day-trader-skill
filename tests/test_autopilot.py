import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from _load import FIX, load

ap = load("autopilot", "autopilot")
ibkr = load("autopilot", "ibkr")

DAY = datetime.date(2026, 10, 28)          # a Wednesday, a TSX and NYSE trading day


def cfg(**kw):
    c = dict(ap.DEFAULTS, market="ca", commission_per_trade=1.0, max_loss_per_trade=50.0)
    c.update(kw)
    return ap.check_config(c)


def bars(rows, day=DAY, start="09:30"):
    """rows: (o, h, l, c) per 5-minute bar from `start`."""
    t = datetime.datetime.combine(day, datetime.datetime.strptime(start, "%H:%M").time())
    out = []
    for o, h, lo, c in rows:
        out.append({"at": t, "o": o, "h": h, "l": lo, "c": c, "v": 1000})
        t += ap.BAR
    return out


RANGE = [(10.0, 10.5, 10.0, 10.2), (10.2, 10.4, 10.1, 10.3), (10.3, 10.5, 10.2, 10.4)]   # 9:30-9:45: 10.00-10.50
BREAK = [(10.4, 10.7, 10.4, 10.6)]                                                       # 9:45 bar closes 10.60
QUIET = (10.4, 10.45, 10.3, 10.4)                                                        # under the range high
FLAT = (10.6, 10.7, 10.58, 10.65)                                                      # opens at 10.60, holds


class Rule(unittest.TestCase):
    def test_breakout_to_target(self):
        b = bars(RANGE + BREAK + [(10.6, 10.8, 10.55, 10.7), (10.7, 11.0, 10.7, 10.95)] + [FLAT] * 80)
        (t,) = ap.day_trades(DAY, {"ABC.TO": b}, cfg())
        # fill: the 9:50 bar's open 10.60 + 0.01 slippage; stop the range's middle, 10.25
        self.assertEqual((t["entry_time"], t["entry"], t["stop"]), ("09:50", 10.61, 10.25))
        self.assertEqual(t["target"], 10.97)                       # 1R: 10.61 + 0.36
        self.assertEqual(t["shares"], 133)                         # (50 - 2 x 1.00) / 0.36
        self.assertEqual((t["reason"], t["exit"], t["exit_time"]), ("target", 10.97, "10:00"))
        self.assertEqual(t["pnl"], 45.88)                          # 0.36 x 133 - 2.00
        self.assertEqual(t["status"], "closed")

    def test_a_bar_that_touches_both_counts_as_the_stop(self):
        b = bars(RANGE + BREAK + [(10.6, 11.2, 10.1, 10.6)] + [FLAT] * 80)
        (t,) = ap.day_trades(DAY, {"ABC.TO": b}, cfg())
        self.assertEqual((t["reason"], t["exit"]), ("stop", 10.24))   # the stop less a cent of slippage

    def test_a_gap_down_fills_past_the_stop(self):
        b = bars(RANGE + BREAK + [(10.6, 10.65, 10.5, 10.55), (10.0, 10.1, 9.9, 10.0)] + [FLAT] * 80)
        (t,) = ap.day_trades(DAY, {"ABC.TO": b}, cfg())
        self.assertEqual((t["reason"], t["exit"]), ("stop", 9.99))
        self.assertLess(t["pnl"], -50)                              # a stop is a plan, not a guarantee

    def test_out_by_flat_by(self):
        b = bars(RANGE + BREAK + [FLAT] * 80)
        (t,) = ap.day_trades(DAY, {"ABC.TO": b}, cfg())
        self.assertEqual((t["reason"], t["exit_time"], t["exit"]), ("flat by 15:55", "15:55", 10.59))

    def test_no_close_above_the_range_no_trade(self):
        b = bars(RANGE + [(10.4, 10.9, 10.3, 10.45)] * 70)        # wicks above, never closes above
        self.assertEqual(ap.day_trades(DAY, {"ABC.TO": b}, cfg()), [])

    def test_no_entry_after_the_cutoff(self):
        b = bars(RANGE + [QUIET] * 70 + BREAK + [FLAT] * 5)       # the break comes at 15:35
        self.assertEqual(ap.day_trades(DAY, {"ABC.TO": b}, cfg()), [])

    def test_trades_per_day_and_the_earliest_first(self):
        late = bars(RANGE + [QUIET] + BREAK + [FLAT] * 80)
        early = bars(RANGE + BREAK + [FLAT] * 80)
        ts = ap.day_trades(DAY, {"AAA.TO": late, "BBB.TO": early}, cfg(max_trades_per_day=1))
        self.assertEqual([t["symbol"] for t in ts], ["BBB.TO"])

    def test_the_daily_loss_limit_stops_new_trades(self):
        loser = bars(RANGE + BREAK + [FLAT, (10.0, 10.1, 9.9, 10.0)] + [FLAT] * 80)
        later = bars(RANGE + [QUIET] * 3 + BREAK + [FLAT] * 80)
        ts = ap.day_trades(DAY, {"AAA.TO": loser, "BBB.TO": later}, cfg(max_daily_loss=40))
        self.assertEqual([t["symbol"] for t in ts], ["AAA.TO"])

    def test_cash_caps_the_second_trade(self):
        a = bars(RANGE + BREAK + [FLAT] * 80)
        b = bars(RANGE + [QUIET] + BREAK + [FLAT] * 80)
        t1, t2 = ap.day_trades(DAY, {"AAA.TO": a, "BBB.TO": b}, cfg(budget=2000))
        self.assertEqual(t1["shares"], 133)
        self.assertEqual(t2["shares"], int((2000 - 133 * 10.61 - 1.0) / 10.61))

    def test_stop_at_the_low_and_a_2r_target(self):
        b = bars(RANGE + BREAK + [FLAT] * 80)
        (t,) = ap.day_trades(DAY, {"ABC.TO": b}, cfg(stop_rule="low", target_r=2))
        self.assertEqual((t["stop"], t["target"]), (10.0, 11.83))

    def test_a_thin_volume_break_is_skipped(self):
        b = bars(RANGE + BREAK + [FLAT] * 80)
        b[3]["v"] = 1400                                            # 1.4x the range bars' 1,000
        self.assertEqual(ap.day_trades(DAY, {"ABC.TO": b}, cfg(min_volume_x=1.5)), [])
        b[3]["v"] = 1600
        (t,) = ap.day_trades(DAY, {"ABC.TO": b}, cfg(min_volume_x=1.5))
        self.assertIn("volume 1.5x", t["setup"])

    def test_vwap_reversion(self):
        # VWAP sits near 10.00; the 9:45 bar closes 2 % under it, the next bar opens 9.80
        rows = [(10.0, 10.05, 9.95, 10.0)] * 3 + [(9.9, 9.9, 9.78, 9.8), (9.8, 9.85, 9.75, 9.82),
                                                  (9.85, 10.0, 9.85, 9.98)] + [(9.98, 9.99, 9.97, 9.98)] * 80
        (t,) = ap.day_trades(DAY, {"ABC.TO": bars(rows)}, cfg(setup="vwap_revert", revert_pct=1.5,
                                                                revert_stop_pct=1.0))
        self.assertEqual((t["entry_time"], t["entry"]), ("09:50", 9.81))
        self.assertEqual(t["stop"], 9.71)                          # 1 % under the entry
        self.assertEqual(t["reason"], "target")                     # back to where VWAP was
        self.assertGreater(t["target"], t["entry"])
        self.assertTrue(t["setup"].startswith("vwap revert 1.5%"))
        self.assertNotIn("or_high", t)

    def test_sweep_variations(self):
        vs = ap.variations()
        self.assertEqual(len(vs), 66)
        self.assertEqual(sum(1 for v in vs if v["setup"] == "vwap_revert"), 12)
        for v in vs:
            ap.check_config(dict(cfg(), **v))

    def test_a_tsx_holiday_has_no_trades(self):
        thanksgiving = datetime.date(2026, 10, 12)
        b = bars(RANGE + BREAK + [FLAT] * 80, day=thanksgiving)
        self.assertEqual(ap.day_trades(thanksgiving, {"ABC.TO": b}, cfg()), [])

    def test_half_cent_prices_under_fifty_cents(self):
        self.assertEqual(ap.to_tick(0.4321, "ca", up=True), 0.435)
        self.assertEqual(ap.to_tick(0.4321, "ca", up=False), 0.43)
        self.assertEqual(ap.to_tick(0.4321, "us", up=True), 0.44)

    def test_a_trade_still_running_stays_open(self):
        b = bars(RANGE + BREAK + [FLAT] * 3)
        (t,) = ap.day_trades(DAY, {"ABC.TO": b}, cfg(), final=False)
        self.assertEqual((t["status"], t["mark"]), ("open", 10.65))

    def test_summary_and_verdict(self):
        trades = [{"status": "closed", "pnl": 40.0, "fees": 2.0, "r": 0.8, "date": "2026-10-27"},
                  {"status": "closed", "pnl": -52.0, "fees": 2.0, "r": -1.04, "date": "2026-10-28"},
                  {"status": "missed", "pnl": 0.0, "fees": 0.0, "r": 0, "date": "2026-10-28"}]
        s = ap.summary(trades)
        self.assertEqual((s["trades"], s["open"], s["net_pnl"], s["gross_pnl"], s["max_drawdown"]),
                         (2, 0, -12.0, -8.0, -52.0))
        self.assertTrue(ap.verdict(s).startswith("Losing on paper, even before commissions."))


class Config(unittest.TestCase):
    def test_cad_keys_and_flags(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "c.json")
        with open(p, "w") as fh:
            json.dump({"market": "ca", "max_loss_per_trade_cad": 80, "max_loss_per_trade_usd": 20,
                       "commission_per_trade": 4.95, "target_r": 1.5}, fh)
        c = ap.read_config(p, target_r=2.0)
        self.assertEqual((c["max_loss_per_trade"], c["target_r"], c["commission_per_trade"]), (80.0, 2.0, 4.95))
        self.assertEqual(ap.symbols_for(c, "ry,shop"), ["RY.TO", "SHOP.TO"])

    def test_bad_values_refused(self):
        for bad in ({"or_minutes": 7}, {"stop_rule": "high"}, {"commission_per_trade": 30.0}):
            with self.assertRaises(ap.AutopilotError):
                cfg(**bad)


class RealData(unittest.TestCase):
    """The last ~60 days of RY.TO and SHOP.TO 5-minute bars, from Yahoo (tests/fixtures/trader-timemachine)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.off = os.path.join(self.d, "charts")
        os.mkdir(self.off)
        for s in ("RY.TO", "SHOP.TO"):
            shutil.copy(os.path.join(FIX, "trader-timemachine", s + "-60d5m.json.gz"),
                        os.path.join(self.off, s + ".json.gz"))
        self.cfg = os.path.join(self.d, "config.json")
        with open(self.cfg, "w") as fh:
            json.dump({"market": "ca", "commission_per_trade": 1.0, "budget": 5000,
                       "max_loss_per_trade_cad": 50}, fh)
        self.cwd = os.getcwd()
        os.chdir(self.d)
        self.addCleanup(os.chdir, self.cwd)

    def run_ap(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ap.main(list(args) + ["--config", self.cfg, "--symbols", "RY.TO,SHOP.TO",
                                       "--offline", self.off, "--now", "2026-10-28 17:00"])
        return rc, out.getvalue()

    def run_ap_at(self, *args, now):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ap.main(list(args) + ["--config", self.cfg, "--symbols", "RY.TO,SHOP.TO",
                                       "--offline", self.off, "--now", now])
        return rc, out.getvalue()

    def test_backtest(self):
        rc, out = self.run_ap("backtest")
        self.assertEqual(rc, 0)
        with open(os.path.join("trader", "autopilot", "backtest.json")) as fh:
            doc = json.load(fh)
        s = ap.summary(doc["trades"])
        self.assertEqual((s["trades"], s["wins"], s["days"]), (62, 21, 46))
        self.assertEqual((s["gross_pnl"], s["fees"], s["net_pnl"]), (-450.11, 124.0, -574.11))
        self.assertIn("Losing on paper, even before commissions.", out)
        self.assertTrue(all(t["entry_time"] >= "09:50" and t["exit_time"] <= "15:55" for t in doc["trades"]))
        with open(os.path.join("trader", "autopilot", "backtest.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("Net P/L after costs", page)
        self.assertEqual(page.count("<tr>") - 1, 62)
        self.assertIn("-C$574.11", page)

    def test_sweep_ranks_every_variation(self):
        rc, out = self.run_ap("sweep")
        self.assertEqual(rc, 0)
        self.assertIn("Tried 66 variations", out)
        self.assertIn("vwap revert", out)
        self.assertIn("first half", out)

    def test_run_after_the_close_logs_no_hindsight_trades(self):
        rc, out = self.run_ap("run", "--once")
        self.assertEqual(rc, 0)
        self.assertIn("simulated fills", out)
        with open(os.path.join("trader", "autopilot", "trades.json")) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["broker"], doc["watching"]), ("sim", {"2026-10-28": "17:00"}))
        self.assertEqual(doc["trades"], [])                        # started at 5 PM: nothing counts
        self.assertTrue(os.path.exists(os.path.join("trader", "autopilot", "dashboard.html")))

    def test_run_counts_only_what_it_watched(self):
        # Oct 23: the rule fills SHOP.TO at 9:55 and RY.TO at 15:20
        self.run_ap_at("run", "--once", now="2026-10-23 09:31")
        self.run_ap_at("run", "--once", now="2026-10-23 17:00")
        log = os.path.join("trader", "autopilot", "trades.json")
        with open(log) as fh:
            doc = json.load(fh)
        self.assertEqual([(t["symbol"], t["entry_time"]) for t in doc["trades"]],
                         [("SHOP.TO", "09:55"), ("RY.TO", "15:20")])
        os.remove(log)                                              # started at noon instead
        self.run_ap_at("run", "--once", now="2026-10-23 12:00")
        self.run_ap_at("run", "--once", now="2026-10-23 17:00")
        with open(log) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["watching"], {"2026-10-23": "12:00"})
        self.assertEqual([t["symbol"] for t in doc["trades"]], ["RY.TO"])

    def run_ap_at(self, *args, now):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ap.main(list(args) + ["--config", self.cfg, "--symbols", "RY.TO,SHOP.TO",
                                       "--offline", self.off, "--now", now])
        return rc, out.getvalue()

    def test_backtest(self):
        rc, out = self.run_ap("backtest")
        self.assertEqual(rc, 0)
        with open(os.path.join("trader", "autopilot", "backtest.json")) as fh:
            doc = json.load(fh)
        s = ap.summary(doc["trades"])
        self.assertEqual((s["trades"], s["wins"], s["days"]), (62, 21, 46))
        self.assertEqual((s["gross_pnl"], s["fees"], s["net_pnl"]), (-450.11, 124.0, -574.11))
        self.assertIn("Losing on paper, even before commissions.", out)
        self.assertTrue(all(t["entry_time"] >= "09:50" and t["exit_time"] <= "15:55" for t in doc["trades"]))
        with open(os.path.join("trader", "autopilot", "backtest.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("Net P/L after costs", page)
        self.assertEqual(page.count("<tr>") - 1, 62)
        self.assertIn("-C$574.11", page)

    def test_sweep_ranks_every_variation(self):
        rc, out = self.run_ap("sweep")
        self.assertEqual(rc, 0)
        self.assertIn("Tried 66 variations", out)
        self.assertIn("vwap revert", out)
        self.assertIn("first half", out)

    def test_run_after_the_close_logs_no_hindsight_trades(self):
        rc, out = self.run_ap("run", "--once")
        self.assertEqual(rc, 0)
        self.assertIn("simulated fills", out)
        with open(os.path.join("trader", "autopilot", "trades.json")) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["broker"], doc["watching"]), ("sim", {"2026-10-28": "17:00"}))
        self.assertEqual(doc["trades"], [])                        # started at 5 PM: nothing counts
        self.assertTrue(os.path.exists(os.path.join("trader", "autopilot", "dashboard.html")))

    def test_run_counts_only_what_it_watched(self):
        self.run_ap_at("run", "--once", now="2026-10-28 09:31")
        rc, _ = self.run_ap_at("run", "--once", now="2026-10-28 17:00")
        with open(os.path.join("trader", "autopilot", "trades.json")) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["watching"], {"2026-10-28": "09:31"})
        replay = ap.day_trades(DAY, {s: b[DAY] for s, b in
                                     ((s, ap.fetch(s, "1d", self.off)) for s in ("RY.TO", "SHOP.TO"))},
                               ap.read_config(self.cfg))
        self.assertEqual([t["symbol"] for t in doc["trades"]], [t["symbol"] for t in replay])


# ---------------------------------------------------------------- IBKR, against a stand-in

class Obj(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeLib(object):
    @staticmethod
    def Stock(sym, exch, cur):
        return Obj(symbol=sym, exchange=exch, currency=cur)

    @staticmethod
    def MarketOrder(action, qty):
        return Obj(action=action, totalQuantity=qty, orderType="MKT", orderId=None)


class FakeIB(object):
    def __init__(self, accounts=("DU1234567",)):
        self.accounts, self.placed, self.cancelled, self.execs = list(accounts), [], [], []
        self.connected, self.next_id = True, 100

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return self.accounts

    def disconnect(self):
        self.connected = False

    def qualifyContracts(self, c):
        return [c]

    def bracketOrder(self, action, qty, limit, tp, sl):
        return [Obj(action=action, totalQuantity=qty, lmtPrice=limit, orderType="LMT"),
                Obj(action="SELL", totalQuantity=qty, lmtPrice=tp, orderType="LMT"),
                Obj(action="SELL", totalQuantity=qty, auxPrice=sl, orderType="STP")]

    def placeOrder(self, c, o):
        self.next_id += 1
        o.orderId = self.next_id
        t = Obj(order=o, contract=c)
        self.placed.append(t)
        return t

    def openTrades(self):
        gone = set(o.orderId for o in self.cancelled)
        return [t for t in self.placed if t.order.orderId not in gone]

    def cancelOrder(self, o):
        self.cancelled.append(o)

    def reqExecutions(self):
        return self.execs

    def fill(self, order_id, side, qty, price, at, fee=1.0):
        ref = next(t.order.orderRef for t in self.placed if t.order.orderId == order_id)
        self.execs.append(Obj(execution=Obj(orderRef=ref, side=side, shares=qty, price=price, orderId=order_id),
                              commissionReport=Obj(commission=-fee), time=at))


class IBKR(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.log = os.path.join(self.d, "trades.json")
        self.ib = FakeIB()
        self.c = cfg()
        self.broker = ibkr.IBKRPaperBroker(self.c, self.log, ap, ib=self.ib, lib=FakeLib)

    def step(self, b, now):
        now = datetime.datetime.combine(DAY, datetime.datetime.strptime(now, "%H:%M").time())
        ts = self.broker.sync(DAY, {"ABC.TO": ap.closed_only(b, now)}, self.c, now, False)
        ap.merge(self.log, ts, {DAY.isoformat()})
        return ts

    def test_the_paper_lock(self):
        with self.assertRaises(ibkr.PaperLockError):
            ibkr.check_paper(["U7654321"], 4002)                    # a live account
        with self.assertRaises(ibkr.PaperLockError):
            ibkr.check_paper(["DU1234567"], 4001)                   # IB Gateway's live port
        with self.assertRaises(ibkr.PaperLockError):
            ibkr.check_paper([], 4002)
        live = FakeIB(accounts=("DU1", "U2"))
        with self.assertRaises(ibkr.PaperLockError):
            ibkr.IBKRPaperBroker(self.c, self.log, ap, ib=live, lib=FakeLib)
        self.assertFalse(live.connected)                            # disconnected before any order
        self.assertEqual(live.placed, [])

    def test_run_refuses_a_live_port_before_connecting(self):
        c = cfg(ibkr_port=4001)
        with self.assertRaises(ap.AutopilotError) as e:
            ap.make_broker("ibkr", c, self.log)
        self.assertIn("LIVE trading port", str(e.exception))

    def test_contracts(self):
        self.assertEqual(ibkr.ibkr_contract("RY.TO"), ("RY", "TSE", "CAD"))
        self.assertEqual(ibkr.ibkr_contract("BBD-B.TO"), ("BBD.B", "TSE", "CAD"))
        self.assertEqual(ibkr.ibkr_contract("AUMB.V"), ("AUMB", "VENTURE", "CAD"))

    def test_bracket_then_target_with_ibkrs_own_fills(self):
        b = bars(RANGE + BREAK + [FLAT] * 80)
        (t,) = self.step(b, "09:50")                                # the 9:45 bar has closed above 10.50
        self.assertEqual(t["status"], "sent")
        entry, tp, sl = self.ib.placed
        self.assertEqual((entry.order.action, entry.order.lmtPrice, entry.order.totalQuantity), ("BUY", 10.63, 133))
        self.assertEqual((tp.order.lmtPrice, sl.order.auxPrice), (t["target"], t["stop"]))
        self.assertTrue(all(p.order.orderRef == "autopilot-20261028-ABC.TO" for p in self.ib.placed))
        self.assertTrue(all(p.order.account == "DU1234567" for p in self.ib.placed))
        self.assertEqual(entry.contract.exchange, "TSE")

        at = datetime.datetime.combine(DAY, datetime.time(9, 51))
        self.ib.fill(entry.order.orderId, "BOT", 133, 10.62, at)
        (t,) = self.step(b, "09:55")
        self.assertEqual((t["status"], t["entry"]), ("open", 10.62))
        self.assertEqual(len(self.ib.placed), 3)                    # no second order for the same symbol

        self.ib.fill(tp.order.orderId, "SLD", 133, 10.97, at + datetime.timedelta(minutes=20), fee=1.25)
        (t,) = self.step(b, "10:15")
        self.assertEqual((t["status"], t["reason"], t["fees"]), ("closed", "target", 2.25))
        self.assertEqual(t["pnl"], round((10.97 - 10.62) * 133 - 2.25, 2))

    def test_an_unfilled_entry_is_cancelled(self):
        b = bars(RANGE + BREAK + [FLAT] * 80)
        self.step(b, "09:50")
        (t,) = self.step(b, "10:00")
        self.assertEqual(t["status"], "missed")
        self.assertEqual(len(self.ib.cancelled), 3)

    def test_flat_by_the_close(self):
        b = bars(RANGE + BREAK + [FLAT] * 80)
        self.step(b, "09:50")
        entry = self.ib.placed[0]
        self.ib.fill(entry.order.orderId, "BOT", 133, 10.62, datetime.datetime.combine(DAY, datetime.time(9, 51)))
        self.step(b, "12:00")
        self.step(b, "15:55")
        flat = self.ib.placed[-1].order
        self.assertEqual((flat.action, flat.orderType, flat.totalQuantity), ("SELL", "MKT", 133))
        self.assertEqual(len(self.ib.cancelled), 2)                 # the target and the stop


if __name__ == "__main__":
    unittest.main()
