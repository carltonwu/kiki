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
  `OPENROUTER_IMAGE_MODEL`, else `openai/gpt-image-2.5-flare`, the speed tier of
  OpenAI's GPT Image 2.5). `/kiki config` checks OpenRouter's image catalog
  (`/api/v1/images/models`) for image output, as it checks chat models for tools.
  When off, the job has no `image_model`, the host leaves the tool out, and the
  prompt doesn't mention it.
- **Host** (`images.py`, `agent.py`). The tool uses Pydantic AI's
  [image generation](https://pydantic.dev/docs/ai/guides/image-generation/) API: an
  `ImageGenerator` over `OpenRouterImageModel`, a small `ImageGenerationModel` for
  OpenRouter's Images API (`POST /api/v1/images`, returning `data[].b64_json` and
  `usage.cost`) with the same key as chat. Pydantic AI's built-in
  `openai:gpt-image-*` model would need a separate OpenAI key and account; this keeps
  every model call on OpenRouter, and any model in OpenRouter's image catalog works
  (GPT Image, Gemini, FLUX, …). The model may pick `aspect_ratio` 1:1, 3:2 or 2:3. GPT
  Image models get `quality: medium` (about $0.03–0.05 for a 1024² image; `auto` can
  choose `high`, roughly 4× that). A PNG/JPEG/WebP/GIF up to 8 MiB is saved; its billed
  cost is added to the run's `usage.cost`. Limits: 2 image calls per run (reserved before each call, so parallel
  calls can't exceed it), 60 s per call (Rust's host deadline,
  `AGENT_HOST_TIMEOUT_MS`, defaults to 180 s), no client retries since attempts may
  be billed, and the call counts toward the 4 tool calls. A refusal, a missing
  image or a provider error becomes a tool error, so Kiki can say she couldn't draw
  it rather than failing the reply.
- **Wire compatibility.** Rust omits `image_model` when off and the host saves
  requests without unset optional fields, so jobs saved before this change still
  match on retry. Deploy the host before (or with) the ingester: an older host
  rejects requests that carry `image_model`.
- **Storage** (`0017_agent_host_images.sql`). Bytes stay out of the job result
  JSON. Rows reference `agent_host_jobs` with `ON DELETE CASCADE`, so a future
  retention policy for jobs also removes images. There is no retention yet.
- **Delivery** (`agent/discord.rs`). Rust loads only rows whose `job_id` matches the
  run's job and that the saved result lists, then attaches them (`kiki-<id>.png`)
  to the first planned message. If the model generated an image but returned
  `NO_REPLY` (with `chat_allow_silence` on), the image is still posted alone.
  Later prompts show it as `[attachment: kiki-<id>.png]` in Kiki's history, like
  other attachments.

## Trade-offs

- **Replay.** If the host dies after drawing but before saving the result, Rust's
  retry draws again (billed again) and the earlier row is never attached. Delivery
  itself is not retried, as before.
- **Chat-style image models** (`openai/gpt-5-image`, listed under `/api/v1/models`
  rather than the image catalog) aren't supported; use their Images API equivalents.
- **Untested against the live API.** The request and response shapes follow
  OpenRouter's image-generation docs; the simulator stubs them.
- **Inputs.** The image model sees only the prompt Kiki writes, not the
  conversation or people's avatars; editing existing images is out of scope.
- **Moderation** is the provider's; Kiki relays its refusal.

## Tests

Python unit tests cover decoding, refusals, limits, the tool being offered only with
a model, and saved rows. Rust tests cover the wire contract and job-scoped loading.
`tools/sim/scenarios/generate-image.yaml` drives it end to end: fakecord's
`sim/image` stub on its fake `/v1/images` draws a 1×1 PNG (or returns a 400 for prompts
containing "refuse"), and its
`POST /channels/{c}/messages` accepts Discord's multipart uploads.
