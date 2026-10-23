"""관심 종목(봇 /add)의 새 SEC 공시를 요약해 개인 대화방으로 전송한다.

대상 공시
  10-Q / 10-K : SEC XBRL 재무 데이터로 매출·영업이익·순이익·희석 EPS와 전년 동기 대비 증감
  8-K (Item 2.02 실적) : 보도자료에서 숫자·핵심 단어가 들어간 문장을 골라 한국어로 번역
  6-K (해외 기업 실적성 공시) : 보도자료 핵심 문장 발췌·번역 (실적과 무관한 6-K는 건너뜀)

AI 요약이 아니라 규칙 기반 발췌(숫자·핵심어 점수)와 기계 번역이다. 정확한 수치는 원문 링크로 확인.

상태: data/filings/state.json — 종목별로 이미 본 공시 번호. 처음 보는 종목은 기존 공시를 기준점으로만 기록한다.
earnings.py가 같은 8-K 실적 공시를 이미 보냈으면(요약 포함) 중복 전송하지 않는다.

사용법:
  python filings.py                                  # 새 공시 확인·전송 (매시간)
  python filings.py --test NFLX --form 10-Q          # 해당 종목 최근 공시 요약 (상태 무시)
  python filings.py --test TSM --form 6-K --dry-run  # 전송 없이 출력
"""

import argparse
import html
import os
import re
import sys
from datetime import date, datetime, timedelta

import earnings
from earnings import Sender, exhibits, load, save, ticker_cik
from reddit_top10 import translate_ko
from sec13f import SEC, money

STATE_PATH = os.path.join("data", "filings", "state.json")
FORMS = {"8-K", "6-K", "10-Q", "10-K"}
FORM_LABEL = {"8-K": "8-K 실적 발표", "6-K": "6-K 실적 발표(해외 기업)", "10-Q": "10-Q 분기보고서", "10-K": "10-K 연간보고서"}
FRESH_DAYS = 5  # 이보다 오래된 미확인 공시는 알리지 않고 기록만
KEEP_SEEN = 80
MAX_BULLETS = 4

# (표시 이름, us-gaap 개념 후보, 단위)
CONCEPTS = [
    ("매출", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
              "RevenueFromContractWithCustomerIncludingAssessedTax"], "USD"),
    ("영업이익", ["OperatingIncomeLoss"], "USD"),
    ("순이익", ["NetIncomeLoss", "ProfitLoss"], "USD"),
    ("희석 EPS", ["EarningsPerShareDiluted"], "USD/shares"),
]
KEYS = ["revenue", "net income", "net loss", "earnings per share", "eps", "operating income", "guidance",
        "outlook", "expects", "record", "increase", "decrease", "grew", "growth", "decline", "margin",
        "cash flow", "subscribers", "backlog", "bookings", "ebitda", "profit", "sales", "billion", "million"]
BAD = ["forward-looking", "safe harbor", "undue reliance", "webcast", "conference call", "dial-in", "dial in",
       "press release", "reconciliation", "non-gaap financial measures", "©", "all rights reserved", "forward looking"]
HEAD_SKIP = re.compile(r"exhibit|immediate release|contact|^\W*\d|nasdaq:|nyse:|investor relations|^\(?\w+\)?$", re.I)
ABBREV_END = re.compile(r"(\b(?:Inc|Corp|Co|Ltd|Mr|Mrs|Ms|Dr|No|vs|St|Jr|Sr|U\.S|Fig|approx)\.|\b[A-Z]\.)$")
EARNINGS_WORDS = re.compile(r"result|earning|quarter|revenue|fiscal|financial|net income|net loss|profit", re.I)


# ---------- 10-Q / 10-K : XBRL ----------

def usd(value):
    return ("-" if value < 0 else "") + money(abs(value))


def yoy_text(cur, prior):
    if prior in (None, 0):
        return ""
    if prior < 0 < cur:
        return "흑자 전환"
    if prior > 0 > cur:
        return "적자 전환"
    return f"{(cur - prior) / abs(prior):+.0%}"


