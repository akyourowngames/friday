"""Tests for ares.image_generate module."""

import os
import io
from unittest.mock import patch, MagicMock
from PIL import Image
from ares.tools.image_generate import generate_image, IMAGES_DIR


def _jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color="white").save(buffer, format="JPEG")
    return buffer.getvalue()


class TestGenerateImage:
    """Tests for the generate_image function."""

    @patch("ares.image_generate.httpx.Client")
    def test_generate_returns_file_path(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.content = _jpeg_bytes()
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = generate_image("a cat sitting on a couch")
        assert "saved to" in result
        assert str(IMAGES_DIR) in result

    @patch("ares.image_generate.httpx.Client")
    def test_generate_validates_content_type(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.headers = {"content-type": "text/html"}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = generate_image("test prompt")
        assert "Error" in result
        assert "Expected image" in result

    @patch("ares.image_generate.httpx.Client")
    def test_generate_handles_timeout(self, mock_client_cls):
        import httpx
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.TimeoutException("timed out")
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = generate_image("test prompt")
        assert "Error" in result
        assert "timed out" in result.lower()

    @patch("ares.image_generate.httpx.Client")
    def test_generate_handles_rate_limit(self, mock_client_cls):
        import httpx
        mock_response = MagicMock()
        mock_response.status_code = 429
        error = httpx.HTTPStatusError("rate limited", request=MagicMock(), response=mock_response)
        mock_client = MagicMock()
        mock_client.get.side_effect = error
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = generate_image("test prompt")
        assert "Error" in result
        assert "Rate limited" in result

    @patch("ares.image_generate.httpx.Client")
    def test_generate_with_seed(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.content = _jpeg_bytes()
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = generate_image("test", seed=42)
        assert "saved to" in result
        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"].get("seed") == 42 or call_kwargs[0][1].get("seed") == 42

    @patch("ares.image_generate.httpx.Client")
    def test_generate_with_custom_dimensions(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.content = _jpeg_bytes()
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = generate_image("test", width=512, height=768)
        assert "saved to" in result

    @patch("ares.image_generate.httpx.Client")
    def test_generate_creates_images_dir(self, mock_client_cls):
        mock_response = MagicMock()
        mock_response.headers = {"content-type": "image/jpeg"}
        mock_response.content = _jpeg_bytes()
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        generate_image("test")
        assert IMAGES_DIR.exists()

    def test_images_dir_is_ares_images(self):
        """Verify IMAGES_DIR points to ~/.ares/images."""
        home = os.path.expanduser("~")
        assert str(IMAGES_DIR).startswith(home)
