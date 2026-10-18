# Image generation

Issue #5 gave Kiki a `generate_image(prompt)` tool; issue #23 reworked it after the
first prod pictures: the ack came after 20–30 s of silence, the image model never
saw what anyone looked like, and the style was left to chance (a cartoon "Ethan"
who looked nothing like him, a generic dog for Nougie). Now Kiki answers first, the
picture follows as its own message, and it's drawn from reference photos found in
the server.

## Flow

```
Rust: reply job (+ image_model) ─▶ host: chat model calls generate_image(prompt, subjects, …)
   │                                 └─ only queues it: result { text: Kiki's ack, drawings: [...] }
   ├─▶ agent_image_jobs rows (pending), then the ack is posted (or her reaction)
   └─▶ per drawing, its own task: POST /v1/images { id: image:<run>:<n>, drawing, … }
          host: references per subject ─▶ vision check ─▶ final prompt ─▶ OpenRouter image model
                ─▶ agent_host_images row (+ prompt, references, cost, latency)
       ◀── { image, caption } or { error, failure_note }
       └─▶ follow-up message replying to the request: the image and Kiki's caption,
           or Kiki's note on why it failed
```

- **The ack is Kiki's.** `generate_image` doesn't draw; it validates the request and
  queues it, and tells the model its reply now is the heads-up that a picture is
  coming. The system prompt asks for that heads-up in her own words for the
  moment and the person, varied like a person would (or just a reaction), with no
  stock phrase to copy. Rust posts the reply as usual, so the ack lands in about the
  time a plain reply takes. If the model gives no ack at all (`NO_REPLY`, no
  reaction), the pending emoji stays on the request until the picture is posted;
  there's no canned ack text.
