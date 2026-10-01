"""Smoke tests for VLM (Vision Language Model) support."""

import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestVlmModelResolution(unittest.TestCase):
    """_get_vlm_model must prefer the dedicated vision model over the main model."""

    def test_prefers_vision_model_over_main(self):
        from ouroboros.tools.vision import _get_vlm_model

        fake = SimpleNamespace(
            OUROBOROS_MODEL_VISION="cloudru::Qwen-VL",
            OUROBOROS_MODEL="gigachat-b2bank::GLM-text",
        )
        with patch("ouroboros.storage.settings.store.load_settings", return_value=fake):
            self.assertEqual(_get_vlm_model(), "cloudru::Qwen-VL")

    def test_falls_back_to_main_when_vision_unset(self):
        from ouroboros.tools.vision import _get_vlm_model

        fake = SimpleNamespace(
            OUROBOROS_MODEL_VISION="",
            OUROBOROS_MODEL="gigachat-b2bank::GigaChat-Pro",
        )
        with patch("ouroboros.storage.settings.store.load_settings", return_value=fake):
            self.assertEqual(_get_vlm_model(), "gigachat-b2bank::GigaChat-Pro")


class TestLLMVisionQuery(unittest.TestCase):
    """Test LLMClient.vision_query() message format."""

    def test_vision_query_url_format(self):
        """vision_query builds correct message format for URL images."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")

        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "I see a test image."}, {"prompt_tokens": 10, "completion_tokens": 5}

        client.chat = mock_chat

        text, _usage = client.vision_query(
            prompt="What do you see?",
            images=[{"url": "https://example.com/test.png"}],
            model="anthropic/claude-sonnet-4.6",
        )

        self.assertEqual(text, "I see a test image.")
        self.assertEqual(len(captured_messages), 1)
        content = captured_messages[0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "What do you see?")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertIn("url", content[1]["image_url"])
        self.assertEqual(content[1]["image_url"]["url"], "https://example.com/test.png")

    def test_vision_query_base64_format(self):
        """vision_query builds correct data URI for base64 images."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")
        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "Base64 image description."}, {}

        client.chat = mock_chat

        fake_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        text, _ = client.vision_query(
            prompt="Describe this.",
            images=[{"base64": fake_b64, "mime": "image/png"}],
        )

        self.assertEqual(text, "Base64 image description.")
        content = captured_messages[0]["content"]
        image_part = content[1]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn(fake_b64, image_part["image_url"]["url"])

    def test_vision_query_multiple_images(self):
        """vision_query handles multiple images in one call."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")
        captured_messages = []

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            captured_messages.extend(messages)
            return {"content": "Two images."}, {}

        client.chat = mock_chat

        client.vision_query(
            prompt="Compare these images.",
            images=[
                {"url": "https://example.com/img1.png"},
                {"url": "https://example.com/img2.png"},
            ],
        )

        content = captured_messages[0]["content"]
        self.assertEqual(len(content), 3)  # text + 2 images

    def test_vision_query_empty_images(self):
        """vision_query works with no images (just text)."""
        from ouroboros.llm import LLMClient

        client = LLMClient(api_key="test-key")

        def mock_chat(messages, model, tools=None, reasoning_effort="low", max_tokens=1024, tool_choice="auto"):
            return {"content": "Text only."}, {}

        client.chat = mock_chat

        text, _ = client.vision_query(prompt="Hello", images=[])
        self.assertEqual(text, "Text only.")


class TestVlmQueryTool(unittest.TestCase):
    """Test the vlm_query tool."""

    def _make_ctx(self):
        from new_core.tools.context import ToolContext
        from new_core.tools.interfaces import NullToolEvents

        ctx = MagicMock(spec=ToolContext)
        ctx.events = NullToolEvents()
        ctx.task_id = "test-task"
        ctx.current_task_type = "task"
        return ctx

    def test_vlm_query_requires_image(self):
        """vlm_query returns error when no image provided."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        result = _vlm_query(ctx, prompt="What is this?")
        self.assertIn("⚠️", result)

    def test_vlm_query_passes_vision_model_to_vision_query(self):
        """Without an explicit model arg, _vlm_query sends OUROBOROS_MODEL_VISION to the client."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        fake = SimpleNamespace(
            OUROBOROS_MODEL_VISION="cloudru::Qwen-VL",
            OUROBOROS_MODEL="gigachat-b2bank::GLM-text",
        )
        with patch("ouroboros.storage.settings.store.load_settings", return_value=fake):
            with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
                mock_client = MagicMock()
                mock_client.vision_query.return_value = ("ok", {})
                mock_get_client.return_value = mock_client

                result = _vlm_query(ctx, prompt="What is this?", image_url="https://example.com/x.png")

        self.assertEqual(result, "ok")
        self.assertEqual(mock_client.vision_query.call_args.kwargs.get("model"), "cloudru::Qwen-VL")

    def test_vlm_query_with_url(self):
        """vlm_query calls VLM with URL image."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()

        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("A logo.", {})
            mock_get_client.return_value = mock_client

            result = _vlm_query(ctx, prompt="What is the logo?", image_url="https://example.com/logo.png")

        self.assertEqual(result, "A logo.")
        call_kwargs = mock_client.vision_query.call_args
        images = call_kwargs[1].get("images") or call_kwargs[0][1]
        self.assertEqual(images[0]["url"], "https://example.com/logo.png")

    def _make_uploads_dir(self):
        """Create a temp uploads directory and patch _allowed_file_roots to point there."""
        import tempfile

        tmpdir = tempfile.mkdtemp()
        uploads = pathlib.Path(tmpdir) / "uploads"
        uploads.mkdir()
        return tmpdir, uploads

    def test_vlm_query_with_file_path(self):
        """vlm_query reads a local PNG from uploads dir and passes base64 to VLM."""
        import base64 as b64mod

        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()

        # Minimal valid 1x1 PNG (89 bytes)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        tmpdir, uploads = self._make_uploads_dir()
        img_path = uploads / "test.png"
        img_path.write_bytes(png_bytes)

        try:
            with patch("ouroboros.tools.vision._allowed_file_roots", return_value=[uploads]):
                with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
                    mock_client = MagicMock()
                    mock_client.vision_query.return_value = ("A small PNG.", {})
                    mock_get_client.return_value = mock_client

                    result = _vlm_query(ctx, prompt="What is this?", file_path=str(img_path))

            self.assertEqual(result, "A small PNG.")
            call_kwargs = mock_client.vision_query.call_args
            images = call_kwargs[1].get("images") or call_kwargs[0][1]
            self.assertEqual(len(images), 1)
            self.assertIn("base64", images[0])
            self.assertEqual(images[0]["mime"], "image/png")
            self.assertEqual(b64mod.b64decode(images[0]["base64"]), png_bytes)
        finally:
            import shutil

            shutil.rmtree(tmpdir)

    def test_vlm_query_file_not_found(self):
        """vlm_query returns error for missing file path."""
        import shutil

        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        tmpdir, uploads = self._make_uploads_dir()
        try:
            with patch("ouroboros.tools.vision._allowed_file_roots", return_value=[uploads]):
                result = _vlm_query(ctx, prompt="Describe this.", file_path=str(uploads / "missing.png"))
            self.assertIn("⚠️", result)
            self.assertIn("not found", result.lower())
        finally:
            shutil.rmtree(tmpdir)

    def test_vlm_query_non_image_rejected(self):
        """vlm_query rejects non-image files (fail-closed MIME)."""
        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        tmpdir, uploads = self._make_uploads_dir()
        txt_path = uploads / "notes.txt"
        txt_path.write_bytes(b"this is plain text, not an image")

        try:
            with patch("ouroboros.tools.vision._allowed_file_roots", return_value=[uploads]):
                result = _vlm_query(ctx, prompt="What is this?", file_path=str(txt_path))
            self.assertIn("⚠️", result)
            self.assertIn("supported image", result.lower())
        finally:
            import shutil

            shutil.rmtree(tmpdir)

    def test_vlm_query_path_outside_uploads_rejected(self):
        """vlm_query rejects paths outside the allowed uploads directory."""

        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        tmpdir, uploads = self._make_uploads_dir()
        # Create a PNG outside the uploads dir
        outside_path = pathlib.Path(tmpdir) / "secret.png"
        outside_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

        try:
            with patch("ouroboros.tools.vision._allowed_file_roots", return_value=[uploads]):
                result = _vlm_query(ctx, prompt="What is this?", file_path=str(outside_path))
            self.assertIn("⚠️", result)
            self.assertIn("uploads directory", result)
        finally:
            import shutil

            shutil.rmtree(tmpdir)

    def test_vlm_query_data_dir_env_isolation(self):
        """When OUROBOROS_DATA_DIR is set, only that dir's uploads/ is allowed (not ~/Ouroboros/data/uploads)."""
        import os as os_mod
        import shutil

        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        # Configure a custom data dir
        tmpdir, _custom_uploads = self._make_uploads_dir()
        # The "default" home uploads path is different
        pathlib.Path("~/Ouroboros/data/uploads").expanduser().resolve()

        # Create a valid PNG in home_uploads area (mocked via a separate temp dir)
        home_tmp, _ = self._make_uploads_dir()
        home_uploads_mock = pathlib.Path(home_tmp) / "uploads"
        home_uploads_mock.mkdir(exist_ok=True)
        img_path = home_uploads_mock / "test.png"
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        img_path.write_bytes(png_bytes)

        try:
            # With OUROBOROS_DATA_DIR pointing to custom tmpdir, home path is NOT allowed
            with patch.dict(os_mod.environ, {"OUROBOROS_DATA_DIR": str(pathlib.Path(tmpdir))}):
                from tests.conftest import refresh_test_config

                refresh_test_config()
                from ouroboros.tools.vision import _allowed_file_roots

                roots = _allowed_file_roots()
                # Only one allowed root: the custom uploads
                self.assertEqual(len(roots), 1)
                self.assertEqual(roots[0], pathlib.Path(tmpdir).resolve() / "uploads")
                # Attempt to read image from home_uploads_mock — should be rejected
                with patch("ouroboros.tools.vision._allowed_file_roots", return_value=roots):
                    result = _vlm_query(ctx, prompt="test", file_path=str(img_path))
            self.assertIn("⚠️", result)
            self.assertIn("uploads directory", result)
        finally:
            shutil.rmtree(tmpdir)
            shutil.rmtree(home_tmp)

    def test_vlm_query_symlink_escape_rejected(self):
        """vlm_query rejects a symlink inside uploads that points outside uploads."""
        import shutil

        from ouroboros.tools.vision import _vlm_query

        ctx = self._make_ctx()
        tmpdir, uploads = self._make_uploads_dir()
        # Create a real PNG outside uploads
        outside = pathlib.Path(tmpdir) / "secret.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        # Create symlink inside uploads pointing to outside file
        symlink = uploads / "link.png"
        symlink.symlink_to(outside)

        try:
            with patch("ouroboros.tools.vision._allowed_file_roots", return_value=[uploads]):
                result = _vlm_query(ctx, prompt="test", file_path=str(symlink))
            # The symlink resolves outside uploads/ so it must be rejected
            self.assertIn("⚠️", result)
            self.assertIn("uploads directory", result)
        finally:
            shutil.rmtree(tmpdir)

    def test_vlm_usage_event_matches_the_loop_producer(self):
        """A VLM call is a model call outside the loop, and must be accounted like one.

        vlm_query used to assemble its own ``llm_usage`` dict, missing provider, api key
        type, model category, source and ts — so VLM spend could not be attributed. Going
        through the events channel puts it on the same producer as every other call.
        """
        import queue as queue_mod

        from ouroboros.observability.pricing import emit_llm_usage_event
        from ouroboros.tools.events import QueueToolEvents
        from ouroboros.tools.vision import _vlm_query

        q = queue_mod.Queue()
        ctx = self._make_ctx()
        ctx.events = QueueToolEvents(q, None, task_id="task-vlm", task_type="chat")

        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        with patch("ouroboros.tools.vision._get_llm_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.vision_query.return_value = ("A logo.", usage)
            mock_get_client.return_value = mock_client
            _vlm_query(ctx, prompt="What is the logo?", image_url="https://example.com/logo.png")

        from_tool = q.get_nowait()
        emit_llm_usage_event(q, "task-vlm", "some/model", usage)
        from_loop = q.get_nowait()

        self.assertEqual(set(from_tool), set(from_loop))
        self.assertEqual(from_tool["type"], "llm_usage")
        self.assertEqual(from_tool["task_id"], "task-vlm")
        self.assertEqual(from_tool["source"], "vlm_query")
        self.assertEqual(from_tool["prompt_tokens"], 11)


if __name__ == "__main__":
    unittest.main()
