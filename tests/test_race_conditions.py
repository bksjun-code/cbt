"""경합 테스트(실제 uvicorn 서버): 한 사람의 요청이 동시에 겹치는 경우.

  - 시험 시작 버튼 더블클릭/여러 탭 → 시험은 1건만
  - 자동 저장과 최종 제출이 겹쳐도 결과가 한 번만 확정
"""
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from sqlalchemy import select

from app.models import AttemptItem, AttemptStatus, ExamAttempt, Role, User
from app.services.security import hash_password
from tests.test_web import PW, env  # noqa: F401

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
N_CANDIDATES = 2  # load00, load01


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def live(env):  # noqa: F811
    build, Session = env
    with Session() as db:
        for i in range(N_CANDIDATES):
            db.add(User(login_id=f"load{i:02d}", name=f"부하{i}", password_hash=hash_password(PW), role=Role.CANDIDATE))
        db.commit()
    app = build()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "서버가 기동하지 않았습니다"
    yield f"http://127.0.0.1:{port}", Session
    server.should_exit = True
    t.join(timeout=15)


def login(client: httpx.Client, login_id: str) -> None:
    tok = CSRF.search(client.get("/login").text).group(1)
    r = client.post("/login", data={"login_id": login_id, "password": PW, "csrf_token": tok})
    assert r.status_code == 303, r.text[:200]


def page_token(client: httpx.Client) -> str:
    return CSRF.search(client.get("/").text).group(1)


def test_double_click_on_start_creates_exactly_one_attempt(live):
    """같은 사용자의 '시험 시작' 요청이 동시에 여러 번 들어와도(더블클릭, 여러 탭) 시험은 1건만 만들어져야 한다."""
    base, Session = live
    with httpx.Client(base_url=base) as c:
        login(c, "load00")
        tok = page_token(c)  # 토큰 발급 요청이 세션 쿠키를 갱신하므로 쿠키는 그 다음에 복사한다
        cookies = dict(c.cookies)
    results, errors = [], []
    barrier = threading.Barrier(10)

    def click():
        try:
            with httpx.Client(base_url=base, cookies=cookies, timeout=60) as cc:
                barrier.wait(timeout=30)
                r = cc.post("/exam/start", data={"csrf_token": tok})
                results.append((r.status_code, r.headers.get("location")))
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=click) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not errors, errors
    assert all(code == 303 for code, _ in results), results
    assert len({loc for _, loc in results}) == 1                       # 모두 같은 시험으로 안내
    with Session() as db:
        uid = db.scalar(select(User).where(User.login_id == "load00")).id
        rows = list(db.scalars(select(ExamAttempt).where(ExamAttempt.user_id == uid)))
        assert len(rows) == 1 and rows[0].status == AttemptStatus.IN_PROGRESS
        assert db.query(AttemptItem).filter(AttemptItem.attempt_id == rows[0].id).count() == 40


def test_concurrent_submit_and_save_on_same_attempt_stays_consistent(live):
    """제출과 답안 저장, 중복 제출이 동시에 와도 500 이 없고 채점 결과는 한 번만 확정되어 이후 바뀌지 않는다."""
    base, Session = live
    with httpx.Client(base_url=base) as c:
        login(c, "load01")
        r = c.post("/exam/start", data={"csrf_token": page_token(c)})
        aid = int(r.headers["location"].rsplit("/", 1)[1])
        tok = page_token(c)  # 토큰 발급 요청이 세션 쿠키를 갱신하므로 쿠키는 그 다음에 복사한다
        cookies = dict(c.cookies)
    statuses, errors = [], []
    barrier = threading.Barrier(8)

    def worker(kind: int):
        try:
            with httpx.Client(base_url=base, cookies=cookies, timeout=60) as cc:
                barrier.wait(timeout=30)
                if kind % 2 == 0:
                    r = cc.post(f"/exam/{aid}/submit", data={"csrf_token": tok})
                else:
                    r = cc.put(f"/exam/{aid}/q/{kind}/answer", data={"selected": "A"},
                               headers={"HX-Request": "true", "X-CSRF-Token": tok})
                statuses.append(r.status_code)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not errors, errors
    assert all(s < 500 for s in statuses), statuses
    with Session() as db:
        a = db.get(ExamAttempt, aid)
        assert a.status == AttemptStatus.SUBMITTED and a.total_score is not None
        first = (a.total_score, a.submitted_at, a.passed)
    with httpx.Client(base_url=base, cookies=cookies) as cc:
        cc.post(f"/exam/{aid}/submit", data={"csrf_token": tok})
        cc.put(f"/exam/{aid}/q/1/answer", data={"selected": "B"}, headers={"HX-Request": "true", "X-CSRF-Token": tok})
    with Session() as db:
        a = db.get(ExamAttempt, aid)
        assert (a.total_score, a.submitted_at, a.passed) == first      # 제출 후 변경 불가
