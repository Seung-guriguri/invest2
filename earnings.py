"""관심 종목 실적 발표 알림 — 개인 대화방(봇 1:1)으로만 전송.

대상 종목
  ⭐ 직접 등록: data/watchlist.json (텔레그램 봇 /add, /del 로 관리)
  🏦 파브라이 13F 보유 종목 (SEC 회사명 → 티커 매칭)
  🕵️ 최근 30일 내부자 매수 다이제스트에 나온 종목

알림
  📅 D-7 / D-1 일정 알림: 발표일·장전/장후(한국시간 환산)·컨센서스 EPS (Nasdaq 실적 캘린더)
  📄 자료 공개: SEC 8-K(Item 2.02) 보도자료·슬라이드 링크, 보도자료 속 웹캐스트 링크, EPS 서프라이즈
  📈 발표 후 반응: 다음 실행 때 주가 변동, StockTwits 강세/약세 비율
  🗓 월요일: 이번 주 실적 발표 일정 요약

상태: data/earnings/state.json (보낸 알림, 다음 발표일 캐시, 최근 자료 링크)

사용법:
  python earnings.py               # 일간 실행 (일정 갱신 + 모든 알림)
  python earnings.py --materials   # 자료 공개 확인만 (매시간 실행용)
  python earnings.py --dry-run     # 전송·상태 저장 없이 출력
"""

import argparse
import glob
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from reddit_top10 import BROWSER_UA, send_telegram
from sec13f import SEC

ET_TZ = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
WATCHLIST_PATH = os.path.join("data", "watchlist.json")
STATE_PATH = os.path.join("data", "earnings", "state.json")
CALENDAR_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
STOCKTWITS_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
OWNER_ID_DEFAULT = "299257234"
SCAN_DAYS = 8  # 오늘부터 며칠 뒤까지 캘린더 확인
PABRAI_CIK = 1549575
NAME_STOP = {"INC", "CORP", "CORPORATION", "CO", "LTD", "PLC", "HLDGS", "HOLDINGS", "NV", "SA", "LLC",
             "THE", "GROUP", "COMPANY", "CLASS", "CL", "COM", "ADR", "ADS", "LP", "N", "V", "S", "A"}
WEEKDAYS = "월화수목금토일"


# ---------- 공통 ----------

def get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json",
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def money_num(text):
    try:
        return float(re.sub(r"[^\d.\-]", "", text.replace("(", "-")))
    except (ValueError, AttributeError):
        return None


def kday(d):
    return f"{d:%m/%d}({WEEKDAYS[d.weekday()]})"


def timing_label(us_day, time_code):
    """장전/장후 → 한국시간 대략 환산."""
    if time_code == "time-pre-market":
        return f"{kday(us_day)} 장 시작 전 → 한국 {kday(us_day)} 저녁(20~22시경)"
    if time_code == "time-after-hours":
        return f"{kday(us_day)} 장 마감 후 → 한국 {kday(us_day + timedelta(days=1))} 새벽(5~6시경)"
    return f"{kday(us_day)} (시간 미정)"


# ---------- 대상 종목 ----------

def norm_tokens(name):
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    return [w for w in words if w not in NAME_STOP]


def name_to_ticker(sec, names):
    """13F 회사명 → 티커 (SEC company_tickers.json, 앞 두 단어 접두 일치)."""
    table = sec.json("https://www.sec.gov/files/company_tickers.json").values()
    index = [(norm_tokens(row["title"]), row["ticker"]) for row in table]
    result = {}
    for name in names:
        want = norm_tokens(name)[:2]
        if not want:
            continue
        for tokens, ticker in index:
            # 13F 이름은 잘려 있을 수 있음(예: RESOUR) → 13F 단어가 회사명 단어의 앞부분이면 일치
            if len(tokens) >= len(want) and all(t.startswith(w) for w, t in zip(want, tokens)):
                result[name] = ticker
                break
    return result


def pabrai_tickers(sec):
    _, filings = sec.latest_13f(PABRAI_CIK, count=1)
    if not filings:
        return set()
    names = [e["name"] for e in sec.holdings(PABRAI_CIK, filings[0]["accession"]).values()]
    return set(name_to_ticker(sec, names).values())


def insider_tickers(days=30):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    tickers = set()
    for path in glob.glob(os.path.join("data", "insider", "candidates", "*.json")):
        if os.path.basename(path)[:10] < cutoff:
            continue
        for row in load(path, {}).get("candidates", []):
            if row.get("sent"):
                tickers.add(row["symbol"])
    return tickers


