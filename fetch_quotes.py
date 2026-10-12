#!/usr/bin/env python3
"""SOXL/TECL/TQQQ 行情抓取 + 告警判定（soxl-price-watch 的脚本路径）.

数据源: Yahoo Finance chart API（免 key）+ Nasdaq API + CNBC quote API（交叉验证）.
覆盖美东 04:00-20:00（盘前/盘中/盘后）。夜盘（20:00-04:00 ET）无免费接口数据
-> status=needs_browser，由浏览器路径接管；脚本在 needs_browser 输出里给出
夜盘浏览器目标（Yahoo Finance 报价页 + TradingView 图表页，见 BROWSER_TARGETS）。
夜盘只有这两家发布 BOATS 夜盘成交价；Nasdaq/Google Finance/CNBC/MarketWatch/
StockAnalysis 的报价页没有夜盘价，只能核对参考收盘，不要拿去凑夜盘第二源。

两阶段提交（防丢告警）：默认只判定并写 staging 文件，不碰主状态文件；
worker 推送（时间线+简报）之后再调 --commit 落盘。任何一步崩了，
要么下轮重发（不丢），要么时间线有底。
休市（周末 Sat 04:00 ET→Sun 20:00 ET、NYSE 假期）返回 status=market_closed，
worker 只发一次休市通知后静默，不推送简报。
用法: python3 fetch_quotes.py [--force] [--state PATH] [--quotes-file JSON_PATH] [--commit]
  --force : 跳过"夜盘窗口"检查（用于测试）
  --state : 状态文件路径（默认正式文件；测试时请指向临时文件）
  --quotes-file : 浏览器路径——worker 把浏览器抓到的报价写成 JSON 文件，
                  脚本统一做告警判定/状态更新/简报格式化（告警逻辑只有这一份实现）
输出: JSON 到 stdout。
"""
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
STATE_DEFAULT = os.path.expanduser("~/workspace/goals/soxl-100/hidden_files/soxl-watch.json")
ETFS = ["SOXL", "TECL", "TQQQ"]
DIVERGE_USD = 0.50

# 夜盘浏览器目标（needs_browser 输出给 worker；与定时任务正文保持一致）。
# TradingView 符号带交易所前缀，写错前缀会 404（2026-10-05 夜盘轮次出过）。
BROWSER_TARGETS = {
    "SOXL": {"yahoo": "https://finance.yahoo.com/quote/SOXL/",
             "tradingview_symbol": "AMEX:SOXL",
             "tradingview": "https://www.tradingview.com/symbols/AMEX-SOXL/"},
    "TECL": {"yahoo": "https://finance.yahoo.com/quote/TECL/",
             "tradingview_symbol": "AMEX:TECL",
             "tradingview": "https://www.tradingview.com/symbols/AMEX-TECL/"},
    "TQQQ": {"yahoo": "https://finance.yahoo.com/quote/TQQQ/",
             "tradingview_symbol": "NASDAQ:TQQQ",
             "tradingview": "https://www.tradingview.com/symbols/NASDAQ-TQQQ/"},
}

# NYSE holidays (ET dates). Add next year each December.
US_HOLIDAYS = {
    # 2026 (remaining)
    "2026-11-26": "Thanksgiving",
    "2026-12-25": "Christmas",
    # 2027
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King Jr. Day",
    "2027-02-15": "Presidents' Day",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving",
    "2027-12-24": "Christmas (observed)",
}


def market_closed_status(now):
    """休市判断。返回 (is_closed, closed_type, detail)，closed_type 为 "weekend" 或 "holiday"。
    周末窗口：周六 04:00 ET 至周日 20:00 ET。其余周末时段（周六 00:00-04:00、
    周日 20:00-24:00）属于周五/周一的隔夜交易时段，正常覆盖。
    注：假期按整个美东日期休市，假期当晚 20:00 后的隔夜时段理论上可能交易，
    此处从简一并休市（一年约 9 天，影响极小）。"""
    iso = now.date().isoformat()
    if iso in US_HOLIDAYS:
        return True, "holiday", US_HOLIDAYS[iso]
    wd = now.weekday()
    t = now.time()
    if wd == 5 and t >= datetime.time(4, 0):
        return True, "weekend", ""
    if wd == 6 and t < datetime.time(20, 0):
        return True, "weekend", ""
    return False, "", ""


