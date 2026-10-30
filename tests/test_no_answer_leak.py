"""정답·해설 비노출 검증 (제출 전 클라이언트로 나가는 모든 뷰).

HTTP 엔드포인트는 ③단계에서 추가되며, 그때 같은 검증을 API 응답 수준으로 확장한다.
여기서는 클라이언트 응답의 유일한 원천인 뷰 모델을 검사한다.
"""
import json

import pytest
from pydantic import ValidationError

from app.models import AttemptStatus
from app.schemas.exam import AttemptView, QuestionView, ResultView, attempt_view, question_view
from app.services import exam
from app.services.app_settings import set_setting
from app.services.results import ResultNotAvailable, build_result
from tests.helpers import make_user

FORBIDDEN_FIELDS = {"correct_keys", "explanation", "reference", "is_correct", "earned_points",
                    "total_score", "passed", "domain_scores", "points"}


def test_view_models_have_no_answer_fields():
    for model in (QuestionView, AttemptView):
        assert not FORBIDDEN_FIELDS & set(model.model_fields), model.__name__


def test_views_reject_extra_fields():
    with pytest.raises(ValidationError):
        QuestionView(seq=1, item_type="SINGLE", stem="s", options=[], selected_keys=[], flagged=False,
                     correct_keys=["A"])


def test_in_progress_payload_contains_no_answer_material(seeded):
    u = make_user(seeded)
    a, _ = exam.start_attempt(seeded, u)
    # 일부 답안 저장 후에도 정답 정보가 섞이지 않아야 함
    exam.save_answer(seeded, a, 1, [a.items[0].option_order[0]])

    payloads = [question_view(ai).model_dump_json() for ai in a.items]
    payloads.append(attempt_view(a).model_dump_json())
    blob = "\n".join(payloads)

    for key in FORBIDDEN_FIELDS:
        assert f'"{key}"' not in blob, key
    for ai in a.items:  # 해설 본문이 새어 나가지 않음
        assert ai.item.explanation not in blob


def test_option_keys_do_not_reveal_correct_answer_position(seeded):
    """보기 key 는 원본 식별자이므로, 응답만으로 '정답이 A' 같은 위치 정보가 드러나지 않아야 한다.
    (전송되는 것은 표시 순서와 key 뿐이며, 정답 key 집합은 어디에도 없다)"""
    u = make_user(seeded)
    a, _ = exam.start_attempt(seeded, u)
    for ai in a.items:
        v = json.loads(question_view(ai).model_dump_json())
        assert set(v) == {"seq", "item_type", "stem", "options", "selected_keys", "flagged"}
        assert [o["key"] for o in v["options"]] == ai.option_order


def test_result_not_available_before_submit(seeded):
    a, _ = exam.start_attempt(seeded, make_user(seeded))
    with pytest.raises(ResultNotAvailable):
        build_result(seeded, a)


def test_result_after_submit_includes_wrong_items_with_explanations(seeded):
    a, _ = exam.start_attempt(seeded, make_user(seeded))
    exam.submit_attempt(seeded, a)
    r = build_result(seeded, a)
    assert isinstance(r, ResultView) and r.status == AttemptStatus.SUBMITTED.value
    assert r.explanations_visible and len(r.wrong_items) == 40
    w = r.wrong_items[0]
    assert w.correct_keys and w.explanation
    assert [d.code for d in r.domains] == ["ARCH", "INSTALL", "EXTRACT", "REPLICAT", "MAPPING", "OPS", "TROUBLE"]
    assert all(d.below_minimum for d in r.domains)  # 전부 0점 → 모든 영역 과락


def test_result_hides_explanations_when_setting_off(seeded):
    set_setting(seeded, "show_explanations", False)
    seeded.commit()
    a, _ = exam.start_attempt(seeded, make_user(seeded))
    exam.submit_attempt(seeded, a)
    r = build_result(seeded, a)
    assert not r.explanations_visible and r.wrong_items == []
    blob = r.model_dump_json()
    assert all(ai.item.explanation not in blob for ai in a.items)  # 해설 본문이 어디에도 없음
    assert r.total_score == 0 and r.passed is False


def test_result_only_lists_wrong_items(seeded):
    a, _ = exam.start_attempt(seeded, make_user(seeded))
    for ai in a.items[:10]:
        exam.save_answer(seeded, a, ai.seq, list(ai.item.correct_keys))
    exam.submit_attempt(seeded, a)
    r = build_result(seeded, a)
    assert len(r.wrong_items) == 30
    assert {w.seq for w in r.wrong_items}.isdisjoint({ai.seq for ai in a.items[:10]})
