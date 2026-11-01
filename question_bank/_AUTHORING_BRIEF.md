# 문항 작성 지침 (Oracle Database / WebLogic 문항 은행)

이 지침은 영역 하나에 70문항짜리 JSON 파일 하나를 만드는 작성자를 위한 것입니다.
기존 OGG 문항(`question_bank/items_EXTRACT.json` 등)과 같은 형식과 수준으로 작성합니다.

## 산출물
- 파일: `question_bank/items_<DOMAIN_CODE>.json` (JSON 배열, UTF-8, 70개 객체)
- 검증: `.venv/Scripts/python tools/validate_bank.py question_bank/items_<CODE>.json --domain <CODE>`
  - **오류 0건**이 될 때까지 고칩니다.
  - 경고도 가능한 한 없앱니다.

## 객체 형식
```json
{
 "domain": "DB_BACKUP",
 "difficulty": "MID",
 "item_type": "SINGLE",
 "product_version": "19c",
 "stem": "English question text",
 "options": {"A": "...", "B": "...", "C": "...", "D": "...", "E": "..."},
 "correct_keys": ["C"],
 "explanation": "한국어 해설. 정답 근거와 오답 보기가 틀린 이유를 보기 내용으로 설명",
 "reference": "Oracle Database 19c - Backup and Recovery User's Guide (검수 필요)",
 "variant_group": "DB-BACKUP-INCR-LEVEL"
}
```
- `product_version`: Oracle Database는 `"19c"`, WebLogic은 `"14c"`(14.1.1 기준)입니다.
- `architecture` 필드는 넣지 않습니다(OGG 전용).
- `reference`: "<제품 버전> - <공식 문서 이름> (검수 필요)" 형식으로 씁니다.

## 분포 (영역당 70문항)
- **난이도**: LOW 20 / MID 35 / HIGH 15
  - LOW: 개념, 용어, 기본 명령
  - MID: 동작 원리, 파라미터·옵션의 효과
  - HIGH: 복합 시나리오, 장애 판단, 미묘한 차이
- **유형**: SINGLE 45 / SCENARIO 13 / MULTI 12
  - SCENARIO: 상황을 2~4문장으로 설명하고 가장 적절한 조치나 원인 하나를 고르는 문제입니다. 정답은 1개입니다.
  - MULTI: 지문 끝에 반드시 `(Choose two.)` 또는 `(Choose all that apply.)`를 넣습니다. 정답은 2개 이상이고, 오답도 1개 이상 있어야 합니다.
- **보기**: 모든 문항이 **5개(A~E)**입니다.
  - 정답 위치는 A~E에 고르게 퍼뜨립니다. 화면에서 보기를 섞어 보여 주더라도 원본 파일이 한쪽으로 쏠리지 않게 합니다.

## 작성 규칙 (검증 도구가 강제)
- **지문과 보기는 영어로만** 씁니다. 한글이 들어가면 오류입니다.
- **해설은 한국어**로 씁니다.
  - 보기를 문자로 가리키지 않습니다. "A는", "(B)", "보기 C", "option D" 같은 표현은 오류입니다. 응시 화면에서는 보기 순서가 섞이기 때문입니다.
  - 대신 보기 내용으로 언급합니다. 예: "RMAN DUPLICATE는 ~이므로 틀리다".
- **정답이 가장 긴 보기가 되지 않게** 씁니다. 정답 1개 문항 중 정답이 혼자 가장 긴 보기인 비율이 35%를 넘으면 경고입니다(우연 수준 20%).
  - 오답 보기도 정답만큼 구체적으로(조건·대상·이유를 붙여) 쓰고, 정답은 필요한 만큼만 간결하게 씁니다.
- "All of the above"나 "None of the above" 보기는 금지입니다.
- 보기 내용이 서로 겹치면 안 됩니다(대소문자·공백 무시).
- 지문은 파일 안에서 서로 달라야 합니다.
- **variant_group** 규칙
  - 대문자, 숫자, 하이픈만 씁니다.
  - 영역 접두사로 시작합니다. 영역 코드의 `_`를 `-`로 바꾸고 `-`를 붙입니다. 예: DB_ARCH → `DB-ARCH-`, WLS_JDBC → `WLS-JDBC-`
  - 같은 개념을 묻는 유사 문항끼리 같은 그룹을 씁니다. 한 그룹에는 **최대 3문항**입니다.
  - 그룹 수는 대략 55개 이상이 되게 합니다. 같은 그룹은 한 시험에 1문항만 출제되므로 그룹이 다양해야 합니다.