def universe(sec):
    """{티커: 출처 아이콘 문자열}"""
    sources = {}
    for t in load(WATCHLIST_PATH, {"tickers": {}})["tickers"]:
        sources.setdefault(t, set()).add("⭐")
    try:
        for t in pabrai_tickers(sec):
            sources.setdefault(t, set()).add("🏦")
    except Exception as e:
        print(f"[경고] 파브라이 보유 종목 조회 실패: {e}", file=sys.stderr)
    for t in insider_tickers():
        sources.setdefault(t, set()).add("🕵️")
    return {t: "".join(sorted(s)) for t, s in sources.items()}


# ---------- 실적 캘린더 ----------

def calendar_rows(day):
    data = get_json(CALENDAR_URL.format(d=day.isoformat()))
    return (data.get("data") or {}).get("rows") or []


def scan_calendar(tickers, start, days):
    """{티커: 일정 정보} — start부터 days일 동안."""
    found = {}
    for i in range(days):
        day = start + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        try:
            rows = calendar_rows(day)
        except Exception as e:
            print(f"[경고] {day} 실적 캘린더 조회 실패: {e}", file=sys.stderr)
            continue
        for row in rows:
            sym = (row.get("symbol") or "").upper()
            if sym in tickers and sym not in found:
                found[sym] = {"date": day.isoformat(), "time": row.get("time", ""),
                              "name": (row.get("name") or "").strip(), "eps_forecast": row.get("epsForecast", ""),
                              "estimates": row.get("noOfEsts", ""), "last_year_eps": row.get("lastYearEPS", ""),
                              "quarter": row.get("fiscalQuarterEnding", "")}
    return found


def actual_eps(symbol, day):
    for row in calendar_rows(day):
        if (row.get("symbol") or "").upper() == symbol:
            return row.get("eps"), row.get("surprise"), row.get("epsForecast")
    return None, None, None


# ---------- 8-K 자료 ----------

def latest_earnings_8k(sec, symbol, since):
    """since(YYYY-MM-DD) 이후 Item 2.02 8-K → {accession, filed, cik, docs:[(type, desc, url)], webcast}"""
    cik = ticker_cik(sec, symbol)
    if not cik:
        return None
    data = sec.json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    r = data["filings"]["recent"]
    for form, acc, filed, items in zip(r["form"], r["accessionNumber"], r["filingDate"], r["items"]):
        if form == "8-K" and "2.02" in (items or "") and filed >= since:
            return {"accession": acc, "filed": filed, "cik": cik, **exhibits(sec, cik, acc)}
    return None


_TICKER_CIK = {}


def ticker_cik(sec, symbol):
    if not _TICKER_CIK:
        for row in sec.json("https://www.sec.gov/files/company_tickers.json").values():
            _TICKER_CIK[row["ticker"].upper()] = int(row["cik_str"])
    return _TICKER_CIK.get(symbol.upper().replace(".", "-")) or _TICKER_CIK.get(symbol.upper())


def exhibits(sec, cik, acc):
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
    page = sec.get(f"{folder}/{acc}-index.htm").decode("utf-8", "replace")
    docs = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 4:
            continue
        link = re.search(r'href="([^"]+)"', cells[2])
        doc_type = re.sub(r"<[^>]+>", "", cells[3]).strip()
        if link and doc_type.startswith("EX-99"):
            url = "https://www.sec.gov" + link.group(1).replace("/ix?doc=", "")
            desc = re.sub(r"<[^>]+>|&nbsp;", " ", cells[1]).strip()
            docs.append((doc_type, desc, url))
    webcast = ""
    for doc_type, _, url in docs:
        if doc_type in ("EX-99.1", "EX-99"):
            try:
                webcast = find_webcast(sec.get(url).decode("utf-8", "replace"))
            except Exception as e:
                print(f"[경고] 보도자료 조회 실패: {e}", file=sys.stderr)
            break
    return {"docs": docs, "webcast": webcast, "index": f"{folder}/{acc}-index.htm"}


def find_webcast(page):
    """보도자료 HTML에서 웹캐스트/컨퍼런스콜 관련 링크 추출."""
    text = html.unescape(page)
    candidates = []
    for m in re.finditer(r'(?:href="(https?://[^"]+)"|(https?://[^\s<>"\')]+|(?:www\.)[^\s<>"\')]+\.[a-z]{2,}[^\s<>"\')]*))',
                         text, re.I):
        url = (m.group(1) or m.group(2) or "").rstrip(".,;)")
        if not url or "sec.gov" in url:
            continue
        context = re.sub(r"<[^>]+>", " ", text[max(0, m.start() - 400): m.end() + 200]).lower()
        score = sum(k in context for k in ("webcast", "conference call", "earnings call", "listen", "live audio"))
        score += sum(k in url.lower() for k in ("webcast", "event", "investor", "/ir", "ir."))
        if score:
            candidates.append((score, url if url.startswith("http") else "https://" + url))
    return max(candidates)[1] if candidates else ""