- **Async split** (option 1 of #23): the drawing is a durable job like replies.
  The reply's result carries `drawings`; Rust saves each as an `agent_image_jobs`
  row (`pending`) before posting anything, then draws it on its own task after the
  run finishes, so the conversation can carry on meanwhile. The host job ID is
  `image:<run>:<n>` in `agent_host_jobs`, with the request snapshotted in the row:
  a retry (host wake-up, ingester restart while `drawing`) gets the saved image
  instead of drawing and paying again. Statuses are `pending → drawing →
  delivering → done | failed`; at startup `drawing` goes back to `pending` and
  `delivering` fails (it may have posted, as for runs). A drawing still pending 15
  minutes after it was queued is dropped (`expired`).
- **Follow-up.** Always a reply to the request, which pings its author: it arrives
  after other messages, and they're waiting for it. Its text is the `caption` Kiki wrote when she
  queued it. It's stored and tagged into the conversation and added to the run's
  `reply_message_ids`, so replies to the picture reach the same conversation, and
  `read_image` can read it back from `agent_host_images` as before.
- **Failure is never silent.** A refusal, provider error or timeout makes the host
  write a note in Kiki's voice: one chat model call with the reply job's own prompt
  (her instructions and the conversation), her ack, and the reason. Rust posts it
  as the follow-up. Only if that call fails too, or the host can't be reached, does
  Rust post its minimal `(couldn't make that picture)`, so the request still gets
  an answer.

## Inputs

The chat model passes:

- `prompt`: the picture, self-contained and literal, structured as OpenAI's
  [image prompting guide](https://developers.openai.com/api/docs/guides/image-prompting)
  recommends: what kind of image first, then the scene (who does what, framing,
  gaze, hands), camera and light as concrete cues rather than mood words, then
  constraints; any text in quotes with its place and "no other text". The tool
  description also asks for real-world details to be right (the old spinal tap
  went into the hip).
- `subjects`: the people, pets and things whose look matters, each with a `name`,
  what she knows of them (`description`), a person's roster ref (`person`; a name
  only one member has gone by resolves too), `me` for herself, and `photos`:
  message IDs she or the people asking already picked. A bare string is taken as a
  name (models slip; two slips failed a prod reply through the tool's retry limit).
- `style` only when asked for or the joke needs it; `aspect_ratio` from what GPT
  Image 2.5 takes on OpenRouter (1:1, 3:2, 2:3, 4:3, 3:4, 16:9, 9:16, 21:9; anything
  else is dropped rather than failing the call); `caption`; and `edit` to change a
  posted photo rather than make a new picture.

The image job (`drawing.py`) then finds references for each subject, at most 3 per
subject and 16 in all (OpenRouter's `input_references` range for GPT Image), best first:

1. **Given photos** (`photos`): used as they are.
2. **Server history**, checked by the vision model (`OPENROUTER_VISION_MODEL`):
   - a thing or pet: posts with images whose text names it ("Nougie got adopted
     today!"), newest first;
   - a person: their own posts first, ranked by captions about themselves ("me
     tonite", "my face rn"), camera JPEGs over PNG screenshots, then posts naming
     or mentioning them; plus their avatar (server avatar if set).
   Up to 8 candidates go to one vision call that describes each image and then
   answers yes or no ("Image 4: selfie of a young man in a car => yes"). It turns
   down memes, art, screenshots, photos where the subject isn't clear, and photos
   of several people or animals where it can't tell which is the subject. The
   check runs with the model's reasoning off: over 8 photos a reasoning model took
   longer than the check's 30 s deadline.
3. **Avatar** for a person, through the same check (it must be a real photo of the
   same person).
4. **Kiki herself**: her earlier pictures saved with a `me` subject, so her look
   stays consistent once she has drawn herself.

A subject with no usable reference is drawn from her description. Images are
downloaded from Discord and sent as bytes (data URLs), never as Discord links:
attachment links are signed and expire after about a day, so the host refreshes
expired ones first with Discord's `POST /attachments/refresh-urls`, which needs
the bot token (`DISCORD_BOT_TOKEN` on the agent service). This also fixes
`read_image` for older messages, which used to come back as expired.

The **final prompt** is Kiki's, then the style, then who's who, following the
guide: each reference is labeled by number and role, with what must stay the same
for identity, and the references are kept to likeness only:

```
<Kiki's prompt>

Style: A real photograph, candid, taken on a phone camera: natural, realistic lighting,
ordinary framing and everyday imperfections. No glamorization, no heavy retouching;
not illustrated, painted or rendered.

Who and what is in it:
- Ethan, shown in reference images 1-2: Keep Ethan's face, facial features, hair, skin
  tone and build the same; don't change Ethan's identity.
- Nougie, shown in reference images 3-5: traumagotchi's dog. Keep Nougie's look (for an
  animal: breed, size, coat colors and markings) the same; don't change Nougie's identity.
Use the reference images only for how these subjects look, not for their backgrounds,
lighting, clothing or framing: make the new picture described above.
```

An edit (`edit: true`) uses the guide's edit phrasing instead: reference image 1 is
the photo to edit; change only what's described and keep everything else (faces,
identity, pose, lighting, framing, background), with no default style line.

`OpenRouterImageModel` sends references as `input_references: [{type: image_url,
image_url: {url: data:…}}]` in order, checked against OpenRouter's image catalog
(`/api/v1/images/models`: `input_references` 0–16 for `openai/gpt-image-2.5-flare`)
and the live API. GPT Image models get `quality: medium`.

## Settings and limits

- `image_generation` (default `true`) and `image_model` (default env
  `OPENROUTER_IMAGE_MODEL`, else `openai/gpt-image-2.5-flare`) as before; `/kiki
  config` checks the image catalog. Off: no `image_model` in the job, no tool, and
  no drawing guidance in the prompt.
- 2 pictures per reply. The image call has 75 s; the whole image job runs inside the
  host's job timeout (`AGENT_HOST_JOB_TIMEOUT_SECS`, 120 s in prod) and Rust's host
  deadline (`AGENT_HOST_TIMEOUT_MS`, 180 s). The reference check has 30 s; past it,
  that subject is drawn without references.
- PNG/JPEG/WebP/GIF results up to 8 MiB, typed by their bytes (`vision.sniff`).

## Observability

Each `agent_host_images` row has the final `prompt`, `model`, `subjects`,
`reference_images` (in order: `message_id` and `attachment_id`, `avatar_of`, or
`drawing_id`), `cost_usd` and `latency_ms`. Each `agent_image_jobs` row has its
status, `error` (`refused`, `failed`, `expired`, `interrupted` or the host error),
the image and the follow-up message IDs, total cost (references, image and any
note) and queued-to-posted latency; conversation costs include it. The host logs
`drawing queued` per tool call and `drawing completed` per job with subject,
reference and vision-call counts, outcome, cost and latency.

## Wire compatibility

- Rust omits `image_model` when off, and the host saves requests without unset
  fields, so jobs saved before a field existed still match on retry.
- Results saved before this change may still list drawn `images`; Rust attaches
  them to the first message as before.
- Deploy the host before (or with) the ingester: an older host has no
  `/v1/images` and rejects the new tool arguments. Without `DISCORD_BOT_TOKEN` on
  the host, history photos older than about a day can't be downloaded, so drawings
  fall back to avatars and descriptions.

## Trade-offs

- **Two messages.** The ack and the picture are separate, about 15–25 s apart in
  prod tests. The issue's alternative, reacting 🎨 on the request, is still
  available to Kiki as her ack (a reaction alone).
- **Likeness is as good as the references.** The vision check judges from the
  photo and its caption whether it plausibly shows the person; it can't recognize
  faces, so a person's references are their own selfie-like posts. People who
  never post photos of themselves get an avatar (if it's a real photo) or a
  description.
- **Replay.** If the host dies after drawing but before saving the result, a retry
  draws again (billed again), as before. Delivery isn't retried.
- **Chat-style image models** (`openai/gpt-5-image`) aren't supported; use their
  Images API equivalents.
- **Moderation** is the provider's; Kiki relays its refusal in her note.

## Tests

Python unit tests cover the OpenRouter request (references in order as data URLs,
the 16 cap), queuing (the ack is the model's own text; lenient arguments; bad refs
and photos as tool errors), reference gathering against a mock Discord (ranking,
expired-link refresh, the vision verdicts, avatars, self-portraits), the final
prompt, saved metadata, and failure notes. Rust tests cover the image job contract
and retries. `tools/sim/scenarios/generate-image.yaml` drives it end to end:
fakecord's `/v1/images` stub records the reference images it got (it rejects links:
references must be inline bytes), its CDN serves avatars and expires old links until
`/attachments/refresh-urls` re-signs them, its chat stub answers the reference
check (memes and screenshots are no) and writes the failure note. The scenario
checks two different acks pass through verbatim, the follow-up replies to the
request with the caption and image, the references and prompt the image model got,
the stored metadata, a refusal followed by Kiki's note (not the fallback), and a
picture with no ack.