def pick_fact(facts, concepts, unit, acc, annual):
    lo, hi = (340, 380) if annual else (80, 100)
    span = lambda r: (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
    for concept in concepts:
        rows = facts.get("us-gaap", {}).get(concept, {}).get("units", {}).get(unit, [])
        cur = [r for r in rows if r.get("accn") == acc and "start" in r and lo <= span(r) <= hi]
        if not cur:
            continue
        cur = max(cur, key=lambda r: r["end"])
        prior_end = date.fromisoformat(cur["end"]) - timedelta(days=365)
        prior = [r for r in rows if "start" in r and lo <= span(r) <= hi
                 and abs((date.fromisoformat(r["end"]) - prior_end).days) <= 12]
        return cur, (max(prior, key=lambda r: r.get("filed", "")) if prior else None)
    return None, None


def xbrl_summary(sec, cik, acc, annual):
    """[(이름, 문자열)], 기준 정보 — 아직 XBRL에 반영되지 않았으면 None."""
    facts = sec.json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json").get("facts", {})
    rows, period, revenue, op_income = [], None, None, None
    for label, concepts, unit in CONCEPTS:
        cur, prior = pick_fact(facts, concepts, unit, acc, annual)
        if not cur:
            continue
        period = period or cur
        value = cur["val"]
        text = f"${value:,.2f}" if unit == "USD/shares" else usd(value)
        change = yoy_text(value, prior["val"] if prior else None)
        if prior:
            prev = f"${prior['val']:,.2f}" if unit == "USD/shares" else usd(prior["val"])
            text += f" (전년 동기 {prev}" + (f", {change}" if change else "") + ")"
        if label == "매출":
            revenue = value
        if label == "영업이익":
            op_income = value
            if revenue:
                text += f" · 영업이익률 {value / revenue:.1%}"
        rows.append((label, text))
    return (rows, period) if rows else None


# ---------- 8-K / 6-K : 보도자료 ----------

def clean_lines(page):
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)</(p|div|tr|li|h\d|table)>|<br\s*/?>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page)).replace("\xa0", " ")
    lines = (re.sub(r"^[•◦▪●·○■□\-–—*]+\s*", "", " ".join(line.split())) for line in text.splitlines())  # 목록 기호 제거
    return [l for l in lines if l]


def split_sentences(lines):
    sentences = []
    for line in lines:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z$“\"(])", line)
        buf = ""
        for part in parts:
            buf = f"{buf} {part}".strip() if buf else part
            if ABBREV_END.search(buf):
                continue  # 약어 뒤 마침표 — 다음 조각과 이어 붙임
            sentences.append(buf)
            buf = ""
        if buf:
            sentences.append(buf)
    return sentences


def headline(lines):
    for line in lines[:25]:
        if 35 <= len(line) <= 260 and not HEAD_SKIP.search(line):
            return line
    return ""


def score_sentence(sentence, position):
    if not 45 <= len(sentence) <= 420:
        return 0
    low = sentence.lower()
    if any(bad in low for bad in BAD):
        return 0
    score = sum(key in low for key in KEYS)
    score += 2 if re.search(r"\$\s?\d", sentence) else 0
    score += 1 if "%" in sentence else 0
    score += 1 if position < 25 else 0
    return score if score >= 3 else 0


def press_release_points(lines, title):
    sentences = split_sentences(lines)
    scored = [(score_sentence(s, i), i, s) for i, s in enumerate(sentences) if s != title]
    best = sorted((t for t in scored if t[0]), key=lambda t: (-t[0], t[1]))[:MAX_BULLETS]
    return [s for _, _, s in sorted(best, key=lambda t: t[1])]


def summarize_press_release(sec, docs):
    """[(제목, 핵심 문장들)] — docs: earnings.exhibits()['docs']."""
    for doc_type, _, url in docs:
        if doc_type in ("EX-99.1", "EX-99"):
            lines = clean_lines(sec.get(url).decode("utf-8", "replace"))
            title = headline(lines)
            return title, press_release_points(lines, title), lines
    return "", [], []