def http_json(url, timeout=20, retries=1):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(2)
    raise last


def yahoo_chart(symbol, range_="1d", interval="1m", prepost=True):
    params = "range={}&interval={}".format(range_, interval)
    if prepost:
        params += "&includePrePost=true"
    url = "https://query1.finance.yahoo.com/v8/finance/chart/{}?{}".format(
        urllib.parse.quote(symbol, safe=""), params)
    d = http_json(url)
    err = d.get("chart", {}).get("error")
    if err:
        raise RuntimeError("yahoo chart error for {}: {}".format(symbol, err))
    res = d["chart"]["result"]
    if not res:
        raise RuntimeError("yahoo empty result for {}".format(symbol))
    return res[0]


def nasdaq_quote(ticker):
    """第二交叉源：Nasdaq API。返回最新价、涨跌幅、真实报价时间戳、是否实时。"""
    url = "https://api.nasdaq.com/api/quote/{}/info?assetclass=etf".format(ticker)
    d = http_json(url)
    pd = d["data"]["primaryData"]
    raw = pd.get("lastSalePrice") or ""
    price = float(str(raw).replace("$", "").replace(",", ""))
    pct_raw = str(pd.get("percentageChange") or "0").replace("%", "").replace("+", "")
    # 真实报价时间戳，如 "Oct 1, 2026 5:38 AM ET"
    ts_raw = pd.get("lastTradeTimestamp") or ""
    qtime = None
    try:
        # 格式: "Oct 1, 2026 5:38 AM ET"
        dt = datetime.datetime.strptime(ts_raw.replace(" ET", ""), "%b %d, %Y %I:%M %p")
        qtime = dt.strftime("%H:%M")
    except Exception:
        pass
    return {"price": price, "pct": float(pct_raw), "qtime": qtime,
            "is_realtime": pd.get("isRealTime") is True}


def nasdaq_implied_close(q, session):
    """Back out Nasdaq's regular-session close from live price / change %.
    Nasdaq's percentageChange is vs the previous regular close in all sessions,
    so always back out when pct is valid (intraday, premarket, after-hours)."""
    if q["pct"] != -100:
        return q["price"] / (1 + q["pct"] / 100.0)
    return q["price"]


def cnbc_quote(ticker):
    """第三交叉源：CNBC quote API。返回与当前时段对应的最新价 + 真实报价时间。
    常规时段取主报价 last；盘前/盘后取 ExtendedMktQuote（CNBC 的扩展时段报价
    与 Yahoo/Nasdaq 是不同的数据管道，可真正交叉核对现价，而不只是核对收盘）。
    ExtendedMktQuote 的 type 与时段对不上时（如盘前时段还挂着昨晚盘后价）不用。"""
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
           "?symbols={}&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1"
           "&output=json").format(ticker)
    d = http_json(url)
    q = d["FormattedQuoteResult"]["FormattedQuote"][0]
    if q.get("code") != 0:
        raise RuntimeError("cnbc quote error for {}".format(ticker))
    session = classify_session(datetime.datetime.now(NY))
    ext = q.get("ExtendedMktQuote") or {}
    want_type = {"premarket": "PRE_MKT", "after-hours": "POST_MKT"}.get(session)
    if want_type and ext.get("type") == want_type and ext.get("last"):
        price = float(str(ext["last"]).replace(",", ""))
        ts_raw = ext.get("last_time") or ""
        src = "extended"
    else:
        price = float(str(q["last"]).replace(",", ""))
        ts_raw = q.get("last_time") or ""
        src = "regular"
    qtime = None
    try:
        # 格式: "2026-10-05T04:11:02.505-0400"；只有日期没有时刻的字符串
        # 解析出来会是 00:00，那是假时间戳，不用。
        if "T" in ts_raw:
            dt = datetime.datetime.fromisoformat(ts_raw)
            qtime = dt.astimezone(NY).strftime("%H:%M")
    except Exception:
        pass
    return {"price": price, "qtime": qtime, "src": src}


