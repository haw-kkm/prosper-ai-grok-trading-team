# Prosper AI Grok Trading Team — 최종 통합 감사 보고서 (Lesson 13)

감사일: 2026-11-15 · 감사 방식: 실제 코드 검사, 전체 pytest, 격리된 오프라인 검증 스크립트, 읽기 전용 DB 검사.
실거래·외부 주문·유료 Grok 호출 없음(모든 검증 스크립트에서 네트워크 시도 0, Grok 요청 0). 비밀 값은 이 문서에 쓰지 않았다.

상태 표기: **VERIFIED WORKING**(이번 또는 이전 실행으로 검증) · **IMPLEMENTED NOT TESTED** · **PARTIAL** · **BLOCKED** · **NOT IMPLEMENTED**

---

## 1. Executive Summary

- 이 프로젝트는 **실거래 기능이 전혀 없는 교육용 모의매매 시스템**이다. 브로커/거래소 주문 코드·라이브러리가 없고(코드 스캔: 브로커 라이브러리 0, 외부 호스트는 api.kraken.com·api.x.ai·docs.x.ai·www.alphavantage.co만).
- "6개 AI 에이전트"는 **대부분 고정된 Python 규칙**이다. 학습하는 모델은 없다. Grok(xAI)은 Market Analyst·News Analyst의 *선택적 연구 의견*으로만 쓰이며 주문·크기·승인·위험 한도에 영향을 주지 않는다. Grok 계정은 HTTP 403(결제 제한) 상태라 실제로는 OFFLINE으로 동작한다.
- 실제 데이터: **Kraken 현물 BTC/ETH/SOL 6개 시간봉 15,886,047개 캔들**(공식 OHLCVT 아카이브 + 최근 API). **미국 주식 0행**, **예측시장 실데이터 없음**.
- Lesson 9 의사결정 엔진, Lesson 10 위험 관리, Lesson 11 백테스트는 **테스트와 격리 실행으로 검증**됐다. Lesson 12 Session Runner는 **존재하지 않는다**.
- 전체 자동 테스트: **169 PASSED, 0 FAILED, 0 SKIPPED, 0 ERROR**.
- 첫 실제 백테스트(BTC/USD 1h, 비용 반영): **Test −9.89%, Buy & Hold +114.97%**. 수익성은 검증되지 않았고 오히려 반대 증거가 있다.
- 주요 보안 결함: **API 인증 없음 + 본문 없는 POST 엔드포인트의 CSRF 노출**, 로컬 로그의 **xAI 팀 ID 1건**(이번에 재발 방지 수정, 기존 줄은 사용자 판단으로 남김).
- **실거래 준비: 불가(NOT READY).**

## 2. 전체 프로젝트 구조

| 항목 | 확인 결과 |
|---|---|
| 경로 | `C:\vscode\prosper-ai-grok-trading-team` |
| Git | **Git 저장소 아님** (`git status` → not a git repository). 브랜치·커밋·diff·Git 기록의 비밀정보 검사 **불가** |
| Python / 의존성 | Python 3.14.5, fastapi 0.115.6, starlette 0.41.3, pydantic 2.14.3, uvicorn 0.32.1, pytest 8.3.4, httpx 0.28.1 (requirements.txt 4개 고정 버전 설치 확인) |
| 앱 | `app/`: config, market(시뮬레이터), broker(구 모의계좌), engine(구 6에이전트 루프), grok, stockdata, cryptodata, us_calendar, indicators, features, decisions(L9/L10), backtest(L11), main(API), `app/agents/` 6개 |
| 대시보드 | `dashboard/index.html` 단일 파일 |
| 테스트 | `tests/` 8개 파일, 169개 |
| 진행 기록 | LESSON6, 7, 9, 10, 11 존재 / **LESSON8, LESSON12 없음** |
| 데이터 | `data/market_data.db`(3.56 GB), `data/paper_account.json`(구 시뮬레이터), `data/decisions.db`(L12 세션 시 생성, NO TRADE 1건), `data/backtests/34f54f91bb2a/`, `data/grok_research.jsonl` |

