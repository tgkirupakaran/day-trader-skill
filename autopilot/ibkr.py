"""The IBKR paper broker for autopilot.py: the same rule, real orders on a PAPER account.

It needs IB Gateway (or TWS) running and logged in to your PAPER account, with
the API turned on (Configure, Settings, API: "Enable ActiveX and Socket Clients",
port 4002 for IB Gateway paper), and one package in this folder only:

    python3 -m pip install ib_async

The rest of the pack stays standard-library only; this file is the one place
that talks to a broker.

The paper lock: it will not connect to IB Gateway's or TWS's live ports (4001,
7496), and once connected it checks every account the login can trade. IBKR
paper accounts start with "DU" (or "DF" for a paper advisor master). Anything
else, and it disconnects before a single order.

Each trade is one bracket order, tagged with an orderRef "autopilot-YYYYMMDD-SYMBOL":
  a BUY limit a little above the trigger bar's close (entry_buffer_pct, 0.2 %),
  a SELL limit at the target and a SELL stop at the stop, both tied to it.
If the BUY has not filled within entry_wait_minutes (10) it is cancelled
("missed"). By flat_by the open brackets are cancelled and what is left is
sold at market. Fills, prices and IBKR's own commissions come from the
account's executions, so the log shows what IBKR did, not what the bars say.
"""

import datetime

PAPER_PREFIXES = ("DU", "DF")
LIVE_PORTS = (4001, 7496)
REF = "autopilot-"


class PaperLockError(Exception):
    pass


def ibkr_contract(sym):
    """RY.TO -> ("RY", "TSE", "CAD"); AUMB.V -> ("AUMB", "VENTURE", "CAD"); BBD-B.TO -> BBD.B."""
    s = sym.upper()
    if s.endswith(".TO"):
        return s[:-3].replace("-", "."), "TSE", "CAD"
    if s.endswith(".V"):
        return s[:-2].replace("-", "."), "VENTURE", "CAD"
    return s.replace("-", " "), "SMART", "USD"


def check_paper(accounts, port):
    if port in LIVE_PORTS:
        raise PaperLockError("port %d is a LIVE trading port; the autopilot only connects to paper "
                             "(IB Gateway paper is 4002, TWS paper is 7497)" % port)
    accounts = [a for a in (accounts or []) if a]
    if not accounts:
        raise PaperLockError("IBKR did not say which account this login trades: not trading")
    live = [a for a in accounts if not a.upper().startswith(PAPER_PREFIXES)]
    if live:
        raise PaperLockError("account %s is not a paper account (paper accounts start with DU): "
                             "log IB Gateway in with your PAPER login" % ", ".join(live))
    return accounts[0]


