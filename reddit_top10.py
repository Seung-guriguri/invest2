"""서브레딧별로 가장 많이 언급된 미국 상장 종목 순위를 텔레그램으로 전송한다.

기본: r/wallstreetbets Top 5 + r/ValueInvesting(가치투자) Top 5

데이터 출처:
  1) ApeWisdom (https://apewisdom.io) — 서브레딧별 최근 24시간 티커 언급 수
  2) ApeWisdom이 지원하지 않는 서브레딧은 Reddit RSS(또는 OAuth API)로 최근
     24시간 글/댓글을 직접 가져와 미국 상장 티커(NASDAQ Trader 종목 목록 기준)
     언급을 센다. RSS에는 추천 수가 없어 언급 횟수만 표시된다.

환경변수:
  TELEGRAM_BOT_TOKEN    BotFather에서 발급받은 봇 토큰
  TELEGRAM_CHAT_ID      메시지를 받을 채팅 ID (개인/그룹/채널)
  REDDIT_CLIENT_ID      (선택) Reddit 앱 client id — 설정하면 RSS 대신 API 사용(추천 수 포함)
  REDDIT_CLIENT_SECRET  (선택) Reddit 앱 secret

사용법:
  python reddit_top10.py            # 텔레그램 전송
  python reddit_top10.py --dry-run  # 전송 없이 메시지만 출력
  python reddit_top10.py --sources wallstreetbets:5,stocks:3
  python reddit_top10.py --mode weekly --dry-run   # 지난 7일 주간 종합 순위

매일 실행 시 data/daily/YYYY-MM-DD.json 에 순위 스냅샷을 저장하고,
월요일(KST)에는 지난 7일 스냅샷을 합산한 주간 종합 순위를 보낸다.
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone

APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/1"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
USER_AGENT = "Mozilla/5.0 (compatible; invest2-reddit-top10/1.0)"
KST = timezone(timedelta(hours=9))
DEFAULT_SOURCES = "wallstreetbets:5,ValueInvesting:5"
SNAPSHOT_SIZE = 100  # 주간 집계를 위해 일간 스냅샷에 저장하는 종목 수
SUBREDDIT_DESCRIPTIONS = {
    "wallstreetbets": "밈주식·옵션·단타 위주의 공격적인 개인투자자 커뮤니티",
    "valueinvesting": "버핏식 가치투자 — 실적·밸류에이션 기반 저평가 종목 토론",
    "stocks": "개별 종목·시장 뉴스를 다루는 일반 주식 토론 커뮤니티",
    "investing": "장기투자·자산배분 중심의 일반 투자 커뮤니티",
    "options": "옵션 전략·거래 토론 커뮤니티",
    "pennystocks": "소형·저가주 위주의 고위험 투자 커뮤니티",
}

SYMBOL_LISTS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]
# 티커와 철자가 같지만 대부분 일반 단어/약어로 쓰이는 것들 ($를 붙이면 집계됨)
STOPWORDS = set("""
A I AM AN AND ANY ARE AS AT BE BIG BY CAN CEO CFO COO CTO DD DO EOD EPS ETF EV
FOR FUD GDP GO HAS HE HOLD IF IN IPO IS IT ITS LOL ME MY NEW NO NOT NOW OF OK
ON ONE OR OUT PE PM RH SEC SO THE TO TV UK UP US USA USD WE YOLO YOU AI ALL
ATH BUY CASH DCF EBIT ROE ROI ROIC FCF FY GAAP IMO IRA LONG LOVE MOAT NYSE
NASDAQ OP OTC PEG REIT RSI SP TLDR TL DR VS WSB ARE BEST CAN CARE EDIT EVER FAST
FUN GOOD HOPE JUST KEY LIFE LOW MAN MOM NEXT NICE OPEN PLAY REAL RUN SAFE SEE
TOP TRUE TWO VERY WELL WIN WORK YEAR ELSE FREE GAIN HUGE INFO LINK MORE MOST
ONCE PAY PER POST RISK SAVE SELL STAY TECH TELL TIME WAY WHO WHY CAT DAY OPEN
""".split())
TICKER_RE = re.compile(r"(?<![A-Za-z0-9$])(\$?)([A-Z]{1,5}(?:\.[AB])?)(?![A-Za-z0-9])")


def http_json(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_apewisdom(source):
    return http_json(APEWISDOM_URL.format(filter=source)).get("results", [])


def load_us_symbols():
    """NASDAQ/NYSE/AMEX 상장 종목 {티커: 회사명}."""
    symbols = {}
    for url in SYMBOL_LISTS:
        rows = http_text(url).splitlines()
        header = rows[0].split("|")
        sym_col = header.index("Symbol") if "Symbol" in header else header.index("ACT Symbol")
        name_col = header.index("Security Name")
        test_col = header.index("Test Issue")
        for row in rows[1:]:
            cols = row.split("|")
            if len(cols) <= max(sym_col, name_col, test_col) or cols[test_col] == "Y":
                continue
            ticker = cols[sym_col]
            if "$" in ticker:  # 우선주 등
                continue
            name = re.split(r" - | Common Stock| Class [A-Z]| Ordinary Shares", cols[name_col])[0]
            symbols.setdefault(ticker, name.strip().rstrip(","))
    return symbols


ATOM = "{http://www.w3.org/2005/Atom}"


class RedditOAuth:
    """Reddit 공식 API (REDDIT_CLIENT_ID/SECRET 필요). 추천 수까지 제공."""

    def __init__(self, client_id, secret):
        self.headers = {"User-Agent": "python:invest2-reddit-top10:v1.0 (by /u/invest2bot)"}
        auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/access_token",
            data=b"grant_type=client_credentials",
            headers={**self.headers, "Authorization": f"Basic {auth}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            token = json.load(resp)["access_token"]
        self.headers["Authorization"] = f"Bearer {token}"

    def listing(self, path, since, max_pages):
        """since(unix time) 이후 항목을 최신순으로 (text, score) 형태로 가져온다."""
        after = None
        for _ in range(max_pages):
            query = {"limit": 100, "raw_json": 1, **({"after": after} if after else {})}
            url = f"https://oauth.reddit.com{path}?{urllib.parse.urlencode(query)}"
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)["data"]
            for child in data["children"]:
                item = child["data"]
                if item["created_utc"] < since:
                    return
                text = " ".join(filter(None, [item.get("title"), item.get("selftext"), item.get("body")]))
                yield text, max(item.get("score", 0), 0)
            after = data.get("after")
            if not after:
                return
            time.sleep(1)


class RedditRSS:
    """Reddit RSS 피드 (인증 불필요, GitHub Actions에서도 접근 가능). 추천 수 없음."""

    headers = {"User-Agent": "python:invest2-reddit-top10:v1.0 (by /u/invest2bot)"}

    def fetch(self, url, retries=4):
        """429(요청 과다) 시 Retry-After 또는 지수 백오프로 재시도."""
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == retries:
                    raise
                wait = as_int(e.headers.get("Retry-After")) or 10 * 2 ** attempt
                print(f"[정보] Reddit 429 → {wait}초 대기 후 재시도", file=sys.stderr)
                time.sleep(min(wait, 90))

    def listing(self, path, since, max_pages):
        after = None
        for page in range(max_pages):
            if page:
                time.sleep(5)
            query = {"limit": 100, **({"after": after} if after else {})}
            url = f"https://www.reddit.com{path}/.rss?{urllib.parse.urlencode(query)}"
            try:
                entries = ET.fromstring(self.fetch(url)).findall(f"{ATOM}entry")
            except Exception as e:
                if page == 0:
                    raise
                print(f"[경고] {path} {page + 1}페이지 실패, 받은 데이터만 사용: {e}", file=sys.stderr)
                return
            for entry in entries:
                ts = entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated")
                if datetime.fromisoformat(ts).timestamp() < since:
                    return
                entry_id = entry.findtext(f"{ATOM}id", "")
                body = html.unescape(re.sub(r"<[^>]+>", " ", entry.findtext(f"{ATOM}content", "")))
                # 댓글(t1_) 제목은 "/u/작성자 on 글제목" 이라 제외
                title = "" if entry_id.startswith("t1_") else entry.findtext(f"{ATOM}title", "")
                yield f"{title} {body}", None
            if len(entries) < 25:
                return
            after = entries[-1].findtext(f"{ATOM}id")


def fetch_reddit_direct(source):
    symbols = load_us_symbols()
    client_id, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    client = RedditOAuth(client_id, secret) if client_id and secret else RedditRSS()
    since = time.time() - 24 * 3600
    mentions, upvotes = defaultdict(int), defaultdict(int)
    rss = isinstance(client, RedditRSS)  # RSS는 요청 제한이 빡빡해 페이지 수를 줄인다
    items = []
    for path, pages in ((f"/r/{source}/new", 2 if rss else 5), (f"/r/{source}/comments", 3 if rss else 10)):
        try:
            items += list(client.listing(path, since, max_pages=pages))
        except Exception as e:
            print(f"[경고] {path} 수집 실패: {e}", file=sys.stderr)
        if rss:
            time.sleep(5)
    if not items:
        raise RuntimeError(f"r/{source} 글/댓글을 하나도 가져오지 못했습니다.")
    for text, score in items:
        found = set()
        for dollar, ticker in TICKER_RE.findall(text):
            if ticker not in symbols:
                continue
            if not dollar and (len(ticker) < 2 or ticker in STOPWORDS):
                continue
            found.add(ticker)
        for ticker in found:
            mentions[ticker] += 1
            upvotes[ticker] += score or 0
    has_score = isinstance(client, RedditOAuth)
    print(f"[정보] r/{source} 직접 수집({type(client).__name__}): 글/댓글 {len(items)}개", file=sys.stderr)
    ranked = sorted(mentions, key=lambda t: (-mentions[t], -upvotes[t]))
    return [
        {"rank": i, "ticker": t, "name": symbols[t], "mentions": mentions[t],
         "upvotes": upvotes[t] if has_score else None}
        for i, t in enumerate(ranked, 1)
    ]


def fetch_top(source):
    """해당 서브레딧의 언급 순위 전체 목록 (ApeWisdom 우선, 없으면 Reddit 직접 수집)."""
    try:
        stocks = fetch_apewisdom(source)
    except Exception as e:
        print(f"[경고] ApeWisdom r/{source} 조회 실패: {e}", file=sys.stderr)
        stocks = []
    if stocks:
        return stocks
    print(f"[정보] ApeWisdom에 r/{source} 데이터 없음 → Reddit 직접 수집", file=sys.stderr)
    return fetch_reddit_direct(source)


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_change(stock):
    if "rank_24h_ago" not in stock:  # Reddit 직접 수집 — 전일 비교 데이터 없음
        return ""
    rank, prev_rank = as_int(stock.get("rank")), as_int(stock.get("rank_24h_ago"))
    if prev_rank is None:
        return "🆕"
    diff = prev_rank - rank
    if diff > 0:
        return f"🔺{diff}"
    if diff < 0:
        return f"🔻{-diff}"
    return "➖"


def format_stock(stock, position):
    ticker = html.escape(str(stock.get("ticker", "?")))
    name = html.escape(html.unescape(str(stock.get("name", ""))))
    mentions = as_int(stock.get("mentions")) or 0
    prev_mentions = as_int(stock.get("mentions_24h_ago"))
    upvotes = as_int(stock.get("upvotes"))
    delta = f" ({mentions - prev_mentions:+,})" if prev_mentions is not None else ""
    votes = f" · 👍 {upvotes:,}" if upvotes is not None else ""
    return [
        f"<b>{position}. ${ticker}</b> {name} {format_change(stock)}".rstrip(),
        f"    💬 {mentions:,}회{delta}{votes}",
    ]


def build_message(sections):
    """sections: [(source, limit, stocks 또는 None(실패))] 목록."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    lines = ["<b>📈 Reddit 언급 미국주식 순위</b>", f"<i>{now} KST · 최근 24시간</i>"]
    for source, limit, stocks in sections:
        lines += ["", f"<b>🔥 r/{html.escape(source)} Top {limit}</b>"]
        if source.lower() in SUBREDDIT_DESCRIPTIONS:
            lines.append(f"<i>{SUBREDDIT_DESCRIPTIONS[source.lower()]}</i>")
        if not stocks:
            lines.append("    데이터를 가져오지 못했습니다.")
            continue
        for position, stock in enumerate(stocks, 1):
            lines += format_stock(stock, position)
    lines += ["", "출처: <a href=\"https://apewisdom.io\">ApeWisdom</a>, Reddit"]
    return "\n".join(lines)


