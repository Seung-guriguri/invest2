# invest2

Reddit에서 가장 많이 언급된 미국 상장 종목을 서브레딧별로 텔레그램에 보내는 스크립트입니다.
기본: **r/wallstreetbets Top 5** + **r/ValueInvesting(가치투자) Top 5**
데이터는 [ApeWisdom](https://apewisdom.io)의 최근 24시간 언급 집계를 사용합니다 (API 키 불필요).

## 메시지 예시

```
📈 Reddit 언급 미국주식 순위
2026-10-13 07:47 KST · 최근 24시간

🔥 r/wallstreetbets Top 5
1. $NVDA NVIDIA 🔺2
    💬 812회 (+143) · 👍 5,120
...

🔥 r/ValueInvesting Top 5
1. $GOOGL Alphabet ➖
    💬 64회 (+5) · 👍 410
...
```

## 설정

1. 텔레그램에서 [@BotFather](https://t.me/BotFather)로 `/newbot` → **봇 토큰** 발급
2. 만든 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어 `"chat":{"id": ...}` 값 확인 → **채팅 ID**
3. GitHub 저장소 → Settings → Secrets and variables → Actions 에 등록
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Actions 탭 → **Reddit Top 10 → Telegram** → *Run workflow* 로 바로 테스트

이후 매주 화~토 오전 07:47(KST)에 자동 전송됩니다. 시간은 `.github/workflows/reddit-top10.yml`의 `cron`(UTC 기준)에서 바꿀 수 있습니다.

## 로컬 실행

```bash
python reddit_top10.py --dry-run                 # 전송 없이 출력만
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python reddit_top10.py
python reddit_top10.py --sources wallstreetbets:5,stocks:3   # 서브레딧:개수 변경
```

Python 3.8+ 표준 라이브러리만 사용합니다.
