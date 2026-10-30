"""브라우저 E2E 테스트(Playwright + 실제 Chromium + 실제 uvicorn 서버).

pytest 의 TestClient 는 JS 를 실행하지 않으므로 자동 저장·타이머·자동 제출·HTMX 이동·CSP 위반은 여기서만 검증된다.
Chromium 이 없으면 건너뛴다(설치: python -m playwright install chromium).
"""
import json
import re
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

sync_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect  # noqa: E402

import uvicorn  # noqa: E402

from app.models import AttemptItem, ExamAttempt, ItemType, utcnow  # noqa: E402
from tests.test_race_conditions import free_port  # noqa: E402
from tests.test_web import PW, env  # noqa: E402, F401


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium 을 실행할 수 없습니다: {str(e)[:120]}")
        yield b
        b.close()


@pytest.fixture()
def server(env):  # noqa: F811
    build, Session = env
    app = build()
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    assert srv.started
    yield f"http://127.0.0.1:{port}", Session
    srv.should_exit = True
    t.join(timeout=15)


@pytest.fixture()
def page(browser, server):
    ctx = browser.new_context(base_url=server[0], locale="ko-KR", viewport={"width": 1100, "height": 800})
    pg = ctx.new_page()
    pg.errors = []
    # 콘솔 error(CSP 위반, 스크립트 로드 실패, JS 예외 포함)를 모두 수집한다
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.set_default_timeout(8000)
    yield pg
    ctx.close()


def sign_in(pg, login_id="cand", password=PW):
    pg.goto("/login")
    pg.fill("input[name=login_id]", login_id)
    pg.fill("input[name=password]", password)
    pg.click("button.primary")


def begin_exam(pg) -> int:
    sign_in(pg)
    pg.click("text=시험 시작")
    pg.wait_for_url(re.compile(r"/exam/\d+$"))
    return int(pg.url.rsplit("/", 1)[1])


def seconds(text: str) -> int:
    m, s = text.strip().split(":")
    return int(m) * 60 + int(s)


# ================= 응시 흐름 =================
def test_login_start_and_exam_page_render_without_console_errors(page):
    begin_exam(page)
    expect(page.locator("h2").first).to_contain_text("문제 1 / 40")
    expect(page.locator("#timer")).to_have_text(re.compile(r"^\d\d:\d\d$"))
    assert page.locator(".nav-item").count() == 40
    assert page.errors == [], page.errors


def test_timer_counts_down_and_starts_near_the_time_limit(page):
    begin_exam(page)
    t1 = seconds(page.locator("#timer").inner_text())
    assert 59 * 60 <= t1 <= 60 * 60
    page.wait_for_timeout(2300)
    t2 = seconds(page.locator("#timer").inner_text())
    assert 1 <= t1 - t2 <= 4, (t1, t2)


def test_selecting_an_option_autosaves_and_persists_after_reload(page):
    begin_exam(page)
    page.locator("label.opt").nth(2).click()
    expect(page.locator("#save-status")).to_contain_text("저장됨")
    expect(page.locator("#navigator")).to_have_attribute("data-unanswered", "39")
    expect(page.locator(".nav-item.answered")).to_have_count(1)
    page.reload()
    expect(page.locator("input[name=selected]:checked")).to_have_count(1)
    assert page.locator("label.opt").nth(2).locator("input").is_checked()
    assert page.errors == [], page.errors


def test_htmx_navigation_swaps_panel_without_full_page_reload(page):
    begin_exam(page)
    page.evaluate("window.__marker = 12345")
    page.click("a.button.primary:has-text('다음')")
    expect(page).to_have_url(re.compile(r"\?q=2$"))
    expect(page.locator("h2").first).to_contain_text("문제 2 / 40")
    assert page.evaluate("window.__marker") == 12345, "전체 페이지가 새로고침됨(HTMX 교체가 아님)"
    page.click("a.button:has-text('이전')")
    expect(page.locator("h2").first).to_contain_text("문제 1 / 40")
    for sel in ("#timer", "#submit-form", ".topbar", "#panel"):  # 패널만 바뀌고 나머지 화면은 유지
        assert page.locator(sel).count() == 1, sel
    page.click(".nav-item >> text='7'")
    expect(page.locator("h2").first).to_contain_text("문제 7 / 40")
    assert page.locator("#navigator").count() == 1, "내비게이터 중복"
    assert page.errors == [], page.errors


