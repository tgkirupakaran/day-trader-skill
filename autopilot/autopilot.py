#!/usr/bin/env python3
"""Autopilot: the opening-range breakout, run by itself on a paper account.

Two ways to fill its trades, one rule and one log:
  --broker sim    (the default) fills are simulated from Yahoo's 5-minute bars,
                  with your commission and some slippage. Standard library only.
  --broker ibkr   orders go to your Interactive Brokers PAPER account through IB
                  Gateway (see ibkr.py). It refuses any account that is not paper.

    python3 autopilot/autopilot.py backtest                   # the last ~60 days
    python3 autopilot/autopilot.py sweep                      # try rule variations
    python3 autopilot/autopilot.py run                        # trade today on paper
    python3 autopilot/autopilot.py run --broker ibkr          # the same, IBKR paper
    python3 autopilot/autopilot.py dashboard                  # -> trader/autopilot/dashboard.html

The rule, the same one trader-entry and trader-exit use:
  1. The opening range is the high and low of the first or_minutes (15: 9:30 to 9:45).
  2. Buy when a 5-minute bar CLOSES above the range high, between no_entries_before
     (09:45) and no_entries_after (15:30). The fill is the NEXT bar's open plus
     slippage: the bot can only act once the bar has closed.
  3. Stop at the middle of the range (stop_rule "mid") or its low ("low"). Target
     target_r (default 1) times the risk above the entry. If one bar touches both,
     the stop counts (the cautious read).
  4. Out by flat_by (15:55) whatever happens. One trade per symbol per day.
  Shares: (max loss - commission in and out) / risk a share, capped by max_order
  and by the cash not already in an open trade. At most max_trades_per_day trades,
  and none after the day's closed losses reach max_daily_loss.

trader/config.json keys it reads ("_cad" for "market": "ca", "_usd" otherwise):
  market, budget, commission_per_trade, max_loss_per_trade_*, max_daily_loss_*,
  max_order_*, max_trades_per_day, no_entries_before, no_entries_after, flat_by,
  and its own: autopilot_symbols (a list), target_r, stop_rule, slippage_per_share,
  or_minutes.

The calendar, the chart fetch and the clock come from trader-entry/entry.py, loaded
from its folder, so the bot uses the very same TSX/NYSE holidays and bars as the
Entry Timer instead of a second copy that could drift.

Paper only. Not financial advice. A paper result is not a promise: real fills are worse.
"""

import argparse
import datetime
import html
import importlib.util
import itertools
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(folder, name, as_name):
    path = os.path.join(HERE, os.pardir, folder, name + ".py") if folder else os.path.join(HERE, name + ".py")
    spec = importlib.util.spec_from_file_location(as_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E = _load("trader-entry", "entry", "autopilot_entry")

OUT_DIR = os.path.join("trader", "autopilot")
BAR = datetime.timedelta(minutes=5)
DEFAULT_SYMBOLS = {
    "ca": ["RY.TO", "TD.TO", "BNS.TO", "BMO.TO", "CM.TO", "NA.TO", "MFC.TO", "SLF.TO", "BN.TO", "ENB.TO",
           "TRP.TO", "CNQ.TO", "SU.TO", "CVE.TO", "IMO.TO", "TOU.TO", "ARX.TO", "CNR.TO", "CP.TO", "SHOP.TO",
           "CSU.TO", "OTEX.TO", "BCE.TO", "T.TO", "RCI.B.TO", "ATD.TO", "L.TO", "DOL.TO", "ABX.TO", "AEM.TO",
           "K.TO", "WPM.TO", "FNV.TO", "TECK.B.TO", "NTR.TO", "CCO.TO", "FM.TO", "GIB.A.TO", "QSR.TO", "WSP.TO"],
    "us": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "AMD", "GOOGL", "NFLX", "AVGO"],
}
DEFAULTS = {
    "market": "us",
    "budget": 5000,
    "commission_per_trade": 0,
    "max_loss_per_trade": 50,
    "max_daily_loss": 150,
    "max_order": None,          # default: the budget
    "max_trades_per_day": 3,
    "no_entries_before": "09:45",
    "no_entries_after": "15:30",
    "flat_by": "15:55",
    "target_r": 1.0,
    "stop_rule": "mid",
    "slippage_per_share": 0.01,
    "or_minutes": 15,
    "setup": "orb",              # "orb" or "vwap_revert"
    "min_volume_x": 0,           # orb: the trigger bar's volume vs the range bars' average (0: off)
    "above_vwap": False,         # orb: the trigger close must also be above the day's VWAP
    "revert_pct": 1.5,           # vwap_revert: buy a close this far under VWAP
    "revert_stop_pct": 1.0,      # vwap_revert: stop this far under the entry
    "autopilot_symbols": None,
    # --broker ibkr only (ibkr.py)
    "ibkr_host": "127.0.0.1",
    "ibkr_port": 4002,           # IB Gateway paper; TWS paper is 7497
    "ibkr_client_id": 17,
    "entry_buffer_pct": 0.2,     # the BUY limit, this far above the trigger close
    "entry_wait_minutes": 10,    # cancel a BUY that has not filled by then
}
MONEY_KEYS = ("max_loss_per_trade", "max_daily_loss", "max_order")
SWEEP_ORB = {"or_minutes": (5, 15, 30), "target_r": (1.0, 2.0, 3.0), "stop_rule": ("mid", "low"),
             "filter": ("none", "volume", "vwap")}