def to_ny(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).astimezone(NY)


def md(d):
    return "{}/{}".format(d.month, d.day)


def classify_session(dt):
    t = dt.time()
    if datetime.time(4, 0) <= t < datetime.time(9, 30):
        return "premarket"
    if datetime.time(9, 30) <= t < datetime.time(16, 0):
        return "intraday"
    if datetime.time(16, 0) <= t < datetime.time(20, 0):
        return "after-hours"
    return "overnight"


def daily_closes(symbol):
    r = yahoo_chart(symbol, range_="10d", interval="1d", prepost=False)
    ts = r.get("timestamp") or []
    q = r["indicators"]["quote"][0]
    closes = q.get("close") or []
    out = []
    for i, t in enumerate(ts):
        if i < len(closes) and closes[i] is not None:
            out.append((to_ny(t).date(), float(closes[i])))
    if len(out) < 2:
        # Fallback: 某些标的的日线接口数据不全时，从 5 分钟线推导日收盘
        r2 = yahoo_chart(symbol, range_="5d", interval="5m", prepost=False)
        ts2 = r2.get("timestamp") or []
        q2 = r2["indicators"]["quote"][0]
        closes2 = q2.get("close") or []
        by_day = {}
        for i, t in enumerate(ts2):
            if i < len(closes2) and closes2[i] is not None:
                d = to_ny(t).date()
                by_day[d] = float(closes2[i])  # 取当天最后一个值即收盘
        out = sorted(by_day.items())
    if not out:
        raise RuntimeError("no daily history for {}".format(symbol))
    return out


def intraday_latest(symbol):
    r = yahoo_chart(symbol, range_="1d", interval="1m", prepost=True)
    ts = r.get("timestamp") or []
    closes = r["indicators"]["quote"][0]["close"]
    for i in range(len(ts) - 1, -1, -1):
        if closes[i] is not None:
            return float(closes[i]), to_ny(ts[i])
    raise RuntimeError("no intraday data for {}".format(symbol))


def fetch_ath(symbol):
    """历史最高价（拆股调整，机构标准）。
    Yahoo 的 high 已做拆股调整，直接取最大即可。
    注意：不做分红调整——机构的价格 ATH 口径只调拆股，
    分红调整是总回报口径，会压低 ATH 导致提前误报。"""
    r = yahoo_chart(symbol, range_="max", interval="1wk", prepost=False)
    q = r["indicators"]["quote"][0]
    highs = q.get("high") or []
    vals = [float(h) for h in highs if h is not None]
    if not vals:
        raise RuntimeError("no history for ATH")
    return max(vals)


def ref_close(dailies, now):
    """最近一个已完成的常规收盘（date, close, prev(date, close) or None）。"""
    boundary = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < boundary:
        boundary -= datetime.timedelta(days=1)
    target = boundary.date()
    cands = [(d, c) for d, c in dailies if d <= target]
    if not cands:
        raise RuntimeError("no daily history before {}".format(target))
    d, c = cands[-1]
    prev = cands[-2] if len(cands) >= 2 else None
    return d, c, prev


def fmt_pct(x):
    return "{:+.2f}%".format(x)


def load_state(path):
    state = {}
    if os.path.exists(path):
        with open(path) as f:
            state = json.load(f)
    if "etfs" not in state:
        state["etfs"] = {}
    for t in ETFS:
        if t not in state["etfs"]:
            state["etfs"][t] = {"dip_level": 0, "dip_ref": "", "ath": 0.0}
    return state


