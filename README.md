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

**버크셔 해서웨이(버핏 → 에이블), 듀케인(드러켄밀러), 틸 매크로(피터 틸), 달랄 스트리트(모니시 파브라이)**의 새 13F 보고서가 SEC EDGAR에 올라오면
직전 분기 대비 변화를 펀드별 메시지로 보냅니다: 🆕 신규 편입 · 📈 추가 매수 · 📉 일부 매도 · ❌ 편출 · 📊 비중 상위 5 (항목별 최대 5개).
추가 매수/일부 매도는 보유 주식 수가 10% 이상 변했고 직전 또는 현재 비중이 1% 이상인 종목이며, 직전→현재 비중을 함께 표시합니다.
비중 0.1% 미만 신규 편입은 '소규모 신규'로 한 줄에 묶어 이름만 표시합니다.
매일 일간 실행 때 함께 확인하며, 펀드별로 마지막으로 알린 보고서를 `data/13f/state.json`에 기록해 중복 전송을 막습니다.

- 필요한 Secret: `SEC_USER_AGENT` — SEC 규정상 `이름 이메일` 형식. 없으면 13F 단계는 건너뜁니다.
- 처음 보는 펀드는 현재 최신 보고서를 기준점으로만 기록하고, 다음 분기 보고서부터 알립니다.
- 테스트: Actions → Run workflow → `mode: sec13f`, `sec13f_force` 체크.
- **파브라이는 상세 모드**: 신규/추가/매도 요약 대신 분기 총액 변화(직전 → 현재, %), 전체 보유 종목의 비중·평가액·주식 수·분기말 주가·직전 대비 변화·추정 매매 금액(주식 수 변화 × 분기말 주가), 편출 종목까지 모두 표시 (`DETAIL_FUNDS`).
- **파브라이 13D/13G 지분 공시 알림** (`STAKE_FILERS`): 본인 명의(CIK 1173334)·Dalal Street(1549575)로 제출되는 5% 이상 대량 보유 공시를 매일 확인해,
  새 공시가 나오면 종목·보유 주식 수·지분율·직전 공시 대비 변화·평가액(현재가 기준)·원문 링크를 전송. 지분 5% 미만 신고는 ⚠️ 표시.
  파브라이는 13G(수동적 기관투자자)로 신고해 분기 말 기준 45일 내 제출되지만, Dalal Street 외 계좌까지 합산된 보유량과 정확한 지분율을 알 수 있음.
- 펀드 목록은 `sec13f.py`의 `FUNDS`에서 수정합니다 (애크먼·클라만·리루 등 CIK 확인된 후보가 주석으로 있음).

## 내부자 매수 다이제스트 (`insider.py`)

SEC EDGAR Form 4를 매일 파싱해 **의미 있는 내부자 장내 매수**만 골라 하루 한 번 별도 메시지로 보냅니다.

- 필터: 거래 코드 `P`(장내 매수)만 · 10b5-1(사전 약정 자동 매매) 제외 · 매수 금액 $100K 이상 · 시가총액 $300M 이상 (Yahoo 주가 × SEC 발행주식수) · CEO/CFO/이사만
  · 신고 매수가가 매수일 주가와 2배 이상 다르면 제외 (해외 통화 표기 등 데이터 오류)
- 점수: 금액(1~4: <$250K / <$1M / <$5M / 그 이상) + 직책(CFO +3, CEO +2, 이사 +1) + 시총(<$2B +2, <$10B +1)
  + 클러스터(+3, 14일 안에 같은 회사 임원·이사 2명 이상이 $25K+ 매수) + 오랜만의 매수(+2, 직전 1년간 이 회사 장내 매수 없음)
  + 하락 중 매수(+1, 52주 고점 대비 -20% 이하) + 보유량 대비 매수(+1: +10%, +2: +50%, +3: +100% 이상 또는 신규 — 직접·간접 보유 합산)
