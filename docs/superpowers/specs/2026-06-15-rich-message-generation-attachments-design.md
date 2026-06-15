# Rich Message Generation Attachments Design

## Goal

Generated media should be inserted into the final Telegram Rich Message at the point where the assistant text refers to it, instead of being sent immediately as separate media messages or converted into media captions.

This applies first to image generation, and the same mechanism should support other generated assets such as audio, video, and music when Telegram Rich Message accepts their URLs.

## API Constraints

Telegram Bot API 10.1 added `sendRichMessage`, `sendRichMessageDraft`, and Rich Message input content. Rich Message media blocks are represented in Rich Markdown by URL-backed media syntax, so generated assets must have externally reachable `http://` or `https://` URLs to be embedded directly.

If a generated result only has bytes, a local file, or a URL Telegram rejects in Rich Message parsing, delivery must fall back to the existing media send/edit paths.

## Architecture

Add a request-scoped attachment collector used by tool delivery and renderers.

Generated tools no longer need to send eligible URL-backed assets immediately. Instead, they attach metadata such as kind, URL, label, and optional note to the collector, then return a concise tool result telling the model that assets are available and should be referenced in the final Rich Markdown.

The final renderer inserts pending attachments into the Rich Message Markdown before calling Telegram. This keeps the result as one Rich Message while preserving direct media fallback.

## Data Flow

1. A generation tool creates or retrieves media URLs.
2. If the URL is `http://` or `https://`, the delivery layer records it as a Rich attachment.
3. The tool result returned to the model includes a Markdown snippet for the attachment, so the model can naturally place it in its final answer.
4. On finalization, the renderer merges any unrendered attachments into the final Rich Markdown.
5. If Rich Message sending fails, fallback uses the existing plain text/media delivery behavior.

## Delivery Rules

Image generation should prefer Rich Message attachment insertion for all generated image URLs.

Synchronous audio generation should use Rich attachment insertion only when it has a usable URL. Byte-only audio continues to use the existing upload/send path.

Asynchronous video and music workers should prefer a Rich Message containing text and an embedded URL-backed block. If Telegram rejects that Rich Message, they fall back to the current `send_video`, `send_audio`, or `editMessageMedia` path.

Guest mode keeps the same single-inline-message constraint. It should prefer editing the inline message to Rich Message text with embedded media; if rejected, it falls back to the current `editMessageMedia` behavior.

## Testing

Add focused tests for:

- Rich attachment Markdown generation and URL validation.
- Image tool records URL-backed generated images instead of immediately sending photos.
- Final renderer sends Rich Message content that contains both assistant text and pending media blocks.
- Non-URL or rejected Rich Message media falls back to existing media delivery.

## Out of Scope

This change does not add file hosting for byte-only generated assets.

This change does not attempt to force exact model placement if the model omits attachment snippets; the renderer should append unrendered attachments as a safety net.