def save_snapshot(data_dir, sections):
    """일간 순위를 data/daily/YYYY-MM-DD.json (KST 날짜) 으로 저장 — 주간 집계용."""
    day = datetime.now(KST).strftime("%Y-%m-%d")
    path = os.path.join(data_dir, "daily", f"{day}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keep = ("ticker", "name", "mentions", "upvotes")
    snapshot = {
        "date": day,
        "sources": {
            source: [{k: stock.get(k) for k in keep} for stock in stocks[:SNAPSHOT_SIZE]]
            for source, _, stocks in sections if stocks
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
    print(f"[정보] 스냅샷 저장: {path}", file=sys.stderr)


def load_week(data_dir, today):
    """today(KST 날짜) 직전 7일간의 일간 스냅샷 목록."""
    snapshots = []
    for back in range(7, 0, -1):
        day = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        path = os.path.join(data_dir, "daily", f"{day}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                snapshots.append(json.load(f))
    return snapshots


def aggregate_week(snapshots, source):
    totals = {}
    for snap in snapshots:
        for stock in snap["sources"].get(source, []):
            entry = totals.setdefault(stock["ticker"], {
                "ticker": stock["ticker"], "name": stock.get("name", ""),
                "mentions": 0, "upvotes": None, "days": 0,
            })
            entry["mentions"] += as_int(stock.get("mentions")) or 0
            if as_int(stock.get("upvotes")) is not None:
                entry["upvotes"] = (entry["upvotes"] or 0) + as_int(stock["upvotes"])
            entry["days"] += 1
    return sorted(totals.values(), key=lambda e: (-e["mentions"], -(e["upvotes"] or 0)))


def build_weekly_message(sources, snapshots):
    days = [snap["date"] for snap in snapshots]
    period = f"{days[0][5:].replace('-', '/')} ~ {days[-1][5:].replace('-', '/')}"
    lines = ["<b>📅 Reddit 주간 종합 순위</b>", f"<i>{period} KST 수집분 · {len(days)}일 합산</i>"]
    for source, limit in sources:
        lines += ["", f"<b>🏆 r/{html.escape(source)} 주간 Top {limit}</b>"]
        if source.lower() in SUBREDDIT_DESCRIPTIONS:
            lines.append(f"<i>{SUBREDDIT_DESCRIPTIONS[source.lower()]}</i>")
        ranked = aggregate_week(snapshots, source)[:limit]
        if not ranked:
            lines.append("    집계할 데이터가 없습니다.")
            continue
        for position, e in enumerate(ranked, 1):
            ticker = html.escape(e["ticker"])
            name = html.escape(html.unescape(e["name"] or ""))
            votes = f" · 👍 {e['upvotes']:,}" if e["upvotes"] is not None else ""
            lines.append(f"<b>{position}. ${ticker}</b> {name}")
            lines.append(f"    💬 {e['mentions']:,}회{votes} · 📆 {e['days']}/{len(days)}일 언급")
    lines += ["", "출처: <a href=\"https://apewisdom.io\">ApeWisdom</a>, Reddit"]
    return "\n".join(lines)


def parse_sources(spec):
    sources = []
    for item in spec.split(","):
        name, _, limit = item.strip().partition(":")
        sources.append((name, int(limit) if limit else 5))
    return sources


def send_telegram(token, chat_id, text):
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    result = http_json(TELEGRAM_URL.format(token=urllib.parse.quote(token, safe=":")), data=payload)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def deliver(message, dry_run):
    if dry_run:
        print(message)
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(message)
        sys.exit("TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 가 비어 있습니다. "
                 "GitHub 저장소 Settings → Secrets and variables → Actions → "
                 "Repository secrets 에 등록했는지 확인하세요.")
    send_telegram(token, chat_id, message)
    print("전송 완료")


def run_daily(sources, data_dir, dry_run):
    sections = []
    for source, limit in sources:
        try:
            stocks = fetch_top(source)
        except Exception as e:  # 한 소스가 실패해도 나머지는 전송
            print(f"[경고] r/{source} 조회 실패: {e}", file=sys.stderr)
            stocks = None
        if not stocks:
            print(f"[경고] r/{source} 결과 없음", file=sys.stderr)
        sections.append((source, limit, stocks))
    if not any(stocks for _, _, stocks in sections):
        sys.exit("어떤 소스에서도 데이터를 가져오지 못했습니다.")
    if not dry_run:
        save_snapshot(data_dir, sections)
    message = build_message([(src, limit, stocks and stocks[:limit]) for src, limit, stocks in sections])
    deliver(message, dry_run)


def run_weekly(sources, data_dir, dry_run):
    snapshots = load_week(data_dir, datetime.now(KST).date())
    if not snapshots:
        sys.exit(f"{data_dir}/daily 에 지난 7일간 일간 스냅샷이 없어 주간 순위를 만들 수 없습니다.")
    deliver(build_weekly_message(sources, snapshots), dry_run)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", default=DEFAULT_SOURCES,
                        help=f"서브레딧:개수 목록, 쉼표 구분 (기본 {DEFAULT_SOURCES})")
    parser.add_argument("--mode", choices=["auto", "daily", "weekly"], default="auto",
                        help="auto: 월요일(KST)이면 weekly, 아니면 daily")
    parser.add_argument("--data-dir", default="data", help="일간 스냅샷 저장 폴더 (기본 data)")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 전송·스냅샷 저장 없이 출력만")
    args = parser.parse_args()

    mode = args.mode
    if mode == "auto":
        mode = "weekly" if datetime.now(KST).weekday() == 0 else "daily"
    sources = parse_sources(args.sources)
    if mode == "weekly":
        run_weekly(sources, args.data_dir, args.dry_run)
    else:
        run_daily(sources, args.data_dir, args.dry_run)


if __name__ == "__main__":
    main()