def summary_block(title, points):
    """번역된 제목·핵심 문장 → 메시지 줄 목록."""
    out = []
    if title:
        out.append(f"<b>{html.escape(translate_ko(title[:240]))}</b>")
    for point in points:
        out.append(f"• {html.escape(translate_ko(point[:300]))}")
    return out


def summary_text(sec, mat):
    """earnings.py의 자료 공개 메시지에 덧붙일 요약 (없으면 빈 문자열)."""
    try:
        title, points, _ = summarize_press_release(sec, mat["docs"])
        block = summary_block(title, points)
    except Exception as e:
        print(f"[경고] 보도자료 요약 실패: {e}", file=sys.stderr)
        return ""
    if not block:
        return ""
    return ("\n\n📝 <b>핵심 요약</b> <i>(자동 발췌·기계 번역)</i>\n" + "\n".join(block))


# ---------- 메시지 ----------

def build_message(sec, sym, cik, name, filing):
    """메시지 문자열 | None(알릴 필요 없음) | "RETRY"(데이터 준비 전)."""
    form, acc = filing["form"], filing["accession"]
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
    head = f"📑 <b>${html.escape(sym)}</b> ⭐ {html.escape(name)} — {FORM_LABEL[form]}"
    lines = [head, f"<i>제출 {filing['filed']}</i>"]
    if form in ("10-Q", "10-K"):
        result = xbrl_summary(sec, cik, acc, annual=(form == "10-K"))
        if not result:
            age = (date.today() - date.fromisoformat(filing["filed"])).days
            if age <= 1:
                return "RETRY"
            lines.append("재무 숫자를 SEC 데이터에서 찾지 못했어요. 원문을 확인하세요.")
        else:
            rows, period = result
            fy, fp = period.get("fy", ""), period.get("fp", "")
            if form == "10-Q":
                lines += [f"<b>FY{fy} {fp}</b> · 기준일 {period['end']} (해당 분기 3개월)", ""]
            else:
                lines += [f"<b>FY{fy}</b> · 기준일 {period['end']} (연간)", ""]
            lines += [f"• {label} {text}" for label, text in rows]
        doc = filing.get("primary")
        lines += ["", f'<a href="{folder}/{doc}">보고서 원문</a>' if doc else f'<a href="{folder}/">공시 폴더</a>',
                  "<i>※ SEC XBRL 재무 데이터 기준 — 회사 발표 수치(조정 항목 등)와 다를 수 있어요</i>"]
        return "\n".join(lines)

    # 8-K / 6-K : 보도자료 발췌
    ex = exhibits(sec, cik, acc)
    docs = ex["docs"]
    if docs:
        title, points, text_lines = summarize_press_release(sec, docs)
    else:  # 6-K는 본문에 바로 쓰는 경우가 있음
        text = sec.get(f"{folder}/{filing['primary']}").decode("utf-8", "replace") if filing.get("primary") else ""
        text_lines = clean_lines(text)
        title = headline(text_lines)
        points = press_release_points(text_lines, title)
    if form == "6-K" and not EARNINGS_WORDS.search(" ".join(text_lines[:30])):
        return None  # 실적과 무관한 6-K
    block = summary_block(title, points)
    lines += [""] + (block or ["보도자료 본문에서 핵심 문장을 뽑지 못했어요. 원문을 확인하세요."])
    links = [f'<a href="{html.escape(url)}">{label}</a>' for doc_type, _, url in docs
             for label in [{"EX-99.1": "보도자료", "EX-99.2": "추가 자료"}.get(doc_type, doc_type)]]
    if ex.get("webcast"):
        links.append(f'🎧 <a href="{html.escape(ex["webcast"])}">웹캐스트</a>')
    links.append(f'<a href="{html.escape(ex["index"])}">공시 전체</a>')
    lines += ["", " · ".join(links), "<i>※ 보도자료 핵심 문장 자동 발췌·기계 번역 — 정확한 수치는 원문으로 확인</i>"]
    return "\n".join(lines)[:4000]