def evaluate_alerts(ticker, price, ref_close, ref_date_str, quote_time_str, sources, state):
    """10% drop from previous regular close + all-time-high breakout. Returns alert list (deterministic).
    机构标准：相对前一交易日收盘价计算涨跌，-10% 触发加仓提醒（每再跌 10% 触发一次，形成阶梯）。"""
    alerts = []
    s = state["etfs"][ticker]
    # 1) Down 10% from previous close -> add alert (ladder per 10%)
    #    用 dip_level 记录已触发的阶梯（10/20/30...），避免同一阶梯重复告警
    if ref_close and ref_close > 0:
        drop_pct = (ref_close - price) / ref_close * 100
        level = int(drop_pct // 10) * 10  # 10, 20, 30...
        last_level = s.get("dip_level", 0)
        last_ref = s.get("dip_ref", "")
        # 新交易日重置阶梯
        if last_ref != ref_date_str:
            last_level = 0
            s["dip_level"] = 0
            s["dip_ref"] = ref_date_str
        if level >= 10 and level > last_level:
            alerts.append({
                "ticker": ticker,
                "kind": "add",
                "headline": "⚠️ {} down {:.1f}% from previous close — consider adding".format(ticker, drop_pct),
                "detail": "Price {:.2f}, down {:.1f}% from previous close {:.2f} ({}); quoted {}; sources: {}".format(
                    price, drop_pct, ref_close, ref_date_str, quote_time_str, sources),
            })
            s["dip_level"] = level
    # 2) All-time-high breakout -> trim alert
    # 机构标准：突破以收盘价确认，防盘中假突破。
    # ATH 只从历史周线数据更新，不拿 tick 价直接覆盖——
    # 否则瞬间刺破又回落会把 ATH 虚抬，导致后续真突破漏报。
    if s["ath"] <= 0:
        try:
            s["ath"] = fetch_ath(ticker)
        except Exception:
            pass  # retry next round if lookup fails
    if s["ath"] > 0 and price > s["ath"]:
        alerts.append({
            "ticker": ticker,
            "kind": "trim",
            "headline": "⚠️ {} hit a new all-time high — consider trimming".format(ticker),
            "detail": "Price {:.2f}, previous ATH {:.2f}; quoted {}; sources: {}".format(
                price, s["ath"], quote_time_str, sources),
        })
        # 突破后从历史数据重新确认 ATH（不直接用 tick 价）
        try:
            new_ath = fetch_ath(ticker)
            if new_ath > s["ath"]:
                s["ath"] = new_ath
        except Exception:
            pass
    return alerts



def build_briefing(quotes, indices, weekend=False):
    """Briefing in English (user's fixed format). quotes: list of dict(ticker/price/change_pct/ref_date/session/quote_time)."""
    lines = []
    for q in quotes:
        pct_str = "(n/a)" if q["change_pct"] is None else fmt_pct(q["change_pct"])
        if weekend:
            lines.append("{} {:.2f} {} · markets closed".format(
                q["ticker"], q["price"], pct_str))
        else:
            lines.append("{} {:.2f} {} · {} {} ET".format(
                q["ticker"], q["price"], pct_str,
                q["session"], q["quote_time"]))
    return "\n".join(lines)


def stage_state(state, path, now):
    """两阶段提交之一：只写 staging 文件，不碰主状态文件。
    worker 推送（时间线+简报）之后再调 --commit 落盘，避免"状态已更新但告警没推送"的丢失。"""
    state["updated_at"] = now.isoformat()
    staging = path + ".staging"
    tmp = staging + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, staging)
    return staging


