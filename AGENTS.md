# AGENTS

에이전트가 바로 실행하기 쉬운 형태의 작업 규칙 요약입니다.

## 0) 시작 전 필수 읽기 (순서 고정)
1. `README.md`
2. `docs/PROJECT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/PROVIDERS.md`
5. `docs/CONVENTIONS.md`
6. `docs/STAGES.md`

## 1) 작업 중 판단 기준 (Always)
- README + 실제 코드(`src/`, `config/`)를 기준으로 판단합니다.
- 코드에 없는 내용은 문서에 단정적으로 쓰지 않습니다.
- API 제한 보수 운영(일일 예산/리저브 우선)을 절대 깨지 않습니다.
- 실행 중단 후 재개 가능하도록 `data/state/state.json` 흐름을 유지합니다.

## 2) 완료 직전 체크리스트 (Done gate)
- [ ] 변경이 README/문서와 충돌하지 않는지 확인
- [ ] 상태 재개 흐름(`data/state/state.json`) 훼손 여부 확인
- [ ] API budget/reserve 원칙 위반 여부 확인
- [ ] `docs/STAGES.md`를 현재 상태로 반드시 업데이트

## 3) 문서 반영 원칙
- 기능/운영 흐름에 영향이 있는 변경은 `docs/STAGES.md`에 기록합니다.
- 단순 오탈자/형식 수정은 필요 시 생략할 수 있지만, 생략 시 PR 본문에 사유를 남깁니다.