## 정확성 원칙 (가장 중요)
- 확실히 아는 사실만 문제로 만듭니다.
  - 버전에 따라 동작이 다르거나 기억이 불확실한 내용(기본값 숫자, 세부 파라미터 이름 등)은 피합니다.
  - 피할 수 없으면 지문에 버전을 명시합니다.
- 오답 보기는 그럴듯하되 **명백히 틀려야** 합니다. 정답이 둘 이상으로 해석될 수 있는 보기는 쓰지 않습니다.
- 담당 영역의 범위 안에서만 출제합니다. 다른 영역 주제와 겹치지 않게 아래 영역 목록을 참고합니다.
- 작성 후 사실관계가 조금이라도 불확실한 문항은 variant_group 목록으로 따로 보고합니다(검수 우선순위용).

## 영역 목록
**Oracle Database 19c**
- DB_ARCH 아키텍처: 인스턴스·메모리 구조(SGA/PGA), 백그라운드 프로세스, CDB/PDB, 시작·종료 단계
- DB_INSTALL 설치·구성: 설치, DBCA, 초기화 파라미터(SPFILE/PFILE), 리스너·Oracle Net(tnsnames, sqlnet)
- DB_STORAGE 저장 구조: 테이블스페이스, 데이터 파일, 세그먼트·익스텐트·블록, Undo/Redo, 아카이브 로그, ASM
- DB_SQL SQL·PL/SQL: SQL(조인·서브쿼리·분석함수), 트랜잭션·락·읽기 일관성, PL/SQL 객체(프로시저·패키지·트리거·예외)
- DB_SEC 사용자·보안: 사용자·권한·롤, 프로파일·비밀번호 정책, 통합 감사, TDE·네트워크 암호화
- DB_BACKUP 백업·복구: RMAN 백업·복구, 증분, 복구 카탈로그, Flashback 기술, Data Pump(expdp/impdp)
- DB_PERF 성능 튜닝: AWR/ASH/ADDM, 실행 계획, 옵티마이저 통계, 인덱스, 대기 이벤트, SQL 튜닝 도구
- DB_HA RAC·Data Guard: RAC(Clusterware, 캐시 퓨전, 서비스), Data Guard(물리/논리 standby, 보호 모드, switchover/failover, Broker)

**Oracle WebLogic Server 14c (14.1.1)**
- WLS_ARCH 아키텍처: 도메인, Admin/Managed Server, Node Manager, 머신, 서버 라이프사이클 상태
- WLS_INSTALL 설치·도메인 구성: 설치, Configuration Wizard, 도메인 템플릿, 디렉터리 구조, 시작 스크립트, boot.properties
- WLS_DEPLOY 애플리케이션 배포: 배포 방식(stage/nostage/external_stage), Deployment Plan, 공유 라이브러리, 프로덕션 재배포, weblogic.Deployer
- WLS_JDBC JDBC·데이터소스: Generic/Multi/Active GridLink 데이터소스, 커넥션 풀 설정, 테스트·축소, 트랜잭션(XA)
- WLS_CLUSTER 클러스터·HA: 클러스터, 세션 복제, 서비스·서버 마이그레이션, 로드밸런싱, 프록시 플러그인, 동적 클러스터
- WLS_SEC 보안: 보안 영역, 인증·인가 제공자, 역할·정책, SSL·KeyStore(Identity/Trust), 임베디드 LDAP
- WLS_OPS 운영·WLST: WLST(온라인/오프라인), 모니터링(Admin Console, 진단), 로그, 작업 관리자(Work Manager)
- WLS_TROUBLE 트러블슈팅: 스레드 덤프, Stuck/Hogging Thread, OutOfMemory, 힙 덤프, 서버 상태 이상(ADMIN/FAILED), 로그 분석