진행 기록과 코드 비교: L6/L7/L9/L10/L11 기록의 완료 항목은 코드와 테스트로 확인됐다. L8 기록은 없고 관련 코드로 `app/features.py`(학습용 피처 준비, 학습 없음)만 있다. L12는 기록·코드 모두 없다.

## 3. 6개 에이전트 구현 상태

| 에이전트 | 파일·주요 함수 | 입력 → 출력 | 성격 | 상태 저장 | 판정 |
|---|---|---|---|---|---|
| Market Analyst | `app/agents/market_analyst.py`: `run`, `analyze_bars`, `market_report`, `_indicators`, `research`(Grok) | 종가 목록/저장 캔들 → SMA5/20, 모멘텀, 변동성, 추세 점수, EMA/RSI/MACD | **규칙 기반**. `research()`만 Grok 의견(표시용) | 없음 | VERIFIED (test_engine, test_stockdata, test_cryptodata) |
| News Analyst | `news_analyst.py`: `load_news`, `run` | `data/news.json`(사용자 제공) → 종목별 점수 | 뉴스 피드 없음. 항목이 있고 Grok 연구를 누를 때만 Grok이 점수. 없으면 0 + NEWS_UNAVAILABLE | 없음 | VERIFIED (test_engine, test_grok); 실제 뉴스 피드는 NOT IMPLEMENTED |
| Strategy Agent | `strategy_agent.py`: `run` | 추세 점수(+뉴스 30%) → BUY/SELL/HOLD/NO TRADE (BUY ≥ 0.45, SELL ≤ −0.45, 공매도 없음) | **고정 규칙** | 없음 | VERIFIED |
| Risk Manager | `risk_manager.py`: `review`, `check_exits` + L10 `decisions._risk`, `buy_limits`, `stop_exit`, `_daily` | 제안·포지션·자산 → APPROVED/REJECTED/NEEDS REVIEW, 크기 | **결정적 코드** | L10: `risk_state`/`risk_events` (SQLite) | VERIFIED (test_risk 34) |
| Portfolio Manager | `portfolio_manager.py`: `run`(구 루프) + L9 `decisions._fill` | 승인 주문 → 가상 체결 | 규칙 | 구: JSON / L9: SQLite | VERIFIED (test_decisions) |
| Performance Analyst | `performance_analyst.py`: `run` | 거래·자산곡선 → 수익률, 승률, PF, MDD, 수수료 | 규칙(통계) | 없음 | VERIFIED |

호출 주체: 구 루프는 `engine.TradingFloor.run_cycle`(랜덤워크 가격, 5초마다), 실데이터 경로는 `decisions.run`/`approve`(API·대시보드), 백테스트는 `backtest._segment`.
**결론: 어떤 에이전트도 학습하지 않는다. "AI"는 선택적 Grok 해석(연구 의견)뿐이며 현재 결제 제한으로 비활성.**

## 4. 주식 데이터 연결 상태 — **PARTIAL**

- 공급자: Alpha Vantage `TIME_SERIES_DAILY`(무료, 키 설정됨) 또는 CSV. 코드·검증·저장·미국 휴장일 달력 구현(`app/stockdata.py`, `app/us_calendar.py`), 모의 API 테스트 28개 통과.
- **SQLite `bars` 0행, `ingest_log` 0행**: 실제 API 수집을 한 번도 실행하지 않음. 시작/종료 시각·누락·중복: 데이터 없음(N/A). 중복은 `bars` PRIMARY KEY로 원천 차단.
- Market Analyst 연결: 코드상 연결(`stockdata.detail` → `market_report`), 실데이터로는 INSUFFICIENT DATA.

## 5. 암호화폐 데이터 연결 상태 — **VERIFIED WORKING (지연·누락 있음)**

