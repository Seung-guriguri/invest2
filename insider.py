"""SEC Form 4 내부자 장내 매수 다이제스트를 하루 한 번 텔레그램으로 보낸다.

필터
  - 거래 코드 P(장내 매수)만, 매수 금액 $100K 이상, 10b5-1(사전 약정 자동 매매) 제외
  - 시가총액 $300M 이상 (Yahoo 주가 × SEC 공시 발행주식수)
  - CEO · CFO · 이사만
  - 신고 매수가가 실제 주가와 2배 이상 차이 나면 제외 (해외 통화 표기 등 데이터 오류)
시그널 / 점수
  금액 점수(1~4) + CEO/CFO(+2) + 클러스터(+3: 14일 안에 같은 회사 임원·이사 2명 이상이 $25K+ 매수)
  + 오랜만의 매수(+2: 해당 내부자가 1년 넘게 이 회사 주식을 사지 않음) + 하락 중 매수(+1: 52주 고점 대비 -20% 이하)
  + 보유량 대비 매수(+1: 기존 보유 +10% 이상, +2: +50% 이상 또는 신규 보유)
점수 상위 5개만 보내고, 없으면 "오늘 의미 있는 내부자 매수 없음" 한 줄을 보낸다.

공시일 기준으로 처리하며, 마지막으로 처리한 날짜를 data/insider/state.json 에 기록해 하루 한 번만 보낸다.

환경변수: SEC_USER_AGENT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

사용법:
  python insider.py                         # 새로 올라온 공시일 처리 후 전송
  python insider.py --dry-run --date 2026-10-09   # 특정 공시일을 전송 없이 출력
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from reddit_top10 import BROWSER_UA, deliver
from sec13f import SEC, local, money

ET_TZ = ZoneInfo("America/New_York")
STATE_PATH = os.path.join("data", "insider", "state.json")
INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d"

MIN_VALUE = 100_000
MIN_MARKET_CAP = 300_000_000
CLUSTER_MIN_VALUE = 25_000  # 클러스터 인원으로 셀 최소 매수 금액
PRICE_TOLERANCE = 2.0  # 신고 매수가 vs 실제 주가 허용 배수
CLUSTER_DAYS = 14
FIRST_BUY_DAYS = 365
DIP = 0.20
TOP_N = 5
MAX_LOOKUPS = 30  # 클러스터/첫 매수 확인 시 조회할 과거 Form 4 최대 개수


# ---------- Form 4 파싱 ----------

def fetch_form4(sec, path):
    """전체 제출 텍스트(.txt)에서 ownershipDocument XML을 꺼내 파싱."""
    text = sec.get(f"https://www.sec.gov/Archives/{path}").decode("utf-8", "replace")
    match = re.search(r"<XML>\s*(.*?)\s*</XML>", text, re.S)
    if not match:
        return None
    return parse_form4(ET.fromstring(match.group(1)))


def text_of(node, path):
    el = node.find(path)
    return (el.text or "").strip() if el is not None and el.text else ""


def parse_form4(root):
    issuer = root.find("issuer")
    owners = []
    for owner in root.findall("reportingOwner"):
        rel = owner.find("reportingOwnerRelationship")
        flag = lambda tag: text_of(rel, tag).lower() in ("1", "true")
        owners.append({
            "cik": text_of(owner, "reportingOwnerId/rptOwnerCik").lstrip("0"),
            "name": text_of(owner, "reportingOwnerId/rptOwnerName"),
            "director": flag("isDirector"),
            "officer": flag("isOfficer"),
            "title": text_of(rel, "officerTitle") if flag("isOfficer") else "",
        })
    purchases = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if text_of(tx, "transactionCoding/transactionCode") != "P":
            continue
        if text_of(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") not in ("", "A"):
            continue
        try:
            shares = float(text_of(tx, "transactionAmounts/transactionShares/value") or 0)
            price = float(text_of(tx, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        try:
            after = float(text_of(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value") or 0)
        except ValueError:
            after = 0
        purchases.append({"date": text_of(tx, "transactionDate/value")[:10], "shares": shares, "price": price,
                          "after": after, "nature": text_of(tx, "ownershipNature/directOrIndirectOwnership/value")})
    symbol = re.split(r"[\s,;/]+", text_of(issuer, "issuerTradingSymbol").upper())[0]
    return {
        "issuer_cik": text_of(issuer, "issuerCik").lstrip("0"),
        "issuer": text_of(issuer, "issuerName"),
        "symbol": "" if symbol in ("", "NONE", "N/A", "NA") else symbol,
        "owners": owners,
        "purchases": purchases,
        "plan_10b5_1": text_of(root, "aff10b5One").lower() in ("1", "true"),
    }


def holding_increase(purchases):
    """매수로 늘어난 보유 비율 (직접 보유 기준, 없으면 전체). 기존 보유가 0이면 inf."""
    direct = [p for p in purchases if p["nature"] == "D"] or purchases
    bought = sum(p["shares"] for p in direct)
    after = direct[-1]["after"]
    before = after - bought
    if not after:
        return None
    return float("inf") if before <= 0 else bought / before


def purchase_value(doc):
    return sum(p["shares"] * p["price"] for p in doc["purchases"])


def roles(owner):
    title = owner["title"]
    found = []
    if re.search(r"\bCEO\b|chief\s+executive", title, re.I):
        found.append("CEO")
    if re.search(r"\bCFO\b|chief\s+financial", title, re.I):
        found.append("CFO")
    if owner["director"]:
        found.append("이사")
    return found


# ---------- 일별 공시 목록 ----------

def form4_paths(sec, day):
    """해당 공시일의 Form 4 목록 (중복 제거). 아직 없거나 휴일이면 None."""
    url = INDEX_URL.format(y=day.year, q=(day.month - 1) // 3 + 1, ymd=day.strftime("%Y%m%d"))
    try:
        text = sec.get(url).decode("latin-1")
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise
    paths = {}
    for line in text.splitlines():
        if line.startswith("4 "):
            path = line.split()[-1]
            paths[path.rsplit("/", 1)[-1]] = path  # accession 기준 중복 제거
    return list(paths.values())


# ---------- 시장 데이터 ----------

def yahoo_chart(symbol):
    """(현재가, [(날짜, 고가, 종가)])"""
    req = urllib.request.Request(CHART_URL.format(sym=symbol.replace(".", "-")), headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.load(resp)["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    days = [datetime.fromtimestamp(t, ET_TZ).date() for t in result.get("timestamp", [])]
    return result["meta"].get("regularMarketPrice"), list(zip(days, quote.get("high") or [], quote.get("close") or []))


def business_days(start, end):
    days, day = 0, start
    while day < end:
        day += timedelta(days=1)
        days += day.weekday() < 5
    return days


def shares_outstanding(sec, cik):
    data = sec.json(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{int(cik):010d}/dei/EntityCommonStockSharesOutstanding.json")
    facts = data["units"]["shares"]
    latest = max(f["end"] for f in facts)
    return sum(f["val"] for f in facts if f["end"] == latest)  # 여러 주식 종류면 합산


# ---------- 시그널 ----------

def past_form4s(sec, cik, start, end):
    """cik(회사 또는 내부자)의 [start, end) 공시일 Form 4 경로 목록 (최신순)."""
    data = sec.json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
    recent = data["filings"]["recent"]
    out = []
    for form, acc, filed in zip(recent["form"], recent["accessionNumber"], recent["filingDate"]):
        if form == "4" and start <= filed < end:
            out.append(f"edgar/data/{int(cik)}/{acc}.txt")
    return out[:MAX_LOOKUPS]


def cluster_buyers(sec, issuer_cik, filed_day, today_buyers):
    """공시일 기준 최근 14일 동안 이 회사 주식을 장내 매수한 내부자 이름 집합."""
    buyers = set(today_buyers)
    start = (filed_day - timedelta(days=CLUSTER_DAYS)).isoformat()
    for path in past_form4s(sec, issuer_cik, start, filed_day.isoformat()):
        doc = fetch_form4(sec, path)
        if doc and counts_for_cluster(doc, doc["owners"][0] if doc["owners"] else None, purchase_value(doc)):
            buyers.add(doc["owners"][0]["name"])
    return buyers


def counts_for_cluster(doc, owner, value):
    """클러스터 인원: 임원·이사가 10b5-1이 아닌 $25K 이상 장내 매수."""
    return (owner is not None and doc["purchases"] and not doc["plan_10b5_1"]
            and (owner["officer"] or owner["director"]) and value >= CLUSTER_MIN_VALUE)


def is_first_buy(sec, owner_cik, issuer_cik, tx_day):
    """해당 내부자가 직전 1년(최근 14일 제외) 동안 이 회사 주식을 장내 매수한 적이 없으면 True."""
    start = (tx_day - timedelta(days=FIRST_BUY_DAYS)).isoformat()
    end = (tx_day - timedelta(days=CLUSTER_DAYS)).isoformat()
    paths = past_form4s(sec, owner_cik, start, end)
    if len(paths) >= MAX_LOOKUPS:
        return False  # 공시가 너무 많아 확인 불가 → 보수적으로 아님
    for path in paths:
        doc = fetch_form4(sec, path)
        if doc and doc["issuer_cik"] == issuer_cik and doc["purchases"]:
            return False
    return True


def amount_score(value):
    return 1 if value < 250_000 else 2 if value < 1_000_000 else 3 if value < 5_000_000 else 4


# ---------- 처리 ----------

def collect(sec, day):
    """공시일 하나의 장내 매수 후보 (금액·직책 필터 적용 전 전체 P 매수 포함)."""
    paths = form4_paths(sec, day)
    if paths is None:
        return None
    print(f"[정보] {day} Form 4 {len(paths)}건 확인", file=sys.stderr)
    buys = []
    for path in paths:
        try:
            doc = fetch_form4(sec, path)
        except Exception as e:
            print(f"[경고] {path}: {e}", file=sys.stderr)
            continue
        if not doc or not doc["purchases"] or not doc["owners"]:
            continue
        value = purchase_value(doc)
        priced = [p for p in doc["purchases"] if p["price"] > 0]
        avg_price = value / sum(p["shares"] for p in priced) if priced else 0
        owner = doc["owners"][0]
        tx_dates = [p["date"] for p in doc["purchases"] if re.match(r"\d{4}-\d{2}-\d{2}$", p["date"])]
        buys.append({**doc, "owner": owner, "roles": roles(owner), "value": value, "avg_price": avg_price,
                     "increase": holding_increase(doc["purchases"]),
                     "tx_date": min(tx_dates) if tx_dates else day.isoformat(), "filed": day})
    return buys


def score_issuers(sec, buys):
    """금액·직책 필터를 통과한 매수를 회사별로 묶고 시총 필터와 시그널 점수 계산."""
    all_buyers = defaultdict(set)
    for b in buys:
        if counts_for_cluster(b, b["owner"], b["value"]):
            all_buyers[b["issuer_cik"]].add(b["owner"]["name"])
    qualified = defaultdict(list)
    for b in buys:
        if b["value"] >= MIN_VALUE and b["roles"] and b["symbol"] and not b["plan_10b5_1"]:
            qualified[b["issuer_cik"]].append(b)

    results = []
    for issuer_cik, items in qualified.items():
        first = items[0]
        try:
            price, highs = yahoo_chart(first["symbol"])
            cap = (price or 0) * shares_outstanding(sec, issuer_cik)
        except Exception as e:
            print(f"[경고] {first['symbol']} 시총 확인 실패: {e}", file=sys.stderr)
            continue
        if cap < MIN_MARKET_CAP:
            continue
        # 신고 매수가 vs 매수일 종가 비교 — 크게 다르면 통화/단위 오류로 보고 제외
        items_checked = []
        for b in items:
            tx = date.fromisoformat(b["tx_date"])
            closes = [c for d, _, c in highs if d <= tx and c]
            ref = closes[-1] if closes else price
            if ref and b["avg_price"] and not (1 / PRICE_TOLERANCE <= b["avg_price"] / ref <= PRICE_TOLERANCE):
                print(f"[경고] {first['symbol']} 매수가 ${b['avg_price']:.2f} vs 주가 ${ref:.2f} — 데이터 이상으로 제외",
                      file=sys.stderr)
                continue
            items_checked.append(b)
        items = items_checked
        if not items:
            continue
        total = sum(b["value"] for b in items)
        filed_day = max(b["filed"] for b in items)
        cluster = cluster_buyers(sec, issuer_cik, min(b["filed"] for b in items), all_buyers[issuer_cik])
        firsts = [b for b in items
                  if is_first_buy(sec, b["owner"]["cik"], issuer_cik, date.fromisoformat(b["tx_date"]))]
        tx_day = date.fromisoformat(min(b["tx_date"] for b in items))
        window = [h for d, h, _ in highs if tx_day - timedelta(days=365) <= d <= tx_day and h]
        high52 = max(window) if window else None
        avg_price = sum(b["avg_price"] * b["value"] for b in items) / total if total else 0
        drawdown = (1 - avg_price / high52) if high52 and avg_price else None

        score = amount_score(total)
        score += 2 if any(r in ("CEO", "CFO") for b in items for r in b["roles"]) else 0
        score += 3 if len(cluster) >= 2 else 0
        score += 2 if firsts else 0
        score += 1 if drawdown is not None and drawdown >= DIP else 0
        increase = max((b["increase"] for b in items if b["increase"] is not None), default=None)
        score += 2 if increase is not None and increase >= 0.5 else 1 if increase is not None and increase >= 0.1 else 0
        results.append({"symbol": first["symbol"], "issuer": first["issuer"], "items": items, "total": total,
                        "cap": cap, "cluster": cluster, "firsts": firsts, "drawdown": drawdown,
                        "increase": increase, "score": score, "filed": filed_day})
    results.sort(key=lambda r: (-r["score"], -r["total"]))
    return results


def build_message(days, results):
    period = days[0].strftime("%m/%d") if len(days) == 1 else f"{days[0]:%m/%d}~{days[-1]:%m/%d}"
    head = f"<b>🕵️ 내부자 매수 다이제스트</b> <i>({period} 공시분)</i>"
    if not results:
        return f"{head}\n오늘 의미 있는 내부자 매수 없음"
    lines = [head, "<i>장내매수(10b5-1 제외) · $100K+ · 시총 $300M+ · CEO/CFO/이사 · 점수순</i>"]
    for i, r in enumerate(results[:TOP_N], 1):
        people = []
        for b in sorted(r["items"], key=lambda b: -b["value"]):
            name = " ".join(w.capitalize() for w in b["owner"]["name"].split())
            people.append(f"{'/'.join(b['roles'])} {html.escape(name)} {money(b['value'])}")
        signals = []
        if len(r["cluster"]) >= 2:
            signals.append(f"👥 클러스터({CLUSTER_DAYS}일 {len(r['cluster'])}명)")
        if r["firsts"]:
            signals.append("🆕 1년+ 만의 매수")
        if r["drawdown"] is not None and r["drawdown"] >= DIP:
            signals.append(f"📉 52주 고점 -{r['drawdown']:.0%}")
        inc = r["increase"]
        if inc is not None and inc >= 0.1:
            signals.append("📦 신규 보유" if inc == float("inf") else f"📦 보유 +{inc:.0%}")
        tx_day = date.fromisoformat(min(b["tx_date"] for b in r["items"]))
        lag = business_days(tx_day, r["filed"])
        timing = f"📅 매수 {tx_day:%m/%d} · 신고 {r['filed']:%m/%d}" + (f" ⚠️ {lag}영업일 지연" if lag > 2 else "")
        lines += [
            "",
            f"<b>{i}. ${html.escape(r['symbol'])}</b> {html.escape(r['issuer'])} — 점수 {r['score']}",
            f"    💰 {money(r['total'])} · 시총 {money(r['cap'])}",
            f"    👔 {' · '.join(people)}",
            f"    {timing}",
        ]
        if signals:
            lines.append(f"    {' · '.join(signals)}")
    lines += ["", "출처: SEC EDGAR Form 4"]
    return "\n".join(lines)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def pending_days(state, today):
    """아직 처리하지 않은 평일 공시일 (최대 5일)."""
    last = date.fromisoformat(state["last_date"]) if "last_date" in state else today - timedelta(days=6)
    days, day = [], last + timedelta(days=1)
    while day <= today:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days[-5:]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="전송·상태 저장 없이 출력만")
    parser.add_argument("--date", help="특정 공시일(YYYY-MM-DD)만 처리 (상태 무시)")
    args = parser.parse_args()

    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        print("[경고] SEC_USER_AGENT 가 없어 내부자 거래 확인을 건너뜁니다.", file=sys.stderr)
        return
    sec = SEC(user_agent)
    state = load_state()
    today = datetime.now(ET_TZ).date()
    first_run = "last_date" not in state
    days = [date.fromisoformat(args.date)] if args.date else pending_days(state, today)
    if first_run and not args.date:
        # 첫 실행: 밀린 날짜는 건너뛰고 가장 최근에 게시된 공시일 하나만 처리
        for day in reversed(days):
            if form4_paths(sec, day) is not None:
                days = [day]
                break
        else:
            days = []

    processed, buys = [], []
    for day in days:
        found = collect(sec, day)
        if found is None:  # 공시 목록 없음: 오늘이면 아직 미게시 → 다음 실행에, 과거면 휴일
            if day == today:
                break
            print(f"[정보] {day} 공시 목록 없음 (휴일)", file=sys.stderr)
            if not args.date:
                state["last_date"] = day.isoformat()
            continue
        processed.append(day)
        buys += found
        if not args.date:
            state["last_date"] = day.isoformat()

    if processed:
        results = score_issuers(sec, buys)
        print(f"[정보] 장내 매수 {len(buys)}건 → 조건 충족 {len(results)}개 회사", file=sys.stderr)
        deliver(build_message(processed, results), args.dry_run)
    else:
        print("내부자 거래: 새로 처리할 공시일 없음")
    if not args.dry_run and not args.date:
        save_state(state)


if __name__ == "__main__":
    main()
