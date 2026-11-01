# Oracle CBT — Oracle 사내 자격시험 온라인 시스템

시험 과목은 **Oracle GoldenGate(OGG)**, **Oracle Database(ORADB)**, **Oracle WebLogic Server(WLS)** 입니다.
과목(제품)마다 출제 영역·Blueprint·문항이 따로 있고, 응시자는 대시보드에서 과목을 골라 시험을 시작합니다.

Rockplace DI팀 내부용 CBT(Computer Based Test) 시스템입니다. 응시자가 접속할 때마다 **관리자가 검수·승인한 문항은행**에서
**출제 기준표(Blueprint)** 에 맞춰 서로 다른 문제 세트를 출제합니다. 시험 중 AI가 문항을 만드는 일은 없습니다.

| 구분 | 내용 |
|---|---|
| Backend | Python 3.10 ~ 3.13 (전체 테스트 통과 확인), FastAPI, SQLAlchemy 2.x, Alembic |
| DB | SQLite (파일 1개) |
| Frontend | Jinja2 템플릿 + HTMX (SPA 아님) |
| 인증 | 세션 로그인, bcrypt 해시, 로그인 5회 실패 시 10분 잠금 |
| 배포 | OCI VM 직접 설치 (Python 가상환경 + systemd + nginx) |
| 테스트 | pytest, 커버리지 99% (404개 테스트) |