읽기 전용 감사 스크립트 결과(총 88초). 모두 `KRAKEN / SPOT`(다른 거래소·선물 혼합 없음). 거부된 행 0, OHLC 비정상 행 0, 거래량 0 행 0. 중복: PRIMARY KEY (exchange, market_type, pair, interval_min, open_time)로 불가능.

| 종목 | 시간봉 | 캔들 수 | 최초 (UTC) | 최근 시작 (UTC) | 누락 캔들(구간 수) | 최대 구간 |
|---|---|---|---|---|---|---|
| BTC/USD | 1m | 5,029,946 | 2013-10-06 21:34 | 2026-11-15 11:31 | 1,864,172 (263,257) | 64,772 |
| | 5m | 1,109,319 | 2013-10-06 21:30 | 2026-11-15 10:50 | 269,498 (34,639) | 12,371 |
| | 15m | 388,517 | 2013-10-06 21:30 | 2026-11-15 11:15 | 71,091 (11,655) | 3,646 |
| | 1h | 103,656 | 2013-10-06 21:00 | 2026-11-15 09:00 | 11,245 (3,261) | 370 |
| | 4h | 27,634 | 2013-10-06 20:00 | 2026-11-15 04:00 | 1,091 (579) | 13 |
| | 1d | 4,776 | 2013-10-06 | 2026-11-14 | 12 (11) | 2 |
| ETH/USD | 1m | 4,522,352 | 2015-08-07 14:03 | 2026-11-15 11:31 | 1,407,417 (442,381) | 64,772 |
| | 5m | 1,054,049 | 2015-08-07 14:00 | 2026-11-15 10:50 | 131,898 (28,594) | 12,371 |
| | 15m | 366,060 | 2015-08-07 14:00 | 2026-11-15 11:15 | 29,258 (6,793) | 3,646 |
| | 1h | 94,964 | 2015-08-07 14:00 | 2026-11-15 09:00 | 3,864 (849) | 370 |
| | 4h | 24,259 | 2015-08-07 12:00 | 2026-11-15 04:00 | 448 (209) | 12 |
| | 1d | 4,112 | 2015-08-07 | 2026-11-14 | 6 (5) | 2 |
| SOL/USD | 1m | 2,361,731 | 2021-06-17 15:32 | 2026-11-15 11:31 | 484,909 (238,228) | 64,772 |
| | 5m | 548,235 | 2021-06-17 15:30 | 2026-11-15 10:50 | 21,086 (5,810) | 12,371 |
| | 15m | 185,561 | 2021-06-17 15:30 | 2026-11-15 11:15 | 4,215 (307) | 3,646 |
| | 1h | 47,041 | 2021-06-17 15:00 | 2026-11-15 09:00 | 402 (14) | 370 |
| | 4h | 11,858 | 2021-06-17 12:00 | 2026-11-15 04:00 | 3 (3) | 1 |
| | 1d | 1,977 | 2021-06-17 | 2026-11-14 | 0 (0) | 0 |

- 출처: 각 시간봉 최근 720개(4h 272, 1d 45)는 `API`(Kraken REST), 나머지는 `CSV` = Kraken 공식 `Kraken_OHLCVT_Full.zip` 가져오기(수집 기록 `crypto_fetch_log`에 파일별 OK 기록).
- 누락의 대부분은 Kraken이 거래 없는 분을 생략하는 특성(초기·1분봉). **공통 최대 구간**: 아카이브 종료(2026-09-30)와 API 시작 사이(1h 기준 370캔들, 1m 64,772 = 약 45일). 누락은 채우지 않고 라벨링한다.
- 지연: 최신 1h 캔들이 수집 시점 기준 수 시간 뒤처짐(L12 세션 시 4캔들 DELAYED).
- 지표 계산 가능: 모든 조합이 25캔들 이상이라 가능.
- **Kraken 원본 CSV ↔ SQLite 일관성: NOT TESTED** — 원본 zip이 프로젝트·다운로드 폴더에 없음(가져오기 요약만 존재).
- 1분봉 메모리: 1년치 BTC 1m(978,169행) 청크 스트리밍 20.9초, Python 최대 메모리 **10.3 MB**(tracemalloc 측정). 백테스트·피처 계산도 청크 방식.