def test_flag_toggle_updates_button_and_navigator(page):
    begin_exam(page)
    page.click("#flag-btn")
    expect(page.locator("#flag-btn")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator(".nav-item.flagged")).to_have_count(1)
    page.click("#flag-btn")
    expect(page.locator(".nav-item.flagged")).to_have_count(0)
    assert page.errors == [], page.errors


def test_clear_selection_button_unselects_and_saves(page):
    begin_exam(page)
    page.locator("label.opt").first.click()
    expect(page.locator("#navigator")).to_have_attribute("data-unanswered", "39")
    page.click("[data-clear]")
    expect(page.locator("input[name=selected]:checked")).to_have_count(0)
    expect(page.locator("#navigator")).to_have_attribute("data-unanswered", "40")
    page.reload()
    expect(page.locator("input[name=selected]:checked")).to_have_count(0)


def test_single_choice_is_radio_and_multi_choice_is_checkbox(page, server):
    _, Session = server
    aid = begin_exam(page)
    with Session() as db:
        types = {ai.seq: ai.item.item_type for ai in db.get(ExamAttempt, aid).items}
    multi_seq = next(s for s, t in types.items() if t == ItemType.MULTI)
    single_seq = next(s for s, t in types.items() if t != ItemType.MULTI)

    page.goto(f"/exam/{aid}?q={multi_seq}")
    assert page.locator("input[name=selected]").first.get_attribute("type") == "checkbox"
    page.locator("label.opt").nth(0).click()
    page.locator("label.opt").nth(1).click()
    expect(page.locator("input[name=selected]:checked")).to_have_count(2)

    page.goto(f"/exam/{aid}?q={single_seq}")
    assert page.locator("input[name=selected]").first.get_attribute("type") == "radio"
    page.locator("label.opt").nth(0).click()
    page.locator("label.opt").nth(1).click()
    expect(page.locator("input[name=selected]:checked")).to_have_count(1)
    assert page.errors == [], page.errors


def test_copy_and_context_menu_are_blocked_only_inside_the_exam_body(page):
    begin_exam(page)
    inside = page.evaluate("""() => {
        const a = new MouseEvent('contextmenu', {bubbles: true, cancelable: true});
        const b = new ClipboardEvent('copy', {bubbles: true, cancelable: true});
        const el = document.querySelector('.exam-body .stem');
        el.dispatchEvent(a); el.dispatchEvent(b);
        return [a.defaultPrevented, b.defaultPrevented];
    }""")
    outside = page.evaluate("""() => {
        const a = new MouseEvent('contextmenu', {bubbles: true, cancelable: true});
        document.querySelector('.topbar').dispatchEvent(a);
        return a.defaultPrevented;
    }""")
    assert inside == [True, True] and outside is False


def submit_via_modal(pg):
    """최종 제출 버튼 → 가운데 팝업에서 '제출'."""
    pg.click("#submit-form button")
    expect(pg.locator("#submit-dialog")).to_be_visible()
    pg.click("#submit-dialog button[value=confirm]")


def native_dialog_guard(pg):
    """브라우저 기본 confirm/alert 가 뜨면 기록한다(이제는 뜨면 안 된다)."""
    seen = []
    pg.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    return seen


def test_submit_confirm_dialog_cancel_then_accept(page):
    aid = begin_exam(page)
    native = native_dialog_guard(page)
    page.click("#flag-btn")                                         # 검토 표시 1개
    expect(page.locator(".nav-item.flagged")).to_have_count(1)

    page.click("#submit-form button")
    dlg = page.locator("#submit-dialog")
    expect(dlg).to_be_visible()
    expect(page.locator("#submit-dialog-unanswered")).to_have_text("미응답 40문항이 있습니다.")
    expect(page.locator("#submit-dialog-flagged")).to_have_text("검토 표시한 문항이 1개 있습니다.")
    assert page.evaluate("document.activeElement.value") == "cancel"  # 실수로 Enter 를 눌러도 제출되지 않게 기본 포커스는 '취소'

    page.click("#submit-dialog button[value=cancel]")               # 취소 → 그대로 응시 화면
    expect(dlg).to_be_hidden()
    page.wait_for_timeout(400)
    assert f"/exam/{aid}" in page.url and "/result" not in page.url

    page.click("#submit-form button")                               # Esc 로도 취소된다
    expect(dlg).to_be_visible()
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    assert "/result" not in page.url

    page.locator("label.opt").first.click()                         # 답을 하나 저장하면 미응답 수가 갱신된다
    expect(page.locator("#navigator")).to_have_attribute("data-unanswered", "39")
    page.click("#submit-form button")
    expect(page.locator("#submit-dialog-unanswered")).to_have_text("미응답 39문항이 있습니다.")
    page.click("#submit-dialog button[value=confirm]")
    page.wait_for_url(re.compile(r"/result$"))
    assert native == [], native                                     # 브라우저 기본 확인창은 쓰지 않는다
    expect(page.locator("h1")).to_contain_text("시험 결과")
    expect(page.locator("svg[aria-label*='레이더']")).to_be_visible()
    expect(page.locator("svg[aria-label*='막대']")).to_be_visible()
    assert page.errors == [], page.errors