def commit_state(path, max_age_min=30):
    """两阶段提交之二：将 staging 提升为主状态文件。staging 不存在或太旧则拒绝。"""
    staging = path + ".staging"
    if not os.path.exists(staging):
        raise RuntimeError("no staging file to commit")
    age_min = (time.time() - os.path.getmtime(staging)) / 60
    if age_min > max_age_min:
        raise RuntimeError("staging file too old ({:.0f} min), refusing".format(age_min))
    with open(staging) as f:
        state = json.load(f)  # 校验 JSON 合法
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    os.remove(staging)
    return True


def validate_worker_quotes(payload, notes):
    """Cross-check worker-provided quotes against Yahoo daily closes.
    Catches swapped price/reference errors (e.g. putting the regular close
    in the price field). Returns None if OK, else an error string."""
    for t in ETFS:
        q = payload["etfs"][t]
        price = float(q["price"])
        if q.get("change_pct") is None:
            notes.append("{}: validation skipped (no change_pct provided)".format(t))
            continue
        reported = float(q["change_pct"])
        ref_date = q["ref_date"]  # "M/D"
        try:
            dailies = daily_closes(t)
        except Exception as e:
            notes.append("{}: validation skipped ({})".format(t, e))
            continue
        m, d = ref_date.split("/")
        want = (int(m), int(d))
        match = None
        for dt, close in reversed(dailies):
            if (dt.month, dt.day) == want:
                match = (dt, close)
                break
        if not match:
            notes.append("{}: validation skipped (no daily bar for {})".format(t, ref_date))
            continue
        _, ref_close = match
        if ref_close == 0:
            continue
        expected = (price - ref_close) / ref_close * 100
        if abs(expected - reported) > 1.0:
            return ("{}: price {:.2f} vs ref {} close {:.2f} implies {:+.2f}%, "
                    "but worker reported {:+.2f}%".format(
                        t, price, ref_date, ref_close, expected, reported))
    return None