## 6. 예측시장 연결 상태 — **NOT IMPLEMENTED**

실제 공급자, 과거 계약 가격, 계약 구조, 유동성, 정산 결과, 조회 기능 모두 없음. `PM-*`는 `app/market.py`의 **랜덤워크 시뮬레이션**뿐이며 의사결정·백테스트는 예측시장을 UNSUPPORTED로 거부(test_decisions, test_risk D, test_backtest).

## 7. Grok API 연결 상태 — **PARTIAL (현재 BLOCKED by billing)**

| 항목 | 결과 |
|---|---|
| XAI_API_KEY / XAI_MODEL 로딩 | `app/config.py`에서 `.env` 로드. `.env`에 키 값 존재(값은 출력하지 않음). 설정된 모델 id가 `grok-4`로, L7 기록상 xAI 목록에 없는 id (확인 필요) |
| 역할별 모델 | **없음** — `XAI_MODEL` 하나만 |
| 실패 처리 | 재시도 없음, 45초 타임아웃, 실패 시 OFFLINE 유지(test_grok 23) |
| BILLING_ERROR 분류 | **미구현**: `grok.py`가 403을 `"auth"`로 분류(결제 제한도 auth로 표시) |
| 자동 호출 | `GROK_AUTO_RESEARCH_SECONDS=0`(꺼짐). 버튼을 눌렀을 때만, 15초 최소 간격 |
| 비용·토큰 기록 | `data/grok_research.jsonl`에 usage 기록, 대시보드에 cost_in_usd_ticks 표시 |
| 키 노출 | 코드 하드코딩 없음(스캔 결과 테스트 파일의 가짜 키 1건뿐). `redact()`로 키 마스킹 |
| 팀 ID 노출 | **로컬 로그에 xAI 팀 ID 형태 값 1건**(이전 403 메시지). `redact()`가 팀 ID를 가리지 못했음 → **이번에 수정**(아래 18절). 기존 로그 줄은 그대로 |
| 학습 여부 | Grok은 주어진 지표/뉴스를 **해석**할 뿐 데이터 수집·학습 없음 |

## 8. 기술적 지표 검증 — **VERIFIED WORKING**

- `app/indicators.py`: SMA, EMA(SMA 시드), Wilder RSI, MACD(12/26/9), Wilder ATR — 각 위치 i는 closes[:i+1]만 사용. 데이터 부족 시 `INSUFFICIENT DATA` 반환(가짜 값 없음).
- 최소 길이: SMA n, EMA n, RSI n+1, MACD slow+signal−1(34), ATR n+1, Market Analyst 25.
- 근거 테스트: `test_stockdata::test_sma_momentum_volatility_and_volume_change_known_values`, `test_insufficient_data_is_reported_not_invented`, `test_indicators_never_use_future_bars`, `test_cryptodata::test_indicator_formulas_and_minimum_data`, `tests/test_features.py`(6).
- 다중 시간봉: `features._align_higher`, `backtest._Higher` — 상위봉은 **닫힌 캔들만**. `test_backtest::test_5_higher_timeframe_uses_only_closed_candles`(10:05 판단 시 10:00–11:00 1시간봉 미사용), 실제 실행에서도 11:00 판단에 4h는 08:00 기준.
- 타임스탬프: 모두 UTC 유닉스초, 미국 주식은 세션 날짜 기준 + 휴장일 달력.

## 9. 의사결정 엔진 검증 (Lesson 9) — **VERIFIED WORKING**

