"""천장 수직 카메라 모델. 수치 근거는 docs/03-hardware.md 1.2절."""

import math
from dataclasses import dataclass

WIDTH = 96
HEIGHT = 72
FPS = 8

# 통행 방향(y) 구역 경계(px): A(바깥, y < 24) | B | C(안쪽, y > 48). docs/04-model.md 1.4절
LINE_A = 24.0
LINE_MID = 36.0
LINE_C = 48.0


@dataclass(frozen=True)
class Camera:
    ceiling_m: float = 2.7
    hfov_deg: float = 54.0
    vfov_deg: float = 41.0

    @property
    def fx(self) -> float:
        return (WIDTH / 2) / math.tan(math.radians(self.hfov_deg / 2))

    @property
    def fy(self) -> float:
        return (HEIGHT / 2) / math.tan(math.radians(self.vfov_deg / 2))

    def project(self, x_m: float, y_m: float, z_m: float) -> tuple[float, float]:
        """월드 좌표(m) → 영상 좌표(px). x는 문 폭 방향, y는 통행 방향(+가 매장 안쪽)."""
        d = self.ceiling_m - z_m
        return WIDTH / 2 + self.fx * x_m / d, HEIGHT / 2 + self.fy * y_m / d

    def px_per_m(self, z_m: float) -> tuple[float, float]:
        d = self.ceiling_m - z_m
        return self.fx / d, self.fy / d

    def to_dict(self) -> dict:
        return {"ceiling_m": self.ceiling_m, "hfov_deg": self.hfov_deg, "vfov_deg": self.vfov_deg}
