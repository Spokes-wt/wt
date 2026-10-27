#!/usr/bin/env python3
"""Positions carry qty (contracts); $ math in the dashboard scales by qty, per-share fields stay per contract.
Spot from Yahoo 1m last (CBOE fallback); options from delayed CBOE -> update data.json -> re-embed into index-wt.html (+ copy to index.html). Missing values stay null (shown as '—')."""
import json, re, datetime, urllib.request, pathlib
D = pathlib.Path(__file__).resolve().parent
URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{}.json"

def get(t):
    req = urllib.request.Request(URL.format(t), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:  # urllib follows redirects
        return json.load(r)

def parse(sym):
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", sym)
    if not m: return None
    return m[1], datetime.datetime.strptime(m[2], "%y%m%d").date(), m[3], int(m[4]) / 1000

def num(x): return x if isinstance(x, (int, float)) and x > 0 else None

def yahoo_spot(t):
    """Near-live last (1m bar). Used when CBOE spot is missing or prior-session."""
    try:
        req = urllib.request.Request(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1m&range=1d",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.load(r)["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if not closes: return None, None
        return round(closes[-1], 2), datetime.datetime.fromtimestamp(res["timestamp"][-1], datetime.timezone.utc)
    except Exception as e:
        print("yahoo fail", t, e); return None, None

def cboe_fresh(ts):
    if not ts: return False
    try:
        # CBOE timestamp is ET wall clock without offset; treat as America/New_York date
        dt = datetime.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        return dt.date() == datetime.date.today()
    except Exception:
        return False

data = json.loads((D / "data.json").read_text())
today = datetime.date.today()
mk = data.setdefault("market", {})
# Always refresh TSLA + SPCX market header (Dave keeps TSLA listed with no open put)
tickers = sorted({p["ticker"] for p in data["positions"]} | {"TSLA", "SPCX"})
for t in tickers:
    try:
        j = get(t)
    except Exception as e:
        print("fetch fail", t, e); continue
    d = j["data"]; opts = d.get("options", [])
    ts = j.get("timestamp")
    # Spot: prefer Yahoo 1m last (near-live); CBOE delayed only as fallback. Options still CBOE.
    y, yts = yahoo_spot(t)
    if y:
        spot = y
        ts = yts.strftime("%Y-%m-%d %H:%M:%S") if yts else ts
        print("spot from yahoo", t, spot)
    else:
        spot = num(d.get("current_price")) if cboe_fresh(ts) else None
        if not spot:
            spot = num(d.get("current_price")) or num(d.get("close"))
        print("spot from cboe fallback", t, spot)
    # ATM IV: nearest expiry in 30-45 DTE (else closest to 37), put strike nearest spot
    exps = sorted({parse(o["option"])[1] for o in opts if parse(o["option"])})
    win = [e for e in exps if 30 <= (e - today).days <= 45] or sorted(exps, key=lambda e: abs((e - today).days - 37))[:1]
    atm = None
    if win and spot:
        puts = [o for o in opts if (p := parse(o["option"])) and p[1] == win[0] and p[2] == "P" and num(o.get("iv"))]
        if puts:
            o = min(puts, key=lambda o: abs(parse(o["option"])[3] - spot))
            atm = {"iv": round(o["iv"] * 100, 1), "strike": parse(o["option"])[3], "exp": win[0].isoformat()}
    mk[t] = {"spot": spot, "prev": num(d.get("prev_day_close")), "iv30": num(d.get("iv30")),
             "atm": atm, "iv_rank": None, "ts": ts}
    if t == "SPCX":
        best = None
        for o in opts:
            q = parse(o["option"])
            if not q or q[2] != "P": continue
            dd = (q[1] - today).days; b = num(o.get("bid")); dl = o.get("delta")
            if not (30 <= dd <= 45) or not b or not isinstance(dl, (int, float)) or not (0.40 <= abs(dl) <= 0.55): continue
            ann = b / q[3] * 365 / dd * 100
            if not best or ann > best["ann"]:
                best = {"strike": q[3], "exp": q[1].isoformat(), "dte": dd, "bid": b, "delta": round(dl, 3),
                        "iv": round(o["iv"] * 100, 1) if num(o.get("iv")) else None, "ann": round(ann, 1)}
        data["redeploy"] = {"as_of": ts, "candidate": best,
                            "note": "bid-based; 30-45 DTE; 0.40-0.55Δ" + ("" if best else " — no put with bid in band")}
    for p in data["positions"]:
        if p["ticker"] != t: continue
        osym = f"{t}{datetime.date.fromisoformat(p['exp']):%y%m%d}{'P' if p['type']=='CSP' else 'C'}{int(round(p['strike']*1000)):08d}"
        o = next((o for o in opts if o["option"] == osym), None)
        if o:
            b, a = num(o.get("bid")), num(o.get("ask"))
            p["bid"], p["ask"], p["last"] = b, a, num(o.get("last_trade_price"))
            p["mark"] = round((b + a) / 2, 3) if b and a else p["last"]
            p["iv"] = round(o["iv"] * 100, 1) if num(o.get("iv")) else None
            p["delta"] = o.get("delta")
        else:
            p["mark"] = None
ts = [v["ts"] for v in mk.values() if v.get("ts")]
data["refreshed_utc"] = max(ts) + "Z" if ts else None
data["refreshed_run"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
(D / "data.json").write_text(json.dumps(data, indent=1))
h = (D / "index-wt.html").read_text()
h = re.sub(r'(<script id="wheel-data" type="application/json">).*?(</script>)',
           lambda m: m[1] + json.dumps(data).replace("</", "<\\/") + m[2], h, flags=re.S)
(D / "index-wt.html").write_text(h)
import shutil; shutil.copyfile(D / "index-wt.html", D / "index.html")
print(json.dumps({t: v for t, v in mk.items()}, indent=1))
for p in data["positions"]: print(p["id"], p.get("bid"), p.get("ask"), p.get("mark"))