- `app/decisions.py`: 제안 레코드(필수 필드 전부), BUY/SELL/HOLD/NO TRADE, 근거(사용 데이터·지표·규칙·위험·누락), 데이터 출처·분석 시각, 위험 검사, 사용자 승인, 가상 체결, 중복 차단, SQLite 저장.
- 근거: `tests/test_decisions.py` 23개, L9 검증 스크립트(A/B/C + 중복·일일손실·만료 PASS), L13 SYNTHETIC DEMO 세션(아래 12절).
- **위험 검사·승인 없이 주문으로 가는 경로 조사:**
  - L9/L10 경로: 없음. 예외는 진입 승인 때 함께 승인된 손절/익절(브래킷) 청산뿐이며 보유 수량 이하만 매도.
  - **구 시뮬레이터(`app/engine.py`)**: `REQUIRE_APPROVAL=false`(현재 설정)일 때 위험 승인된 BUY가 **사람 승인 없이 자동 체결**되고, `decide()` 승인 시 위험 재검사가 없다. 랜덤워크 가격·별도 JSON 계좌라 실데이터·실주문과 무관하지만, 과제 기준상 **높은 우선순위 결함으로 보고**한다(L10 테스트로 새 장부를 건드리지 않음은 확인).

## 10. 위험 관리 기능 검증 (Lesson 10) — **VERIFIED WORKING**

| 기능 | 근거 |
|---|---|
| 포지션 한도(보유+신규 15%) | test_risk B, L10 최종검증 2a |
| 전체 노출 75% / 시장별(코인 30%, 예측 0%) | test_risk C, D |
| 거래당 위험 1%(수수료·슬리피지 포함) | test_risk E |
| 일일 손실 −3%, 날짜 변경 재설정, 손상 시 차단 | test_risk G, H; L10 최종검증 3a–3e |
| 가상 손절(갭·동일 캔들·불량 캔들) + 수수료·슬리피지 | test_risk J, K, L, V |
| 연속 손실 쿨다운 | test_risk M, N; 4a–4d |
| Kill Switch 래치, 재시작 유지, 승인 없는 해제 차단 | test_risk O–S; 실제 서버 재시작 검증(PID 변경 후 유지) |
| 중복 실행 방지 | test_risk U; HTTP 동시 승인 6건 → 1건 |

구 시뮬레이터(`app/broker.py`)의 일일 손실 기준 미초기화 버그는 **남아 있음**(새 엔진에는 영향 없음).

## 11. 사용자 승인 시스템 검증 — **VERIFIED WORKING (인증 없음)**

승인 전 주문 차단, 위험 거부 제안 승인 불가, 만료, 내용 해시, 잔고 변경 시 차단, 승인 시 전체 위험 재검사, 동시 승인 1회 체결이 테스트로 확인됨. **단, 승인 API에 인증/권한 검증이 없다**(17절).

## 12. 모의 주문 및 포트폴리오 검증 — **VERIFIED WORKING**

L13 격리 SYNTHETIC DEMO 세션(기존 L9/L10 엔진, 임시 DB, 테스트 전용 승인 이벤트):
Market(SYNTHETIC 50,000, 추세 0.8) → News NEWS_UNAVAILABLE → Strategy BUY 0.3 BTC, 손절 47,500 → Risk RISK_APPROVED(22개 검사) → 승인 전 체결 0건 → **테스트 승인 이벤트** → 체결 BUY 0.3 @ 50,000, 수수료 15, 현금 100,000→84,985 → Performance 자산 99,985 → **새 프로세스에서 체결 1건·포지션·HUMAN_APPROVED 보존**. 보호 파일(paper_account.json, decisions.db, 기존 백테스트 결과) 해시 불변.

## 13. 백테스트 신뢰성 평가 (Lesson 11) — **VERIFIED WORKING (수익성은 부정적)**

- 실데이터·시간순 70/30·워밍업 분리·다음 시가 체결·수수료/슬리피지·L10 위험 규칙·Buy & Hold·N/A 처리: `tests/test_backtest.py` 18개.
- **재현성: 저장된 실행 34f54f91bb2a 설정으로 재실행 → Train/Test 지표 8개 모두 동일**, 코드 버전 해시 동일(ad117dedb673).
- 결과(BTC/USD 1h, 수수료 0.40% 가정·미검증, 슬리피지 0.10%):

