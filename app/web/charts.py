"""서버 측 SVG 차트 좌표 계산 (외부 JS 라이브러리 없이 엄격한 CSP 유지).

주의: 템플릿에서 dict 키를 속성처럼 읽으므로 values/items/keys 같은 dict 메서드 이름은 키로 쓰지 않는다."""
import math


def _pt(cx: float, cy: float, r: float, i: int, n: int) -> tuple[float, float]:
    ang = -math.pi / 2 + 2 * math.pi * i / n
    return cx + r * math.cos(ang), cy + r * math.sin(ang)


def _poly(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def radar(labels: list[str], ratios: list[float], min_ratio: float, size: int = 440, pad: int = 104) -> dict:
    """영역별 득점률 레이더. 최소 득점률(과락선) 다각형도 함께 계산한다."""
    n = len(labels)
    c = size / 2
    r = c - pad
    if n < 3:
        return {"size": size, "rings": [], "axes": [], "area": "", "threshold": "", "labels": []}
    rings = [_poly([_pt(c, c, r * f, i, n) for i in range(n)]) for f in (0.25, 0.5, 0.75, 1.0)]
    axes = [_pt(c, c, r, i, n) for i in range(n)]
    area = _poly([_pt(c, c, r * max(0.0, min(1.0, v)), i, n) for i, v in enumerate(ratios)])
    threshold = _poly([_pt(c, c, r * min_ratio, i, n) for i in range(n)])
    label_pos = []
    for i, text in enumerate(labels):
        x, y = _pt(c, c, r + 22, i, n)
        anchor = "middle" if abs(x - c) < 8 else ("start" if x > c else "end")
        label_pos.append({"x": round(x, 1), "y": round(y + 4, 1), "text": text, "anchor": anchor})
    return {
        "size": size, "c": c, "rings": rings, "axes": [(round(x, 1), round(y, 1)) for x, y in axes],
        "area": area, "threshold": threshold, "labels": label_pos,
    }