class IBKRPaperBroker(object):
    name = "ibkr"

    def __init__(self, cfg, log_path, ap, ib=None, lib=None):
        """ap: the autopilot module (the rule, the log). ib/lib: for tests, a stand-in."""
        self.cfg, self.log_path, self.ap = cfg, log_path, ap
        host = cfg.get("ibkr_host") or "127.0.0.1"
        port = int(cfg.get("ibkr_port") or 4002)
        check_paper(["DU"], port)                       # the port, before any connection
        if lib is None:
            try:
                import ib_async as lib
            except ImportError:
                raise ap.AutopilotError("--broker ibkr needs the ib_async package: python3 -m pip install ib_async")
        self.lib = lib
        self.ib = ib or lib.IB()
        if not self.ib.isConnected():
            self.ib.connect(host, port, clientId=int(cfg.get("ibkr_client_id") or 17), timeout=20)
        try:
            self.account = check_paper(self.ib.managedAccounts(), port)
        except PaperLockError:
            self.ib.disconnect()
            raise
        self.contracts = {}

    def wait(self, seconds):
        self.ib.sleep(seconds)                          # keeps the connection's events flowing

    def close(self):
        try:
            self.ib.disconnect()
        except Exception:
            pass

    # -------------------------------------------------------- orders

    def contract(self, sym):
        if sym not in self.contracts:
            s, exch, cur = ibkr_contract(sym)
            c = self.lib.Stock(s, exch, cur)
            self.ib.qualifyContracts(c)
            self.contracts[sym] = c
        return self.contracts[sym]

    def send(self, rec, limit):
        bracket = self.ib.bracketOrder("BUY", rec["shares"], limit, rec["target"], rec["stop"])
        ids = []
        for o, role in zip(bracket, ("entry", "target", "stop")):
            o.orderRef = rec["ref"]
            o.account = self.account
            o.tif = "DAY"
            t = self.ib.placeOrder(self.contract(rec["symbol"]), o)
            ids.append(t.order.orderId)
        rec["order_ids"] = dict(zip(("entry", "target", "stop"), ids))

    def cancel(self, rec, roles):
        live = {t.order.orderId: t for t in self.ib.openTrades()}
        for role in roles:
            t = live.get(rec.get("order_ids", {}).get(role))
            if t:
                self.ib.cancelOrder(t.order)

    def flatten(self, rec, qty):
        self.cancel(rec, ("target", "stop"))
        o = self.lib.MarketOrder("SELL", qty)
        o.orderRef, o.account, o.tif = rec["ref"], self.account, "DAY"
        t = self.ib.placeOrder(self.contract(rec["symbol"]), o)
        rec.setdefault("order_ids", {})["flat"] = t.order.orderId

    # -------------------------------------------------------- the day

    def fills_by_ref(self, day):
        out = {}
        tag = REF + day.strftime("%Y%m%d") + "-"
        for f in self.ib.reqExecutions():
            ref = getattr(f.execution, "orderRef", "") or ""
            if ref.startswith(tag):
                out.setdefault(ref, []).append(f)
        return out

    def sync(self, day, by_symbol, cfg, now, final, since=None):
        ap = self.ap
        bounds = ap.day_bounds(day, cfg)
        if not bounds:
            return []
        close_dt, flat_dt = bounds
        today = [t for t in ap.read_log(self.log_path)["trades"] if t["date"] == day.isoformat()]
        mine = {t["symbol"]: t for t in today}
        fills = self.fills_by_ref(day)
        for t in today:
            self.update(t, fills.get(t["ref"], []), by_symbol.get(t["symbol"]), now)

        # new signals: act as soon as the trigger bar has closed
        if now < flat_dt:
            for sym, bars in sorted(by_symbol.items()):
                if sym in mine:
                    continue
                hit = ap.find_trigger([b for b in bars if b["at"] < close_dt], day, cfg, close_dt)
                if not hit:
                    continue
                trig = bars[hit["j"] - 1]
                if now - (trig["at"] + ap.BAR) > datetime.timedelta(minutes=10) or \
                        (since and trig["at"] + ap.BAR < since):
                    continue                            # a stale signal: the moment has passed
                ok, cash = ap.admit(today, now, cfg)
                if not ok:
                    break
                ref_px = trig["c"]
                rec = ap.plan_trade(day, sym, hit, trig["at"] + ap.BAR, ref_px, cfg, cash)
                if not rec:
                    continue
                buf = float(cfg.get("entry_buffer_pct") or 0.2) / 100
                limit = ap.to_tick(ref_px * (1 + buf), cfg["market"], up=True)
                rec.update(status="sent", ref=REF + day.strftime("%Y%m%d") + "-" + sym, limit=limit,
                           sent_at=now.strftime("%H:%M"), fees=0.0, account=self.account)
                self.send(rec, limit)
                today.append(rec)
                mine[sym] = rec

        # flat by the close
        if now >= flat_dt:
            for t in today:
                left = t.get("filled", 0) - t.get("sold", 0)
                if t["status"] == "open" and left > 0 and "flat" not in t.get("order_ids", {}):
                    self.flatten(t, left)
                elif t["status"] == "sent":
                    self.cancel(t, ("entry", "target", "stop"))
                    t["status"], t["reason"] = "missed", "not filled by flat_by"
        return today

    def update(self, t, fills, bars, now):
        """Entry, exit and commissions from the account's executions."""
        if t["status"] in ("closed", "missed"):
            return
        ids = t.get("order_ids", {})
        role_of = {v: k for k, v in ids.items()}
        buys = [f for f in fills if f.execution.side == "BOT"]
        sells = [f for f in fills if f.execution.side == "SLD"]
        bought = sum(f.execution.shares for f in buys)
        sold = sum(f.execution.shares for f in sells)
        fees = sum(abs(getattr(f.commissionReport, "commission", 0) or 0) for f in fills)
        t["fees"] = self.ap.money(fees)
        if bought:
            t["filled"] = int(bought)
            t["entry"] = round(sum(f.execution.price * f.execution.shares for f in buys) / bought, 4)
            t["shares"] = int(bought)
            t["risk_total"] = self.ap.money((t["entry"] - t["stop"]) * bought) or t["risk_total"]
        if sold:
            t["sold"] = int(sold)
        if bought and sold >= bought:
            last = max(sells, key=lambda f: f.time)
            px = sum(f.execution.price * f.execution.shares for f in sells) / sold
            reason = {"target": "target", "stop": "stop", "flat": "flat by %s" % self.cfg["flat_by"]}.get(
                role_of.get(last.execution.orderId), "sold")
            at = self.ap.E.to_eastern(last.time.replace(tzinfo=None)) if last.time.tzinfo else last.time
            self.ap.close(t, at.strftime("%H:%M"), px, reason)
            t["fees"] = self.ap.money(fees)
            t["pnl"] = self.ap.money((px - t["entry"]) * bought - fees)
            return
        if bought:
            t["status"] = "open"
            if bars:
                t["mark"] = self.ap.money(bars[-1]["c"])
                t["pnl"] = self.ap.money((bars[-1]["c"] - t["entry"]) * bought - fees)
                t["r"] = round(t["pnl"] / t["risk_total"], 2) if t["risk_total"] else 0.0
            return
        wait = int(self.cfg.get("entry_wait_minutes") or 10)
        sent = datetime.datetime.combine(now.date(), datetime.datetime.strptime(t["sent_at"], "%H:%M").time())
        if now - sent >= datetime.timedelta(minutes=wait):
            self.cancel(t, ("entry", "target", "stop"))
            t["status"], t["reason"], t["pnl"] = "missed", "entry not filled in %d minutes" % wait, 0.0