## 목차
1. [서버 배포 (OCI VM)](#1-서버-배포-oci-vm)
2. [로컬 개발 실행](#2-로컬-개발-실행)
3. [처음 운영을 시작하는 순서](#3-처음-운영을-시작하는-순서)
4. [관리자 계정 만들기](#4-관리자-계정-만들기)
5. [응시자 등록](#5-응시자-등록)
6. [문항 업로드](#6-문항-업로드)
7. [설정과 보안](#7-설정과-보안)
8. [백업과 복구](#8-백업과-복구)
9. [테스트](#9-테스트)
10. [구조와 설계 메모](#10-구조와-설계-메모)
11. [알려진 한계](#11-알려진-한계)

---

## 1. 서버 배포 (OCI VM)

GitHub 저장소를 OCI VM에 내려받아 **Python 가상환경 + systemd + nginx(HTTPS)** 로 운영합니다. 설정 견본은 [`deploy/`](deploy/) 에 있습니다.

> **검증 상태**: `deploy/` 의 systemd 유닛과 nginx 설정은 실제 OCI 서버에서 실행해 보지 못했습니다(작성 PC는 Windows).
> 앱의 기동 순서(마이그레이션 → seed → 서버)와 `backup.sh` 는 로컬에서 동작을 확인했습니다.
> 처음 배포할 때 `journalctl -u ogg-cbt -f` 와 `nginx -t` 로 확인하세요.

### 사전 확인
* **Python 3.10 이상** (`python3 --version`). 3.10, 3.11, 3.12, 3.13 에서 전체 테스트(브라우저 E2E·경합 테스트 포함)가 통과하는 것을 확인했고,
  GitHub Actions(CI)가 3.12/3.13 을 계속 검증합니다. 3.10 미만에서는 문법(`X | None`) 때문에 동작하지 않습니다.
  (테스트 환경은 Windows 이며 Linux 에서의 실행은 CI 가 처음 검증합니다.)
  OS 기본 Python 이 오래됐다면(예: Oracle Linux 8 의 3.6/3.9) 새 버전을 설치하세요.
* **CPU 구조** — OCI Ampere(ARM, aarch64) 인스턴스일 수 있습니다. 의존성(`bcrypt`, `uvloop`, `pydantic-core` 등)은 aarch64 용 사전 빌드 패키지가 있는 편이지만,
  `pip install` 이 컴파일을 시도하며 실패하면 `gcc`, `python3-devel`, `libffi-devel`, `openssl-devel` 등이 필요할 수 있습니다.
* **nginx 와 HTTPS 인증서**(사내 도메인). 세션 쿠키가 `Secure` 라서 HTTPS 가 아니면 로그인되지 않습니다.
* **OCI 네트워크**: 보안 목록/NSG 에서 **443(필요하면 80)만** 열고 앱 포트(8000)는 열지 마세요. 앱은 `127.0.0.1` 에만 바인딩됩니다.
  VM 안의 방화벽(firewalld/ufw/iptables)도 같은 기준으로 확인합니다.
* 이미 같은 VM 에서 다른 서비스가 8000 포트를 쓰고 있다면 `ogg-cbt.service` 와 nginx 설정의 포트를 함께 바꾸세요.

### 설치
```bash
# 1) 서비스 전용 계정과 디렉터리 (로그인 불가 계정)
sudo useradd --system --home-dir /opt/ogg-cbt --create-home --shell /usr/sbin/nologin cbt
sudo install -d -o cbt -g cbt -m 750 /var/lib/ogg-cbt          # SQLite DB 위치
sudo install -d -o root -g cbt -m 750 /etc/ogg-cbt             # 설정 위치

# 2) 코드 내려받기 (비공개 저장소면 배포 키(deploy key) 또는 토큰 필요)
sudo -u cbt git clone https://github.com/<조직>/<저장소>.git /opt/ogg-cbt/app

# 3) 가상환경과 의존성
sudo -u cbt python3 -m venv /opt/ogg-cbt/.venv
sudo -u cbt /opt/ogg-cbt/.venv/bin/pip install -r /opt/ogg-cbt/app/requirements.txt

# 4) 설정 파일 (SECRET_KEY 를 새로 생성해 넣습니다)
sudo cp /opt/ogg-cbt/app/deploy/ogg-cbt.env.example /etc/ogg-cbt/env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"      # 출력값을 SECRET_KEY 에 붙여넣기
sudo vi /etc/ogg-cbt/env
sudo chown root:cbt /etc/ogg-cbt/env && sudo chmod 640 /etc/ogg-cbt/env

# 5) 서비스 등록·시작 (시작할 때 마이그레이션과 기본 seed 가 자동 실행됨)
sudo cp /opt/ogg-cbt/app/deploy/ogg-cbt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ogg-cbt
sudo systemctl status ogg-cbt
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/login      # 200 이면 앱 정상

# 6) nginx (server_name 과 인증서 경로를 환경에 맞게 수정)
sudo cp /opt/ogg-cbt/app/deploy/proxy_params_cbt /etc/nginx/proxy_params_cbt
sudo cp /opt/ogg-cbt/app/deploy/nginx-ogg-cbt.conf /etc/nginx/conf.d/ogg-cbt.conf
sudo vi /etc/nginx/conf.d/ogg-cbt.conf
sudo nginx -t && sudo systemctl reload nginx

# 7) 관리자 계정 생성 (4절)
sudo -u cbt /opt/ogg-cbt/app/deploy/cbt-cli.sh create-admin admin "관리자"
```

* `.env` 와 DB 파일은 저장소에 없습니다. **`/etc/ogg-cbt/env`(비밀값)와 `/var/lib/ogg-cbt/cbt.db`(응시 데이터)는 서버에만 존재**하며, 코드를 다시 받아도 유지됩니다.
* 시작 시 자동 실행되는 `alembic upgrade head` 와 `python -m app.cli seed`(영역 7개 + 기본 Blueprint)는 여러 번 실행해도 안전합니다. 샘플 문항은 넣지 않습니다.
* **워커는 1개**입니다. SQLite 단일 파일이고 만료된 시험을 자동 제출하는 스케줄러가 프로세스 안에서 돌기 때문에 워커/인스턴스를 늘리지 마세요.
* nginx 설정에는 `/login` 의 **IP 단위 속도 제한**(분당 10회)이 들어 있습니다. 앱의 계정별 잠금(5회/10분)을 보완합니다.
* 로그는 `journalctl -u ogg-cbt` 로 봅니다.

### 업데이트
```bash
cd /opt/ogg-cbt/app
sudo -u cbt git pull
sudo -u cbt /opt/ogg-cbt/.venv/bin/pip install -r requirements.txt
sudo systemctl restart ogg-cbt        # 마이그레이션이 자동 적용됨
```
배포 전에 **백업을 먼저** 받으세요(8절). 마이그레이션이 실패하면 서비스가 기동하지 않으니 `journalctl` 로 원인을 확인하고, 필요하면 백업으로 복구합니다.
시험 진행 중에 재시작하면 응시자는 잠깐 오류를 볼 수 있으나, 진행 중인 시험은 DB 에 저장되어 있어 재접속하면 이어집니다. 가능하면 시험이 없는 시간에 배포하세요.

## 2. 로컬 개발 실행

Python 3.10 이상(3.10 ~ 3.13 에서 전체 테스트 통과 확인). 저장소에 올리면 GitHub Actions 가 3.12/3.13 에서 테스트를 자동 실행합니다.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env                 # 로컬 HTTP 개발이므로 .env 에서 COOKIE_SECURE=false 로 바꿉니다

alembic upgrade head                 # DB 생성 (data/ 폴더는 자동 생성)
python -m app.cli seed --samples     # 영역 + 기본 Blueprint + 개발용 샘플 문항 64개
python -m app.cli create-admin admin "관리자"
uvicorn app.main:create_app --factory --reload --port 8000
```

http://127.0.0.1:8000 에 접속합니다. 관리자는 로그인 후 `/admin`, 응시자는 `/` 로 이동합니다.

> **샘플 문항 주의**: `--samples` 문항 64개는 AI가 작성한 개발 확인용 초안이며 **기술적 정확성이 검증되지 않았습니다.**
> 운영 DB 에는 `--samples` 없이 seed 하세요(서비스 시작 시 자동 seed 는 샘플 없이 실행됩니다). 샘플은 화면에 "샘플(검수 필요)" 배지가 붙습니다.

## 3. 처음 운영을 시작하는 순서

**승인된 문항이 충분히 쌓이기 전에는 응시자가 시험을 시작할 수 없습니다.** 아래 순서를 지켜 주세요.

1. **관리자 계정 생성** ([4절](#4-관리자-계정-만들기))
2. **문항 등록** — 관리자 화면 `문항` 또는 `문항 업로드`. 등록된 문항은 모두 **DRAFT** 입니다.
   문항의 영역 코드가 과목을 정합니다(아래 [6절](#6-문항-업로드)의 영역 표).
3. **검수·승인** — 문항 상세에서 `검수 요청(REVIEWED)` → `승인(APPROVED)`. 반려하면 사유가 기록됩니다.
   (작성자 본인 승인을 막으려면 `설정`에서 "작성자·승인자 분리"를 켭니다.)
4. **Blueprint 가용 문항 확인** — `Blueprint` 화면의 영역×난이도 표에서 **빨간 셀(문항 부족)** 이 없는지 확인합니다.
   기본 Blueprint 는 OGG 용 40문항(LOW 12 / MID 20 / HIGH 8, 배점 2.0 / 2.5 / 3.25 = 100점)이며, 영역 7개 × 난이도 3개 셀마다 문항 수가 정해져 있습니다.
   **활성 Blueprint 는 과목마다 1개**입니다. Oracle Database·WebLogic 은 기본 Blueprint 가 없으므로, 문항을 승인한 뒤
   `Blueprint` 화면의 `새 Oracle Database Blueprint` 등으로 만들고 활성화해야 응시자 화면에서 '준비 중'이 시험 시작 버튼으로 바뀝니다.
   같은 `variant group` 문항은 한 시험에 1개만 나오므로, **셀별 서로 다른 variant group 수**가 필요 문항 수 이상이어야 합니다.
5. **응시자 등록** ([5절](#5-응시자-등록))
6. 응시자에게 시험 접속 주소와 초기 비밀번호를 안전하게 전달합니다.

문항이 부족하면 응시자에게는 "시험을 구성할 수 없습니다. 관리자에게 문의해 주세요"만 보이고,
부족한 셀 목록은 관리자 화면(Blueprint, 감사 로그의 `EXAM_CREATE_REFUSED`)에서 확인합니다.

### 시험 규칙 요약
* 합격: **총점 70점 이상 그리고 모든 영역 득점률 40% 이상**(과락). 값은 Blueprint 에서 바꿉니다.
* MULTI(복수 정답)는 완전 일치만 정답입니다(Blueprint 옵션으로 부분 점수 허용 가능).
* 최근 2회 시험에 나온 문항은 우선 제외하되, 후보가 부족하면 완화하고 감사 로그(`EXAM_SELECTION_RELAXED`)에 남깁니다.
* 동시 응시는 과목과 관계없이 1건뿐이고, 새로고침·재접속하면 진행 중인 시험을 이어서 봅니다.
* 최근 출제 제외는 같은 과목의 시험만 셉니다.
* **한 계정은 한 곳에서만 로그인됩니다.** 다른 PC 에서 같은 아이디로 로그인하면 이전 PC 는 다음 동작 때 로그인 화면으로 이동하며
  '다른 곳에서 같은 아이디로 로그인하여 로그아웃되었습니다'를 보여 줍니다(감사 로그 `SESSION_REPLACED`). 시험은 새 PC 에서 이어서 봅니다.
  이전 PC 에서 마지막 자동 저장 이후 고른 답은 저장되지 않습니다. 로그아웃·비밀번호 변경·관리자 비밀번호 재설정도 다른 곳의 로그인을 끊습니다.
* 시간 제한은 서버 기준이며, 만료되면 자동 제출됩니다. 정답과 해설은 제출 전에는 절대 브라우저로 전송되지 않습니다.

### 합격증
* 합격한 응시자는 **응시 이력**과 **결과 화면**에서 '합격증 PDF'를 내려받습니다. 관리자는 결과 상세에서 내려받을 수 있습니다.
* 처음 내려받을 때 인증번호(`RP-과목코드-합격일-무작위6자리`, 예: `RP-OGG-…`, `RP-ORADB-…`, `RP-WLS-…`)가 발급되고, 합격증의 QR 코드나 **`/verify`** 에서 누구나 진위를 확인할 수 있습니다
  (성명·자격·합격일만 표시, 아이디·점수는 표시하지 않음).
* 채점 보정으로 불합격이 되거나 계정이 삭제되면 그 합격증은 진위 확인에서 '확인되지 않음'으로 나옵니다.
* 자격명은 과목 이름으로 정해집니다(`Oracle GoldenGate 사내 자격시험`, `Oracle Database 사내 자격시험`, `Oracle WebLogic Server 사내 자격시험`).
  발급 기관은 `.env` 의 `CERTIFICATE_ISSUER`(기본 `Rockplace DI팀`)로 바꿀 수 있습니다.
  사내 자격 인증이므로 Oracle 로고·서명은 쓰지 않으며, 공식 자격증이 아니라는 안내 문구가 들어갑니다.
* 한글 글꼴은 나눔 글꼴(SIL OFL 1.1, `app/assets/fonts/OFL.txt`)을 함께 배포합니다.

## 4. 관리자 계정 만들기

관리자 계정은 화면이 아니라 **CLI 로만** 만듭니다.

```bash
# 서버(OCI): 서비스와 같은 설정을 읽는 도우미 스크립트
sudo -u cbt /opt/ogg-cbt/app/deploy/cbt-cli.sh create-admin <login_id> "<이름>"
# 로컬 개발
python -m app.cli create-admin <login_id> "<이름>"
```

비밀번호는 프롬프트로 입력합니다(10자 이상, 화면에 표시되지 않음). 같은 login_id 가 있으면 거부됩니다.

**관리자 비밀번호를 잊었을 때**(관리자 계정은 화면에서 재설정할 수 없습니다):

```bash
sudo -u cbt /opt/ogg-cbt/app/deploy/cbt-cli.sh reset-password <login_id>   # 로컬: python -m app.cli reset-password <login_id>
```

새 비밀번호를 입력하면 해시가 교체되고 로그인 잠금도 풀립니다. 이 작업은 감사 로그(`CLI_PASSWORD_RESET`)에 남습니다(비밀번호는 기록하지 않음).

## 5. 응시자 등록 (이메일 가입 신청 → 관리자 승인)

1. 응시자에게 접속 주소를 알려 줍니다. 응시자는 로그인 화면의 **'가입 신청'** 에서 이메일과 이름을 입력합니다.
2. 관리자는 **응시자** 메뉴의 '가입 승인 대기'에서 **승인** 또는 **반려** 합니다(대시보드에 대기 인원이 표시됩니다).
3. 승인하면 비밀번호(12자, 무작위)가 만들어져 **응시자 이메일로 발송**됩니다. 메일 발송에 실패하면 승인되지 않습니다.
4. 응시자는 이메일을 아이디로 로그인하고, 오른쪽 위 **'비밀번호 변경'** 에서 원하는 비밀번호로 바꿀 수 있습니다(선택).

* **비밀번호를 잊었을 때**: 로그인 화면의 **'비밀번호를 잊으셨나요?'** 에 이메일을 넣으면 재설정 링크가 메일로 갑니다(30분, 1회용).
  링크를 열고 **'새 비밀번호 받기'** 를 누르면 새 비밀번호가 메일로 발송됩니다. 링크를 여는 것만으로는 바뀌지 않습니다(메일 보안 스캐너의 자동 클릭 대비).
* 가입 신청 단계에서는 메일을 보내지 않습니다. 메일은 관리자가 승인할 때만 나가므로, 남의 이메일로 신청해도 그 사람에게 메일이 가지 않습니다.
* 가입 신청·재발급 요청은 계정 존재 여부와 관계없이 같은 안내가 나옵니다. 같은 IP 에서 10분에 5회까지, 재설정 메일은 계정당 1시간에 3회까지입니다.
* 승인 대기·반려 상태에서는 로그인할 수 없고, 반려된 사람은 다시 신청할 수 있습니다.
* 관리자가 **비밀번호 재설정** 버튼으로 새 비밀번호를 만들면 화면에 한 번만 표시됩니다(메일 발송 아님). 잠긴 계정은 **잠금 해제** 할 수 있습니다.
* 관리자 계정은 이 흐름과 무관하게 CLI(`create-admin`, `reset-password`)로만 관리합니다.
* **삭제**: 응시자 목록의 '삭제' → 확인 화면에서 진행합니다. 계정과 그 사람의 응시 기록(답안 포함)이 **완전히 삭제되며 되돌릴 수 없습니다**(결과·합격률·문항 분석에서도 빠짐). 감사 로그는 남기되 계정 연결만 끊습니다. 관리자 계정은 삭제할 수 없습니다.
* 이전의 CSV 일괄 등록 기능은 삭제했습니다.

## 6. 문항 업로드

관리자 화면 `문항 업로드` 에서 JSON 또는 CSV 를 올립니다(최대 2MB, 1,000문항). **검증을 모두 통과해야** 등록되며, 전부 **DRAFT** 로 들어갑니다.
오류가 한 건이라도 있으면 아무것도 등록되지 않고 행 번호별 사유가 표시됩니다(입력값은 되돌려 보여주지 않음).
같은 영역에 지문이 같은 문항(공백·대소문자 무시)이 이미 있거나 파일 안에 중복이 있으면 거부됩니다.

**JSON** (배열 또는 `{"items": [...]}`)

```json
[
  {
    "domain": "EXTRACT",
    "difficulty": "MID",
    "item_type": "SINGLE",
    "product_version": "19c",
    "architecture": "MA",
    "stem": "Integrated Extract 사용 시 소스 DB에서 반드시 선행되어야 하는 설정은?",
    "options": {"A": "...", "B": "...", "C": "...", "D": "...", "E": "..."},
    "correct_keys": ["B"],
    "explanation": "정답 근거와 오답 보기별 오류 이유",
    "reference": "공식 문서명/장 또는 사내 문서명",
    "variant_group": "EXT-INTEGRATED-PREREQ"
  }
]
```

**CSV** (열: `domain,difficulty,item_type,stem,option_A,option_B,[option_C..E],correct_keys,explanation,variant_group,[product_version,architecture,reference]`,
복수 정답은 `A|C`)

| 필드 | 허용 값 |
|---|---|
| domain (OGG) | `ARCH` `INSTALL` `EXTRACT` `REPLICAT` `MAPPING` `OPS` `TROUBLE` |
| domain (Oracle Database) | `DB_ARCH` `DB_INSTALL` `DB_STORAGE` `DB_SQL` `DB_SEC` `DB_BACKUP` `DB_PERF` `DB_HA` |
| domain (WebLogic) | `WLS_ARCH` `WLS_INSTALL` `WLS_DEPLOY` `WLS_JDBC` `WLS_CLUSTER` `WLS_SEC` `WLS_OPS` `WLS_TROUBLE` |
| difficulty | `LOW` `MID` `HIGH` |
| item_type | `SINGLE`(정답 1개) `MULTI`(정답 2개 이상) `SCENARIO`(정답 1개) |
| product_version | 비우면 과목 기본 버전(OGG·DB `19c`, WebLogic `14c`). 예전 이름 `ogg_version` 도 받습니다 |
| architecture | OGG 전용. `CLASSIC` `MA` `BOTH`(기본). 다른 과목은 비워 둡니다 |
| options | 키 `A`~`E` 중 2~5개, 보기 내용 중복 불가 |

영역·과목 목록은 `app/catalog.py` 에 있습니다. 한 파일에 여러 과목 문항을 섞어 올려도 됩니다.

`variant_group` 은 **같은 개념을 묻는 유사 문항의 묶음 이름**입니다. 같은 그룹 문항은 한 시험에 1개만 출제됩니다.

### 문항 수정 정책
* 출제된 적이 있는 문항은 **수정할 수 없습니다.** `신규 버전 만들기` 를 쓰면 원본은 RETIRED, 복사본이 새 DRAFT(v+1)로 생깁니다.
* 출제 이력이 없는 REVIEWED/APPROVED 문항을 수정하면 DRAFT 로 되돌아가 재검수를 거칩니다.
* 문항 오류가 시험 후에 발견되면 문항 상세의 **채점 보정**(전원 정답 인정 / 채점 제외)을 쓰면 영향받는 결과가 자동 재채점됩니다(사유 필수, 변경 전·후가 감사 로그에 기록).

## 7. 설정과 보안

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `SECRET_KEY` | (개발용 기본값) | 세션 서명 키. 운영에서는 필수 변경 |
| `COOKIE_SECURE` | `true` | `true` 면 HTTPS 에서만 쿠키 전송. 로컬 HTTP 는 `false`. 서버는 `/etc/ogg-cbt/env` 에서 설정 |
| `DATABASE_URL` | `sqlite:///./data/cbt.db` | 서버에서는 `sqlite:////var/lib/ogg-cbt/cbt.db` (절대 경로) |
| `SMTP_HOST` / `SMTP_PORT` | - / `587` | 메일 서버(587=STARTTLS, 465=SSL). Gmail 은 `smtp.gmail.com` |
| `SMTP_USER` / `SMTP_PASSWORD` | - | 메일 계정. Gmail 은 계정 비밀번호가 아니라 **앱 비밀번호** |
| `SMTP_FROM` | - | 보내는 사람 주소 |
| `PUBLIC_BASE_URL` | - | 메일 속 링크의 주소 = 응시자가 접속하는 주소(예: `https://cbt.example.com`). 요청 헤더로 만들지 않아 링크 위조를 막습니다 |

메일 설정(`SMTP_HOST`, `SMTP_FROM`, `PUBLIC_BASE_URL`)이 없으면 가입 승인을 할 수 없습니다(승인 버튼 비활성).

적용된 보안 조치: 모든 SQL 은 ORM/바인드 변수, CSRF 토큰(폼·HTMX), 세션 쿠키 HttpOnly·SameSite=Lax·Secure(운영),
엄격한 CSP(`script-src 'self'`, `style-src 'self'`, 인라인 없음), `X-Frame-Options: DENY`, 문항·결과 페이지 `no-store`,
로그인 실패 5회 잠금, 비밀번호 bcrypt, CSV/Excel 내보내기 수식 인젝션 방어, 관리자 행위(문항 승인, Blueprint 변경, 결과 보정,
내보내기, 설정, 계정 관리 등) 감사 로그 기록. 감사 로그는 관리자 화면 `감사 로그` 에서 조회합니다.

화면의 시각은 **KST** 로 표시되고 DB 에는 UTC 로 저장됩니다.

## 8. 백업과 복구

모든 데이터는 SQLite 파일 하나(`/var/lib/ogg-cbt/cbt.db`)에 있습니다. WAL 모드이므로 **실행 중 파일을 그냥 복사하지 말고** 백업 스크립트(SQLite 백업 API)를 쓰세요.

```bash
# 백업 디렉터리 준비(1회)
sudo install -d -o cbt -g cbt -m 700 /var/backups/ogg-cbt

# 수동 백업 (서비스 실행 중에도 안전)
sudo -u cbt /opt/ogg-cbt/app/deploy/backup.sh

# 매일 02:30 자동 백업 + 14일 보관 (cbt 사용자의 crontab: sudo -u cbt crontab -e)
30 2 * * *  /opt/ogg-cbt/app/deploy/backup.sh >> /var/backups/ogg-cbt/backup.log 2>&1
```

**복구**
```bash
sudo systemctl stop ogg-cbt
sudo -u cbt rm -f /var/lib/ogg-cbt/cbt.db-wal /var/lib/ogg-cbt/cbt.db-shm
sudo -u cbt cp /var/backups/ogg-cbt/cbt-YYYY-MM-DD_HHMM.db /var/lib/ogg-cbt/cbt.db
sudo systemctl start ogg-cbt          # 시작 시 마이그레이션이 자동 적용됨
```

* 백업 파일에는 응시자 이름·결과와 비밀번호 해시가 들어 있습니다. 권한을 제한하고(스크립트는 700/600 으로 만듭니다),
  **같은 VM 안에만 두지 말고** OCI Object Storage 등 다른 위치로도 복사해 두세요(디스크 장애 대비).
* `backup.sh` 는 로컬에서 동작(유효한 백업 생성, 오래된 파일 삭제)을 확인했습니다. **복구 절차는 문서화만 했으니 운영 전에 한 번 리허설**하세요.

## 9. 테스트

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium      # 브라우저 E2E 용(없으면 해당 테스트만 건너뜀)

pytest -q --cov=app --cov-branch           # 전체(약 2~5분). 브라우저 E2E·경합 테스트 포함
pytest -q --ignore=tests/test_e2e_browser.py --ignore=tests/test_race_conditions.py   # 빠른 실행(약 1분)
alembic check                              # 모델과 마이그레이션이 일치하는지
```

| 종류 | 파일 | 무엇을 보장하나 |
|---|---|---|
| 단위·서비스 | `test_selection`, `test_grading`, `test_exam_service`, `test_item_admin` 등 | 출제 비율·variant_group·최근 제외·seed 재현성, 채점·과락, 검수 워크플로 |
| 속성 기반(무작위 수백 건) | `test_properties.py` | 출제 결과를 **브루트포스 정답과 비교**(유효한 시험이 있으면 반드시 생성), 채점의 단조성·순서 무관성 등 |
| 정답 비노출 | `test_no_answer_leak.py`, E2E | 제출 전 브라우저가 받는 모든 응답에 정답·해설이 없음 |
| 보안·퍼징 | `test_fuzz_security.py`, `test_web.py` | CSRF, 권한, 세션 위조, SQL/템플릿 인젝션 문자열, 깨진 업로드 파일에서 500 이 나지 않음 |
| 경합 | `test_race_conditions.py` | 실제 서버에서 시작 버튼 동시 클릭 시 시험 1건, 자동 저장·제출이 겹쳐도 결과 1회 확정 |
| 한 곳 로그인 | `test_single_session.py` | 다른 PC 로그인 시 이전 PC 로그아웃, 로그아웃·비밀번호 변경 후 이전 쿠키 무효 |
| 마이그레이션 | `test_migrations_data.py` | 데이터가 있는 구버전 DB 를 올려도 데이터 보존, 재실행·되돌리기 안전 |
| 브라우저 E2E | `test_e2e_browser.py` | 실제 Chromium: 자동 저장, 타이머, 시간 종료 자동 제출, HTMX 이동, **모든 화면 콘솔 오류(CSP 위반) 없음**, 모바일 폭 |

**테스트의 실효성 점검(변이 테스트)**: 핵심 로직에 버그를 하나씩 일부러 넣고 테스트가 실패하는지 확인합니다.

```bash
python tools/mutation_check.py report.json            # 전체 약 101개, 10분 내외. 실행 중에는 파일을 편집하지 마세요
python tools/mutation_check.py report.json GRD-04     # 특정 변이만
```

마지막 측정: 101개 중 96개 검출. 남은 5개는 동작이 같은 동치 변이입니다(합격선·과락선 경계의 `<`/`<=` 는 허용오차 때문에 동일,
탐욕 선택 단계의 일부 변경은 정확 해법 대체 경로가 같은 결과를 내므로 동일).

## 10. 구조와 설계 메모

```
app/
  main.py            앱 팩토리(미들웨어, 보안 헤더, 스케줄러)   실행: uvicorn app.main:create_app --factory
  models/            ORM 모델        schemas/  입력 검증·클라이언트 응답 모델
  services/          출제·채점·응시·문항 관리·분석 등 비즈니스 로직
  routers/           응시자 화면(exam), 관리자 화면(admin_*)
  templates/ static/ Jinja2 템플릿, CSS/JS(htmx 포함)
alembic/             마이그레이션          seeds/  개발용 샘플 문항
deploy/              서버 배포 견본(systemd, nginx, 백업·CLI 스크립트)   .github/workflows/  CI
tools/               변이 테스트 도구(mutation_check.py)
tests/               pytest
```

명세 대비 달라진 점(의도적):
* **JSONB → SQLAlchemy `JSON`**: SQLite 를 쓰므로 공용 JSON 타입을 사용합니다.
* **별도 DB 서버 없음**: SQLite 파일을 서버 디스크(`/var/lib/ogg-cbt`)에 저장합니다.
* **추가 테이블/컬럼**: `item_reviews`(검수 이력·반려 사유), `item_overrides`(채점 보정), `app_settings`,
  문항의 `version`·`previous_version_id`·`is_sample`, 응시의 `domain_scores`·`pass_score`·`domain_min_ratio`(채점 시점 스냅샷),
  `attempt_items.earned_points`·`flagged`.
* **배점**: 기본 Blueprint 의 HIGH 배점은 합계 100점을 맞추기 위해 3.5 가 아니라 **3.25** 입니다(12×2.0 + 20×2.5 + 8×3.25 = 100).
* **문항 초안 생성 모듈(명세 9항, 선택 기능)은 구현하지 않았습니다.** 외부 LLM API 로 데이터를 보내야 하는 기능이라 보류했으며,
  문항은 수동 등록과 JSON/CSV 업로드로 만듭니다. 나중에 추가할 수 있도록 업로드와 같은 스키마 검증(`ItemIn`)을 공용으로 두었습니다.

## 11. 알려진 한계

* 세션은 서명된 쿠키 방식이지만 사용자별 세션 버전으로 **로그아웃·다른 곳 로그인·비밀번호 변경 시 이전 쿠키를 무효화**합니다(유효 최대 8시간).
* **앱 자체에는 IP 단위 로그인 제한이 없습니다.** 계정별 잠금만 있어, 특정 계정을 반복 시도해 잠그는 공격이 가능합니다. 제공된 nginx 설정의 `/login` 속도 제한을 반드시 적용하세요.
* **SQLite 단일 인스턴스**입니다. 다중 서버/워커 확장에는 PostgreSQL 전환이 필요합니다(JSON 타입만 써서 전환은 쉽게 설계).
* **서버 1대 + 파일 DB** 구조라 서버/디스크 장애가 곧 서비스 중단입니다. 정기 백업(8절)과 외부 보관이 필수입니다.
* 관리자 계정의 비밀번호 재설정은 화면이 아니라 CLI(`reset-password`)로만 가능하고, 계정 삭제 기능은 관리자·응시자 모두 없습니다.
* 문항 분석은 종료된 응시를 메모리에 올려 집계합니다. 응시 기록이 수만 건 규모가 되면 SQL 집계로 바꿔야 합니다.
* 우클릭·복사 방지는 보조 수단일 뿐이며, 핵심 보호는 서버 측 정답 비노출입니다.
* 비밀번호는 요청하신 방식대로 **이메일 본문에 평문으로** 발송됩니다. 메일함이 노출되면 비밀번호도 노출되므로, 받은 뒤 변경을 안내하세요.
* 브라우저 확인은 Chromium 에서 정상 흐름 위주로 했습니다(모바일, 다른 브라우저는 미확인).
* 샘플 문항 64개는 AI 초안이라 정확성이 보증되지 않습니다. **운영 문항은 반드시 사내 전문가가 검수하세요.**
