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
    # 2026 Q2부터 운용사(1336528) 대신 상장 지주사 Pershing Square Inc.가 13F-HR 제출
    ("Pershing Square", "빌 애크먼", 2026053, None),
    ("Duquesne Family Office", "스탠리 드러켄밀러", 1536411, None),
    ("Baupost Group", "세스 클라만", 1061768, None),
    ("Himalaya Capital", "리루", 1709323, None),
    ("Dalal Street (Pabrai)", "모니시 파브라이", 1549575, None),
    ("Aquamarine Capital", "가이 스파이어", 1404599, None),  # 2022 Q2 이후 13F 미제출 — 재개 시 자동 알림
    ("Tiger Global", "체이스 콜먼", 1167483, None),
    ("Coatue Management", "필립 라퐁", 1135730, None),
    ("Thiel Macro", "피터 틸", 1562087, None),
]
MAX_ITEMS = 10  # 신규/매도 각각 최대 표시 개수
STALE_DAYS = 270  # 최신 13F가 이보다 오래되면 더 이상 제출하지 않는 것으로 보고 건너뜀
STATE_PATH = os.path.join("data", "13f", "state.json")


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


def build_fund_message(fund, manager, cur, prev, cur_hold, prev_hold):
    total = sum(e["value"] for e in cur_hold.values()) or 1
    prev_total = sum(e["value"] for e in prev_hold.values()) or 1
    new = sorted((k for k in cur_hold if k not in prev_hold), key=lambda k: -cur_hold[k]["value"])
    exits = sorted((k for k in prev_hold if k not in cur_hold), key=lambda k: -prev_hold[k]["value"])
    lines = [
        f"<b>🏦 13F 포트폴리오 변화 — {html.escape(fund)}</b>",
        f"<i>{html.escape(manager)}</i>",
        f"{quarter(cur['period'])} ({cur['period']} 기준) · 제출 {cur['filed']}",
        f"총 {money(total)} · {len(cur_hold)}종목 (직전 {quarter(prev['period'])}: {money(prev_total)} · {len(prev_hold)}종목)",
        "",
        f"<b>🆕 신규 편입 ({len(new)})</b>",
    ]
    lines += [f" • {label(k, cur_hold[k], cur_hold)} — {money(cur_hold[k]['value'])} (비중 {cur_hold[k]['value'] / total:.1%})"
              for k in new[:MAX_ITEMS]] or [" • 없음"]
    if len(new) > MAX_ITEMS:
        lines.append(f" • …외 {len(new) - MAX_ITEMS}종목")
    lines += ["", f"<b>❌ 전량 매도 ({len(exits)})</b>"]
    lines += [f" • {label(k, prev_hold[k], prev_hold)} — 직전 {money(prev_hold[k]['value'])} (비중 {prev_hold[k]['value'] / prev_total:.1%})"
              for k in exits[:MAX_ITEMS]] or [" • 없음"]
    if len(exits) > MAX_ITEMS:
        lines.append(f" • …외 {len(exits) - MAX_ITEMS}종목")
    lines += ["", "출처: SEC EDGAR 13F-HR"]
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="전송·상태 저장 없이 출력만")
    parser.add_argument("--force", action="store_true", help="이미 알린 보고서여도 최신 변화를 보냄")
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
            deliver(build_fund_message(fund, manager, cur, prev, cur_hold, prev_hold), args.dry_run)
            state[key] = cur["accession"]
            sent += 1
        except Exception as e:  # 한 펀드가 실패해도 나머지는 계속
            print(f"[경고] {fund} 처리 실패: {e}", file=sys.stderr)

    print(f"13F: 새 보고서 {sent}건 전송")
    if not args.dry_run:
        save_state(state)


if __name__ == "__main__":
    main()