MIN_TRADES = 30                  # the sweep's star wants at least this many trades
SWEEP_VWAP = {"revert_pct": (1.0, 1.5, 2.0, 3.0), "revert_stop_pct": (0.5, 1.0, 1.5)}


class AutopilotError(Exception):
    pass


# ---------------------------------------------------------------- config

def read_config(path, market=None, **override):
    cfg = dict(DEFAULTS)
    doc = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise AutopilotError("%s is not valid JSON: %s" % (path, e))
    cfg["market"] = (market or doc.get("market") or "us").lower()
    cur = "_cad" if cfg["market"] == "ca" else "_usd"
    for k in DEFAULTS:
        v = doc.get(k + cur, doc.get(k)) if k in MONEY_KEYS else doc.get(k)
        if v is not None:
            cfg[k] = v
    cfg.update((k, v) for k, v in override.items() if v is not None)
    return check_config(cfg)


def check_config(cfg):
    E.use_market(cfg["market"])
    for k in ("no_entries_before", "no_entries_after", "flat_by"):
        E.parse_hhmm(cfg[k], k)
    for k in ("budget", "commission_per_trade", "max_loss_per_trade", "max_daily_loss",
              "target_r", "slippage_per_share", "min_volume_x", "revert_pct", "revert_stop_pct"):
        cfg[k] = float(cfg[k])
    cfg["max_order"] = float(cfg["max_order"] or cfg["budget"])
    cfg["max_trades_per_day"] = int(cfg["max_trades_per_day"])
    cfg["or_minutes"] = int(cfg["or_minutes"])
    if cfg["or_minutes"] % 5 or cfg["or_minutes"] <= 0:
        raise AutopilotError("or_minutes wants a multiple of 5, not %s" % cfg["or_minutes"])
    if cfg["stop_rule"] not in ("mid", "low"):
        raise AutopilotError('stop_rule wants "mid" or "low", not %s' % cfg["stop_rule"])
    if cfg["setup"] not in ("orb", "vwap_revert"):
        raise AutopilotError('setup wants "orb" or "vwap_revert", not %s' % cfg["setup"])
    if cfg["revert_pct"] <= 0 or cfg["revert_stop_pct"] <= 0:
        raise AutopilotError("revert_pct and revert_stop_pct must be above 0")
    cfg["above_vwap"] = bool(cfg["above_vwap"])
    if cfg["target_r"] <= 0:
        raise AutopilotError("target_r must be above 0")
    if cfg["max_loss_per_trade"] <= 2 * cfg["commission_per_trade"]:
        raise AutopilotError("max_loss_per_trade must be more than the commission in and out")
    return cfg


def symbols_for(cfg, cli=None):
    syms = cli or cfg.get("autopilot_symbols") or DEFAULT_SYMBOLS[cfg["market"]]
    if isinstance(syms, str):
        syms = syms.split(",")
    syms = [s.strip().upper() for s in syms if s.strip()]
    if cfg["market"] == "ca":
        syms = [E.ca_symbol(s) for s in syms]
    return syms


# ---------------------------------------------------------------- prices

def tick(price, market):
    """TSX: a cent, or half a cent under $0.50. US: a cent."""
    return 0.005 if market == "ca" and price < 0.5 else 0.01


def to_tick(x, market, up):
    t = tick(x, market)
    n = x / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * t, 3)


def money(x):
    return round(float(x), 2) + 0.0


# ---------------------------------------------------------------- bars

def eastern_bars(chart):
    """Yahoo bars -> {date: [bar...]} in New York time, 5-minute grid only."""
    days = {}
    for b in chart["bars"]:
        at = E.to_eastern(E.utc_of(b["t"]))
        if at.minute % 5 or at.second:
            continue                              # Yahoo's live tick, not a bar
        d = dict(b)
        d["at"] = at
        days.setdefault(at.date(), []).append(d)
    for d in days:
        days[d].sort(key=lambda b: b["at"])
    return days


def closed_only(bars, now_eastern):
    """A bar counts once it has closed."""
    return [b for b in bars if b["at"] + BAR <= now_eastern]


# ---------------------------------------------------------------- the rule