| | Train 2013-10-06 → 2023-04-13 | Test 2023-04-13 → 2026-11-15 |
|---|---|---|
| Total Return / Net Profit | +4.02% / +4,023.54 | **−9.89% / −9,888.83** |
| 거래 / 승률 / PF | 132 / 37.1% / 1.07 | 73 / 15.1% / 0.24 |
| MDD / Sharpe | 10.19% / 0.134 | 10.15% / −2.031 |
| Fees / Slippage | 16,985.73 / 3,463.54 | 8,379.20 / 2,078.92 |
| Buy & Hold 수익률 / MDD | +24,555% / 95.6% | +114.97% / 53.8% |

- 한계: Kill Switch가 Train 2015-01-15, Test 2023-09-25에 발동해 이후 거래가 없음(실제 거래 기간이 짧음). 단일 종목, 뉴스 없음, 비용 가정 미검증, 데이터 누락(INCOMPLETE).
- **과최적화 흔적: 없음** — 실행 기록은 1건뿐이고 전략 파라미터(0.45/−0.45)는 백테스트 이전부터 고정. 단, 이 Test 결과를 보고 전략을 고치면 새 미사용 기간이 필요.

## 14. 데이터베이스 무결성

- `market_data.db`: PK로 중복 불가, 비정상 행 0, 거부 행 0, 인덱스는 PK 자동 인덱스(구간 조회에 사용). WAL 모드. 감사 중 파일 변경 없음(수정 시각 20:34 유지).
- `decisions.db`: 트랜잭션(BEGIN IMMEDIATE), `fills.proposal_id UNIQUE`(직접 삽입 시 거부 확인), 추가형 마이그레이션, 원자적 체결.
- 백테스트 결과: 원자적 파일 쓰기, 중단 시 INTERRUPTED 표시(test_backtest).
- 구 `paper_account.json`: 단일 JSON 원자적 교체(tmp→replace), 트랜잭션 없음.

## 15. 대시보드 검증

| 요구 화면 | 실제 | 판정 |
|---|---|---|
| Overview / Agents / Portfolio / Performance / Risk & Decisions(구) | 존재, `/api/state` 200 | VERIFIED (이전 Lesson 브라우저 확인) · 이번 감사는 API만 확인 |
| Stocks | "Stocks (Real)" — 데이터 0이라 DATA UNAVAILABLE 표시 | PARTIAL |
| Crypto | "Crypto (Real)" `/api/crypto` 200 (응답 약 13초: 대용량 COUNT) | VERIFIED, 느림 |
| Trade Decision Center | "Decision Center (L9)" | VERIFIED (L9 브라우저, 이중 클릭 1회 체결) |
| Risk Control Center | "Risk Control (L10)" | VERIFIED (L10 최종검증: 대시보드=API=DB 15개 값 일치, 승인 전후 2회) |
| Backtesting Lab | "Backtest Lab (L11)" | VERIFIED (L11 브라우저, DEMO 라벨, 390px) |
| Market Intelligence | 없음 | NOT IMPLEMENTED |
| Prediction Markets | 실데이터 화면 없음("Sim Markets"는 시뮬레이션) | NOT IMPLEMENTED |
| Session Results | 없음 | NOT IMPLEMENTED |

## 16. 전체 테스트 결과

| 구분 | 결과 |
|---|---|
| pytest 전체 (최종) | **PASSED 169 · FAILED 0 · SKIPPED 0 · ERROR 0** (test_api 3, cryptodata 24, decisions 23, engine 10, features 6, grok 23, risk 34, stockdata 28, backtest 18) |
| 통합(격리 스크립트) | 데이터 감사, 비밀 스캔, CSRF/콘텐츠 타입 검사, 백테스트 재현, SYNTHETIC 세션 + 재시작 — 모두 실행 |
| NOT TESTED | Kraken 원본 CSV↔DB 대조(원본 없음), 실제 Alpha Vantage 수집, 실제 Grok 호출(금지), Git 기록 비밀 검사(저장소 아님), 이번 감사 중 구 화면 브라우저 재검사 |