- **7점 이상** 회사를 최대 10개 전송, 없으면 "오늘 의미 있는 내부자 매수 없음" 한 줄.
- 모든 후보(점수 미달 포함)를 `data/insider/candidates/YYYY-MM-DD.json`에 점수 내역과 함께 저장 → 기준점 보정·사후 성과 검증용.
- 매수일·신고일을 함께 표시하고, 신고가 3영업일 이상 늦으면 ⚠️ 표시.
- 종목마다 🏢 업종(SEC 분류)과 회사 소개 한 줄(Nasdaq 회사 프로필 첫 문장)을 한국어로 번역해 표시.
- 공시일(미 동부 기준) 단위로 처리하고 마지막 처리일을 `data/insider/state.json`에 기록 — 같은 날을 두 번 보내지 않습니다.
  그날 공시 목록이 아직 게시되지 않았으면 다음 실행에 묶어서 보냅니다.
- 테스트: Actions → Run workflow → `mode: insider`, `insider_date`에 공시일 입력 (상태에 영향 없음).
- 필요한 Secret: `SEC_USER_AGENT` (13F와 공용).

## 티커 조회 봇 (`ticker_bot.py`)

텔레그램 봇과의 **1:1 대화방**에 티커를 보내면(예: `TSLA`, `$nvda`, `AAPL MSFT` — 최대 3개) 답장합니다.

- 📊 StockTwits: 최근 메시지 30개의 강세/약세 비율, 인기 메시지 3개(번역), 트렌딩 중이면 화제 이유
- 🔥 Reddit: ApeWisdom 기준 r/wallstreetbets·주식 서브레딧 전체 순위와 24시간 언급 수(전일 대비), 최근 1주일 주요 서브레딧 글(번역, 링크)
- 상시 서버 없이 **cron-job.org가 1분마다** `ticker-bot.yml` 워크플로를 실행 → 쌓인 메시지를 처리 (응답까지 1~2분)
  - cron-job.org 작업: URL `https://api.github.com/repos/Seung-guriguri/invest2/actions/workflows/ticker-bot.yml/dispatches`,
    POST, 헤더는 일간 작업과 동일, 본문 `{"ref":"main"}`, 실행 주기 매 1분
- 허용 사용자만 응답 (`BOT_ALLOWED_USERS` Secret, 기본값은 소유자 ID). 채널·그룹 메시지는 무시
- 📅 `TSLA 실적` → 다음 실적 발표일(장전/장후, 한국시간)·컨센서스 EPS, 최근 SEC 실적 공시 자료 링크
- ⭐ `/add TSLA`, `/del TSLA`, `/list` → 실적 알림 관심 종목 관리 (`data/watchlist.json`, 봇 워크플로가 커밋)
- 테스트: Actions → Ticker Bot → Run workflow → `test_ticker`에 티커 입력 (로그에만 출력, `TSLA 실적`도 가능)

## 실적 발표 알림 (`earnings.py`) — 개인 대화방 전용

봇 1:1 대화방(`BOT_OWNER_ID`, 기본 소유자 ID)으로만 전송합니다.

- 대상: ⭐ 직접 등록(`/add`) + 🏦 파브라이 13F 보유 종목(SEC 회사명→티커 매칭) + 🕵️ 최근 30일 내부자 다이제스트 종목
- 📅 **D-7 / D-1 일정 알림**: 발표일, 장전/장후와 한국시간 환산, 컨센서스 EPS·애널리스트 수·전년 동기 EPS (Nasdaq 실적 캘린더, 일간 실행)
- 📄 **자료 공개 알림**: SEC 8-K(Item 2.02)의 보도자료(EX-99.1)·추가 자료(EX-99.2) 링크, 보도자료 속 컨퍼런스콜 웹캐스트 링크, EPS 서프라이즈
  — `earnings-materials.yml`이 매시간 확인 (발표일 전후 종목만)
- 📈 **발표 후 반응**: 다음 일간 실행 때 발표 직전 종가 대비 주가 변동, EPS, StockTwits 강세/약세
- 🗓 **월요일 주간 요약**: 이번 주 관심 종목 실적 발표 일정
- 상태: `data/earnings/state.json`. 테스트: Actions → Run workflow → `mode: earnings`, `dry_run`
- 해외 기업(6-K 제출)은 자료 공개 알림이 오지 않을 수 있음. 웹캐스트 링크는 보도자료에 있을 때만 추출