def vwap_series(bars, open_dt):
    """The day's running VWAP after each bar (from the open), by bar index."""
    pv = vol = 0.0
    out = []
    for b in bars:
        if b["at"] >= open_dt:
            v = float(b.get("v") or 0)
            pv += (b["h"] + b["l"] + b["c"]) / 3 * v
            vol += v
        out.append(pv / vol if vol else b["c"])
    return out


def setup_name(cfg):
    if cfg["setup"] == "vwap_revert":
        return "vwap revert %g%%, stop %g%%" % (cfg["revert_pct"], cfg["revert_stop_pct"])
    extra = (", volume %gx" % cfg["min_volume_x"] if cfg["min_volume_x"] else "") + \
        (", above vwap" if cfg["above_vwap"] else "")
    return "orb %dm, stop %s, %gR%s" % (cfg["or_minutes"], cfg["stop_rule"], cfg["target_r"], extra)


def find_trigger(bars, day, cfg, close_dt):
    """The first signal of the day, or None: {"j": index of the fill bar (the one
    after the signal bar; it may not have opened yet), and how to set the stop and
    target}. Signal bars must close between no_entries_before and no_entries_after."""
    open_dt = datetime.datetime.combine(day, E.OPEN)
    first = datetime.datetime.combine(day, E.parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    last = E.early_cutoff(day, E.parse_hhmm(cfg["no_entries_after"], "no_entries_after"), close_dt)
    vw = vwap_series(bars, open_dt)
    if cfg["setup"] == "vwap_revert":
        for i, b in enumerate(bars):
            end = b["at"] + BAR
            if b["at"] < open_dt or end < first:
                continue
            if end > last:
                return None
            if b["c"] <= vw[i] * (1 - cfg["revert_pct"] / 100):
                return {"j": i + 1, "stop_pct": cfg["revert_stop_pct"], "target_px": vw[i]}
        return None
    or_end = open_dt + datetime.timedelta(minutes=cfg["or_minutes"])
    rng = [b for b in bars if open_dt <= b["at"] < or_end]
    if len(rng) < cfg["or_minutes"] // 5:
        return None
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    avg_v = sum(float(b.get("v") or 0) for b in rng) / len(rng)
    for i, b in enumerate(bars):
        end = b["at"] + BAR
        if b["at"] < or_end or end < first:
            continue
        if end > last:
            return None
        if b["c"] > hi:
            if cfg["min_volume_x"] and float(b.get("v") or 0) < cfg["min_volume_x"] * avg_v:
                return None                        # the first break was on thin volume: no trade today
            if cfg["above_vwap"] and b["c"] <= vw[i]:
                return None
            return {"j": i + 1, "or_high": hi, "or_low": lo,
                    "stop_px": (hi + lo) / 2 if cfg["stop_rule"] == "mid" else lo}
    return None


def shares_for(entry, risk, cfg, cash):
    fee = cfg["commission_per_trade"]
    n = int((cfg["max_loss_per_trade"] - 2 * fee) / risk + 1e-9)
    n = min(n, int(cfg["max_order"] / entry), int(max(cash - fee, 0) / entry))
    return max(n, 0)


def plan_trade(day, sym, sig, fill_at, fill_open, cfg, cash):
    """The order the rule wants: entry, stop, target, shares. None if it can't size one."""
    m, slip, fee = cfg["market"], cfg["slippage_per_share"], cfg["commission_per_trade"]
    entry = to_tick(fill_open + slip, m, up=True)
    if "stop_pct" in sig:
        stop = to_tick(entry * (1 - sig["stop_pct"] / 100), m, up=False)
    else:
        stop = to_tick(sig["stop_px"], m, up=False)
    if entry <= stop:
        return None                                # it opened back under the stop
    risk = entry - stop
    if "target_px" in sig:
        target = to_tick(sig["target_px"], m, up=True)
        if target <= entry:
            return None                                # already back at the target
    else:
        target = to_tick(entry + cfg["target_r"] * risk, m, up=True)
    n = shares_for(entry, risk, cfg, cash)
    if n < 1:
        return None
    rec = {"date": day.isoformat(), "symbol": sym, "setup": setup_name(cfg),
           "entry_time": fill_at.strftime("%H:%M"), "entry": entry, "stop": stop, "target": target,
           "shares": n, "fees": money(2 * fee), "risk_total": money(risk * n), "status": "open"}
    if "or_high" in sig:
        rec.update(or_high=round(sig["or_high"], 3), or_low=round(sig["or_low"], 3))
    return rec


def trade_out(bars, j, rec, cfg, flat_dt, final):
    """Walk from the fill bar to the exit and fill in the exit fields of rec."""
    m, slip = cfg["market"], cfg["slippage_per_share"]
    stop, target = rec["stop"], rec["target"]
    for b in bars[j:]:
        if b["at"] >= flat_dt:
            return close(rec, b["at"], to_tick(max(b["o"] - slip, 0), m, up=False), "flat by %s" % cfg["flat_by"])
        if b["l"] <= stop:                         # a gap down fills past the stop
            return close(rec, b["at"] + BAR, to_tick(max(min(stop, b["o"]) - slip, 0), m, up=False), "stop")
        if b["h"] >= target:                       # a limit sell: the target, or a better open
            return close(rec, b["at"] + BAR, max(target, b["o"]), "target")
    last = bars[-1]
    if final:
        return close(rec, last["at"] + BAR, to_tick(max(last["c"] - slip, 0), m, up=False), "close")
    rec.update(status="open", mark=money(last["c"]),
               pnl=money((last["c"] - rec["entry"]) * rec["shares"] - rec["fees"]))
    rec["r"] = round(rec["pnl"] / rec["risk_total"], 2)
    return rec


def close(rec, at, px, reason):
    """Record an exit. at: a datetime or "HH:MM"; px: the fill price a share."""
    rec.update(status="closed", exit_time=at if isinstance(at, str) else at.strftime("%H:%M"),
               exit=round(px, 3), reason=reason)
    rec.pop("mark", None)
    rec["pnl"] = money((px - rec["entry"]) * rec["shares"] - rec["fees"])
    rec["r"] = round(rec["pnl"] / rec["risk_total"], 2) if rec["risk_total"] else 0.0
    return rec


def day_bounds(day, cfg):
    sess = E.session(day)
    if not sess:
        return None
    close_dt = datetime.datetime.combine(day, sess[1])
    flat_dt = min(datetime.datetime.combine(day, E.parse_hhmm(cfg["flat_by"], "flat_by")), close_dt - BAR)
    return close_dt, flat_dt


def day_signals(day, by_symbol, cfg):
    """[(fill time, symbol, bars, signal)] in fill order, for the bars so far."""
    b = day_bounds(day, cfg)
    if not b:
        return []
    close_dt, flat_dt = b
    out = []
    for sym, bars in by_symbol.items():
        bars = [x for x in bars if x["at"] < close_dt]
        hit = find_trigger(bars, day, cfg, close_dt)
        if hit and hit["j"] < len(bars) and bars[hit["j"]]["at"] < flat_dt:
            out.append((bars[hit["j"]]["at"], sym, bars, hit))
    out.sort(key=lambda c: (c[0], c[1]))
    return out


def admit(taken, at, cfg):
    """Can one more trade start at `at`? (ok, cash free)."""
    hhmm = at.strftime("%H:%M")
    taken = [t for t in taken if t["status"] != "missed"]   # an entry that never filled
    if len(taken) >= cfg["max_trades_per_day"]:
        return False, 0
    lost = sum(t["pnl"] for t in taken if t["status"] == "closed" and t["exit_time"] <= hhmm)
    if lost <= -cfg["max_daily_loss"]:
        return False, 0
    busy = sum(t["entry"] * t["shares"] for t in taken
               if t["status"] != "closed" or t["exit_time"] > hhmm)
    return True, cfg["budget"] - busy


def day_trades(day, by_symbol, cfg, final=True, since=None):
    """Every simulated paper trade on `day`, across symbols, in the order they fill.
    by_symbol: {symbol: [bars of that day, New York time, closed only]}. since: the
    bot only counts fills at or after this time (when it started watching)."""
    b = day_bounds(day, cfg)
    if not b:
        return []
    flat_dt = b[1]
    taken = []
    for at, sym, bars, sig in day_signals(day, by_symbol, cfg):
        if since and at < since:
            continue                               # before the bot was watching: hindsight
        ok, cash = admit(taken, at, cfg)
        if not ok:
            break
        rec = plan_trade(day, sym, sig, at, bars[sig["j"]]["o"], cfg, cash)
        if rec:
            taken.append(trade_out(bars, sig["j"], rec, cfg, flat_dt, final))
    return taken


def replay(data, cfg, now):
    """Every day in data ({symbol: {date: bars}}) up to now -> trades."""
    trades = []
    for day in sorted(set(d for bars in data.values() for d in bars)):
        if day > now.date():
            continue
        by = {s: closed_only(b[day], now) for s, b in data.items() if day in b}
        trades += day_trades(day, by, cfg, final=day < now.date() or now.time() >= E.CLOSE)
    return trades


# ---------------------------------------------------------------- results

def summary(trades):
    trades = [t for t in trades if t["status"] != "missed"]
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    net = sum(t["pnl"] for t in closed)
    peak = run = dd = 0.0
    for t in closed:
        run += t["pnl"]
        peak = max(peak, run)
        dd = min(dd, run - peak)
    n = len(closed)
    return {
        "trades": n, "open": len(trades) - n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n, 3) if n else None,
        "net_pnl": money(net), "fees": money(sum(t["fees"] for t in closed)),
        "gross_pnl": money(net + sum(t["fees"] for t in closed)),
        "avg_win": money(sum(t["pnl"] for t in wins) / len(wins)) if wins else None,
        "avg_loss": money(sum(t["pnl"] for t in losses) / len(losses)) if losses else None,
        "expectancy": money(net / n) if n else None,
        "avg_r": round(sum(t["r"] for t in closed) / n, 2) if n else None,
        "max_drawdown": money(dd),
        "days": len(set(t["date"] for t in closed)),
    }


