"""간단한 IP 단위 요청 제한(프로세스 메모리). 워커 1개 구조이므로 충분하다.

nginx 뒤에서는 uvicorn 이 X-Forwarded-For 로 실제 접속 IP 를 넣어 준다(같은 서버의 프록시만 신뢰).
"""
import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_sec: int):
        self.limit, self.window = limit, window_sec
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True
