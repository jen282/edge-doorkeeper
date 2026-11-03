"""머리 중심점 히트맵 모델 (기본안 A). 구조와 근거는 docs/04-model.md 1.3절."""

import keras
from keras import layers

INPUT_SHAPE = (72, 96, 1)  # 높이, 너비, 채널
STRIDE = 8
GRID_H, GRID_W = INPUT_SHAPE[0] // STRIDE, INPUT_SHAPE[1] // STRIDE  # 9, 12

# (출력 채널, stride). 마지막 s1 블록은 수용 영역을 넓혀 이웃한 머리를 구분하기 위함
DW_BLOCKS = ((16, 1), (24, 2), (24, 1), (32, 2), (32, 1), (32, 1))


def _dw_block(x, filters: int, stride: int):
    x = layers.DepthwiseConv2D(3, strides=stride, padding="same", use_bias=False)(x)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    x = layers.Conv2D(filters, 1, use_bias=False)(x)
    x = layers.BatchNormalization()(x)
    return layers.ReLU()(x)


def build_model() -> keras.Model:
    """입력: 0~1로 정규화한 그레이스케일. 출력: 9×12 격자별 '머리 중심일 확률'."""
    inp = keras.Input(INPUT_SHAPE, name="frame")
    x = layers.Conv2D(8, 3, strides=2, padding="same", use_bias=False)(inp)
    x = layers.BatchNormalization()(x)
    x = layers.ReLU()(x)
    for filters, stride in DW_BLOCKS:
        x = _dw_block(x, filters, stride)
    out = layers.Conv2D(1, 1, activation="sigmoid", name="heatmap")(x)
    return keras.Model(inp, out, name="head_heatmap")