def mode_quotes_file(qf_path, state_path):
    """浏览器路径：worker 把浏览器抓到的报价写成 JSON 文件，脚本统一做告警判定/状态更新/简报格式化。
    JSON 格式: {"etfs": {"SOXL": {"price": 154.37, "change_pct": 4.40, "ref_date": "9/30",
      "session": "overnight", "quote_time": "00:03", "sources": "Yahoo Finance+TradingView"}, ...}}
    单位约定（硬性）：所有 change_pct 一律为百分点，2.31 表示 +2.31%，0.23 表示 +0.23%；
    禁止写小数形式（如 0.0231），否则简报涨跌幅会算错。
    校验：脚本会用 Yahoo 日线反推 change_pct 并与 worker 提供的值比对，偏差超 1 个百分点
    则 status=validation_failed 拒收（防把收盘价填成现价）。人工复核确认无误后可用
    --skip-validation 跳过校验。
    """
    now = datetime.datetime.now(NY)
    with open(qf_path) as f:
        payload = json.load(f)
    notes = []
    # Deterministic guard: refuse to brief on inconsistent worker data.
    if "--skip-validation" not in sys.argv:
        try:
            verr = validate_worker_quotes(payload, notes)
        except Exception as e:
            verr = "validation error: {}".format(e)
        if verr:
            print(json.dumps({"status": "validation_failed", "reason": verr,
                              "as_of_et": now.strftime("%Y-%m-%d %H:%M")},
                             ensure_ascii=False))
            return 1
    try:
        state = load_state(state_path)
    except Exception as e:
        print("WARN: state file unreadable ({}); rebuilding".format(e), file=sys.stderr)
        state = {"etfs": {t: {"dip_level": 0, "dip_ref": "", "ath": 0.0} for t in ETFS}}
        notes.append("State file corrupted; baseline rebuilt")
    alerts_all, quotes = [], []
    for t in ETFS:
        q = payload["etfs"][t]
        price = float(q["price"])
        # 交叉核验在代码里强制留痕：来源不足 2 家自动记 degraded note，
        # 不靠 worker 自觉，避免单源报价静默通过。
        src_names = [s.strip() for s in str(q.get("sources", "")).replace(",", "+").split("+") if s.strip()]
        if len(src_names) < 2:
            notes.append("{}: single-source quote ({}) — degraded, no cross-verification".format(
                t, q.get("sources", "") or "none stated"))
        qtime_str = "{} {} ET".format(q["session"], q["quote_time"])
        # 从 price 和 change_pct 反推参考收盘价（机构标准：相对前一收盘计算）
        cp = q.get("change_pct")
        ref_close_b = price / (1 + float(cp) / 100.0) if cp is not None else 0.0
        alerts_all.extend(evaluate_alerts(t, price, ref_close_b, q["ref_date"], qtime_str, q.get("sources", ""), state))
        cp = q.get("change_pct")
        quotes.append({
            "ticker": t, "price": round(price, 2),
            "change_pct": round(float(cp), 2) if cp is not None else None,
            "ref_date": q["ref_date"], "session": q["session"],
            "quote_time": q["quote_time"],
        })
    indices = []
    briefing = build_briefing(quotes, indices, weekend=False)
    stage_state(state, state_path, now)
    out = {"status": "ok", "mode": "quotes-file",
           "as_of_et": now.strftime("%Y-%m-%d %H:%M"),
           "quotes": quotes, "indices": indices, "alerts": alerts_all,
           "notes": notes, "briefing": briefing}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main():
    force = "--force" in sys.argv
    state_path = STATE_DEFAULT
    if "--state" in sys.argv:
        state_path = sys.argv[sys.argv.index("--state") + 1]
    if "--commit" in sys.argv:
        try:
            commit_state(state_path)
            print(json.dumps({"status": "committed", "state_path": state_path},
                             ensure_ascii=False))
            return 0
        except Exception as e:
            print(json.dumps({"status": "commit_failed", "reason": str(e)},
                             ensure_ascii=False))
            return 1
    if "--quotes-file" in sys.argv:
        qf = sys.argv[sys.argv.index("--quotes-file") + 1]
        return mode_quotes_file(qf, state_path)

    now = datetime.datetime.now(NY)
    out = {"as_of_et": now.strftime("%Y-%m-%d %H:%M"), "status": "ok",
           "sources": ["Yahoo Finance chart API", "Nasdaq API", "CNBC quote API"]}

    def need_browser(reason):
        out["status"] = "needs_browser"
        out["reason"] = reason
        out["browser_targets"] = BROWSER_TARGETS
        print(json.dumps(out, ensure_ascii=False))
        return 0

    weekend = False
    if not force:
        closed, ctype, cdetail = market_closed_status(now)
        if closed:
            oc = {"status": "market_closed", "closed_type": ctype,
                  "as_of_et": now.strftime("%Y-%m-%d %H:%M")}
            if ctype == "holiday":
                oc["holiday_name"] = cdetail
            print(json.dumps(oc, ensure_ascii=False))
            return 0
        # 不在休市期：走正常报价流程。
    else:
        # --force 测试时：真正的周末休市期才显示"markets closed"
        _, ctype, _ = market_closed_status(now)
        weekend = (ctype == "weekend")
    if not force:
        if classify_session(now) == "overnight":
            return need_browser("Overnight session (20:00-04:00 ET); script sources have no overnight quotes")

    notes = []
    try:
        state = load_state(state_path)
    except Exception as e:
        print("WARN: state file unreadable ({}); rebuilding".format(e), file=sys.stderr)
        state = {"etfs": {t: {"dip_level": 0, "dip_ref": "", "ath": 0.0} for t in ETFS}}
        notes.append("State file corrupted; baseline rebuilt")
    quotes, indices, alerts_all = [], [], []

    try:
        for t in ETFS:
            dailies = daily_closes(t)
            rd, rc, prev = ref_close(dailies, now)
            if weekend:
                price, session, qtime = rc, "closed", "{} close".format(md(rd))
                chg = (rc - prev[1]) / prev[1] * 100 if prev else None
                chg_ref = md(prev[0]) if prev else md(rd)
            else:
                price, qdt = intraday_latest(t)
                session = classify_session(qdt)
                qtime = qdt.strftime("%H:%M")
                age_min = (now - qdt).total_seconds() / 60
                limit = 15 if session == "intraday" else 60
                # 先尝试 Nasdaq 兜底（机构做法：单源坏了不阻塞）
                # 再决定是否因过期而转浏览器
                use_nasdaq_fallback = False
                nq_cached = None
                try:
                    nq_pre = nasdaq_quote(t)
                    nq_cached = nq_pre
                    if not force and age_min > 30 and abs(price - nq_pre["price"]) > DIVERGE_USD:
                        # 必须有真实时间戳才用 Nasdaq 兜底，否则跳过该标的（不造假）
                        if nq_pre["qtime"]:
                            notes.append("{}: Yahoo live {:.2f} ({} min old) vs Nasdaq live {:.2f}, divergence ${:.2f} — using Nasdaq".format(
                                t, price, int(age_min), nq_pre["price"], abs(price - nq_pre["price"])))
                            price = nq_pre["price"]
                            chg = (price - rc) / rc * 100
                            qtime = nq_pre["qtime"]
                            use_nasdaq_fallback = True
                            age_min = 0
                        else:
                            notes.append("{}: Nasdaq timestamp unavailable, skipping fallback".format(t))
                except Exception:
                    pass  # Nasdaq 不可用则走原逻辑
                if not force and not use_nasdaq_fallback and age_min > limit:
                    return need_browser(
                        "{} latest quote {} is {:.0f} min stale".format(t, qdt.strftime("%H:%M"), age_min))
                chg = (price - rc) / rc * 100
                chg_ref = md(rd)
            # 第二源交叉：Nasdaq 反推的常规收盘价 vs Yahoo 参考收盘（同一口径才有意义）
            # 复用上面已抓的 Nasdaq 数据，避免重复请求
            try:
                nq = nq_cached if nq_cached else nasdaq_quote(t)
                nq_close = nasdaq_implied_close(nq, session)
                if abs(nq_close - rc) > DIVERGE_USD:
                    notes.append("{}: Nasdaq-implied close {:.2f} vs Yahoo close {:.2f}, divergence > ${:.2f}".format(
                        t, nq_close, rc, DIVERGE_USD))
            except Exception as e:
                notes.append("{}: Nasdaq cross-check unavailable ({})".format(t, e))
            # 第三源交叉：CNBC 现价 vs 采用价（不同数据管道，能核对现价本身；
            # 只记分歧 note，不改价——选源仍按时间戳新鲜度规则）
            srcs = ["Yahoo Finance", "Nasdaq"]
            if not weekend:
                try:
                    cq = cnbc_quote(t)
                    srcs.append("CNBC")
                    if cq["qtime"] and abs(cq["price"] - price) > DIVERGE_USD:
                        notes.append("{}: CNBC live {:.2f} ({} ET) vs used {:.2f}, divergence ${:.2f}".format(
                            t, cq["price"], cq["qtime"], price, abs(cq["price"] - price)))
                except Exception as e:
                    notes.append("{}: CNBC cross-check unavailable ({})".format(t, e))
            qtime_str = "{} {} ET".format(session, qtime) if not weekend else "markets closed"
            alerts_all.extend(evaluate_alerts(t, price, rc, chg_ref, qtime_str, "+".join(srcs), state))
            quotes.append({
                "ticker": t, "price": round(price, 2), "change_pct": round(chg, 2) if chg is not None else None,
                "ref_date": chg_ref, "session": session, "quote_time": qtime,
                "sources": srcs,
            })
    except Exception as e:
        return need_browser("Script fetch failed: {}".format(e))

    briefing = build_briefing(quotes, [], weekend)
    stage_state(state, state_path, now)

    out.update(quotes=quotes, indices=[], alerts=alerts_all,
               notes=notes, briefing=briefing)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