# ---------- 반응 ----------

def price_reaction(symbol, us_day, time_code):
    from insider import yahoo_chart
    price, rows = yahoo_chart(symbol)
    base_day = us_day if time_code == "time-after-hours" else us_day - timedelta(days=1)
    closes = [(d, c) for d, _, c in rows if c and d <= base_day]
    if not closes or not price:
        return None
    base = closes[-1][1]
    return base, price, price / base - 1


def stocktwits_sentiment(symbol):
    data = get_json(STOCKTWITS_STREAM.format(sym=urllib.parse.quote(symbol)))
    msgs = data.get("messages") or []
    pick = lambda s: sum(1 for m in msgs if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == s)
    return pick("Bullish"), pick("Bearish"), len(msgs)


# ---------- 메시지 ----------

def tag(sym, src):
    return f"<b>${html.escape(sym)}</b> {src}"


def msg_reminder(sym, src, info, days_left):
    us_day = date.fromisoformat(info["date"])
    head = "D-1" if days_left <= 1 else f"D-{days_left}"
    lines = [f"📅 {tag(sym, src)} {html.escape(info.get('name', ''))} 실적 발표 <b>{head}</b>",
             timing_label(us_day, info.get("time"))]
    if info.get("eps_forecast"):
        lines.append(f"컨센서스 EPS {info['eps_forecast']} (애널리스트 {info.get('estimates') or '-'}명)"
                     + (f" · 전년 동기 {info['last_year_eps']}" if info.get("last_year_eps") else ""))
    if info.get("quarter"):
        lines.append(f"<i>회계 분기: {info['quarter']}</i>")
    return "\n".join(lines)


def msg_materials(sym, src, info, mat, eps):
    lines = [f"📄 {tag(sym, src)} 실적 자료 공개 <i>(SEC 8-K, {mat['filed']})</i>"]
    for doc_type, desc, url in mat["docs"]:
        label = {"EX-99.1": "보도자료", "EX-99.2": "추가 자료(슬라이드·주주서한 등)"}.get(doc_type, doc_type)
        lines.append(f'• <a href="{html.escape(url)}">{label}</a> <i>{html.escape(desc[:60])}</i>')
    if mat.get("webcast"):
        lines.append(f'• 🎧 <a href="{html.escape(mat["webcast"])}">컨퍼런스콜 웹캐스트</a> <i>(보도자료 속 링크)</i>')
    else:
        lines.append("• 🎧 웹캐스트 링크는 보도자료에서 찾지 못했어요 — 회사 IR 페이지를 확인하세요")
    lines.append(f'• <a href="{html.escape(mat["index"])}">공시 전체 보기</a>')
    actual, surprise, forecast = eps
    if actual:
        sp = money_num(surprise or "")
        lines.append(f"EPS <b>{actual}</b> vs 예상 {forecast or '-'}" + (f" ({sp:+.1f}% 서프라이즈)" if sp is not None else ""))
    return "\n".join(lines)


def msg_reaction(sym, src, info, reaction, senti, eps):
    lines = [f"📈 {tag(sym, src)} 실적 발표 후 반응"]
    if reaction:
        base, now, chg = reaction
        lines.append(f"주가 ${base:,.2f} → ${now:,.2f} (<b>{chg:+.1%}</b>, 발표 직전 종가 대비)")
    actual, surprise, forecast = eps
    if actual:
        sp = money_num(surprise or "")
        lines.append(f"EPS {actual} vs 예상 {forecast or '-'}" + (f" ({sp:+.1f}%)" if sp is not None else ""))
    if senti and senti[2]:
        bull, bear, total = senti
        lines.append(f"StockTwits 최근 {total}개: 🟢 강세 {bull} · 🔴 약세 {bear}")
    return "\n".join(lines)


def msg_weekly(entries):
    lines = ["<b>🗓 이번 주 관심 종목 실적 발표</b>"]
    if not entries:
        return "\n".join(lines + ["이번 주에는 관심 종목 실적 발표가 없어요."])
    for sym, src, info in sorted(entries, key=lambda e: (e[2]["date"], e[0])):
        us_day = date.fromisoformat(info["date"])
        when = {"time-pre-market": "장전", "time-after-hours": "장후"}.get(info.get("time"), "시간 미정")
        eps = f" · 예상 EPS {info['eps_forecast']}" if info.get("eps_forecast") else ""
        lines.append(f"• {kday(us_day)} {when} — {tag(sym, src)} {html.escape(info.get('name', ''))}{eps}")
    lines.append("<i>⭐ 직접 등록 · 🏦 파브라이 보유 · 🕵️ 내부자 매수</i>")
    return "\n".join(lines)


# ---------- 실행 ----------

