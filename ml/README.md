# ml — 학습·양자화·평가 게이트

머리 중심점 히트맵 모델(입력 96×72 int8, 출력 12×9)을 학습하고 int8로 변환한 뒤, CI 게이트로 평가한다.

- 설계: `docs/04-model.md`, `docs/09-ci.md`
- 예정 파일: `train.py`, `evaluate.py`, `postproc.py`, `gates.yaml`, `model.tflite`, `model_float.tflite`, `tests/`
