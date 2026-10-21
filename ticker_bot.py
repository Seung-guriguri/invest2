"""텔레그램 1:1 대화방에서 티커를 보내면 Reddit·StockTwits 동향을 답장한다.

상시 서버 없이, cron-job.org가 1분마다 GitHub Actions를 실행하고
이 스크립트가 그동안 쌓인 메시지(getUpdates)를 한 번에 처리한 뒤 종료한다.
처리한 메시지는 Telegram에 확인(offset) 처리하므로 저장소에 상태를 남기지 않는다.

입력 예: TSLA / $nvda / AAPL MSFT (한 번에 최대 3개)

환경변수: TELEGRAM_BOT_TOKEN, BOT_ALLOWED_USERS(쉼표 구분 텔레그램 사용자 ID; 비우면 모두 허용)

사용법:
  python ticker_bot.py                 # 대기 중인 메시지 처리
  python ticker_bot.py --test TSLA     # 텔레그램 없이 TSLA 답장 내용만 출력
"""

import argparse
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
from datetime import datetime, timezone

from reddit_top10 import (APEWISDOM_URL, BROWSER_UA, as_int, first_sentence, http_json,
                          translate_ko)

API = "https://api.telegram.org/bot{token}/{method}"
STOCKTWITS_STREAM = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
STOCKTWITS_TRENDING = "https://api.stocktwits.com/api/2/trending/symbols/equities.json"
REDDIT_SEARCH = ("https://www.reddit.com/r/wallstreetbets+stocks+investing+StockMarket/search/.rss"
                 "?q={q}&restrict_sr=on&sort=new&t=week&limit=25")
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_TICKERS = 3
TICKER_RE = re.compile(r"\$?([A-Za-z]{1,5}(?:\.[A-Za-z])?)\b")
HELP = ("🔎 티커를 보내면 Reddit·StockTwits 동향을 알려드려요.\n"
        "예: <code>TSLA</code>, <code>$nvda</code>, <code>AAPL MSFT</code> (최대 3개)\n\n"
        "📅 실적: <code>TSLA 실적</code> → 다음 발표일·최근 실적 자료 링크\n"
        "⭐ 관심 종목(실적 알림): <code>/add TSLA</code> · <code>/del TSLA</code> · <code>/list</code>\n"
        "응답까지 1~2분 걸릴 수 있어요.")


# ---------- 텔레그램 ----------

def tg(token, method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} 실패: {result}")
    return result["result"]