def test_time_up_triggers_automatic_submission_without_confirm(page, server):
    _, Session = server
    aid = begin_exam(page)
    with Session() as db:
        db.get(ExamAttempt, aid).expires_at = utcnow() + timedelta(seconds=3)  # 곧 만료되도록 서버 시각을 조정
        db.commit()
    page.reload()
    t = seconds(page.locator("#timer").inner_text())
    assert t <= 3, t
    # 시간 종료 시에는 확인 팝업 없이 바로 제출돼야 한다(팝업이 뜨면 결과 화면으로 넘어가지 않아 타임아웃)
    page.wait_for_url(re.compile(r"/result$"), timeout=15000)
    expect(page.locator("h1")).to_contain_text("시험 결과")
    with Session() as db:
        assert db.get(ExamAttempt, aid).status.value in ("SUBMITTED", "EXPIRED")


def test_server_time_overrides_client_countdown_after_save(page, server):
    """저장 응답의 남은 시간(서버 기준)으로 타이머가 보정된다: DB 에서 만료를 당기면 다음 저장 후 화면 시간이 줄어든다."""
    _, Session = server
    aid = begin_exam(page)
    with Session() as db:
        db.get(ExamAttempt, aid).expires_at = utcnow() + timedelta(minutes=10)
        db.commit()
    page.locator("label.opt").first.click()
    expect(page.locator("#save-status")).to_contain_text("저장됨")
    page.wait_for_timeout(1200)
    t = seconds(page.locator("#timer").inner_text())
    assert 8 * 60 <= t <= 10 * 60, t


def test_browser_never_receives_answers_or_explanations_before_submit(page, server):
    _, Session = server
    bodies = []

    def grab(resp):
        try:
            if resp.url.startswith(server[0]) and "text/html" in (resp.headers.get("content-type") or ""):
                bodies.append(resp.text())
        except Exception:  # noqa: BLE001
            pass

    page.on("response", grab)
    aid = begin_exam(page)
    for _ in range(6):
        page.locator("label.opt").nth(1).click()
        expect(page.locator("#save-status")).to_contain_text("저장됨")
        page.click("#flag-btn")
        if page.locator("a.button.primary:has-text('다음')").count():
            page.click("a.button.primary:has-text('다음')")
            page.wait_for_timeout(250)
    page.wait_for_timeout(300)
    with Session() as db:
        expl = [ai.item.explanation for ai in db.get(ExamAttempt, aid).items]
    blob = "\n".join(bodies)
    assert len(blob) > 5000
    assert not [e for e in expl if e in blob], "해설이 제출 전 브라우저로 전송됨"
    for word in ("correct_keys", "is_correct", "정답 근거"):
        assert word not in blob
    # DOM 에도 정답/해설 표시가 없다
    html = page.content()
    assert "explain" not in html and "정답" not in page.locator(".question").inner_text()


def test_resume_in_second_tab_shows_same_attempt_with_saved_answers(page):
    aid = begin_exam(page)
    page.locator("label.opt").nth(3).click()
    expect(page.locator("#save-status")).to_contain_text("저장됨")
    tab2 = page.context.new_page()
    tab2.goto("/")
    expect(tab2.locator("text=이어서 응시")).to_be_visible()
    tab2.click("text=이어서 응시")
    tab2.wait_for_url(re.compile(rf"/exam/{aid}$"))
    assert tab2.locator("label.opt").nth(3).locator("input").is_checked()


def test_back_button_after_submit_does_not_reopen_exam(page):
    aid = begin_exam(page)
    submit_via_modal(page)
    page.wait_for_url(re.compile(r"/result$"))
    page.go_back()
    page.wait_for_load_state()
    assert "/result" in page.url or page.locator("#timer").count() == 0, "제출 후 응시 화면이 다시 열림"
    page.goto(f"/exam/{aid}")
    page.wait_for_url(re.compile(r"/result$"))


