# Rich Message Generation Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generated URL-backed media is delivered inside Telegram Rich Message output instead of being sent as separate media messages.

**Architecture:** Add a small Rich attachment collector in `app/core/richmsg.py`, pass it through the existing delivery layer, and merge pending attachment Markdown into renderer finalization. Keep current media delivery as fallback for non-URL assets or Rich Message failures.

**Tech Stack:** Python 3, aiogram 3.29, pytest, Telegram Bot API 10.1 Rich Message.

---

### Task 1: Rich Attachment Model

**Files:**
- Modify: `app/core/richmsg.py`
- Test: `tests/test_richmsg.py`

- [ ] **Step 1: Write failing tests**

Add tests for URL validation, Markdown snippets, and appending unrendered attachments.

- [ ] **Step 2: Run red tests**

Run: `pytest tests/test_richmsg.py -v`
Expected: import or assertion failures because attachment APIs do not exist.

- [ ] **Step 3: Implement attachment helpers**

Add `RichAttachment`, `RichAttachmentCollector`, `is_rich_media_url`, and `merge_attachments`.

- [ ] **Step 4: Run green tests**

Run: `pytest tests/test_richmsg.py -v`
Expected: all tests pass.

### Task 2: Delivery Collector Integration

**Files:**
- Modify: `app/core/delivery.py`
- Modify: `app/handlers/pipeline.py`
- Test: `tests/test_pipeline_quota.py`

- [ ] **Step 1: Write failing tests**

Add a fake delivery with a collector and assert `generate_image` records image URLs without direct photo sends.

- [ ] **Step 2: Run red test**

Run: `pytest tests/test_pipeline_quota.py -v`
Expected: image attachment test fails because delivery has no attachment API.

- [ ] **Step 3: Implement delivery APIs**

Add `attach_rich_media` to `MediaDelivery`, `DirectDelivery`, and `GuestDelivery`; wire a request collector from `run_chat_pipeline` into both delivery and renderer.

- [ ] **Step 4: Update image tool**

Make `generate_image` prefer `attach_rich_media("image", url, label=...)`; if it returns false, call existing `send_photo`.

- [ ] **Step 5: Run green test**

Run: `pytest tests/test_pipeline_quota.py -v`
Expected: all tests pass.

### Task 3: Renderer Finalization Merge

**Files:**
- Modify: `app/core/streaming.py`
- Test: `tests/test_streaming.py`

- [ ] **Step 1: Write failing renderer tests**

Add tests proving `DraftRenderer`, `EditRenderer`, and `GuestRenderer` include collector media Markdown in final Rich Message text.

- [ ] **Step 2: Run red tests**

Run: `pytest tests/test_streaming.py -v`
Expected: new tests fail because finalization ignores the collector.

- [ ] **Step 3: Implement renderer merge**

Accept an optional `RichAttachmentCollector` in renderer constructors and call `merge_attachments` before final `SendRichMessage` or `edit_message_text`.

- [ ] **Step 4: Run green tests**

Run: `pytest tests/test_streaming.py -v`
Expected: all tests pass.

### Task 4: Worker Rich Message Preference

**Files:**
- Modify: `app/core/workers.py`
- Test: `tests/test_workers.py`

- [ ] **Step 1: Write failing worker tests**

Add tests proving successful video/music completion first attempts Rich Message text with embedded URL-backed media.

- [ ] **Step 2: Run red tests**

Run: `pytest tests/test_workers.py -v`
Expected: new tests fail because workers currently send media directly.

- [ ] **Step 3: Implement rich-first worker delivery**

Add helpers that build Rich Markdown for generated video/audio and try `SendRichMessage` or `edit_message_text(rich_message=...)` before current media fallback.

- [ ] **Step 4: Run green tests**

Run: `pytest tests/test_workers.py -v`
Expected: all tests pass.

### Task 5: Full Verification

**Files:**
- No production changes.

- [ ] **Step 1: Run targeted tests**

Run: `pytest tests/test_richmsg.py tests/test_streaming.py tests/test_pipeline_quota.py tests/test_workers.py -v`
Expected: all targeted tests pass.

- [ ] **Step 2: Run full suite**

Run: `pytest -q`
Expected: all tests pass.

## Self-Review

Spec coverage: the plan covers URL-backed Rich attachments, image generation, renderer insertion, worker rich-first delivery, and fallback preservation.

No placeholders remain. Type names are consistent across tasks.
