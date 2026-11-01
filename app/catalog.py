"""시험 과목(제품)과 출제 영역 정의. seed 와 문항 스키마 검증이 함께 쓴다.

영역 코드는 제품 간에도 유일해야 한다(업로드 파일의 domain 코드만으로 제품이 정해짐).
"""

# (code, name, default_version, sort_order)
PRODUCTS = [
    ("OGG", "Oracle GoldenGate", "19c", 1),
    ("ORADB", "Oracle Database", "19c", 2),
    ("WLS", "Oracle WebLogic Server", "14c", 3),
]

# 제품 코드 → [(영역 code, name, description)]
DOMAINS = {
    "OGG": [
        ("ARCH", "아키텍처", "GoldenGate 구성요소, Classic/Microservices 아키텍처"),
        ("INSTALL", "설치·환경", "설치, DB 사전 설정, 보충 로깅, 자격증명"),
        ("EXTRACT", "Extract", "Extract/Integrated Extract/Data Pump"),
        ("REPLICAT", "Replicat", "Replicat 유형, 적용 옵션, 충돌 처리"),
        ("MAPPING", "매핑·변환", "MAP/TABLE, COLMAP, 변환 함수, DEFGEN"),
        ("OPS", "운영·모니터링", "상태/지연 모니터링, Manager 운영 파라미터"),
        ("TROUBLE", "트러블슈팅", "오류 분석, 장애 대응"),
    ],
    "ORADB": [
        ("DB_ARCH", "아키텍처", "인스턴스·메모리 구조, 백그라운드 프로세스, CDB/PDB"),
        ("DB_INSTALL", "설치·구성", "설치, DBCA, 초기화 파라미터, 리스너·Net 구성"),
        ("DB_STORAGE", "저장 구조", "테이블스페이스, 데이터 파일, Undo/Redo, ASM"),
        ("DB_SQL", "SQL·PL/SQL", "SQL, 트랜잭션·락, PL/SQL 객체"),
        ("DB_SEC", "사용자·보안", "사용자·권한·롤, 프로파일, 감사, 암호화"),
        ("DB_BACKUP", "백업·복구", "RMAN 백업·복구, Flashback, Data Pump"),
        ("DB_PERF", "성능 튜닝", "AWR/ASH, 실행 계획, 옵티마이저 통계, 대기 이벤트"),
        ("DB_HA", "RAC·Data Guard", "RAC, Data Guard, 고가용성 구성"),
    ],
    "WLS": [
        ("WLS_ARCH", "아키텍처", "도메인, Admin/Managed Server, Node Manager"),
        ("WLS_INSTALL", "설치·도메인 구성", "설치, Configuration Wizard, 도메인 템플릿"),
        ("WLS_DEPLOY", "애플리케이션 배포", "배포 방식, Deployment Plan, 공유 라이브러리"),
        ("WLS_JDBC", "JDBC·데이터소스", "Generic/Multi/GridLink 데이터소스, 커넥션 풀"),
        ("WLS_CLUSTER", "클러스터·HA", "클러스터, 세션 복제, 마이그레이션, 로드밸런싱"),
        ("WLS_SEC", "보안", "보안 영역, 인증 제공자, SSL·KeyStore"),
        ("WLS_OPS", "운영·WLST", "WLST, 모니터링, 로그, 작업 관리자"),
        ("WLS_TROUBLE", "트러블슈팅", "스레드 덤프, Stuck Thread, 메모리 문제 분석"),
    ],
}

DOMAIN_CODES = {code for domains in DOMAINS.values() for code, _, _ in domains}
