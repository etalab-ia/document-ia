import os
from pathlib import Path

import pytest
from PIL import Image

from document_ia_worker.core.preprocessing import yoloworld_crop as crop_mod
from document_ia_worker.core.preprocessing.yoloworld_crop import (
    DetectionBox,
    compute_merged_crop_box,
    crop_image_to_merged_boxes,
    detect_boxes,
)


def _make_image(path: Path, size: tuple[int, int] = (100, 80)) -> Path:
    image = Image.new("RGB", size, "white")
    image.save(path)
    return path


def test_no_box_keeps_image_unchanged():
    image = Image.new("RGB", (100, 80), "white")

    result = crop_image_to_merged_boxes(image, [], margin=20)

    assert result.size == image.size
    assert result.tobytes() == image.tobytes()


def test_multiple_boxes_are_merged_into_one_crop():
    crop_box = compute_merged_crop_box(
        [
            DetectionBox(10, 20, 30, 40, 0.9),
            DetectionBox(50, 5, 70, 60, 0.8),
        ],
        (100, 80),
        margin=5,
    )

    assert crop_box == (5, 0, 75, 65)


def test_margin_is_clamped_to_image_bounds():
    crop_box = compute_merged_crop_box(
        [DetectionBox(2, 3, 95, 77, 0.9)],
        (100, 80),
        margin=20,
    )

    assert crop_box == (0, 0, 100, 80)


def test_confidence_threshold_is_applied(monkeypatch, tmp_path):
    class FakeBoxes:
        xyxy = [[1, 2, 10, 20], [20, 25, 50, 60]]
        conf = [0.2, 0.8]

    class FakeResult:
        boxes = FakeBoxes()

    class FakeModel:
        def __init__(self):
            self.classes = None
            self.predict_kwargs = None

        def set_classes(self, classes):  # noqa: ANN001
            self.classes = classes

        def predict(self, image, **kwargs):  # noqa: ANN001, ARG002
            self.predict_kwargs = kwargs
            if kwargs["conf"] > 0.2:
                FakeResult.boxes.xyxy = [FakeBoxes.xyxy[1]]
                FakeResult.boxes.conf = [FakeBoxes.conf[1]]
            return [FakeResult()]

    fake_model = FakeModel()
    monkeypatch.setattr(
        crop_mod,
        "get_yoloworld_model",
        lambda: fake_model,
    )
    fake_clip = tmp_path / "clip" / "ViT-B-32.pt"
    monkeypatch.setattr(
        crop_mod,
        "resolve_and_verify_clip_path",
        lambda: fake_clip,
    )

    boxes = detect_boxes(
        Image.new("RGB", (100, 80), "white"),
        class_name="book",
        confidence_threshold=0.25,
        iou=0.6,
    )

    assert fake_model.classes == ["book"]
    assert fake_model.predict_kwargs["conf"] == 0.25
    assert fake_model.predict_kwargs["iou"] == 0.6
    assert boxes == [DetectionBox(20, 25, 50, 60, 0.8)]


def test_detect_boxes_uses_verified_offline_clip_and_caches_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ultralytics.nn.text_model as text_model_mod
    import ultralytics.utils as ultralytics_utils

    models_dir = tmp_path / ".models"
    clip_path = models_dir / "clip" / "ViT-B-32.pt"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"fake-verified-clip")

    verify_calls = 0

    def fake_resolve_clip() -> Path:
        nonlocal verify_calls
        verify_calls += 1
        return clip_path

    class FakeBoxes:
        xyxy = [[5, 5, 50, 50]]
        conf = [0.95]

    class FakeResult:
        boxes = FakeBoxes()

    class FakeModel:
        def __init__(self) -> None:
            self.set_classes_calls: list[list[str]] = []

        def set_classes(self, classes: list[str]) -> None:
            self.set_classes_calls.append(classes)

        def predict(self, _image: Image.Image, **_kwargs: object) -> list[FakeResult]:
            return [FakeResult()]

    fake_model = FakeModel()
    monkeypatch.setattr(crop_mod, "resolve_and_verify_clip_path", fake_resolve_clip)
    monkeypatch.setattr(crop_mod, "get_yoloworld_model", lambda: fake_model)
    monkeypatch.setattr(crop_mod, "_LAST_MODEL_INSTANCE", None)
    monkeypatch.setattr(crop_mod, "_LAST_CLASS_NAME", None)

    orig_settings_dir = ultralytics_utils.SETTINGS["weights_dir"]
    orig_utils_dir = ultralytics_utils.WEIGHTS_DIR
    orig_tm_dir = text_model_mod.WEIGHTS_DIR
    try:
        image = Image.new("RGB", (100, 80), "white")
        detect_boxes(image, class_name="book", confidence_threshold=0.25)
        detect_boxes(image, class_name="book", confidence_threshold=0.25)

        assert verify_calls == 2
        assert fake_model.set_classes_calls == [["book"]]
        assert ultralytics_utils.SETTINGS["weights_dir"] == str(models_dir)
        assert ultralytics_utils.WEIGHTS_DIR == models_dir
        assert text_model_mod.WEIGHTS_DIR == models_dir

        detect_boxes(image, class_name="passport", confidence_threshold=0.25)
        assert fake_model.set_classes_calls == [["book"], ["passport"]]
    finally:
        ultralytics_utils.SETTINGS["weights_dir"] = orig_settings_dir
        ultralytics_utils.WEIGHTS_DIR = orig_utils_dir
        text_model_mod.WEIGHTS_DIR = orig_tm_dir


def test_model_path_uses_yoloworld_environment_variable(monkeypatch):
    monkeypatch.setenv("YOLOWORLD_PATH", "/tmp/custom-yoloworld.pt")
    monkeypatch.setattr(crop_mod, "_MODEL_CACHE", None)
    monkeypatch.setattr(
        crop_mod,
        "resolve_and_verify_yoloworld_path",
        lambda: Path(os.environ["YOLOWORLD_PATH"]),
    )

    class FakeYOLOWorld:
        def __init__(self, path):
            assert path == "/tmp/custom-yoloworld.pt"

    monkeypatch.setattr(
        "ultralytics.YOLOWorld",
        FakeYOLOWorld,
    )

    assert isinstance(crop_mod.get_yoloworld_model(), FakeYOLOWorld)


def test_invalid_box_falls_back_to_original_image():
    image = Image.new("RGB", (100, 80), "white")

    result = crop_image_to_merged_boxes(
        image,
        [DetectionBox(50, 20, 10, 40, 0.9)],
        margin=10,
    )

    assert result.size == image.size
    assert result.tobytes() == image.tobytes()
