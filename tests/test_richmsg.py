"""Rich Message attachment helper tests."""
from __future__ import annotations

from app.core.richmsg import (
    RichAttachment,
    RichAttachmentCollector,
    is_rich_media_url,
    merge_attachments,
)


def test_is_rich_media_url_only_accepts_http_urls():
    assert is_rich_media_url("https://cdn.test/image.jpg")
    assert is_rich_media_url("http://cdn.test/image.jpg")
    assert not is_rich_media_url("data:image/png;base64,xxx")
    assert not is_rich_media_url("file:///tmp/a.png")
    assert not is_rich_media_url("")


def test_rich_attachment_markdown_for_image():
    att = RichAttachment(kind="image", url="https://cdn.test/a.png", label="图片 1")

    assert att.markdown == "![图片 1](https://cdn.test/a.png)"


def test_rich_attachment_markdown_for_audio_is_media_block():
    att = RichAttachment(kind="audio", url="https://cdn.test/speech.mp3", label="Voice")

    assert att.markdown == "![Voice](https://cdn.test/speech.mp3)"


def test_collector_rejects_non_url_media():
    collector = RichAttachmentCollector()

    assert collector.add("image", "data:image/png;base64,xxx") is None
    assert collector.pending() == []


def test_collector_deduplicates_same_url():
    collector = RichAttachmentCollector()

    first = collector.add("audio", "https://cdn.test/speech.mp3", label="Voice")
    second = collector.add("audio", "https://cdn.test/speech.mp3", label="Duplicate")

    assert second is first
    assert len(collector.pending()) == 1
    assert collector.pending()[0].label == "Voice"


def test_merge_attachments_appends_unrendered_media():
    collector = RichAttachmentCollector()
    collector.add("image", "https://cdn.test/a.png", label="生成图片 1")
    collector.add("image", "https://cdn.test/b.png", label="生成图片 2")

    merged = merge_attachments("结果如下:", collector)

    assert "结果如下:" in merged
    assert "![生成图片 1](https://cdn.test/a.png)" in merged
    assert "![生成图片 2](https://cdn.test/b.png)" in merged


def test_merge_attachments_does_not_duplicate_existing_media():
    collector = RichAttachmentCollector()
    collector.add("image", "https://cdn.test/a.png", label="生成图片 1")

    merged = merge_attachments(
        "结果如下:\n\n![生成图片 1](https://cdn.test/a.png)",
        collector,
    )

    assert merged.count("https://cdn.test/a.png") == 1


def test_merge_attachments_appends_audio_block_when_model_used_plain_link():
    collector = RichAttachmentCollector()
    collector.add("audio", "https://cdn.test/speech.mp3", label="生成语音")

    merged = merge_attachments(
        "语音已生成: [生成语音](https://cdn.test/speech.mp3)",
        collector,
    )

    assert "[生成语音](https://cdn.test/speech.mp3)" in merged
    assert "![生成语音](https://cdn.test/speech.mp3)" in merged