# ---------- 실행 ----------

def list_filings(data):
    r = data["filings"]["recent"]
    rows = []
    for form, acc, filed, items, primary in zip(r["form"], r["accessionNumber"], r["filingDate"],
                                                 r["items"], r["primaryDocument"]):
        if form in FORMS and (form != "8-K" or "2.02" in (items or "")):
            rows.append({"form": form, "accession": acc, "filed": filed, "primary": primary})
    return rows  # 최신순


def already_sent_by_earnings(sym, acc):
    state = load(earnings.STATE_PATH, {})
    return state.get("last_materials", {}).get(sym, {}).get("accession") == acc


def check_ticker(sec, sym, state, send):
    cik = ticker_cik(sec, sym)
    if not cik:
        print(f"[경고] {sym}: SEC에서 CIK를 찾지 못했습니다", file=sys.stderr)
        return 0
    data = sec.json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    filings = list_filings(data)
    seen = state.setdefault("seen", {}).get(sym)
    if seen is None:  # 처음 보는 종목 → 기준점만 기록
        state["seen"][sym] = [f["accession"] for f in filings][:KEEP_SEEN]
        print(f"[정보] {sym}: 기준점 기록 ({len(filings)}건)", file=sys.stderr)
        return 0
    sent = 0
    for filing in reversed(filings):  # 오래된 것부터
        acc = filing["accession"]
        if acc in seen:
            continue
        fresh = (date.today() - date.fromisoformat(filing["filed"])).days <= FRESH_DAYS
        if fresh and not (filing["form"] == "8-K" and already_sent_by_earnings(sym, acc)):
            try:
                message = build_message(sec, sym, cik, data.get("name", sym), filing)
            except Exception as e:
                print(f"[경고] {sym} {filing['form']} 요약 실패: {e}", file=sys.stderr)
                message = None
            if message == "RETRY":
                print(f"[정보] {sym} {filing['form']}: 재무 데이터 준비 전 — 다음 실행에 재시도", file=sys.stderr)
                continue
            if message:
                send(message)
                sent += 1
        seen.append(acc)
    state["seen"][sym] = seen[-KEEP_SEEN:]
    return sent


def run_test(sec, sym, form, send):
    cik = ticker_cik(sec, sym)
    if not cik:
        sys.exit(f"{sym}: SEC에서 CIK를 찾지 못했습니다")
    data = sec.json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    candidates = [f for f in list_filings(data) if f["form"] == form][:15]
    for filing in candidates:
        message = build_message(sec, sym, cik, data.get("name", sym), filing)
        if message and message != "RETRY":
            send(message)
            return
        print(f"[정보] {filing['accession']} 건너뜀 (실적 관련 아님 또는 데이터 없음)", file=sys.stderr)
    print(f"{sym}: 최근 {form} 중 요약할 공시를 찾지 못했어요")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", help="이 티커의 최근 공시 하나를 요약 (상태 무시)")
    parser.add_argument("--form", default="8-K", choices=sorted(FORMS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        print("[경고] SEC_USER_AGENT 가 없어 공시 요약을 건너뜁니다.", file=sys.stderr)
        return
    sec, send = SEC(ua), Sender(args.dry_run)
    if args.test:
        run_test(sec, args.test.upper(), args.form, send)
        return
    watch = sorted(load(earnings.WATCHLIST_PATH, {"tickers": {}})["tickers"])
    state = load(STATE_PATH, {})
    sent = sum(check_ticker(sec, sym, state, send) for sym in watch)
    print(f"공시 요약: 관심 종목 {len(watch)}개 확인, {sent}건 전송")
    if not args.dry_run:
        save(STATE_PATH, state)


if __name__ == "__main__":
    main()