def verdict(s):
    if not s["trades"]:
        return "No trades yet."
    tail = " Too few trades to judge: wait for 100 or more." if s["trades"] < 100 else ""
    if s["net_pnl"] > 0:
        head = "Profitable on paper so far, after commissions and slippage."
    elif s["net_pnl"] == 0:
        head = "Breaking even on paper, after costs."
    elif s["gross_pnl"] > 0:
        head = "Losing on paper: the rule wins before costs, but commissions and slippage eat it."
    else:
        head = "Losing on paper, even before commissions."
    return head + tail


def read_log(path):
    if not os.path.exists(path):
        return {"trades": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def merge(path, new, dates):
    """Replace the records of `dates` in the log at path with `new`."""
    old = read_log(path)
    keep = [t for t in old["trades"] if t["date"] not in dates]
    doc = dict(old, trades=sorted(keep + new, key=lambda t: (t["date"], t["entry_time"], t["symbol"])))
    write_json(path, doc)
    return doc


# ---------------------------------------------------------------- data

def fetch(sym, rng, offline=None):
    if offline:
        text = E.read_text(E.offline_file(offline, sym))
    else:
        text = E.yahoo_get(E.chart_path(sym, rng, "5m"))
    ch = E.parse_chart(text, sym)
    if ch["symbol"] != E.yahoo_symbol(sym):
        raise AutopilotError("the chart holds %s, not %s" % (ch["symbol"], sym))
    return eastern_bars(ch)


def fetch_all(syms, rng, offline=None, pause=0.5):
    data, missing = {}, []
    for i, sym in enumerate(syms):
        try:
            data[sym] = fetch(sym, rng, offline)
        except Exception as e:  # one bad ticker should not stop the run
            missing.append("%s (%s)" % (sym, e))
        if not offline and i + 1 < len(syms):
            time.sleep(pause)
    return data, missing


def now_eastern(text=None):
    return E.to_eastern(E.parse_now(text) if text else E.utc_now())


# ---------------------------------------------------------------- dashboard

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e2dc;--up:#1a7f4b;--down:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1f1f1d;--ink:#ecebe6;--mute:#a3a29b;--line:#34342f;--up:#4cc38a;--down:#f2766b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--mute);margin:0 0 20px}
.verdict{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:16px;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:21px;font-variant-numeric:tabular-nums}.tile span{color:var(--mute);font-size:13px}
.up{color:var(--up)}.down{color:var(--down)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:20px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:14px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--mute);font-weight:600}td.l,th.l{text-align:left}svg text{fill:var(--mute);font-size:12px}
"""


def fmt_money(v, sign):
    if v is None:
        return "-"
    return ("-" if v < 0 else "") + sign + "{:,.2f}".format(abs(v))


def tone(v):
    return "" if v is None else ("up" if v > 0 else "down" if v < 0 else "")


def equity_svg(trades, sign):
    closed = [t for t in trades if t["status"] == "closed"]
    if not closed:
        return "<p>No closed trades yet.</p>"
    pts, run = [0.0], 0.0
    for t in closed:
        run += t["pnl"]
        pts.append(run)
    w, h, left = 1000, 260, 80
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0

    def x(i):
        return left + (w - left - 10) * i / max(len(pts) - 1, 1)

    def y(v):
        return 12 + (h - 40) * (hi - v) / span

    line = " ".join("%.1f,%.1f" % (x(i), y(v)) for i, v in enumerate(pts))
    col = "var(--up)" if pts[-1] >= 0 else "var(--down)"
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="running profit and loss after costs">'
            '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="var(--line)" stroke-dasharray="4 4"/>'
            '<text x="%d" y="%.1f" text-anchor="end">0</text>'
            '<text x="%d" y="16" text-anchor="end">%s</text><text x="%d" y="%d" text-anchor="end">%s</text>'
            '<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>'
            '<text x="%d" y="%d">trade 1</text><text x="%d" y="%d" text-anchor="end">trade %d</text></svg>'
            % (w, h, left, w - 10, y(0), y(0), left - 8, y(0) + 4, left - 8, fmt_money(hi, sign),
               left - 8, h - 24, fmt_money(lo, sign), col, line, left, h - 4, w - 10, h - 4, len(pts) - 1))


def render_dashboard(doc, cfg, title, refresh=None, now=None):
    trades = doc["trades"]
    s = summary(trades)
    sign = "C$" if cfg["market"] == "ca" else "$"
    esc = html.escape

    def m(v):
        return fmt_money(v, sign)

    tiles = [
        ("Net P/L after costs", m(s["net_pnl"]), tone(s["net_pnl"])),
        ("P/L before costs", m(s["gross_pnl"]), tone(s["gross_pnl"])),
        ("Closed trades", "%d" % s["trades"] + (" (+%d open)" % s["open"] if s["open"] else ""), ""),
        ("Win rate", "-" if s["win_rate"] is None else "%.0f%%" % (100 * s["win_rate"]), ""),
        ("Average per trade", m(s["expectancy"]), tone(s["expectancy"])),
        ("Average win / loss", "%s / %s" % (m(s["avg_win"]), m(s["avg_loss"])), ""),
        ("Commissions paid", m(s["fees"]), ""),
        ("Worst drop from a high", m(s["max_drawdown"]), tone(s["max_drawdown"])),
    ]
    rows = []
    for t in reversed(trades):
        pnl = t.get("pnl")
        rows.append(
            "<tr><td class=l>%s</td><td class=l>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d</td>"
            "<td>%s</td><td>%s</td><td class=l>%s</td><td class='%s'>%s</td><td>%s</td></tr>" % (
                esc(t["date"]), esc(t["symbol"]), esc(t["entry_time"]), "%.3f" % t["entry"] if t["entry"] < 0.5
                else "%.2f" % t["entry"], "%.2f" % t["stop"], "%.2f" % t["target"], t["shares"],
                esc(t.get("exit_time") or "-"), "-" if t.get("exit") is None else "%.2f" % t["exit"],
                esc(t.get("reason") or "still open"), tone(pnl), m(pnl), "%.2f" % (t.get("r") or 0)))
    head = ("<tr><th class=l>Date</th><th class=l>Ticker</th><th>In at</th><th>Entry</th><th>Stop</th>"
            "<th>Target</th><th>Shares</th><th>Out at</th><th>Exit</th><th class=l>Why out</th>"
            "<th>P/L after costs</th><th>R</th></tr>")
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
    source = {"ibkr": "IBKR paper account fills", "sim": "simulated fills"}.get(doc.get("broker"), "simulated fills")
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>%s<title>Autopilot P/L</title>"
        "<style>%s</style></head><body><main><h1>%s</h1>"
        "<p class=sub>Paper trades only, %s. Rule: %s. Commission %s each way, slippage %s a share. Updated %s.</p>"
        "<div class=verdict>%s</div><div class=tiles>%s</div>"
        "<div class=card><b>Running P/L after costs, trade by trade</b>%s</div>"
        "<div class=card><table>%s%s</table></div>"
        "<p class=sub>Not financial advice. Paper fills are kinder than real ones.</p></main></body></html>" % (
            meta, CSS, esc(title), source,
            esc(setup_name(cfg)),
            m(cfg["commission_per_trade"]), m(cfg["slippage_per_share"]),
            (now or now_eastern()).strftime("%Y-%m-%d %H:%M ET"), esc(verdict(s)),
            "".join("<div class=tile><span>%s</span><b class='%s'>%s</b></div>" % (a, c, esc(b)) for a, b, c in tiles),
            equity_svg(trades, sign), head, "".join(rows) or "<tr><td class=l colspan=12>No trades yet.</td></tr>"))


def write_dashboard(cfg, which="paper", refresh=None, out=None, now=None):
    src = os.path.join(OUT_DIR, "trades.json" if which == "paper" else "backtest.json")
    doc = read_log(src)
    title = "Autopilot: paper trades" if which == "paper" else "Autopilot: backtest on the last ~60 days"
    path = out or os.path.join(OUT_DIR, "dashboard.html" if which == "paper" else "backtest.html")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_dashboard(doc, cfg, title, refresh, now))
    return path, summary(doc["trades"])


def print_summary(s, sign):
    wr = "-" if s["win_rate"] is None else "%.0f%%" % (100 * s["win_rate"])
    print("%d closed trades over %d days, %d won (%s). Before costs %s, after costs %s, %s a trade; "
          "commissions %s, worst drop %s." % (
              s["trades"], s["days"], s["wins"], wr, fmt_money(s["gross_pnl"], sign),
              fmt_money(s["net_pnl"], sign), fmt_money(s["expectancy"], sign), fmt_money(s["fees"], sign),
              fmt_money(s["max_drawdown"], sign)))
    print(verdict(s))


# ---------------------------------------------------------------- the paper broker

class SimBroker(object):
    """Fills come from the bars themselves: the trades day_trades() simulates."""
    name = "sim"

    def sync(self, day, by_symbol, cfg, now, final, since=None):
        return day_trades(day, by_symbol, cfg, final=final, since=since)

    def wait(self, seconds):
        time.sleep(seconds)

    def close(self):
        pass


def make_broker(name, cfg, log_path):
    if name == "sim":
        return SimBroker()
    ibkr = _load(None, "ibkr", "autopilot_ibkr")
    try:
        me = sys.modules.get(__name__) or type(sys)("autopilot")
        if me.__dict__ is not globals():
            me.__dict__.update(globals())              # loaded by path, as the tests do
        return ibkr.IBKRPaperBroker(cfg, log_path, me)
    except ibkr.PaperLockError as e:
        raise AutopilotError("paper lock: %s" % e)
    except (OSError, TimeoutError) as e:
        raise AutopilotError("could not reach IB Gateway at %s:%s (%s): is it running and logged in "
                             "with your paper login?" % (cfg["ibkr_host"], cfg["ibkr_port"], e))


# ---------------------------------------------------------------- commands

def cmd_backtest(a, cfg):
    data, missing = fetch_all(symbols_for(cfg, a.symbols), "60d", a.offline, a.pause)
    trades = replay(data, cfg, now_eastern(a.now))
    write_json(os.path.join(OUT_DIR, "backtest.json"), {"broker": "sim", "symbols": sorted(data), "trades": trades})
    path, s = write_dashboard(cfg, "backtest")
    if missing:
        print("skipped: " + "; ".join(missing))
    print_summary(s, E.SIGN)
    print("dashboard: " + path)
    return 0


def variations():
    """Every rule the sweep tries, as config overrides."""
    out = []
    flt = {"none": {}, "volume": {"min_volume_x": 1.5}, "vwap": {"above_vwap": True}}
    for orm, tr, sr, f in itertools.product(*SWEEP_ORB.values()):
        v = dict(setup="orb", or_minutes=orm, target_r=tr, stop_rule=sr, min_volume_x=0, above_vwap=False)
        v.update(flt[f])
        out.append(v)
    for rp, sp in itertools.product(*SWEEP_VWAP.values()):
        out.append(dict(setup="vwap_revert", revert_pct=rp, revert_stop_pct=sp))
    return out


def cmd_sweep(a, cfg):
    data, missing = fetch_all(symbols_for(cfg, a.symbols), "60d", a.offline, a.pause)
    now = now_eastern(a.now)
    days = sorted(set(d for bars in data.values() for d in bars if d <= now.date()))
    half = days[len(days) // 2].isoformat() if days else ""
    rows = []
    for v in variations():
        c = check_config(dict(cfg, **v))
        trades = replay(data, c, now)
        rows.append((setup_name(c), v, summary(trades), summary([t for t in trades if t["date"] < half]),
                     summary([t for t in trades if t["date"] >= half])))
    rows.sort(key=lambda r: -(r[2]["net_pnl"]))
    if missing:
        print("skipped: " + "; ".join(missing))
    print("%d symbols, %d days. Ranked by net P/L after costs. 'first half' and 'second half' split the days:"
          % (len(data), len(days)))
    print("a variation that only works in one half is probably luck. * = made money in both halves, on %d+ "
          "trades (fewer proves nothing).\n" % MIN_TRADES)
    print("%-44s %6s %5s %12s %12s %11s %11s" % (
        "rule", "trades", "won", "before costs", "after costs", "first half", "second half"))
    for name, v, s, early, late in rows:
        both = "*" if early["net_pnl"] > 0 and late["net_pnl"] > 0 and s["trades"] >= MIN_TRADES else " "
        print("%s%-43s %6d %4s%% %12s %12s %11s %11s" % (
            both, name, s["trades"], "-" if s["win_rate"] is None else "%.0f" % (100 * s["win_rate"]),
            fmt_money(s["gross_pnl"], E.SIGN), fmt_money(s["net_pnl"], E.SIGN),
            fmt_money(early["net_pnl"], E.SIGN), fmt_money(late["net_pnl"], E.SIGN)))
    best = rows[0][1] if rows else {}
    print("\nTried %d variations: the best of many looks good partly by chance. Paper trade the one you pick "
          "before you trust it. The top one in trader/config.json would be:\n  %s" % (
              len(rows), json.dumps({k: v for k, v in best.items() if v not in (0, False) or k == "setup"})))
    return 0


def one_pass(broker, syms, cfg, now, offline=None, log_path=None):
    day = now.date()
    sess = E.session(day)
    if not sess:
        return None
    by = {}
    for sym in syms:
        try:
            by[sym] = closed_only(fetch(sym, "1d", offline).get(day, []), now)
        except Exception as e:
            print("%s: %s" % (sym, e), file=sys.stderr)
    final = now.time() >= sess[1]
    # A paper trade counts only if the bot was watching when it happened: started
    # at 2 PM (or after the close), it does not log the morning's trades with hindsight.
    watching = dict(read_log(log_path).get("watching") or {})
    first = watching.setdefault(day.isoformat(), now.strftime("%H:%M"))
    since = datetime.datetime.combine(day, datetime.datetime.strptime(first, "%H:%M").time())
    trades = broker.sync(day, by, cfg, now, final, since)
    doc = merge(log_path, trades, {day.isoformat()})
    if doc.get("broker") != broker.name or doc.get("watching") != watching:
        doc.update(broker=broker.name, watching=watching)
        write_json(log_path, doc)
    return trades


def cmd_run(a, cfg):
    syms = symbols_for(cfg, a.symbols)
    log_path = os.path.join(OUT_DIR, "trades.json")
    broker = make_broker(a.broker, cfg, log_path)
    print("Autopilot on paper (%s): %d symbols on the %s. Ctrl+C stops it." % (
        "IBKR paper account" if a.broker == "ibkr" else "simulated fills", len(syms), E.EXCHANGE))
    try:
        while True:
            now = now_eastern(a.now)
            sess = E.session(now.date())
            opened = sess and datetime.datetime.combine(now.date(), sess[0])
            shut = sess and datetime.datetime.combine(now.date(), sess[1])
            if sess and opened <= now:
                trades = one_pass(broker, syms, cfg, now, a.offline, log_path)
                write_dashboard(cfg, "paper", refresh=a.every, now=now)
                print("%s  %d trades today (%d open), today's P/L %s" % (
                    now.strftime("%H:%M"), len(trades), sum(1 for t in trades if t["status"] == "open"),
                    fmt_money(sum(t["pnl"] for t in trades), E.SIGN)))
            if a.once or a.now:
                break
            if not sess or now >= shut + datetime.timedelta(minutes=5):
                nxt = E.next_open(now.date())
                print("market closed; next open %s ET" % nxt)
                if not nxt:
                    break
                wait = datetime.datetime.strptime(nxt, "%Y-%m-%d %H:%M") - now
                broker.wait(max(min(wait.total_seconds(), 3600), 60))
            elif now < opened:
                broker.wait(max(min((opened - now).total_seconds(), 600), 30))
            else:
                broker.wait(a.every)
    finally:
        broker.close()
    path, s = write_dashboard(cfg, "paper")
    print_summary(s, E.SIGN)
    print("dashboard: " + path)
    return 0


def cmd_dashboard(a, cfg):
    path, s = write_dashboard(cfg, a.source, out=a.out)
    print_summary(s, E.SIGN)
    print("dashboard: " + path)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Autopilot: the opening-range breakout on a paper account.")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("backtest", "sweep", "run", "dashboard"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--market", choices=["ca", "us"])
        if name == "dashboard":
            continue
        p.add_argument("--symbols", help="comma-separated; default autopilot_symbols or a liquid list")
        p.add_argument("--offline", help="a folder of saved Yahoo charts (SYMBOL.json[.gz])")
        p.add_argument("--now", help='pretend it is this New York time, e.g. "2026-10-28 16:05"')
        p.add_argument("--or-minutes", type=int)
        p.add_argument("--target-r", type=float)
        p.add_argument("--stop-rule", choices=["mid", "low"])
        p.add_argument("--setup", choices=["orb", "vwap_revert"])
        p.add_argument("--min-volume-x", type=float)
        p.add_argument("--above-vwap", action="store_const", const=True)
        p.add_argument("--revert-pct", type=float)
        p.add_argument("--revert-stop-pct", type=float)
    for name in ("backtest", "sweep"):
        sub.choices[name].add_argument("--pause", type=float, default=0.5, help="seconds between Yahoo requests")
    sub.choices["run"].add_argument("--broker", choices=["sim", "ibkr"], default="sim")
    sub.choices["run"].add_argument("--every", type=int, default=60, help="seconds between passes")
    sub.choices["run"].add_argument("--once", action="store_true", help="one pass, then stop (for a scheduler)")
    sub.choices["dashboard"].add_argument("--from", dest="source", choices=["paper", "backtest"], default="paper")
    sub.choices["dashboard"].add_argument("--out")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        cfg = read_config(a.config, a.market, **{k: getattr(a, k, None) for k in (
            "or_minutes", "target_r", "stop_rule", "setup", "min_volume_x", "above_vwap", "revert_pct",
            "revert_stop_pct")})
        return {"backtest": cmd_backtest, "sweep": cmd_sweep, "run": cmd_run, "dashboard": cmd_dashboard}[a.cmd](a, cfg)
    except (AutopilotError, E.EntryError) as e:
        print("autopilot: %s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
