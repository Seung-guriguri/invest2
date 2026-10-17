"""관심 펀드의 13F 보고서가 새로 올라오면 직전 분기 대비 신규 편입·전량 매도 종목을 텔레그램으로 보낸다.

데이터: SEC EDGAR (API 키 불필요). SEC 규정상 User-Agent 에 이름과 연락처 이메일이 필요하다.

환경변수:
  SEC_USER_AGENT      예: "Hong Gildong hong@example.com"  (없으면 실행하지 않음)
  TELEGRAM_BOT_TOKEN  텔레그램 봇 토큰
  TELEGRAM_CHAT_ID    받을 채팅 ID

상태: data/13f/state.json 에 펀드별로 마지막으로 알린 보고서 번호를 저장해 같은 보고서를 두 번 보내지 않는다.
처음 보는 펀드는 현재 최신 보고서를 기준점으로 기록만 하고 보내지 않는다 (--force 로 강제 전송 가능).

사용법:
  python sec13f.py                    # 새 13F가 있으면 전송
  python sec13f.py --dry-run --force  # 각 펀드의 최신 보고서 변화를 전송 없이 출력
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

from reddit_top10 import deliver

# (표시 이름, 운용자, CIK 또는 None) — CIK가 None이면 EDGAR에서 이름으로 검색
FUNDS = [
    ("Berkshire Hathaway", "워런 버핏 → 그레그 에이블", 1067983, None),
    ("Duquesne Family Office", "스탠리 드러켄밀러", 1536411, None),
    ("Thiel Macro", "피터 틸", 1562087, None),
    ("Dalal Street (Pabrai)", "모니시 파브라이", 1549575, None),
    # 추가 후보 (CIK 확인 완료):
    # ("Pershing Square", "빌 애크먼", 2026053, None),  # 2026 Q2부터 상장 지주사 Pershing Square Inc.가 제출
    # ("Baupost Group", "세스 클라만", 1061768, None),
    # ("Himalaya Capital", "리루", 1709323, None),
    # ("Tiger Global", "체이스 콜먼", 1167483, None),
    # ("Coatue Management", "필립 라퐁", 1135730, None),
]
MAX_ITEMS = 5  # 항목별 최대 표시 개수
CHANGE_MIN = 0.10  # 추가 매수/일부 매도: 보유 주식 수가 이 비율 이상 변한 종목 (가격 변동만으로 인한 비중 변화 제외)
CHANGE_MIN_WEIGHT = 0.01  # …그리고 직전 또는 현재 비중이 이 이상인 종목만 (작은 포지션 잡음 제외)
SMALL_NEW = 0.001  # 비중 이 미만 신규 편입은 '소규모 신규'로 묶어 이름만 표시
STALE_DAYS = 270  # 최신 13F가 이보다 오래되면 더 이상 제출하지 않는 것으로 보고 건너뜀
STATE_PATH = os.path.join("data", "13f", "state.json")
# 13D/13G(5% 이상 지분 공시)를 추적할 인물: (표시 이름, 신고 CIK 목록)
STAKE_FILERS = {"모니시 파브라이": [1173334, 1549575]}  # 본인 명의, Dalal Street
STAKE_FORMS = {"SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A",
               "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}
# 전체 보유 내역(주식 수·평가액·직전 대비 변화·분기말 주가)을 상세히 보여줄 펀드
DETAIL_FUNDS = {1549575: "13F에는 미국 상장 증권만 보고되므로, 해외 상장 종목 비중이 큰 파브라이 펀드는 "
                         "포트폴리오의 일부만 나타납니다."}


class SEC:
    def __init__(self, user_agent):
        self.headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}

    def get(self, url):
        time.sleep(0.2)  # SEC 요청 제한(초당 10회) 준수
        req = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def json(self, url):
        return json.loads(self.get(url))

    def find_cik(self, name):
        query = urllib.parse.urlencode({"action": "getcompany", "company": name, "type": "13F-HR",
                                        "dateb": "", "owner": "include", "count": "10", "output": "atom"})
        text = self.get(f"https://www.sec.gov/cgi-bin/browse-edgar?{query}").decode("utf-8", "replace")
        match = re.search(r"<cik>(\d+)</cik>", text) or re.search(r"CIK=(\d{10})", text)
        return int(match.group(1)) if match else None

    def latest_13f(self, cik, count=2):
        """최근 13F-HR 보고서 목록 (최신순, 보고 기준일이 서로 다른 것만)."""
        data = self.json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        recent = data["filings"]["recent"]
        filings, periods = [], set()
        for form, acc, filed, period in zip(recent["form"], recent["accessionNumber"],
                                            recent["filingDate"], recent["reportDate"]):
            if form != "13F-HR" or period in periods:
                continue
            periods.add(period)
            filings.append({"accession": acc, "filed": filed, "period": period})
            if len(filings) == count:
                break
        return data.get("name", ""), filings

    def holdings(self, cik, accession):
        """보고서의 보유 종목 {(cusip, put/call): {name, value, shares}} (value는 달러)."""
        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
        items = self.json(f"{folder}/index.json")["directory"]["item"]
        xml_files = [i["name"] for i in items if i["name"].lower().endswith(".xml")
                     and "primary_doc" not in i["name"].lower()]
        if not xml_files:
            raise RuntimeError(f"{accession}: 보유 종목 XML을 찾지 못했습니다")
        root = ET.fromstring(self.get(f"{folder}/{xml_files[0]}"))
        result = defaultdict(lambda: {"name": "", "class": "", "value": 0, "shares": 0})
        for row in root.iter():
            if local(row.tag) != "infoTable":
                continue
            fields = {local(el.tag): (el.text or "").strip() for el in row.iter()}
            key = (fields.get("cusip", ""), fields.get("putCall", ""))
            entry = result[key]
            entry["name"] = fields.get("nameOfIssuer", "")
            entry["class"] = fields.get("titleOfClass", "")
            entry["value"] += int(float(fields.get("value") or 0))
            entry["shares"] += int(float(fields.get("sshPrnamt") or 0))
        # 빈 보고서용 자리표시 행(NA, 0 등) 제외
        return {k: v for k, v in result.items() if v["value"] > 1 and k[0].strip("0")}


def local(tag):
    return tag.rsplit("}", 1)[-1]


def to_dollars(holdings, period):
    """value 단위를 달러로 맞춤. 2023년 이전 13F는 천 달러 단위이고, 이후에도 천 달러로 내는 곳이 있어
    주당 가격(value/주식수)의 중앙값이 $1 미만이면 천 달러 단위로 판단한다."""
    prices = sorted(e["value"] / e["shares"] for e in holdings.values() if e["shares"] > 0)
    in_thousands = period < "2023-01-01" or (prices and prices[len(prices) // 2] < 1)
    if in_thousands:
        for entry in holdings.values():
            entry["value"] *= 1000
    return holdings


def money(value):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= size:
            return f"${value / size:,.1f}{unit}"
    return f"${value:,.0f}"


def quarter(period):
    year, month = period[:4], int(period[5:7])
    return f"{year} Q{(month - 1) // 3 + 1}"


def label(key, entry, holdings):
    name = entry["name"].title()
    # 같은 회사의 다른 주식 종류(예: Zillow A/C)가 있으면 종류를 붙임
    same = sum(1 for k, e in holdings.items() if e["name"] == entry["name"] and k[1] == key[1])
    if same > 1 and entry["class"]:
        name += f" {entry['class'].upper()}"
    if key[1]:
        name += f" ({key[1]})"
    return html.escape(name)


def shares_fmt(n):
    for unit, size, digits in (("M", 1e6, 2), ("K", 1e3, 1)):
        if n >= size:
            return f"{n / size:,.{digits}f}{unit}주"
    return f"{n:,.0f}주"


def detail_section(cur_hold, prev_hold, total, prev_total):
    """보유 종목 전체 + 편출 종목을 주식 수·평가액·직전 대비 변화와 함께 표시."""
    lines = ["", f"<b>📋 전체 보유 내역 ({len(cur_hold)})</b>"]
    for i, k in enumerate(sorted(cur_hold, key=lambda k: -cur_hold[k]["value"]), 1):
        e = cur_hold[k]
        price = e["value"] / e["shares"] if e["shares"] else 0
        if k not in prev_hold:
            change = f"🆕 신규 (약 {money(e['value'])} 매수 추정)"
        else:
            before = prev_hold[k]["shares"]
            diff = e["shares"] - before
            pct = diff / before if before else 0
            prev_w = prev_hold[k]["value"] / prev_total
            if abs(pct) < 0.005:
                change = "변동 없음"
            else:
                # 추정 매매 금액 = 주식 수 변화 × 분기말 주가 (실제 체결가와 다를 수 있음)
                verb = "매수" if diff > 0 else "매도"
                change = f"주식 수 {pct:+.0%} ({abs(diff):,.0f}주, 약 {money(abs(diff) * price)} {verb} 추정)"
            change += f" · 직전 비중 {prev_w:.1%}"
        lines.append(f" {i}. <b>{label(k, e, cur_hold)}</b> — 비중 {e['value'] / total:.1%}")
        lines.append(f"     {money(e['value'])} · {shares_fmt(e['shares'])}"
                     + (f" · 분기말 ${price:,.2f}" if price else "") + f" · {change}")
    exits = sorted((k for k in prev_hold if k not in cur_hold), key=lambda k: -prev_hold[k]["value"])
    for k in exits:
        e = prev_hold[k]
        lines.append(f" ❌ <b>{label(k, e, prev_hold)}</b> — 전량 매도 "
                     f"(직전 {money(e['value'])} · {shares_fmt(e['shares'])} · 비중 {e['value'] / prev_total:.1%})")
    lines.append("<i>※ 매매 금액은 주식 수 변화 × 분기말 주가로 추정한 값이며 실제 체결가와 다를 수 있습니다.</i>")
    return lines


def build_fund_message(fund, manager, cur, prev, cur_hold, prev_hold, cik=None):
    total = sum(e["value"] for e in cur_hold.values()) or 1
    prev_total = sum(e["value"] for e in prev_hold.values()) or 1
    weight = lambda k: cur_hold[k]["value"] / total
    prev_weight = lambda k: prev_hold[k]["value"] / prev_total

    new_all = sorted((k for k in cur_hold if k not in prev_hold), key=lambda k: -weight(k))
    new = [k for k in new_all if weight(k) >= SMALL_NEW]
    small_new = [k for k in new_all if weight(k) < SMALL_NEW]
    exits = sorted((k for k in prev_hold if k not in cur_hold), key=lambda k: -prev_weight(k))
    changed = []
    for k in cur_hold.keys() & prev_hold.keys():
        before, after = prev_hold[k]["shares"], cur_hold[k]["shares"]
        if (before and abs(after - before) / before >= CHANGE_MIN
                and max(weight(k), prev_weight(k)) >= CHANGE_MIN_WEIGHT):
            changed.append((k, (after - before) / before))
    increased = sorted((c for c in changed if c[1] > 0), key=lambda c: -(weight(c[0]) - prev_weight(c[0])))
    decreased = sorted((c for c in changed if c[1] < 0), key=lambda c: weight(c[0]) - prev_weight(c[0]))
    top = sorted(cur_hold, key=lambda k: -weight(k))

    def section(title, items, fmt):
        out = ["", f"<b>{title} ({len(items)})</b>"]
        out += [fmt(item) for item in items[:MAX_ITEMS]] or [" • 없음"]
        if len(items) > MAX_ITEMS:
            out.append(f" • …외 {len(items) - MAX_ITEMS}종목")
        return out

    def moved(c):
        k, pct = c
        return (f" • {label(k, cur_hold[k], cur_hold)} — {prev_weight(k):.1%} → {weight(k):.1%}"
                f" (주식 수 {pct:+.0%})")

    lines = [
        f"<b>🏦 13F — {html.escape(fund)}</b> <i>{html.escape(manager)}</i>",
        f"{quarter(cur['period'])} 보유 현황 (제출 {cur['filed']}) · 총 {money(total)} · {len(cur_hold)}종목",
        f"<i>비교: 직전 {quarter(prev['period'])}</i>",
    ]
    if cik in DETAIL_FUNDS:  # 상세 모드: 전체 보유 내역으로 대체
        change = (total - prev_total) / prev_total if prev_total else 0
        lines[1] = (f"{quarter(cur['period'])} 보유 현황 (제출 {cur['filed']}) · "
                    f"총 {money(prev_total)} → <b>{money(total)}</b> ({change:+.0%}) · "
                    f"{len(prev_hold)} → {len(cur_hold)}종목")
        lines += detail_section(cur_hold, prev_hold, total, prev_total)
        lines += ["", f"<i>ℹ️ {html.escape(DETAIL_FUNDS[cik])}</i>", "", "출처: SEC EDGAR 13F-HR"]
        return "\n".join(lines)
    lines += section("🆕 신규 편입", new,
                     lambda k: f" • {label(k, cur_hold[k], cur_hold)} — 비중 {weight(k):.1%}")
    if small_new:
        names = ", ".join(label(k, cur_hold[k], cur_hold) for k in small_new[:MAX_ITEMS])
        more = f" 외 {len(small_new) - MAX_ITEMS}" if len(small_new) > MAX_ITEMS else ""
        lines.append(f" <i>· 소규모 신규(비중 0.1% 미만) {len(small_new)}: {names}{more}</i>")
    lines += section("📈 추가 매수 (주식 수 +10%↑, 비중 1%↑)", increased, moved)
    lines += section("📉 일부 매도 (주식 수 -10%↓, 비중 1%↑)", decreased, moved)
    lines += section("❌ 편출 (전량 매도)", exits,
                     lambda k: f" • {label(k, prev_hold[k], prev_hold)} — 직전 비중 {prev_weight(k):.1%}")
    lines += ["", f"<b>📊 비중 상위 {MAX_ITEMS}</b>"]
    lines += [f" {i}. {label(k, cur_hold[k], cur_hold)} — {weight(k):.1%}" for i, k in enumerate(top[:MAX_ITEMS], 1)]
    lines += ["", "출처: SEC EDGAR 13F-HR"]
    return "\n".join(lines)


# ---------- 13D/13G 지분 공시 ----------

def stake_filings(sec, cik):
    data = sec.json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    r = data["filings"]["recent"]
    return [{"form": f, "accession": a, "filed": fd, "cik": cik}
            for f, a, fd in zip(r["form"], r["accessionNumber"], r["filingDate"]) if f in STAKE_FORMS]


def parse_stake(sec, filing):
    """새 XML 형식(2024.12~) 13D/13G의 핵심 수치. 옛 텍스트 형식이면 기본 정보만."""
    folder = f"https://www.sec.gov/Archives/edgar/data/{filing['cik']}/{filing['accession'].replace('-', '')}"
    info = {**filing, "url": folder + "/"}
    try:
        root = ET.fromstring(sec.get(folder + "/primary_doc.xml"))
    except Exception:
        return info
    fields = {}
    for el in root.iter():
        name = local(el.tag)
        if el.text and el.text.strip() and name not in fields:
            fields[name] = el.text.strip()
    num = lambda v: float(re.sub(r"[^\d.]", "", v or "") or 0)
    info.update({
        "issuer": fields.get("issuerName", ""),
        "issuer_cik": fields.get("issuerCik", "").lstrip("0"),
        "shares": num(fields.get("reportingPersonBeneficiallyOwnedAggregateNumberOfShares")
                      or fields.get("amountBeneficiallyOwned")),
        "percent": num(fields.get("classPercent")),
        "event_date": fields.get("eventDateRequiresFilingThisStatement") or fields.get("dateOfEvent", ""),
        "rule": fields.get("designateRulePursuantThisScheduleFiled", ""),
    })
    return info


def stake_ticker_price(sec, issuer_cik):
    try:
        tickers = sec.json(f"https://data.sec.gov/submissions/CIK{int(issuer_cik):010d}.json").get("tickers") or []
        if not tickers:
            return "", None
        from insider import yahoo_chart  # 순환 import 방지
        price, _ = yahoo_chart(tickers[0])
        return tickers[0], price
    except Exception as e:
        print(f"[경고] 지분 공시 종목 시세 조회 실패: {e}", file=sys.stderr)
        return "", None


def build_stake_message(person, info, prev, ticker, price):
    form = info["form"]
    kind = ("13D (경영 참여 가능성 있는 대량 보유)" if "13D" in form else
            "13G (수동적 투자 목적 대량 보유)")
    lines = [f"<b>📢 {html.escape(person)} 지분 공시 — {html.escape(form)}</b>", f"<i>{kind}</i>"]
    if info.get("issuer"):
        name = html.escape(info["issuer"]) + (f" (${html.escape(ticker)})" if ticker else "")
        lines += ["", f"<b>{name}</b>"]
        stake = f"보유 {info['shares']:,.0f}주 · 지분 <b>{info['percent']:.2f}%</b>"
        if prev:
            delta_pct = info["percent"] - prev["percent"]
            delta_sh = info["shares"] - prev["shares"]
            stake += (f"\n직전 공시({prev['filed']}) {prev['percent']:.2f}% · {prev['shares']:,.0f}주 → "
                      f"{delta_pct:+.2f}%p · {delta_sh:+,.0f}주")
        else:
            stake += "\n🆕 이 종목 첫 지분 공시"
        lines.append(stake)
        if price and info["shares"]:
            lines.append(f"평가액 약 {money(info['shares'] * price)} (현재가 ${price:,.2f} 기준)")
        if info["percent"] < 5:
            lines.append("⚠️ 지분 5% 미만 — 대량 보유 공시 의무 종료(지분 축소) 신호")
        lines.append(f"기준일 {info.get('event_date') or '-'} · 제출 {info['filed']}")
    else:
        lines += ["", f"제출 {info['filed']} (구 형식 공시 — 상세 수치는 원문 참고)"]
    lines.append(f'<a href="{info["url"]}">공시 원문</a>')
    if "13G" in form:
        lines.append("<i>ℹ️ 13G(수동적 기관투자자)는 분기 말 기준 45일 안에 제출되며, Dalal Street 외 "
                     "다른 운용 계좌까지 합산된 보유량입니다.</i>")
    return "\n".join(lines)


def check_stakes(sec, state, dry_run, force):
    """추적 인물의 새 13D/13G를 전송. 처음이면 기존 공시를 기준점으로만 기록."""
    seen = state.setdefault("stake_seen", [])
    history = state.setdefault("stake_history", {})  # issuer_cik → 마지막 지분 정보
    first_run = not seen
    sent = 0
    for person, ciks in STAKE_FILERS.items():
        filings = {}
        for cik in ciks:
            for f in stake_filings(sec, cik):
                filings.setdefault(f["accession"], f)  # 여러 CIK에 같은 공시 → 하나로
        ordered = sorted(filings.values(), key=lambda f: (f["filed"], f["accession"]))
        for filing in ordered:
            is_new = filing["accession"] not in seen
            target = (is_new and not first_run) or (force and filing is ordered[-1])
            # 새 XML 형식만 수치 파싱 (첫 실행 땐 직전 비교용 이력 확보, 이후엔 새 공시만)
            if target or (first_run and filing["form"].startswith("SCHEDULE")):
                info = parse_stake(sec, filing)
            else:
                info = filing
            key = info.get("issuer_cik")
            if target:
                prev = history.get(key) if key else None
                if prev and prev.get("accession") == filing["accession"]:  # 강제 재전송: 그 이전 공시와 비교
                    prev = prev.get("prev")
                ticker, price = stake_ticker_price(sec, key) if key else ("", None)
                deliver(build_stake_message(person, info, prev, ticker, price), dry_run)
                sent += 1
            if key and info.get("percent"):
                old = history.get(key)
                if not (old and old.get("accession") == info["accession"]):
                    record = {k: info[k] for k in ("accession", "filed", "percent", "shares", "issuer")}
                    record["prev"] = {k: v for k, v in old.items() if k != "prev"} if old else None
                    history[key] = record
            if is_new:
                seen.append(filing["accession"])
    if first_run and not force:
        print(f"[정보] 13D/13G 첫 실행: 기존 공시 {len(seen)}건을 기준점으로 기록", file=sys.stderr)
    return sent


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="전송·상태 저장 없이 출력만")
    parser.add_argument("--force", action="store_true", help="이미 알린 보고서여도 최신 변화를 보냄")
    parser.add_argument("--only", type=int, help="이 CIK의 펀드만 처리 (테스트용)")
    args = parser.parse_args()

    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        print("[경고] SEC_USER_AGENT 가 없어 13F 확인을 건너뜁니다. "
              "Repository secrets 에 '이름 이메일' 형식으로 등록하세요.", file=sys.stderr)
        return
    sec = SEC(user_agent)
    state = load_state()
    sent = 0

    for fund, manager, cik, search in FUNDS:
        if args.only and cik != args.only:
            continue
        try:
            cik = cik or sec.find_cik(search)
            if not cik:
                print(f"[경고] {fund}: EDGAR에서 CIK를 찾지 못했습니다", file=sys.stderr)
                continue
            sec_name, filings = sec.latest_13f(cik)
            print(f"[정보] {fund}: CIK {cik} ({sec_name}) 최신 13F "
                  f"{filings[0]['period'] if filings else '없음'}", file=sys.stderr)
            if len(filings) < 2:
                continue
            age = (datetime.now() - datetime.strptime(filings[0]["filed"], "%Y-%m-%d")).days
            if age > STALE_DAYS:
                print(f"[경고] {fund}: 최근 13F가 {filings[0]['filed']} 제출분이라 건너뜀 "
                      f"(더 이상 제출하지 않는 것으로 보임)", file=sys.stderr)
                continue
            cur, prev = filings
            key = str(cik)
            if state.get(key) == cur["accession"] and not args.force:
                continue
            if key not in state and not args.force:
                state[key] = cur["accession"]  # 처음 보는 펀드: 기준점만 기록하고 다음 보고서부터 알림
                print(f"[정보] {fund}: 기준점 기록 ({cur['period']})", file=sys.stderr)
                continue
            cur_hold = to_dollars(sec.holdings(cik, cur["accession"]), cur["period"])
            prev_hold = to_dollars(sec.holdings(cik, prev["accession"]), prev["period"])
            deliver(build_fund_message(fund, manager, cur, prev, cur_hold, prev_hold, cik), args.dry_run)
            state[key] = cur["accession"]
            sent += 1
        except Exception as e:  # 한 펀드가 실패해도 나머지는 계속
            print(f"[경고] {fund} 처리 실패: {e}", file=sys.stderr)

    if not args.only or args.only in {c for ciks in STAKE_FILERS.values() for c in ciks}:
        try:
            sent += check_stakes(sec, state, args.dry_run, args.force)
        except Exception as e:
            print(f"[경고] 13D/13G 확인 실패: {e}", file=sys.stderr)
    print(f"13F/13D/13G: 새 공시 {sent}건 전송")
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
