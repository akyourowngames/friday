"""Tests for ares.image_edit module."""

import os
import json
import pytest
from PIL import Image
from ares.tools.image_edit import (
    image_info,
    resize_image,
    convert_image,
    crop_image,
    _human_size,
)


@pytest.fixture
def test_png(tmp_path):
    """Create a test PNG image."""
    path = tmp_path / "test.png"
    img = Image.new("RGB", (200, 100), color=(255, 0, 0))
    img.save(path)
    return str(path)


@pytest.fixture
def test_jpg(tmp_path):
    """Create a test JPEG image."""
    path = tmp_path / "test.jpg"
    img = Image.new("RGB", (400, 300), color=(0, 255, 0))
    img.save(path)
    return str(path)


@pytest.fixture
def test_rgba(tmp_path):
    """Create a test RGBA (transparent) image."""
    path = tmp_path / "test_rgba.png"
    img = Image.new("RGBA", (150, 150), color=(0, 0, 255, 128))
    img.save(path)
    return str(path)


class TestImageInfo:
    """Tests for image_info function."""

    def test_basic_info(self, test_png):
        result = image_info(test_png)
        assert "Format: PNG" in result
        assert "200\u00d7100" in result
        assert "Mode: RGB" in result

    def test_jpg_info(self, test_jpg):
        result = image_info(test_jpg)
        assert "JPEG" in result
        assert "400\u00d7300" in result

    def test_file_not_found(self):
        result = image_info("/nonexistent/file.png")
        assert "Error" in result
        assert "not found" in result.lower()

    def test_human_size(self):
        assert _human_size(0) == "0.0 B"
        assert _human_size(1024) == "1.0 KB"
        assert _human_size(1024 * 1024) == "1.0 MB"

    def test_rgba_mode(self, test_rgba):
        result = image_info(test_rgba)
        assert "RGBA" in result


class TestResizeImage:
    """Tests for resize_image function."""

    def test_resize_by_width(self, test_png):
        result = resize_image(test_png, width=100)
        assert "Resized" in result
        assert "200\u00d7100" in result
        assert "100\u00d750" in result

    def test_resize_by_height(self, test_png):
        result = resize_image(test_png, height=50)
        assert "Resized" in result
        assert "100\u00d750" in result

    def test_resize_by_percent(self, test_jpg):
        result = resize_image(test_jpg, percent=50)
        assert "Resized" in result
        assert "200\u00d7150" in result

    def test_resize_to_fit_box(self, test_jpg):
        result = resize_image(test_jpg, width=100, height=100)
        assert "Resized" in result
        assert "100\u00d775" in result

    def test_resize_no_params(self, test_png):
        result = resize_image(test_png)
        assert "Error" in result

    def test_resize_to_output_path(self, test_png, tmp_path):
        os.environ["ARES_ASSET_MANIFEST"] = str(tmp_path / "manifest.jsonl")
        output = str(tmp_path / "resized.png")
        result = resize_image(test_png, width=50, output=output)
        assert os.path.exists(output)
        assert "Manifest:" in result
        rows = [
            json.loads(line)
            for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert rows[-1]["action"] == "resize_image"
        assert rows[-1]["width"] == 50
        os.environ.pop("ARES_ASSET_MANIFEST", None)

    def test_resize_file_not_found(self):
        result = resize_image("/nonexistent.png", width=100)
        assert "Error" in result


class TestConvertImage:
    """Tests for convert_image function."""

    def test_png_to_jpeg(self, test_png, tmp_path):
        output = str(tmp_path / "converted.jpg")
        result = convert_image(test_png, format="jpeg", output=output)
        assert "saved" in result.lower() or "converted" in result.lower()
        assert os.path.exists(output)

    def test_jpeg_to_png(self, test_jpg, tmp_path):
        output = str(tmp_path / "converted.png")
        result = convert_image(test_jpg, format="png", output=output)
        assert os.path.exists(output)

    def test_rgba_to_jpeg(self, test_rgba, tmp_path):
        output = str(tmp_path / "converted.jpg")
        result = convert_image(test_rgba, format="jpeg", output=output)
        assert os.path.exists(output)

    def test_convert_no_output_overwrites(self, test_png):
        result = convert_image(test_png, format="jpeg")
        assert "saved" in result.lower() or "converted" in result.lower()

    def test_convert_file_not_found(self):
        result = convert_image("/nonexistent.png", format="jpeg")
        assert "Error" in result

    def test_convert_quality_parameter(self, test_jpg, tmp_path):
        output = str(tmp_path / "low_quality.jpg")
        result = convert_image(test_jpg, format="jpeg", output=output, quality=10)
        assert os.path.exists(output)
        assert os.path.getsize(output) < os.path.getsize(test_jpg)


class TestCropImage:
    """Tests for crop_image function."""

    def test_basic_crop(self, test_jpg):
        result = crop_image(test_jpg, left=50, top=50, right=200, bottom=200)
        assert "Cropped" in result
        assert "150\u00d7150" in result

    def test_crop_to_output(self, test_jpg, tmp_path):
        output = str(tmp_path / "cropped.jpg")
        result = crop_image(test_jpg, left=0, top=0, right=100, bottom=100, output=output)
        assert os.path.exists(output)

    def test_crop_file_not_found(self):
        result = crop_image("/nonexistent.png", left=0, top=0, right=10, bottom=10)
        assert "Error" in result

    def test_crop_invalid_region(self, test_jpg):
        result = crop_image(test_jpg, left=100, top=100, right=10, bottom=10)
        assert "Error" in result

    def test_crop_out_of_bounds_clamps(self, test_jpg):
        result = crop_image(test_jpg, left=0, top=0, right=9999, bottom=9999)
        assert "Cropped" in result
        assert "400\u00d7300" in result
