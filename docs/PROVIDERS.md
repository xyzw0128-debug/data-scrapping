# PROVIDERS

## Twelve Data
- **역할**: 일봉 OHLCV 기본 수집(`src.main`), 종목 카탈로그(`src.universe`, `src.fetch_tickers`).
- **무료(Basic) 한도**: 키당 분당 8크레딧, 하루 800크레딧(00:00 UTC 초기화).
- **이 저장소 설정**(`config/providers.yaml`): `daily_limit: 800`, `daily_reserve: 100`(키당 하루 700 사용), `per_minute_limit: 7`, `max_symbols_per_run: 700`(순환 종목 수), `max_credits_per_run: 1800`(실행당 크레딧 상한, 순환 700종목에서 먼저 멈춰 보통 약 1,760 사용), `outputsize: 5000`.
- **크레딧 비용(2026-10-20 `/api_usage` 실측)**: `time_series` 1(5000행도 1), `/api_usage` 1, `/stocks`·`/etfs` 0, 403 응답 0.
- **무료 키에서 막힌 것**: `/dividends`, `/splits`, `/splits_calendar`, `/dividends_calendar`(403, Grow 이상 전용; `/dividends`는 AAPL만 예외적으로 열림). 대신 `time_series`의 `adjust` 옵션을 씁니다.
  - `adjust=splits`(기본): 분할 반영 OHLCV. Yahoo `Close`와 일치합니다.
  - `adjust=all`: 배당까지 반영한 종가(`adj_close`). 2016년 이후 Yahoo `Adj Close`와 0.003% 이내지만, 2012~2013년 일부 배당(예: NVDA 첫 배당 3회, SPY 2012-09-21)이 빠져 있습니다.
  - `adjust=none`: 실제 거래 가격. 분할 이벤트 계산에 씁니다.
- **주의사항**:
  - 키는 순서대로 다 쓰고 넘어갑니다. 실행 시작 때 `/api_usage`로 서버 사용량을 읽어 `state.json`보다 크면 그 값을 씁니다. 두 번 시도해도 읽지 못한 키는 그 실행에서 제외합니다. 실행당 상한이 다 차서 `/api_usage`를 부를 수 없으면 조회를 건너뛰고 키를 제외하지 않습니다(그 실행의 유료 호출은 상한이 모두 막음).
  - 네트워크 오류·응답 유실·5xx처럼 청구 여부가 불확실한 시도도 크레딧을 차감합니다.
  - "for the current minute" 응답은 60초 쉬고 같은 키로 다시 시도합니다(최대 2번, 크레딧 미차감). "for the day" 응답은 그 키를 소진 처리하고 다음 키로 넘어갑니다.
  - 404(종목 없음)는 다시 요청하지 않고 30일 뒤 재시도합니다.
  - 알려진 데이터 결함(2026-10-23 스냅숏·원본 응답·Yahoo 캐시 대조): 휴장일 봉(2,956개·349종목, 핵심 8개, 2024년 1,828개), 기업 이벤트 근처의 하루 빈칸(MNST 2026-08-10 분할 전날, FISV 2025-11-12 FI → FISV 티커 변경 즈음, FISV는 Yahoo에도 없음), 같은 티커의 옛 회사 이력(GM 2009-01-21~2010-09-14, 거래량 0). 수집기는 `src.data_rules`로 처리합니다(`docs/DESIGN.md` 3절 저장 규칙).
  - 무료 플랜은 개인·비상업 용도입니다.

## Finnhub
- **역할**: 종목별 company news(`src.finnhub_news`).
- **무료 한도**: 분당 60회(응답 헤더 `X-Ratelimit-Limit: 60`), 하루 한도 없음. 과거 뉴스는 약 1년.
- **이 저장소 설정**: 분당 50회(`FINNHUB_PER_MINUTE`), 첫 수집 365일(`FINNHUB_DAYS_BACK`), 종목당 실행마다 최대 10페이지(`FINNHUB_MAX_PAGES`), 대상은 전체 활성 종목(핵심 종목 먼저).
- **주의사항**:
  - 응답은 최신 기사 약 250건에서 잘립니다. 200건 이상이고 가장 오래된 기사가 시작일보다 늦으면 `to`를 당겨 다시 요청합니다.
  - 다 못 받은 구간은 `data/state/finnhub_news.json`에 저장해 다음 실행에서 이어 받습니다.
  - 종목 표기는 `BRK.B`처럼 점 표기를 씁니다(`BRK-B`는 0건).
  - HTTP 429 응답 시 60초 대기 후 1회 재시도합니다.

## FRED
- **역할**: 거시 지표 시리즈(`src.fred`), 16개(`config/fred_series.txt`).
- **알려진 한도**: 분당 120회(제3자 문서 기준, 응답 헤더 없음). 실행당 약 48회 호출(관측값·메타·발표처).
- **주의사항**:
  - `macro.first_seen_at`은 그 관측값을 처음 받은 시각이고 값이 수정돼도 유지됩니다.
  - BAA10Y(Moody's), VIXCLS(Cboe)는 설명에 복제·저장 제한 문구가 있어 `series_meta.personal_use_only`로 표시합니다. 개인 연구용으로만 쓰고 `src.export_dataset`은 기본으로 제외합니다.
  - IR3TIB01KRM156N은 한국 월간 3개월 금리이고 관측일 대비 약 44일 뒤 공개됩니다.
  - 점검 중에는 오류 코드 없이 HTTP 200과 HTML 점검 페이지("Site Maintenance | St. Louis Fed")를 돌려줍니다(2026-10-21 23:06 UTC에 시작해 10-22 02:26 UTC에도 계속). `src.fred`는 이를 `FredUnavailable`(상태 코드·Content-Type·페이지 제목)로 기록하고 남은 시리즈는 요청하지 않습니다. 관측값은 매번 전체 이력을 받으므로 다음 성공 실행에서 빠진 날이 채워집니다.

## Alpha Vantage
- **역할**: 보수적 백업 OHLCV(`src.alpha_vantage`), `run_daily.sh`에서 기본 꺼짐.
- **이 저장소 설정**: `daily_limit: 25`, `daily_reserve: 5`, `per_minute_limit: 5`, `max_symbols_per_run: 5`.
- **주의사항**: `TIME_SERIES_DAILY`는 분할 미반영 가격이고 Twelve Data와 같은 `ohlcv` 테이블에 덮어쓰므로, 켜면 가격 기준이 섞입니다.

## 공개 데이터
- **S&P 500 구성종목**: fja05680/sp500 GitHub CSV(stockpred와 같은 출처). 개인이 관리해 실제 지수 변경보다 늦게 반영될 수 있습니다(2026-10-20 기준 마지막 변경 2026-08-18).