def test_exam_layout_fits_a_phone_screen_without_horizontal_scroll(browser, server):
    ctx = browser.new_context(base_url=server[0], viewport={"width": 375, "height": 740})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    for step in ("login", "exam", "result"):
        if step == "login":
            pg.goto("/login")
        elif step == "exam":
            sign_in(pg)
            pg.click("text=시험 시작")
            pg.wait_for_url(re.compile(r"/exam/\d+$"))
        else:
            submit_via_modal(pg)
            pg.wait_for_url(re.compile(r"/result$"))
        overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 1, (step, overflow)
    assert pg.errors == [], pg.errors
    ctx.close()


# ================= 관리자 화면: 전 화면 콘솔 오류 없음 =================
ADMIN_PAGES = ["/admin", "/admin/items", "/admin/items/1", "/admin/items/new", "/admin/items/upload", "/admin/blueprints",
               "/admin/blueprints/1", "/admin/blueprints/new", "/admin/results", "/admin/users", "/admin/settings",
               "/admin/audit", "/admin/analytics"]


def test_every_admin_page_loads_in_a_real_browser_without_console_errors(page, server):
    _, Session = server
    from app.models import User
    from app.services import exam

    with Session() as db:  # 결과/분석/상세 화면에 데이터가 있도록 응시 1건 생성
        u = db.scalar(select(User).where(User.login_id == "cand"))
        a, _ = exam.start_attempt(db, u)
        exam.submit_attempt(db, a)
        aid, item_id = a.id, a.items[0].item_id
    sign_in(page, "admin")
    for path in ADMIN_PAGES + [f"/admin/results/{aid}", f"/admin/analytics/items/{item_id}", f"/admin/items/{item_id}"]:
        resp = page.goto(path)
        assert resp.status == 200, (path, resp.status)
        assert page.locator("nav.adminnav").count() == 1, path
        page.wait_for_load_state("networkidle")
        assert page.errors == [], (path, page.errors)


def test_admin_uploads_items_through_the_real_file_chooser(page):
    sign_in(page, "admin")
    page.goto("/admin/items/upload")
    payload = [{"domain": "OPS", "difficulty": "LOW", "item_type": "SINGLE", "stem": "브라우저 업로드 지문",
                "options": {"A": "가", "B": "나"}, "correct_keys": ["A"], "explanation": "해설", "variant_group": "E2E-UP"}]
    page.set_input_files("input[type=file]", files=[{"name": "q.json", "mimeType": "application/json",
                                                    "buffer": json.dumps(payload, ensure_ascii=False).encode()}])
    page.click("button:has-text('검증 후 등록')")
    page.wait_for_url(re.compile(r"/admin/items\?status=DRAFT"))
    expect(page.locator(".notice")).to_contain_text("1개 문항을 DRAFT 로 등록했습니다")
    expect(page.locator("tbody")).to_contain_text("브라우저 업로드 지문")
    assert page.errors == [], page.errors


def test_blueprint_grid_shows_availability_and_sums_in_browser(page):
    sign_in(page, "admin")
    page.goto("/admin/blueprints/1")
    assert page.locator("input[name^=count_]").count() == 21
    expect(page.locator("tfoot")).to_contain_text("40문항")
    expect(page.locator("tfoot")).to_contain_text("100점")
    assert page.locator("td.short").count() == 0
    assert page.errors == [], page.errors


def test_candidate_cannot_open_admin_pages_in_browser(page):
    sign_in(page, "cand")
    resp = page.goto("/admin")
    assert resp.status == 403
    expect(page.locator("body")).to_contain_text("관리자 전용")


def test_submit_popup_is_centered_on_screen_and_dims_the_background(page):
    """요청 사항: 최종 제출 확인 팝업은 화면 가운데에 표시된다(데스크톱·휴대폰 폭 모두)."""
    begin_exam(page)
    for width, height in ((1100, 800), (375, 740)):
        page.set_viewport_size({"width": width, "height": height})
        page.click("#submit-form button")
        box = page.locator("#submit-dialog").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        assert abs(cx - width / 2) <= 2, (width, cx)                # 가로 가운데
        assert abs(cy - height / 2) <= 2, (height, cy)              # 세로 가운데
        assert box["x"] >= 0 and box["x"] + box["width"] <= width   # 화면 밖으로 넘치지 않음
        backdrop = page.evaluate("getComputedStyle(document.querySelector('#submit-dialog'), '::backdrop').backgroundColor")
        assert backdrop not in ("rgba(0, 0, 0, 0)", "transparent"), backdrop
        page.keyboard.press("Escape")
        expect(page.locator("#submit-dialog")).to_be_hidden()
    assert page.errors == [], page.errors