## 17. 보안 취약점

| 심각도 | 내용 | 위치 | 대응 |
|---|---|---|---|
| **높음(노출 시)/중간** | **모든 API에 인증·권한 없음.** 서버에 접근 가능한 누구나 승인·거절·Kill Switch 해제 요청 가능 | `app/main.py` 전체 | 로컬(127.0.0.1)로만 실행 유지, 토큰 인증 추가 |
| **중간** | **본문 없는 POST 엔드포인트의 CSRF**: 서버 실행 중 사용자가 악성 웹페이지를 열면 브라우저가 "simple request"로 `/api/reset`(구 모의계좌 초기화), `/api/toggle`, `/api/approval-mode`, `/api/decisions/run`, `/api/risk/check-stops`, `/api/grok/test·research`(크레딧이 있으면 유료 호출) 등을 실행시킬 수 있음. 실측: 본문 없는 `POST /api/toggle` → 200 | `app/main.py` | 사용자 정의 헤더/CSRF 토큰 요구, 상태 변경 엔드포인트에 JSON 본문 강제 |
| 낮음(완화됨) | JSON 본문 엔드포인트(승인·Kill Switch 해제·백테스트)는 `text/plain` 위장 요청을 거부(실측 422) | FastAPI 기본 동작 | 유지 |
| 중간 | xAI 팀 ID가 로컬 로그에 1건 저장, 대시보드 `last_error`로 노출 가능 | `data/grok_research.jsonl`, `app/grok.py` | `redact()` 수정 완료(재발 방지). 기존 줄은 사용자가 직접 삭제/가림 권장 |
| 낮음 | `.env`에 실제 키 존재(정상), `.gitignore`가 `.env` 제외. Git 미사용이라 추적 여부 확인 불가 | `.env`, `.gitignore` | Git 도입 시 `.env` 미추적 확인 |
| 없음 | SQL Injection: 모든 사용자 값은 매개변수 바인딩. f-string은 내부 상수·열 이름만 | decisions/backtest/stockdata/cryptodata | — |
| 정보 | `GET /api/decisions`가 상태를 변경(만료·일일 기준·Kill Switch 발동) | `decisions.state()` | 안전 방향이지만 읽기/쓰기 분리 권장 |

## 18. 남아 있는 버그 / 이번 감사의 수정

**이번 수정(1건, 최소):** `app/grok.py` `redact()`가 xAI 팀 ID(UUID)를 가리도록 1줄 추가 → 검증 후 전체 pytest 169 통과.

**남은 버그·결함:**
1. 구 시뮬레이터: 승인 없이 자동 체결(승인 OFF 시), `decide()` 위험 재검사 없음, 일일 기준 미초기화 (`app/engine.py`, `app/broker.py`).
2. Grok 403을 `auth`로 분류(BILLING 미구분), 모델 id `grok-4` 확인 필요 (`app/grok.py`, `.env`).
3. API 인증 없음 + CSRF (`app/main.py`).
4. `/api/crypto` 응답 약 13초, `/api/backtest/options` 약 2.5초(대용량 COUNT/MIN·MAX) — 측정값.
5. 데이터: 주식 0행, 암호화폐 4시간 이상 지연, 2026-10 아카이브↔API 구간 누락.
6. 구현 없음: Lesson 12 Session Runner, 예측시장, 뉴스 피드, ML 학습, Walk-Forward(구조만), 다종목 백테스트, 대시보드 3개 화면.

## 19. 실거래 준비 여부 평가 — **NOT READY**

