"""관리 CLI.

  python -m app.cli seed [--samples]
  python -m app.cli create-admin <login_id> <name>
  python -m app.cli reset-password <login_id>      (관리자 포함 모든 계정, 잠금도 해제)
"""
import argparse
import getpass
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Role, User
from app.seed import run_seed
from app.services.audit import log_audit
from app.services.security import hash_password

MIN_PASSWORD_LEN = 10
MAX_PASSWORD_BYTES = 72  # bcrypt 한도(초과분은 무시되거나 오류가 되므로 처음부터 막는다). 한글은 1글자 3바이트


def _ask_password() -> str | None:
    pw = getpass.getpass(f"비밀번호({MIN_PASSWORD_LEN}자 이상): ")
    if len(pw.encode("utf-8")) > MAX_PASSWORD_BYTES:
        print(f"비밀번호가 너무 깁니다(UTF-8 {MAX_PASSWORD_BYTES}바이트 이하, 한글은 글자당 3바이트)", file=sys.stderr)
        return None
    if len(pw) < MIN_PASSWORD_LEN or pw != getpass.getpass("비밀번호 확인: "):
        print("비밀번호가 짧거나 일치하지 않습니다", file=sys.stderr)
        return None
    return pw


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="영역/기본 Blueprint 적재")
    s.add_argument("--samples", action="store_true", help="개발용 샘플 문항 적재(운영 금지)")
    a = sub.add_parser("create-admin", help="관리자 계정 생성")
    a.add_argument("login_id")
    a.add_argument("name")
    r = sub.add_parser("reset-password", help="계정 비밀번호 재설정(관리자 포함, 잠금 해제)")
    r.add_argument("login_id")
    args = p.parse_args(argv)

    with SessionLocal() as db:
        if args.cmd == "seed":
            print(run_seed(db, samples=args.samples))
            return 0

        if args.cmd == "reset-password":
            user = db.scalar(select(User).where(User.login_id == args.login_id))
            if user is None:
                print("존재하지 않는 login_id 입니다", file=sys.stderr)
                return 1
            pw = _ask_password()
            if pw is None:
                return 1
            user.password_hash = hash_password(pw)
            user.session_version += 1
            user.failed_login_count = 0
            user.locked_until = None
            log_audit(db, None, "CLI_PASSWORD_RESET", user.login_id, {"role": user.role.value})  # 비밀번호는 기록하지 않는다
            db.commit()
            print("비밀번호를 재설정했습니다")
            return 0

        if db.scalar(select(User).where(User.login_id == args.login_id)):
            print("이미 존재하는 login_id 입니다", file=sys.stderr)
            return 1
        pw = _ask_password()
        if pw is None:
            return 1
        db.add(User(login_id=args.login_id, name=args.name, password_hash=hash_password(pw), role=Role.ADMIN))
        log_audit(db, None, "CLI_ADMIN_CREATED", args.login_id)
        db.commit()
        print("관리자 생성 완료")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