class Sender:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat = os.environ.get("BOT_OWNER_ID") or OWNER_ID_DEFAULT

    def __call__(self, text):
        if self.dry_run or not self.token:
            print(text, "\n-----")
            return
        send_telegram(self.token, self.chat, text)


def check_materials(sec, state, send, today):
    """발표일 전후 종목의 8-K 실적 공시 확인 → 새로 나오면 링크 전송."""
    for sym, info in state.get("upcoming", {}).items():
        us_day = date.fromisoformat(info["date"])
        if not (-1 <= (today - us_day).days <= 3):
            continue
        rec = state.setdefault("sent", {}).setdefault(sym, {})
        if rec.get("materials_for") == info["date"]:
            continue
        try:
            mat = latest_earnings_8k(sec, sym, (us_day - timedelta(days=1)).isoformat())
        except Exception as e:
            print(f"[경고] {sym} 8-K 확인 실패: {e}", file=sys.stderr)
            continue
        if not mat:
            continue
        try:
            eps = actual_eps(sym, us_day)
        except Exception:
            eps = (None, None, None)
        send(msg_materials(sym, info.get("src", ""), info, mat, eps))
        rec["materials_for"] = info["date"]
        rec["materials_at"] = datetime.now(KST).isoformat(timespec="minutes")
        state.setdefault("last_materials", {})[sym] = {"filed": mat["filed"], "docs": mat["docs"],
                                                        "webcast": mat["webcast"], "index": mat["index"]}


def run_daily(sec, state, send, today, weekly):
    tickers = universe(sec)
    print(f"[정보] 실적 추적 종목 {len(tickers)}개: {', '.join(sorted(tickers))}", file=sys.stderr)
    found = scan_calendar(set(tickers), today, SCAN_DAYS)
    upcoming = state.setdefault("upcoming", {})
    for sym, info in found.items():
        upcoming[sym] = {**info, "src": tickers[sym]}
    # 지난 일정 정리 (반응 알림까지 끝났거나 10일 지난 것)
    for sym in list(upcoming):
        if (today - date.fromisoformat(upcoming[sym]["date"])).days > 10 or sym not in tickers:
            upcoming.pop(sym)
    sent = state.setdefault("sent", {})

    for sym, info in upcoming.items():
        rec = sent.setdefault(sym, {})
        if rec.get("for") != info["date"]:
            rec.clear()
            rec["for"] = info["date"]
        days_left = (date.fromisoformat(info["date"]) - today).days
        if 0 <= days_left <= 1 and not rec.get("d1"):
            send(msg_reminder(sym, info["src"], info, days_left))
            rec["d1"] = rec["d7"] = True
        elif 2 <= days_left <= 7 and not rec.get("d7"):
            send(msg_reminder(sym, info["src"], info, days_left))
            rec["d7"] = True

    check_materials(sec, state, send, today)

    # 자료 공개 후 다음 일간 실행에 반응 알림
    for sym, info in upcoming.items():
        rec = sent.get(sym, {})
        us_day = date.fromisoformat(info["date"])
        if rec.get("materials_for") == info["date"] and not rec.get("reaction") and today > us_day:
            try:
                reaction = price_reaction(sym, us_day, info.get("time"))
            except Exception as e:
                print(f"[경고] {sym} 주가 반응 조회 실패: {e}", file=sys.stderr)
                reaction = None
            try:
                senti = stocktwits_sentiment(sym)
            except Exception:
                senti = None
            try:
                eps = actual_eps(sym, us_day)
            except Exception:
                eps = (None, None, None)
            send(msg_reaction(sym, info["src"], info, reaction, senti, eps))
            rec["reaction"] = True

    if weekly:
        week_end = today + timedelta(days=6)
        entries = [(s, i["src"], i) for s, i in upcoming.items()
                   if today <= date.fromisoformat(i["date"]) <= week_end]
        send(msg_weekly(entries))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--materials", action="store_true", help="자료 공개 확인만")
    parser.add_argument("--weekly", choices=["auto", "yes", "no"], default="auto",
                        help="주간 일정 요약 전송 (auto: 월요일 KST)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        print("[경고] SEC_USER_AGENT 가 없어 실적 알림을 건너뜁니다.", file=sys.stderr)
        return
    sec = SEC(ua)
    state = load(STATE_PATH, {})
    send = Sender(args.dry_run)
    today = datetime.now(ET_TZ).date()  # 미국 동부 기준 날짜
    if args.materials:
        check_materials(sec, state, send, today)
    else:
        weekly = args.weekly == "yes" or (args.weekly == "auto" and datetime.now(KST).weekday() == 0)
        run_daily(sec, state, send, today, weekly)
    if not args.dry_run:
        save(STATE_PATH, state)


if __name__ == "__main__":
    main()