- 설계상 실거래 연결이 없고(정상), 추가하더라도: 전략이 비용 반영 Test에서 손실(PF 0.24), 수익성 검증 없음, API 인증 없음, 데이터 지연·누락, 비용 가정 미검증, 장기 모의 운영 기록 없음.
- 실거래 전 최소 조건: Walk-Forward로 검증된 양(+)의 기대값, 인증·CSRF 방어, 실시간 데이터 파이프라인, 수개월 이상의 모의 자동 운영, 독립 코드 리뷰, 소액·종이매매(paper/sandbox) 단계.

## 20. 다음 개발 우선순위 (추가 개발은 이번 감사에서 하지 않음)

| 과제 | 필요성 | 현재 상태 | 선행 조건 | 난이도 | 검증 방법 | 우선순위 |
|---|---|---|---|---|---|---|
| A. 실제 시장 데이터 보강 | 주식 0행, 코인 지연·구간 누락 | 수집기 구현, 미실행/미갱신 | Alpha Vantage 무료 한도, Kraken fetch | 낮음 | `quality` 경고 0, 누락 구간 감소 | 2 |
| B. 학습용 데이터셋 | ML 전제 | `features.py`(피처·라벨·시간분할) 구현, 데이터셋 미생성 | A, E | 중간 | 누출 테스트, 재현 해시 | 4 |
| C. LightGBM 예측 모델 | 규칙 전략 개선 후보 | 없음(의존성도 없음) | B, E | 중간~높음 | Walk-Forward OOS 성능 vs 규칙/B&H | 6 |
| D. 다중 시간봉 모델 | 상위봉 정보 활용 | 정렬 로직만 | B, C | 높음 | 상위봉 누출 테스트 + OOS | 7 |
| **E. Walk-Forward 검증** | 단일 70/30은 과최적화·우연에 취약; 모든 개선의 평가 기준 | `_segment` 구조만, 미노출 | 없음(현 엔진 재사용) | 중간 | 창별 결과 분포, 창 간 일관성, 비용 포함 | **1** |
| F. 비용 포함 전략 개선 | Test 손실, 비용이 수익을 잠식 | 규칙 전략 1개 | E(없으면 Test 과최적화) | 중간 | Walk-Forward OOS만으로 판단 | 3 |
| G. Grok 연구 효과 비교 | AI 해석의 가치 확인 | 연구 기능 있음, 결제 차단 | 크레딧, 시점 검증된 입력 | 중간 | 동일 기간 A/B, 비용 대비 효과 | 8 |
| H. 장기 모의 자동매매 | 실운영 안정성 | 승인 필수 구조, 세션 러너 없음 | 인증·CSRF 수정, 실시간 데이터 | 중간 | 수개월 운영 로그·위험 이벤트 | 5 |

**가장 중요한 다음 과제: E. 시계열 Walk-Forward 검증.**
이유: 현재 유일한 평가가 단일 70/30 분할이고 Kill Switch로 실제 거래 기간이 짧아 결론이 불안정하다. 전략 개선(F)이나 ML(C, D)을 먼저 하면 같은 Test 구간에 맞추는 과최적화 위험이 크다. Walk-Forward는 기존 백테스트 엔진(`_segment`)을 재사용해 적은 비용으로 만들 수 있고, 이후 모든 개선의 공정한 평가 기준이 된다.

---

증거 위치: 테스트 `tests/*.py`; 감사 스크립트와 출력(임시 폴더, 재부팅 시 삭제 가능) `C:\Users\kkm05\AppData\Local\Temp\claude\e9ba2c095a5e71ab\7aeaf75c-63d9-4f05-93ea-fe3f602009fe\scratchpad\` (`audit_data.py`/`audit_data.json`, `audit_checks.py`, `final_l10_check.py`, `verify_l9.py`, `verify_l10.py`); 진행 기록 `docs/LESSON6~11_PROGRESS.md`, `docs/LESSON13_PROGRESS.md`; 백테스트 `data/backtests/34f54f91bb2a/`.
