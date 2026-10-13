# invest2

Reddit에서 가장 많이 언급된 미국 상장 종목을 서브레딧별로 텔레그램에 보내는 스크립트입니다.
기본: **r/wallstreetbets Top 10** + **r/dividends(배당주) Top 5** + **StockTwits 트렌딩 Top 5**
- r/wallstreetbets: [ApeWisdom](https://apewisdom.io)의 최근 24시간 언급 집계 (전일 대비 순위·언급 변화 포함)
  + **🚀 언급 급증 Top 5**: 상위 300개 중 오늘 20회 이상 & 전일 대비 2배 이상(또는 신규)인 종목을 증가량 순으로. 기준은 `SURGE_RULES`에서 조정.
- r/dividends: ApeWisdom에 데이터가 없으면 Reddit RSS로 최근 24시간 글/댓글을 직접 모아 미국 상장 티커 언급을 셉니다.
  RSS에는 추천 수가 없어 언급 횟수만 표시되며, Reddit 요청 제한 때문에 실행에 몇 분 걸릴 수 있습니다.
  (선택) Reddit 앱을 만들어 `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` Secret을 등록하면 공식 API로 추천 수까지 가져옵니다.
- StockTwits 트렌딩: StockTwits 트렌딩 종목과 StockTwits가 제공하는 화제 요약의 첫 문장을 한국어로 번역해 붙입니다 (일간 메시지만).

## 메시지 예시

```
📈 미국주식 커뮤니티 화제 종목
2026-10-13 12:40 KST · 최근 24시간

🔥 r/wallstreetbets Top 10
1. $NVDA NVIDIA 🔺2
    💬 812회 (+143) · 👍 5,120
...

🔥 r/dividends Top 5
1. $SCHD Schwab US Dividend Equity ETF
    💬 12회
...

📊 StockTwits 트렌딩 Top 5
미국 개인투자자 SNS에서 지금 가장 화제인 종목
1. $ONDS Ondas Inc
    💡 Ondas와 Palantir의 국방 AI 벤처 Regna AI 출범이 논의를 이끌고 있습니다.
...
```

## 설정

1. 텔레그램에서 [@BotFather](https://t.me/BotFather)로 `/newbot` → **봇 토큰** 발급
2. 만든 봇에게 아무 메시지나 보낸 뒤 `https://api.telegram.org/bot<토큰>/getUpdates` 를 열어 `"chat":{"id": ...}` 값 확인 → **채팅 ID**
3. GitHub 저장소 → Settings → Secrets and variables → Actions 에 등록
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Actions 탭 → **Reddit Top 10 → Telegram** → *Run workflow* 로 바로 테스트
   (`dry_run` 체크 시 텔레그램 전송 없이 로그에만 출력)

이후 자동 전송됩니다.
- **화~토 12:40 (KST)**: 최근 24시간 일간 순위. 실행할 때마다 `data/daily/YYYY-MM-DD.json`에 순위 스냅샷을 커밋합니다.
- **월 12:40 (KST)**: 지난 7일간 스냅샷을 합산한 **주간 종합 순위** (언급 합계 · 추천 합계 · 언급된 일수).
  미국 기준 월~금 활동이 반영되며 주말은 수집하지 않습니다.

GitHub 예약 실행이 가끔 누락돼 13:40 / 15:10(KST)에 백업 실행이 있으며, 그날 이미 보냈으면 바로 종료합니다.

Actions에서 수동 실행 시 `mode`로 daily / weekly를 직접 고를 수 있습니다. 시간은 `.github/workflows/reddit-top10.yml`의 `cron`(UTC 기준)에서 바꿀 수 있습니다.

## 로컬 실행

```bash
python reddit_top10.py --dry-run                 # 전송 없이 출력만
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python reddit_top10.py
python reddit_top10.py --sources wallstreetbets:5,stocks:3   # 서브레딧:개수 변경
```

Python 3.8+ 표준 라이브러리만 사용합니다.

## 13F 기관 포트폴리오 변화 (`sec13f.py`)

관심 펀드(버크셔, 퍼싱스퀘어, 듀케인, 바우포스트, 히말라야, 파브라이, 아쿠아마린, 타이거 글로벌, 코튜, 틸 매크로)의
새 13F 보고서가 SEC EDGAR에 올라오면 직전 분기 대비 **신규 편입 / 전량 매도** 종목을 별도 메시지로 보냅니다.
매일 일간 실행 때 함께 확인하며, 펀드별로 마지막으로 알린 보고서를 `data/13f/state.json`에 기록해 중복 전송을 막습니다.

- 필요한 Secret: `SEC_USER_AGENT` — SEC 규정상 `이름 이메일` 형식 (예: `Hong Gildong hong@example.com`). 없으면 13F 단계는 건너뜁니다.
- 처음 실행 시에는 현재 최신 보고서를 기준점으로만 기록하고, 다음 분기 보고서부터 알립니다.
- 테스트: Actions → Run workflow → `mode: sec13f`, `sec13f_force` 체크 → 각 펀드의 최신 변화를 바로 받아볼 수 있습니다.
- 펀드 목록은 `sec13f.py`의 `FUNDS`에서 수정합니다.

