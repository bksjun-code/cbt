"""간이 변이 테스트 하니스.

실행: python tools/mutation_check.py [결과.json] [변이ID,변이ID...]
  - 저장소 루트에서 실행. 소스 파일을 하나씩 바꿔 가며 테스트를 돌리므로 실행 중에는 파일을 편집하지 말 것.
  - 끝나면(예외가 나도) 원본으로 복구하고 SHA-256 으로 확인한다.

각 변이: 소스의 문자열 1곳을 의도적으로 틀리게 바꾼 뒤, 관련 테스트가 실패하는지(=변이를 잡았는지) 확인한다.
통과해 버리는 변이(survived)는 테스트가 그 버그를 못 잡는다는 뜻이다.
원본은 변이마다 반드시 복원하고, 마지막에 SHA-256 으로 전부 원복됐는지 검증한다.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "mutation_report.json"

T_SEL = ["tests/test_selection.py", "tests/test_edge_cases.py", "tests/test_exam_service.py"]
T_GRADE = ["tests/test_grading.py", "tests/test_overrides.py"]
T_EXAM = ["tests/test_exam_service.py", "tests/test_grading.py", "tests/test_edge_cases.py"]
T_WEB = ["tests/test_web.py", "tests/test_admin_web_b.py", "tests/test_edge_cases.py"]
T_ITEM = ["tests/test_item_admin.py", "tests/test_admin_items_web.py", "tests/test_item_import.py"]
T_ANALYSIS = ["tests/test_item_analysis.py", "tests/test_analytics_web.py"]
T_BP = ["tests/test_blueprint_admin.py", "tests/test_admin_web_b.py"]
T_USERS = ["tests/test_results_users_admin.py", "tests/test_item_import.py"]
T_LEAK = ["tests/test_no_answer_leak.py", "tests/test_web.py"]

M = []  # (id, 파일, old, new, 테스트)


def m(mid, file, old, new, tests):
    M.append((mid, file, old, new, tests))


# ---- 출제 ----
m("SEL-01 variant_group 중복 허용", "app/services/selection.py", "if it.variant_group in used_groups:", "if False:", T_SEL)
m("SEL-02 최근 제외 무시", "app/services/selection.py", "fresh = [c for c in cands if c.id not in excluded_item_ids]", "fresh = [c for c in cands]", T_SEL)
m("SEL-03 제외 완화 불가", "app/services/selection.py", "stale = [c for c in cands if c.id in excluded_item_ids]", "stale = []", T_SEL)
m("SEL-04 사용한 그룹 미기록", "app/services/selection.py", "used_groups.add(it.variant_group)", "pass", T_SEL)
m("SEL-05 APPROVED 아닌 문항도 후보", "app/services/selection.py", ".where(Item.status == ItemStatus.APPROVED,", ".where(Item.status != ItemStatus.RETIRED,", T_SEL)
m("SEL-06 후보 셔플 제거(탐욕 단계)", "app/services/selection.py", "        rng.shuffle(fresh)\n        rng.shuffle(stale)\n        chosen: list[Item] = []",
  "        rng.shuffle(stale)\n        chosen: list[Item] = []", T_SEL)
m("SEL-07 난이도 정렬 옵션 반전", "app/services/selection.py", "if bp.sort_by_difficulty:", "if not bp.sort_by_difficulty:", T_SEL)
m("SEL-08 셀당 1문항 초과 선택", "app/services/selection.py", "if len(chosen) == r.item_count:", "if len(chosen) == r.item_count + 1:", T_SEL)
m("SEL-09 완화 기록 누락", "app/services/selection.py", "        if reused:\n", "        if False:\n", T_SEL)
m("SEL-10 보기 셔플 제거", "app/services/selection.py", "rng.shuffle(keys)", "pass", T_SEL)
m("SEL-11 최근 이력에 타 사용자 포함", "app/services/selection.py", "ExamAttempt.user_id == user_id, ExamAttempt.status != AttemptStatus.IN_PROGRESS", "ExamAttempt.status != AttemptStatus.IN_PROGRESS", T_SEL)
m("SEL-12 부족 판정에 variant 무시", "app/services/selection.py", "return self.distinct_groups < self.needed", "return self.approved_items < self.needed", T_SEL)
m("SEL-13 셀 처리 순서(여유순) 제거", "app/services/selection.py", "statuses[(r.domain_id, r.difficulty)].distinct_groups - r.item_count,", "0,", T_SEL)
m("SEL-14 완화 기록 수치 오류", "app/services/selection.py", "r.item_count - reused, reused))", "r.item_count, 0))", T_SEL)
m("SEL-15 시드 무시(고정 시드)", "app/services/selection.py", "rng = random.Random(seed)", "rng = random.Random(0)", T_SEL)
# ---- 채점 ----
m("GRD-01 복수 정답 상위집합도 정답", "app/services/grading.py", "if g.selected == g.correct:", "if g.selected >= g.correct:", T_GRADE)
m("GRD-02 영역 최소 경계(=) 과락 처리", "app/services/grading.py", 'd["ratio"] + EPS < domain_min_ratio', 'd["ratio"] + EPS <= domain_min_ratio', T_GRADE)
m("GRD-03 합격선 경계(=) 불합격 처리", "app/services/grading.py", "passed = total + EPS >= pass_score and not failed", "passed = total + EPS > pass_score and not failed", T_GRADE)
m("GRD-04 과락 무시", "app/services/grading.py", "passed = total + EPS >= pass_score and not failed", "passed = total + EPS >= pass_score", T_GRADE)
m("GRD-05 100점 환산 오류", "app/services/grading.py", "total_earned / total_max * 100", "total_earned / total_max * 10", T_GRADE)
m("GRD-06 오답 섞인 부분점수 허용", "app/services/grading.py", "if partial_credit and g.item_type == ItemType.MULTI and g.selected <= g.correct:", "if partial_credit and g.item_type == ItemType.MULTI:", T_GRADE)
m("GRD-07 부분점수 분모 오류", "app/services/grading.py", "g.points * len(g.selected) / len(g.correct)", "g.points * len(g.selected) / len(g.selected)", T_GRADE)
m("GRD-08 채점 제외가 합계에 포함", "app/services/grading.py", "        if g.excluded:\n            continue", "        if False:\n            continue", T_GRADE)
m("GRD-09 배점 0 영역도 과락", "app/services/grading.py", 'if d["max"] > 0 and d["ratio"]', 'if True and d["ratio"]', T_GRADE)
m("GRD-10 정답 인정이 무응답 제외", "app/services/grading.py", "    if g.override == OverrideMode.CREDIT_ALL:\n        return ItemGrade(g.points, True)", "    if g.override == OverrideMode.CREDIT_ALL and g.selected:\n        return ItemGrade(g.points, True)", T_GRADE)
# ---- 응시 ----
m("EXM-01 유예시간 0", "app/services/exam.py", "GRACE = timedelta(seconds=5)", "GRACE = timedelta(seconds=0)", T_EXAM)
m("EXM-02 유예시간 과다", "app/services/exam.py", "GRACE = timedelta(seconds=5)", "GRACE = timedelta(seconds=500)", T_EXAM)
m("EXM-03 단일문항 복수선택 허용", "app/services/exam.py", "if ai.item.item_type != ItemType.MULTI and len(keys) > 1:", "if False:", T_EXAM)
m("EXM-04 존재하지 않는 보기 허용", "app/services/exam.py", "if not set(keys) <= set(ai.option_order):", "if False:", T_EXAM)
m("EXM-05 종료된 시험 수정 허용", "app/services/exam.py", 'raise AttemptClosed("이미 종료된 시험입니다")', "pass", T_EXAM)
m("EXM-06 제한시간 2배", "app/services/exam.py", "expires_at=now + timedelta(minutes=bp.time_limit_min),", "expires_at=now + timedelta(minutes=bp.time_limit_min * 2),", T_EXAM)
m("EXM-07 이어하기 반전", "app/services/exam.py", "if not is_expired(active, now):", "if is_expired(active, now):", T_EXAM)
m("EXM-08 만료 처리 상태 오류", "app/services/exam.py", "finalize_attempt(db, active, AttemptStatus.EXPIRED, active.expires_at)", "finalize_attempt(db, active, AttemptStatus.SUBMITTED, active.expires_at)", T_EXAM)
m("EXM-09 지각 제출 상태 오류", "app/services/exam.py", "return finalize_attempt(db, attempt, AttemptStatus.EXPIRED, attempt.expires_at)\n    return finalize_attempt(db, attempt, AttemptStatus.SUBMITTED, now)", "return finalize_attempt(db, attempt, AttemptStatus.SUBMITTED, attempt.expires_at)\n    return finalize_attempt(db, attempt, AttemptStatus.SUBMITTED, now)", T_EXAM)
m("EXM-10 선택 해제 시 빈 목록 저장", "app/services/exam.py", "ai.selected_keys = sorted(keys) if keys else None", "ai.selected_keys = sorted(keys)", T_EXAM)
m("EXM-11 최근 제외 이력 미적용", "app/services/exam.py", "excluded = recent_item_ids(db, user.id, bp.recent_exclude_n)", "excluded = set()", T_EXAM + ["tests/test_selection.py"])
m("EXM-12 종료 시 재채점(멱등성 깨짐)", "app/services/exam.py", "    if attempt.status != AttemptStatus.IN_PROGRESS:\n        return attempt\n    bp = db.get", "    bp = db.get", T_EXAM)
# ---- 인증/보안 ----
m("SEC-01 잠금 임계값 off-by-one", "app/services/auth.py", "if user.failed_login_count >= settings.login_max_failures:", "if user.failed_login_count > settings.login_max_failures:", T_WEB)
m("SEC-02 잠금 중 로그인 허용", "app/services/auth.py", "if user.locked_until and user.locked_until > now:", "if False:", T_WEB)
m("SEC-03 성공 시 실패 횟수 미초기화", "app/services/auth.py", "        user.failed_login_count = 0\n        user.locked_until = None\n        log_audit(db, user.id, \"LOGIN_OK\"", "        log_audit(db, user.id, \"LOGIN_OK\"", T_WEB)
m("SEC-04 잠금 시간 0분", "app/services/auth.py", "minutes=settings.login_lock_minutes)", "minutes=0)", T_WEB)
m("SEC-05 CSRF 전면 해제", "app/web/deps.py", "if request.method in SAFE_METHODS:", "if True:", T_WEB)
m("SEC-06 CSRF 토큰 값 미비교", "app/web/deps.py", "if not expected or not supplied or not secrets.compare_digest(str(supplied), expected):", "if not expected or not supplied:", T_WEB)
m("SEC-07 관리자가 응시 가능", "app/web/deps.py", "if user.role != Role.CANDIDATE:", "if False:", T_WEB)
m("SEC-08 응시자가 관리자 화면 접근", "app/web/deps.py", "if user.role != Role.ADMIN:", "if False:", T_WEB)
m("SEC-09 타인 시험 접근(IDOR)", "app/routers/exam.py", "if attempt is None or attempt.user_id != user.id:", "if attempt is None:", T_WEB)
m("SEC-10 세션 쿠키 SameSite 제거", "app/main.py", 'same_site="lax"', 'same_site="none"', T_WEB)
m("SEC-11 보안 헤더 CSP 제거", "app/main.py", 'resp.headers.setdefault("Content-Security-Policy", CSP)', "pass", T_WEB)
# ---- 정답 비노출 ----
m("LEAK-01 응시 뷰 extra 허용", "app/schemas/exam.py", 'model_config = ConfigDict(extra="forbid", frozen=True)', "model_config = ConfigDict(frozen=True)", T_LEAK)
m("LEAK-02 응시 뷰에 해설 필드 추가", "app/schemas/exam.py", "    flagged: bool\n\n\nclass AttemptView", "    flagged: bool\n    explanation: str = ''\n\n\nclass AttemptView", T_LEAK)
m("LEAK-03 제출 전 결과 조회 허용", "app/services/results.py", "if attempt.status == AttemptStatus.IN_PROGRESS:\n        raise ResultNotAvailable", "if False:\n        raise ResultNotAvailable", T_LEAK)
m("LEAK-04 해설 비공개 설정 무시", "app/services/results.py", "    if visible:\n", "    if True:\n", T_LEAK)
m("LEAK-05 채점 제외 문항이 오답 해설에 노출", "app/services/results.py", "if ai.is_correct is not False:", "if ai.is_correct:", ["tests/test_overrides.py", "tests/test_no_answer_leak.py"])
# ---- 문항 관리 ----
m("ITM-01 출제 이력 문항 수정 허용", "app/services/item_admin.py", "    if has_history(db, item.id):\n        raise WorkflowError(\"출제 이력이 있는 문항은 수정할 수 없습니다.", "    if False:\n        raise WorkflowError(\"출제 이력이 있는 문항은 수정할 수 없습니다.", T_ITEM)
m("ITM-02 승인 문항 수정 후 재검수 생략", "app/services/item_admin.py", "if prev_status in (S.REVIEWED, S.APPROVED):", "if False:", T_ITEM)
m("ITM-03 DRAFT 에서 바로 승인", "app/services/item_admin.py", '"approve": ({S.REVIEWED}, S.APPROVED', '"approve": ({S.DRAFT, S.REVIEWED}, S.APPROVED', T_ITEM)
m("ITM-04 반려 사유 불필요", "app/services/item_admin.py", 'if action == "reject" and not reason:', "if False:", T_ITEM)
m("ITM-05 작성자·승인자 분리 무시", "app/services/item_admin.py", 'get_setting(db, "require_separate_approver", False)', "False", T_ITEM + ["tests/test_admin_web_b.py"])
m("ITM-06 폐기본과도 중복 판정", "app/services/item_admin.py", "q = select(Item).where(Item.domain_id == domain_id, Item.status != S.RETIRED)", "q = select(Item).where(Item.domain_id == domain_id)", T_ITEM)
m("ITM-07 업로드 all-or-nothing 해제", "app/services/item_import.py", "    if result.errors:\n        log_audit(db, actor.id, \"ITEM_IMPORT_REJECTED\"", "    if False:\n        log_audit(db, actor.id, \"ITEM_IMPORT_REJECTED\"", T_USERS)
m("ITM-08 업로드 크기 상한 제거", "app/services/item_import.py", "if len(raw) > MAX_BYTES:", "if False:", T_USERS)
# ---- Blueprint/분석/기타 ----
m("BPR-01 배점 합계 검증 제거", "app/services/blueprint_admin.py", "elif abs(f.total_points - TOTAL_POINTS) > EPS:", "elif False:", T_BP)
m("BPR-02 부족 셀 활성화 허용", "app/services/blueprint_admin.py", "    if short:\n        raise BlueprintError(\"문항이 부족한", "    if False:\n        raise BlueprintError(\"문항이 부족한", T_BP)
m("BPR-03 기존 활성 Blueprint 유지", "app/services/blueprint_admin.py", "db.execute(update(Blueprint).where(Blueprint.id != bp.id).values(is_active=False))", "pass", T_BP)
m("ANA-01 집단 크기 50%", "app/services/item_analysis.py", "return max(1, round(n * GROUP_RATIO))", "return max(1, round(n * 0.5))", T_ANALYSIS)
m("ANA-02 상위 집단 정답률 오계산", "app/services/item_analysis.py", "st.p_upper = sum(flags[i] for i in upper) / k", "st.p_upper = sum(flags[i] for i in lower) / k", T_ANALYSIS)
m("ANA-03 표본 경계(=) 부족 처리", "app/services/item_analysis.py", "st.enough = st.n >= min_sample", "st.enough = st.n > min_sample", T_ANALYSIS)
m("ANA-04 변별도 경고 기준 0.1", "app/services/item_analysis.py", "D_WARN = 0.2", "D_WARN = 0.1", T_ANALYSIS)
m("ANA-05 총점 정렬 방향 반전", "app/services/item_analysis.py", "key=lambda i: (-rows[i][0], rows[i][1]))", "key=lambda i: (rows[i][0], rows[i][1]))", T_ANALYSIS)
m("ANA-06 정답 판정이 부분집합 허용", "app/services/item_analysis.py", "return bool(selected) and set(selected) == set(correct)", "return bool(selected) and set(selected) <= set(correct)", T_ANALYSIS)
m("EXP-01 수식 인젝션 방어에서 @ 누락", "app/services/results_admin.py", 'FORMULA_PREFIXES = ("=", "+", "-", "@", "\\t", "\\r")', 'FORMULA_PREFIXES = ("=", "+", "-", "\\t", "\\r")', T_USERS)
m("OVR-01 보정 후 재채점 안 함", "app/services/grade_overrides.py", "    res = _regrade_item_attempts(db, item.id)\n    log_audit(db, actor.id, \"RESULT_OVERRIDE_SET\"", "    res = OverrideResult()\n    log_audit(db, actor.id, \"RESULT_OVERRIDE_SET\"", ["tests/test_overrides.py"])
m("OVR-02 보정 사유 불필요(설정)", "app/services/grade_overrides.py", "    reason = (reason or \"\").strip()\n    if not reason:\n        raise OverrideError(\"보정 사유를 입력해야 합니다\")", "    reason = (reason or \"\").strip()", ["tests/test_overrides.py", "tests/test_admin_web_b.py"])
m("TIM-01 KST 일자 경계 오류", "app/services/timeutil.py", "datetime.combine(d, time.min, tzinfo=KST)", "datetime.combine(d, time.min, tzinfo=timezone.utc)", ["tests/test_results_users_admin.py"])

# ================= 2차: 이번에 고친 코드 + JS/템플릿(브라우저 E2E) =================
T_EXACT = ["tests/test_properties.py::test_known_counterexamples_are_now_solved_for_every_seed",
           "tests/test_properties.py::test_exact_fallback_picks_the_not_recently_used_item_within_a_group",
           "tests/test_properties.py::test_exact_fallback_prefers_not_recently_used_items_and_reports_reuse",
           "tests/test_properties.py::test_refusal_is_correct_exam_is_refused_only_when_no_valid_exam_exists"]
T_FUZZ = ["tests/test_fuzz_security.py", "tests/test_edge_cases.py"]
E2E = "tests/test_e2e_browser.py::"

m("SEL-16 정확 해법(매칭) 대체 경로 비활성", "app/services/selection.py", "exact = _exact_assignment(ordered, cells, excluded_item_ids, rng)", "exact = None", T_EXACT)
m("SEL-17 매칭에서 재배정(증강 경로) 제거", "app/services/selection.py", "if g not in owner or augment(owner[g], seen):", "if g not in owner:", T_EXACT)
m("SEL-18 대체 경로가 최근 문항 우선 회피 안 함", "app/services/selection.py", "pool = [i for i in items if i.id not in excluded] or items", "pool = items", T_EXACT)
m("SEL-20 대체 경로 그룹 셔플 제거(해 고착)", "app/services/selection.py", "        rng.shuffle(fresh)\n        rng.shuffle(stale)\n        slots.extend",
  "        slots.extend", T_EXACT + ["tests/test_properties.py::test_exact_assignment_itself_explores_all_solutions_across_seeds"])
m("SEL-19 대체 경로 재사용 기록 누락", "app/services/selection.py", "reused = sum(1 for it in chosen if it.id in excluded_item_ids)", "reused = 0", T_EXACT)
m("IMP-01 JSON 중첩/숫자 폭탄 처리 제거", "app/services/item_import.py", "except (RecursionError, ValueError):", "except ZeroDivisionError:", T_FUZZ)
m("IMP-02 문항 CSV csv.Error 처리 제거", "app/services/item_import.py", "except csv.Error as e:  # 필드 크기 한도 초과, NUL 문자 등", "except ZeroDivisionError as e:", T_FUZZ)
m("CLI-01 bcrypt 72바이트 검사 제거", "app/cli.py", 'if len(pw.encode("utf-8")) > MAX_PASSWORD_BYTES:', "if False:", ["tests/test_edge_cases.py"])
m("HTTP-01 OverflowError 404 처리 제거", "app/main.py", "@app.exception_handler(OverflowError)", "@app.exception_handler(ZeroDivisionError)", T_FUZZ)
m("FLT-01 enum 필터 검증 제거", "app/services/item_admin.py", "if difficulty in Difficulty.__members__:", "if difficulty:", T_FUZZ)
m("PAGE-01 페이지 상한 제거", "app/routers/admin_items.py", "page = min(max(page, 1), 100_000)", "page = max(page, 1)", T_FUZZ)
m("SCH-01 stem 길이 제한 제거", "app/schemas/item.py", "stem: str = Field(max_length=4000)", "stem: str", T_FUZZ)
m("SCH-02 variant_group 길이 제한 제거", "app/schemas/item.py", "variant_group: str = Field(max_length=100)", "variant_group: str", T_FUZZ)
m("SES-01 세션 서명 검증 약화(고정 키)", "app/main.py", "secret_key=settings.secret_key.get_secret_value(), session_cookie=", "secret_key='fixed-key', session_cookie=", T_FUZZ)
m("MIME-01 .js MIME 타입 text/plain", "app/main.py", 'mimetypes.add_type("text/javascript", ".js")', 'mimetypes.add_type("text/plain", ".js")', ["tests/test_web.py"])
# --- 브라우저 E2E(JS/템플릿) ---
m("JS-01 시간 종료 자동 제출 제거", "app/static/js/app.js", "if (left <= 0 && !submitting) {", "if (false) {", [E2E + "test_time_up_triggers_automatic_submission_without_confirm"])
m("JS-02 선택 해제 후 저장 이벤트 제거", "app/static/js/app.js", 'if (changed) form.dispatchEvent(new Event("change", { bubbles: true }));', "", [E2E + "test_clear_selection_button_unselects_and_saves"])
m("JS-03 복사/우클릭 방지 제거", "app/static/js/app.js", '["contextmenu", "copy", "cut", "dragstart"].forEach(function (ev) {', "[].forEach(function (ev) {", [E2E + "test_copy_and_context_menu_are_blocked_only_inside_the_exam_body"])
m("JS-04 제출 확인 없이 바로 제출", "app/static/js/app.js", "        if (submitting) return; // 시간 종료 자동 제출은 확인 없이\n        e.preventDefault();", "        return;", [E2E + "test_submit_confirm_dialog_cancel_then_accept"])
m("JS-06 팝업에서 취소해도 제출", "app/static/js/app.js", 'if (dlg.returnValue === "confirm" && !submitting) finalSubmit();', "if (!submitting) finalSubmit();", [E2E + "test_submit_confirm_dialog_cancel_then_accept"])
m("CSS-01 팝업 가운데 정렬 깨짐", "app/static/css/app.css", "dialog.modal { border: 0;", "dialog.modal { margin: 0; border: 0;", [E2E + "test_submit_popup_is_centered_on_screen_and_dims_the_background"])
m("JS-05 서버 남은 시간 보정 제거", "app/static/js/app.js", "if (!isNaN(r)) deadline = Date.now() + r * 1000;", "if (false) deadline = Date.now() + r * 1000;", [E2E + "test_timer_counts_down_and_starts_near_the_time_limit"])
m("TPL-01 base.html 에 인라인 style 추가(CSP 위반)", "app/templates/base.html", "<body hx-headers=", '<body style="margin:0" hx-headers=', [E2E + "test_login_start_and_exam_page_render_without_console_errors"])
m("TPL-02 htmx 인디케이터 스타일 주입 허용(CSP 위반)", "app/templates/base.html", """<meta name="htmx-config" content='{"includeIndicatorStyles": false}'>""", "", [E2E + "test_login_start_and_exam_page_render_without_console_errors"])
m("TPL-03 선택 변경 시 자동 저장 트리거 제거", "app/templates/_panel.html", 'hx-trigger="change, every 30s"', 'hx-trigger="every 30s"', [E2E + "test_selecting_an_option_autosaves_and_persists_after_reload"])
m("TPL-04 문항 이동을 전체 새로고침으로 변경", "app/templates/_panel.html", 'hx-get="/exam/{{ attempt.id }}/q/{{ prev_seq }}" hx-target="#panel" hx-swap="outerHTML"', 'hx-get="/exam/{{ attempt.id }}/q/{{ prev_seq }}" hx-target="body" hx-swap="outerHTML"', [E2E + "test_htmx_navigation_swaps_panel_without_full_page_reload"])
m("TPL-05 응시 화면에 해설 표시(정답 노출)", "app/templates/_panel.html", '<p class="stem">{{ q.stem }}</p>', '<p class="stem">{{ q.stem }}</p><p class="explain">{{ attempt.items[q.seq - 1].item.explanation }}</p>', [E2E + "test_browser_never_receives_answers_or_explanations_before_submit"])

ONLY = sys.argv[2].split(",") if len(sys.argv) > 2 else None


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(tests, timeout=240):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    t0 = time.time()
    try:
        r = subprocess.run([PY, "-B", "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider", *tests],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
        return r.returncode, time.time() - t0, (r.stdout + r.stderr)[-400:]
    except subprocess.TimeoutExpired:
        return 124, time.time() - t0, "TIMEOUT"


files = sorted({ROOT / f for _, f, _, _, _ in M})
originals = {p: p.read_bytes() for p in files}
hashes = {p: sha(p) for p in files}
results = []

try:
    # 기준선: 변이 없이 각 테스트 묶음이 통과해야 한다
    groups = {tuple(t) for *_, t in M}
    base_ok = True
    for g in sorted(groups):
        rc, dt, tail = run(list(g))
        if rc != 0:
            base_ok = False
            print("기준선 실패:", g, tail)
    if not base_ok:
        sys.exit("기준선이 깨져 있어 중단")
    print("기준선 통과, 변이", len(M), "개 실행", flush=True)

    for mid, f, old, new, tests in M:
        if ONLY and mid.split()[0] not in ONLY:
            continue
        p = ROOT / f
        src = originals[p].decode("utf-8")
        if src.count(old) < 1:
            results.append({"id": mid, "status": "NOT_APPLIED"})
            print("미적용(문자열 없음):", mid, flush=True)
            continue
        try:
            p.write_bytes(src.replace(old, new, 1).encode("utf-8"))
            rc, dt, tail = run(tests)
        finally:
            p.write_bytes(originals[p])
        status = "KILLED" if rc != 0 else "SURVIVED"
        results.append({"id": mid, "status": status, "secs": round(dt, 1), "tests": tests if status == "SURVIVED" else None,
                        "tail": tail if rc == 124 else None})
        print(f"{status:9} {mid}  ({dt:.1f}s)", flush=True)
finally:
    for p, data in originals.items():
        p.write_bytes(data)
    bad = [str(p) for p in files if sha(p) != hashes[p]]
    summary = {"restored_ok": not bad, "mismatched": bad}
    OUT.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("원복 검증:", "OK (모든 파일 해시 일치)" if not bad else bad, flush=True)
