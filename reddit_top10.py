"""Reddit에서 가장 많이 언급된 미국 상장 종목 Top N을 텔레그램으로 전송한다.

데이터 출처: ApeWisdom (https://apewisdom.io) — r/wallstreetbets, r/stocks,
r/investing 등 주식 관련 서브레딧의 최근 24시간 티커 언급 수를 집계한다.

환경변수:
  TELEGRAM_BOT_TOKEN  BotFather에서 발급받은 봇 토큰
  TELEGRAM_CHAT_ID    메시지를 받을 채팅 ID (개인/그룹/채널)

사용법:
  python reddit_top10.py            # 텔레그램 전송
  python reddit_top10.py --dry-run  # 전송 없이 메시지만 출력
"""

import argparse
import html
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/{filter}/page/1"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
USER_AGENT = "Mozilla/5.0 (compatible; invest2-reddit-top10/1.0)"
KST = timezone(timedelta(hours=9))


def http_json(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_top(limit, source):
    results = http_json(APEWISDOM_URL.format(filter=source)).get("results", [])
    return results[:limit]


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_change(stock):
    rank, prev_rank = as_int(stock.get("rank")), as_int(stock.get("rank_24h_ago"))
    if prev_rank is None:
        return "🆕"
    diff = prev_rank - rank
    if diff > 0:
        return f"🔺{diff}"
    if diff < 0:
        return f"🔻{-diff}"
    return "➖"


def build_message(stocks):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    lines = [f"<b>📈 Reddit 언급 미국주식 Top {len(stocks)}</b>", f"<i>{now} KST · 최근 24시간</i>", ""]
    for stock in stocks:
        ticker = html.escape(str(stock.get("ticker", "?")))
        name = html.escape(html.unescape(str(stock.get("name", ""))))
        mentions = as_int(stock.get("mentions")) or 0
        prev_mentions = as_int(stock.get("mentions_24h_ago"))
        upvotes = as_int(stock.get("upvotes")) or 0
        delta = f" ({mentions - prev_mentions:+,})" if prev_mentions is not None else ""
        lines.append(f"<b>{stock.get('rank')}. ${ticker}</b> {name} {format_change(stock)}")
        lines.append(f"    💬 {mentions:,}회{delta} · 👍 {upvotes:,}")
    lines += ["", "출처: <a href=\"https://apewisdom.io\">ApeWisdom</a>"]
    return "\n".join(lines)


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10, help="표시할 종목 수 (기본 10)")
    parser.add_argument("--source", default="all-stocks",
                        help="ApeWisdom 필터 (all-stocks, wallstreetbets, stocks, investing 등)")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 전송 없이 출력만")
    args = parser.parse_args()

    stocks = fetch_top(args.limit, args.source)
    if not stocks:
        sys.exit("ApeWisdom에서 데이터를 가져오지 못했습니다.")
    message = build_message(stocks)

    if args.dry_run:
        print(message)
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경변수를 설정하세요.")
    send_telegram(token, chat_id, message)
    print(f"Top {len(stocks)} 전송 완료")


if __name__ == "__main__":
    main()
