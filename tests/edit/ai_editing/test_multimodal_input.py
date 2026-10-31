"""多模态图片输入测试 — image_attachment + token 估算 + transport 翻译 + 消息模型。

覆盖：
* image_attachment：message_content（纯文本 vs 多模态）、extract_text、has_image、
  count/estimate、encode_bytes/encode_qimage（含降采样）。
* context_budget：多模态消息按固定常数计图片 token（不按 base64 字符高估）。
* OpenAIResponsesClient._to_responses_content：image_url → input_image 翻译。
* 消息模型：AssistantRequest/AssistantMessage 携带 attachments；assistant_service
  保留含图消息。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.image_attachment import (
    IMAGE_TOKEN_ESTIMATE,
    FileAttachment,
    ImageAttachment,
    attachment_from_qimage,
    count_images_in_content,
    encode_bytes_to_data_url,
    encode_qimage_to_data_url,
    estimate_image_tokens,
    extract_text_from_content,
    has_image_content,
    message_content,
)
from src.features.edit.ai_editing.context_budget import estimate_window_usage


# ================================================================
# 纯逻辑：message_content / extract / count / estimate
# ================================================================


class TestContentBuilding:
    def test_no_attachments_returns_plain_string(self):
        assert message_content("hello", ()) == "hello"
        assert message_content("hello", None) == "hello"
        assert message_content("", ()) == ""

    def test_with_attachments_returns_list(self):
        att = ImageAttachment(data_url="data:image/jpeg;base64,AAA=", width=10, height=10)
        content = message_content("describe this", [att])
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "describe this"}
        assert content[1] == {"type": "image_url", "image_url": {"url": att.data_url}}

    def test_multiple_attachments(self):
        a1 = ImageAttachment(data_url="data:image/jpeg;base64,A=", width=1, height=1)
        a2 = ImageAttachment(data_url="data:image/jpeg;base64,B=", width=2, height=2)
        content = message_content("two", [a1, a2])
        assert isinstance(content, list)
        image_parts = [p for p in content if p.get("type") == "image_url"]
        assert len(image_parts) == 2

    def test_file_attachment_builds_transport_neutral_file_part(self, tmp_path):
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")
        att = FileAttachment(
            path=str(path), mime="video/mp4", filename="clip.mp4", size_bytes=5,
        )
        content = message_content("analyze", [att])
        assert content[-1]["type"] == "file"
        assert content[-1]["media_kind"] == "video"
        assert content[-1]["file_path"] == str(path)
        assert att.to_dict()["filename"] == "clip.mp4"
        assert "path" not in att.to_dict()

    def test_extract_text_from_string(self):
        assert extract_text_from_content("hello") == "hello"
        assert extract_text_from_content("") == ""
        assert extract_text_from_content(None) == ""

    def test_extract_text_from_multimodal_drops_images(self):
        content = [
            {"type": "text", "text": "line1"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "line2"},
        ]
        assert extract_text_from_content(content) == "line1\nline2"

    def test_has_image_content(self):
        assert has_image_content("text") is False
        assert has_image_content([{"type": "text", "text": "x"}]) is False
        assert has_image_content([{"type": "image_url", "image_url": {"url": "x"}}]) is True

    def test_count_and_estimate(self):
        content = [
            {"type": "image_url", "image_url": {"url": "a"}},
            {"type": "image_url", "image_url": {"url": "b"}},
            {"type": "text", "text": "x"},
        ]
        assert count_images_in_content(content) == 2
        assert estimate_image_tokens(content) == 2 * IMAGE_TOKEN_ESTIMATE
        assert estimate_image_tokens("plain text") == 0


# ================================================================
# 编码：bytes + QImage（含降采样）
# ================================================================


class TestEncoding:
    def test_encode_bytes_to_data_url(self):
        url = encode_bytes_to_data_url(b"\x89PNG", mime="image/png")
        assert url.startswith("data:image/png;base64,")

    def test_encode_qimage_downscales(self, qapp):
        from PyQt6.QtGui import QImage
        big = QImage(4000, 3000, QImage.Format.Format_RGB32)
        big.fill(0x00ff0000)
        url, w, h = encode_qimage_to_data_url(big, max_long_edge=512)
        assert url.startswith("data:image/jpeg;base64,")
        assert max(w, h) == 512  # 长边被限制到 512
        assert w == 512 and h <= 512

    def test_encode_qimage_keeps_small(self, qapp):
        from PyQt6.QtGui import QImage
        small = QImage(100, 60, QImage.Format.Format_RGB32)
        small.fill(0x000000ff)
        url, w, h = encode_qimage_to_data_url(small, max_long_edge=512)
        assert (w, h) == (100, 60)
        assert url.startswith("data:image/jpeg;base64,")

    def test_encode_null_image_raises(self, qapp):
        from PyQt6.QtGui import QImage
        null = QImage()
        with pytest.raises(ValueError):
            encode_qimage_to_data_url(null)

    def test_attachment_from_qimage(self, qapp):
        from PyQt6.QtGui import QImage
        img = QImage(200, 100, QImage.Format.Format_RGB32)
        img.fill(0x00ff00)
        att = attachment_from_qimage(img, source="frame_grab")
        assert att.mime == "image/jpeg"
        assert att.source == "frame_grab"
        assert att.data_url.startswith("data:image/jpeg;base64,")
        assert att.width == 200 and att.height == 100
        # to_dict 不含 data_url（过大 / 不可信）
        assert "data_url" not in att.to_dict()


# ================================================================
# Token 估算：图片按固定常数，不按 base64 字符高估
# ================================================================


class TestTokenEstimation:
    def test_text_message_estimated_normally(self):
        usage = estimate_window_usage(
            [{"role": "user", "content": "hello world"}], window_limit=8000,
        )
        assert usage.window_prompt is not None
        assert usage.window_prompt < 100  # 短文本

    def test_image_message_uses_fixed_cost(self, qapp):
        from PyQt6.QtGui import QImage
        att = attachment_from_qimage(
            QImage(2000, 2000, QImage.Format.Format_RGB32), source="paste",
        )
        # 一个巨大的 base64 data URL —— 必须按固定常数估算，而不是按字符数
        multimodal = message_content("see this", [att])
        usage = estimate_window_usage(
            [{"role": "user", "content": multimodal}], window_limit=8000,
        )
        # 文本 ~3 token + 4 overhead + 1 张图固定常数；绝不接近 base64 字符数级别
        assert usage.window_prompt < IMAGE_TOKEN_ESTIMATE + 200
        assert usage.window_prompt > IMAGE_TOKEN_ESTIMATE


# ================================================================
# Transport：Responses client image_url → input_image 翻译
# ================================================================


class TestResponsesTranslation:
    def test_plain_text(self):
        from src.features.edit.ai_editing.llm_client import OpenAIResponsesClient
        assert OpenAIResponsesClient._to_responses_content("hi") == [
            {"type": "input_text", "text": "hi"},
        ]

    def test_multimodal_translates(self):
        from src.features.edit.ai_editing.llm_client import OpenAIResponsesClient
        content = [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA="}},
        ]
        out = OpenAIResponsesClient._to_responses_content(content)
        assert out[0] == {"type": "input_text", "text": "describe"}
        assert out[1] == {
            "type": "input_image", "image_url": "data:image/jpeg;base64,AAA=",
        }

    def test_empty_list_falls_back_to_text(self):
        from src.features.edit.ai_editing.llm_client import OpenAIResponsesClient
        out = OpenAIResponsesClient._to_responses_content([])
        assert out == [{"type": "input_text", "text": ""}]

    def test_file_parts_map_to_video_audio_and_file_inputs(self):
        from src.features.edit.ai_editing.llm_client import OpenAIResponsesClient
        content = [
            {"type": "file", "file_path": "/tmp/a.mp4", "filename": "a.mp4", "mime": "video/mp4", "media_kind": "video"},
            {"type": "file", "file_path": "/tmp/a.wav", "filename": "a.wav", "mime": "audio/wav", "media_kind": "audio"},
            {"type": "file", "file_path": "/tmp/a.pdf", "filename": "a.pdf", "mime": "application/pdf", "media_kind": "file"},
        ]
        out = OpenAIResponsesClient._to_responses_content(content)
        assert [item["type"] for item in out] == [
            "input_video", "input_audio", "input_file",
        ]

    def test_responses_transport_uploads_file_before_request(self, tmp_path, monkeypatch):
        from src.features.edit.ai_editing.llm_client import (
            LLMRequestPolicy,
            OpenAIResponsesClient,
        )
        from src.features.edit.ai_editing.provider_profiles import AiProviderProfile

        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF-test")
        uploaded = []

        def fake_upload(url, headers, file_path, **kwargs):
            uploaded.append((url, headers, file_path, kwargs))
            return {"id": "file-test", "status": "processed"}

        monkeypatch.setattr(
            "src.features.edit.ai_editing.llm_client._post_multipart_file",
            fake_upload,
        )
        profile = AiProviderProfile(
            profile_id="ark",
            display_name="Ark",
            provider_kind="volcengine_responses",
            endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
            model_id="doubao-seed-2.0-lite",
        )
        client = OpenAIResponsesClient(
            profile, "key", policy=LLMRequestPolicy(max_retries=0),
        )
        content = message_content("read", [FileAttachment(
            path=str(path), mime="application/pdf", filename="report.pdf", size_bytes=9,
        )])
        prepared = client._prepare_responses_content(content)
        assert prepared == [
            {"type": "input_text", "text": "read"},
            {
                "type": "input_file",
                "file_id": "file-test",
            },
        ]
        assert uploaded[0][0].endswith("/api/plan/v3/files")

    def test_plain_document_uses_safe_local_text_fallback(self, tmp_path):
        from src.features.edit.ai_editing.llm_client import OpenAIResponsesClient
        from src.features.edit.ai_editing.provider_profiles import AiProviderProfile

        path = tmp_path / "notes.md"
        path.write_text("第一行\n第二行", encoding="utf-8")
        profile = AiProviderProfile(
            profile_id="ark",
            display_name="Ark",
            provider_kind="volcengine_responses",
            endpoint="https://ark.cn-beijing.volces.com/api/plan/v3",
            model_id="doubao-seed-2.0-lite",
        )
        client = OpenAIResponsesClient(profile, "key")
        content = message_content("summarize", [FileAttachment(
            path=str(path), mime="text/markdown", filename="notes.md", size_bytes=14,
        )])
        prepared = client._prepare_responses_content(content)
        assert prepared[-1]["type"] == "input_text"
        assert "第一行" in prepared[-1]["text"]


# ================================================================
# 消息模型：attachments 流转
# ================================================================


class TestMessageModel:
    def test_request_carries_attachments(self):
        from src.features.edit.ai_editing.assistant_events import AssistantRequest
        att = ImageAttachment(data_url="data:image/jpeg;base64,A=", width=1, height=1)
        req = AssistantRequest(request_id="r1", text="hi", attachments=(att,))
        assert len(req.attachments) == 1
        # 默认无附件
        assert AssistantRequest(request_id="r2", text="hi").attachments == ()

    def test_message_carries_attachments(self):
        from src.features.edit.ai_editing.assistant_events import AssistantMessage
        att = ImageAttachment(data_url="data:image/jpeg;base64,A=", width=1, height=1)
        msg = AssistantMessage(message_id="m1", role="user", text="hi", attachments=(att,))
        assert len(msg.attachments) == 1

    def test_service_recent_keeps_image_only_message(self):
        from src.features.edit.ai_editing.assistant_service import AssistantService
        from src.features.edit.ai_editing.assistant_events import AssistantMessage
        svc = AssistantService.__new__(AssistantService)
        svc._messages = []
        # 仅图片、无文本的消息必须被保留
        svc._messages.append(AssistantMessage(
            message_id="img-only", role="user", text="",
            attachments=(ImageAttachment(data_url="data:image/jpeg;base64,A=", width=1, height=1),),
        ))
        recent = svc.recent_chat_messages(limit=10)
        assert len(recent) == 1
        assert recent[0].attachments
