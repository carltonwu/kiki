# Image generation

Issue #5: Kiki can draw a picture and post it in Discord through the bot. The chat
model decides when, through a `generate_image(prompt)` tool; the picture is attached
to her first message of that turn.

## Flow

```
Rust: reply job (+ image_model) ─▶ Python host: chat model calls generate_image(prompt)
   │                                 └─▶ OpenRouter image model ─▶ agent_host_images row
   │◀── result { text, images: [{id, mime_type}] } ◀────────────────┘
   └─▶ load rows for this job ─▶ POST /channels/{c}/messages (multipart, first message)
```

- **Settings.** `image_generation` (default `true`) and `image_model` (default env
  `OPENROUTER_IMAGE_MODEL`, else `google/gemini-2.5-flash-image`). `/kiki config`
  checks OpenRouter's catalog for image output, as it checks chat models for tools.
  When off, the job has no `image_model`, the host leaves the tool out, and the
  prompt doesn't mention it.
- **Host** (`images.py`, `agent.py`). OpenRouter chat completions with
  `modalities: ["image", "text"]`, the same key as chat. The first inline
  PNG/JPEG/WebP/GIF up to 8 MiB is saved; its billed cost is added to the run's
  `usage.cost`. Limits: 2 images per run, 60 s per call (the job timeout is 120 s),
  no client retries since attempts may be billed, and the call counts toward the 4
  tool calls. A refusal, a missing image or a provider error becomes a tool error,
  so Kiki can say she couldn't draw it rather than failing the reply.
- **Storage** (`0017_agent_host_images.sql`). Bytes stay out of the job result
  JSON. Rows reference `agent_host_jobs` with `ON DELETE CASCADE`, so a future
  retention policy for jobs also removes images. There is no retention yet.
- **Delivery** (`agent/discord.rs`). Rust loads only rows whose `job_id` matches the
  run's job and that the saved result lists, then attaches them (`kiki-<id>.png`)
  to the first planned message. If the model generated an image but returned
  `NO_REPLY`, the image is still posted alone. Later prompts show it as
  `[attachment: kiki-<id>.png]` in Kiki's history, like other attachments.

## Trade-offs

- **Replay.** If the host dies after drawing but before saving the result, Rust's
  retry draws again (billed again) and the earlier row is never attached. Delivery
  itself is not retried, as before.
- **Image-only models.** Models that accept only `modalities: ["image"]` (some
  FLUX variants) are not handled; the Gemini image models accept image + text.
- **Inputs.** The image model sees only the prompt Kiki writes, not the
  conversation or people's avatars; editing existing images is out of scope.
- **Moderation** is the provider's; Kiki relays its refusal.

## Tests

Python unit tests cover decoding, refusals, limits, the tool being offered only with
a model, and saved rows. Rust tests cover the wire contract and job-scoped loading.
`tools/sim/scenarios/generate-image.yaml` drives it end to end: fakecord's
`sim/image` stub draws a 1×1 PNG (or refuses prompts containing "refuse"), and its
`POST /channels/{c}/messages` accepts Discord's multipart uploads.
