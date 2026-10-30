"""Blueprint 일관성 검증: 문항 수 합계, 배점 합계(100점)."""
from app.models import Blueprint

TOTAL_POINTS = 100.0
EPS = 1e-6


class BlueprintError(ValueError):
    pass


def validate_blueprint(bp: Blueprint, total_points: float = TOTAL_POINTS) -> None:
    errors: list[str] = []
    count = sum(r.item_count for r in bp.rules)
    points = sum(r.item_count * r.points_per_item for r in bp.rules)
    if count != bp.total_items:
        errors.append(f"규칙 문항 수 합계({count})가 total_items({bp.total_items})와 다릅니다")
    if abs(points - total_points) > EPS:
        errors.append(f"배점 합계({points:g})가 {total_points:g}점이 아닙니다")
    if not 0 <= bp.domain_min_ratio <= 1:
        errors.append("domain_min_ratio 는 0~1 사이여야 합니다")
    if not 0 < bp.pass_score <= total_points:
        errors.append("pass_score 범위가 올바르지 않습니다")
    if errors:
        raise BlueprintError("; ".join(errors))