def get_json(url, ua=BROWSER_UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


# ---------- StockTwits ----------

def stocktwits(symbol):
    """(회사명, 최근 메시지 감성 집계, 인기 메시지, 트렌딩 요약)"""
    data = get_json(STOCKTWITS_STREAM.format(sym=urllib.parse.quote(symbol)))
    title = (data.get("symbol") or {}).get("title", "")
    messages = data.get("messages") or []
    bull = sum(1 for m in messages if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bullish")
    bear = sum(1 for m in messages if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bearish")
    popular = sorted(messages, key=lambda m: -((m.get("likes") or {}).get("total") or 0))
    picks = []
    for m in popular:
        body = " ".join(re.sub(r"https?://\S+", "", m.get("body", "")).split())
        if len(re.sub(r"\$[A-Za-z.]+", "", body).strip()) >= 15:  # 티커만 있는 짧은 글 제외
            if len(picks) == 3:
                break
            sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            picks.append((sentiment, (m.get("likes") or {}).get("total") or 0, body[:140]))
    summary = ""
    try:
        for s in get_json(STOCKTWITS_TRENDING).get("symbols", []):
            if s.get("symbol", "").upper() == symbol:
                summary = first_sentence((s.get("trends") or {}).get("summary", ""))
                break
    except Exception as e:
        print(f"[경고] StockTwits 트렌딩 조회 실패: {e}", file=sys.stderr)
    return title, (bull, bear, len(messages)), picks, summary


# ---------- Reddit ----------

def apewisdom_rank(symbol, source):
    """ApeWisdom 상위 300위 안의 순위 정보 (없으면 None)."""
    for page in range(1, 4):
        data = http_json(APEWISDOM_URL.format(filter=source, page=page))
        for row in data.get("results", []):
            if row.get("ticker", "").upper() == symbol:
                return row
        if page >= (as_int(data.get("pages")) or 0):
            break
    return None


def reddit_posts(symbol, limit=3):
    """주요 주식 서브레딧에서 최근 1주일 안에 이 티커를 제목에 언급한 글."""
    query = urllib.parse.quote(f'"{symbol}" OR "${symbol}"')
    req = urllib.request.Request(REDDIT_SEARCH.format(q=query),
                                 headers={"User-Agent": "python:invest2-ticker-bot:v1.0 (by /u/invest2bot)"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                root = ET.fromstring(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(8 * 2 ** attempt)  # Reddit 요청 제한: 8·16·32초 대기 후 재시도
                continue
            raise
    pattern = re.compile(rf"(?<![A-Za-z]){re.escape(symbol)}(?![A-Za-z])")
    posts = []
    for entry in root.findall(f"{ATOM}entry"):
        title = entry.findtext(f"{ATOM}title", "")
        if not pattern.search(title):
            continue
        sub = (entry.find(f"{ATOM}category").get("label", "") if entry.find(f"{ATOM}category") is not None else "")
        link = entry.find(f"{ATOM}link").get("href", "") if entry.find(f"{ATOM}link") is not None else ""
        posts.append({"title": title, "sub": sub, "link": link,
                      "date": (entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated") or "")[:10]})
        if len(posts) == limit:
            break
    return posts


# ---------- 답장 ----------

def build_reply(symbol):
    lines = []
    title = ""
    try:
        title, (bull, bear, total), picks, summary = stocktwits(symbol)
        lines += ["", "<b>📊 StockTwits</b>"]
        if total:
            lines.append(f"  최근 메시지 {total}개 중 🟢 강세 {bull} · 🔴 약세 {bear} · 표시 없음 {total - bull - bear}")
        else:
            lines.append("  최근 메시지 없음")
        if summary:
            lines.append(f"  💡 화제 이유: {html.escape(translate_ko(summary))}")
        for sentiment, likes, body in picks:
            mark = {"Bullish": "🟢", "Bearish": "🔴"}.get(sentiment, "⚪")
            lines.append(f"  {mark} {html.escape(translate_ko(body))} <i>(👍{likes})</i>")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"❓ <b>${html.escape(symbol)}</b> — StockTwits에서 찾을 수 없는 티커예요. 철자를 확인해 주세요."
        lines += ["", f"<b>📊 StockTwits</b>\n  조회 실패: {html.escape(str(e))}"]
    except Exception as e:
        lines += ["", f"<b>📊 StockTwits</b>\n  조회 실패: {html.escape(str(e))}"]

    lines += ["", "<b>🔥 Reddit</b>"]
    for source, label in (("wallstreetbets", "r/wallstreetbets"), ("all-stocks", "주식 서브레딧 전체")):
        try:
            row = apewisdom_rank(symbol, source)
        except Exception as e:
            lines.append(f"  {label}: 조회 실패 ({html.escape(str(e))})")
            continue
        if row:
            now, prev = as_int(row.get("mentions")) or 0, as_int(row.get("mentions_24h_ago"))
            delta = f" (전일 {prev:,}회)" if prev is not None else ""
            lines.append(f"  {label}: <b>{row.get('rank')}위</b> · 24시간 언급 {now:,}회{delta}")
        else:
            lines.append(f"  {label}: 상위 300위 밖 (언급 적음)")
    try:
        posts = reddit_posts(symbol)
        if posts:
            lines.append("  최근 1주일 글:")
            for p in posts:
                lines.append(f'  • <a href="{html.escape(p["link"])}">{html.escape(translate_ko(p["title"]))}</a>'
                             f" <i>({html.escape(p['sub'])} {p['date'][5:]})</i>")
        else:
            lines.append("  최근 1주일 제목 언급 글 없음")
    except Exception as e:
        lines.append(f"  최근 글 조회 실패: {html.escape(str(e))}")

    head = f"<b>🔎 ${html.escape(symbol)}</b>" + (f" {html.escape(title)}" if title else "") + " 커뮤니티 동향"
    now = datetime.now(timezone.utc).strftime("%m/%d %H:%M UTC")
    return "\n".join([head, f"<i>{now} 기준</i>"] + lines)


# ---------- 관심 종목 / 실적 ----------

def watchlist_command(text):
    """/add, /del, /list 처리 → 답장 문자열 (data/watchlist.json 수정, 커밋은 워크플로가 담당)."""
    import earnings
    cmd, _, rest = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    data = earnings.load(earnings.WATCHLIST_PATH, {"tickers": {}})
    tickers = parse_tickers(rest) if rest else []
    if cmd == "/add" and tickers:
        for t in tickers:
            data["tickers"].setdefault(t, {"added": datetime.now(timezone.utc).date().isoformat()})
        earnings.save(earnings.WATCHLIST_PATH, data)
        return f"⭐ 추가: {', '.join(tickers)}\n다음 일간 실행(12:40)부터 실적 일정을 챙겨요."
    if cmd == "/del" and tickers:
        removed = [t for t in tickers if data["tickers"].pop(t, None) is not None]
        earnings.save(earnings.WATCHLIST_PATH, data)
        return f"🗑 삭제: {', '.join(removed) or '없음'}"
    if cmd == "/list":
        state = earnings.load(earnings.STATE_PATH, {})
        upcoming = state.get("upcoming", {})
        lines = ["<b>⭐ 직접 등록 관심 종목</b>"]
        for t in sorted(data["tickers"]):
            nxt = upcoming.get(t)
            when = f" — 다음 실적 {nxt['date'][5:]}" if nxt else ""
            lines.append(f"• {t}{when}")
        if len(lines) == 1:
            lines.append("• 없음 (<code>/add TSLA</code>로 추가)")
        auto = sorted((t, i) for t, i in upcoming.items() if t not in data["tickers"])
        if auto:
            lines += ["", "<b>자동 포함 (8일 내 실적 발표)</b>"]
            lines += [f"• {t} {i.get('src', '')} — {i['date'][5:]}" for t, i in auto]
        return "\n".join(lines)
    return HELP


def earnings_reply(symbol):
    import earnings
    from sec13f import SEC
    today = datetime.now(earnings.ET_TZ).date()
    state = earnings.load(earnings.STATE_PATH, {})
    info = state.get("upcoming", {}).get(symbol)
    if not info:
        info = earnings.scan_calendar({symbol}, today, 30).get(symbol)
    lines = [f"<b>📅 ${html.escape(symbol)} 실적</b>"]
    if info:
        lines.append("다음 발표: " + earnings.timing_label(earnings.date.fromisoformat(info["date"]), info.get("time")))
        if info.get("eps_forecast"):
            lines.append(f"컨센서스 EPS {info['eps_forecast']} (애널리스트 {info.get('estimates') or '-'}명)"
                         + (f" · 전년 동기 {info['last_year_eps']}" if info.get("last_year_eps") else ""))
    else:
        lines.append("30일 안에 예정된 실적 발표가 없어요 (Nasdaq 캘린더 기준).")
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if ua:
        try:
            since = (today - earnings.timedelta(days=120)).isoformat()
            mat = earnings.latest_earnings_8k(SEC(ua), symbol, since)
            if mat:
                lines += ["", f"<b>📄 최근 실적 자료</b> <i>(8-K {mat['filed']})</i>"]
                for doc_type, desc, url in mat["docs"]:
                    label = {"EX-99.1": "보도자료", "EX-99.2": "추가 자료"}.get(doc_type, doc_type)
                    lines.append(f'• <a href="{html.escape(url)}">{label}</a> <i>{html.escape(desc[:50])}</i>')
                if mat.get("webcast"):
                    lines.append(f'• 🎧 <a href="{html.escape(mat["webcast"])}">컨퍼런스콜 웹캐스트</a>')
            else:
                lines.append("최근 4개월 안에 SEC 실적 공시(8-K 2.02)가 없어요 (해외 기업은 6-K로 내기도 해요).")
        except Exception as e:
            lines.append(f"실적 자료 조회 실패: {html.escape(str(e))}")
    return "\n".join(lines)


def parse_tickers(text):
    tickers = []
    for match in TICKER_RE.finditer(text or ""):
        sym = match.group(1).upper()
        if sym not in tickers:
            tickers.append(sym)
    return tickers[:MAX_TICKERS]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", help="이 티커의 답장 내용만 출력 (텔레그램 사용 안 함)")
    args = parser.parse_args()
    if args.test:
        want = "실적" in args.test
        for sym in parse_tickers(args.test):
            print(earnings_reply(sym) if want else build_reply(sym), "\n")
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN 이 없습니다.")
    allowed = {u.strip() for u in os.environ.get("BOT_ALLOWED_USERS", "").split(",") if u.strip()}

    updates = tg(token, "getUpdates", timeout=0, allowed_updates=["message"])
    if not updates:
        print("새 메시지 없음")
        return
    # 바로 확인 처리 → 처리 도중 다음 실행이 같은 메시지에 중복 답장하지 않음 (실패 시 사용자가 다시 보내면 됨)
    tg(token, "getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)
    for update in updates:
        msg = update.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            continue  # 1:1 대화만 처리 (채널·그룹 무시)
        chat_id, user_id = msg["chat"]["id"], str((msg.get("from") or {}).get("id", ""))
        if allowed and user_id not in allowed:
            tg(token, "sendMessage", chat_id=chat_id, text="이 봇은 등록된 사용자만 쓸 수 있어요.")
            continue
        text = msg.get("text", "").strip()
        if text.startswith("/"):
            try:
                reply = watchlist_command(text)
            except Exception as e:
                reply = f"⚠️ 명령 처리 중 오류: {html.escape(str(e))}"
            tg(token, "sendMessage", chat_id=chat_id, text=reply, parse_mode="HTML")
            continue
        want_earnings = "실적" in text or "earnings" in text.lower()
        tickers = parse_tickers(re.sub(r"(?i)earnings", "", text))
        if not tickers:
            tg(token, "sendMessage", chat_id=chat_id, text=HELP, parse_mode="HTML")
            continue
        for sym in tickers:
            try:
                reply = earnings_reply(sym) if want_earnings else build_reply(sym)
            except Exception as e:
                reply = f"⚠️ ${html.escape(sym)} 조회 중 오류: {html.escape(str(e))}"
            tg(token, "sendMessage", chat_id=chat_id, text=reply, parse_mode="HTML",
               disable_web_page_preview=True)
            print(f"답장: {sym} → {chat_id}")


if __name__ == "__main__":
    main()
