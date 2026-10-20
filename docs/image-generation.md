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
          host: candidates per subject ─▶ vision picker ─▶ final prompt ─▶ OpenRouter image model
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
  an answer. A safety block (OpenRouter passes OpenAI's `400 "rejected by the
  safety system… safety_violations=[…]"` through) is a `ContentFilterError`, and
  the note's reason says asking again won't get it through. Before, the reason
  was "HTTP 400": Kiki called it a glitch, invited a retry, and the retry was
  blocked the same way. Other 4xx log their status and error code.
- **A fallback model draws what the safety filter blocks.** When the server's
  image model's filter blocks a picture, the host draws it again with
  `OPENROUTER_IMAGE_FALLBACK_MODEL` on the agent service (default
  `qwen/qwen-image-2.1-turbo`; empty turns it off), whose provider filters
  differently. Only safety blocks fall back: other errors would fail the same
  way. The fallback gets the references it takes (from OpenRouter's image
  catalog: 4 for Qwen, 16 for GPT Image), each subject keeping its best ones in
  turn, then the shared images, renumbered in its own prompt. A ratio it doesn't
  take maps to the nearest (21:9 to 16:9). The saved image and the job result
  name the model that drew it. If both block it, Kiki's note says so. The
  fallback goes through the same OpenRouter key, so the workspace's guardrails
  must allow its provider. Qwen's only provider, Alibaba Cloud, isn't
  zero-data-retention.
- **Pictures still drawing are in the prompt.** Each run's context lists the
  channel's pictures that are queued but not yet posted: their request, its
  author, and the caption. The tool says not to queue them again. Without this,
  a run started during the 15–25 s a picture takes saw the request and Kiki's
  "give me a sec" but no picture, and drew it again. In prod, one meme was drawn
  five times in two minutes, triggered by "wtf" and "Wow I kinda look like Steve".

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
  only one member has gone by resolves too, and a name several have gone by means
  the one going by it now, else the one in the conversation: in prod Ethan had
  once been nicknamed "Reece", so "Reece" resolved to nobody and was searched for
  as a thing, which made a game character and a monkey his references), `me` for
  herself, and `photos`:
  message IDs she or the people asking already picked, or a Discord message link
  or attachment link they pasted. An attachment link's message is found by its
  attachment ID in the link's channel; an attachment's snowflake comes just
  before its message's, and the link names that one image (`ID:N`). A bare
  string is taken as a name (models slip; two slips failed a prod reply through
  the tool's retry limit), and a message ID inside a name with no `photos` is
  taken as a photo ("Reece (from the selfie he shared, message 156…)" was drawn
  without the selfie in prod).
- `shared`: the messages a picture is made from that don't show a subject (the
  photo, meme, template or post to redraw), when they aren't among what Rust
  adds itself (below): a picture from earlier in the chat that a follow-up asks
  for again. Like `photos`, a message ID, a Discord link, or an X or Reddit post's
  own link (the newest message in the channel sharing it, under any host or link
  fixer). `ID:N` takes only a message's Nth image, numbered as `read_image` and
  `read_link` number them: "the first pic" of a two-photo tweet, without the
  other. A link fixer's deleted original stands for its repost. Each pick must
  have an image (an attachment, a link preview, or a post a reader takes), or
  the call is a tool error.
- `style` only when asked for or the joke needs it; `aspect_ratio` from what GPT
  Image 2.5 takes on OpenRouter (1:1, 3:2, 2:3, 4:3, 3:4, 16:9, 9:16, 21:9; anything
  else is dropped rather than failing the call); `caption`; and `edit` to change a
  posted photo rather than make a new picture.

The image job (`media/drawing.py`) then finds references for each subject, at most 3 per
subject and 16 in all (OpenRouter's `input_references` range for GPT Image), best first:

1. **Given photos** (`photos`): used as they are. A picture Kiki drew
   (`kiki-<id>.png`) isn't a photo of anyone: as a subject's photo, outside an edit
   or her own self-portrait, it's a tool error that tells her to put it in `shared`
   (in prod her "starring reece" picture became Reece's likeness).
2. **Candidates** from memory and the server history:
   - messages with images that Kiki's memory of the subject cites (its live
     entries' sources), first;
   - a thing or pet: up to 40 posts whose text names it ("Nougie got adopted
     today!"), newest first;
   - a person: up to 40 of their own posts, ranked by captions about themselves
     ("me tonite", "my face rn"), then posts naming or mentioning them; plus their
     avatar (server avatar if set).
   Each post gives up to 2 images, re-ranked by what Discord says of each
   attachment (`likely`): camera JPEGs (`IMG_…`, `PXL_…`) over PNG screenshots,
   real sizes over thumbnails (under 400 px), a photo's shape over a phone
   screen's (taller than 2:1), and over 3 years old last. The best 24 go on.
3. **The picker** (`Gatherer.pick`), a Pydantic AI agent on the vision model
   (`OPENROUTER_VISION_MODEL`), looks at them in parallel batches of 12 with their
   captions, and gives a structured verdict per image (`PromptedOutput(Picks)`:
   what it shows, `kind` photo/screenshot/meme/art/other, whether it clearly shows
   the subject, and `clarity` 0-3). An output validator checks there's one verdict
   per image and sends a `ModelRetry` once if not. It also sees the drawing's
   prompt, to prefer what the picture needs (the face for a portrait, the whole
   body for an outfit), and up to 2 given photos as known photos of the subject
   (lettered, not candidates), so it approves only the same person or animal.
   Photos of the subject with clarity 1 or more are kept, clearest first. It
   turns down memes, art, game renders, screenshots, photos where the subject
   isn't clear, and photos of several people or animals where it can't tell which
   is the subject. It runs with the model's reasoning off and a 30 s deadline per
   batch: over 8 photos a reasoning model took longer than that. Prompted output
   (JSON in the text) works with any vision model, even without tool calling.
   The avatar is judged with the rest (it must be a real photo of the same person).
4. **Kiki herself**: her earlier pictures saved with a `me` subject, so her look
   stays consistent once she has drawn herself.

**Shared images** come after every subject's references. These are the images
people shared with the request: attached to a trigger message, or in a message a
trigger replies to or links (a message link or a pasted attachment link), and in
turn what that replies to or links, up to 3 steps and, past the first, half an
hour back. A message of Kiki's leads to what she was answering, and her picture
also to what it was drawn from, so "you can do it" replying to her refusal, or
"redo it" replying to her picture, still means the tweet the request was about.
A link fixer's deleted original is its repost. Kiki's own pictures don't count,
and a message linking an X or Reddit post counts even without a preview. Rust
works these out while building the context. The prompt shows the chain, with
image markers, so Kiki can use them as `photos`. Rust adds the ones the drawing
doesn't use as `Drawing.shared`, after Kiki's own `shared` picks (a message she
picked one image of isn't added whole), at most 4 messages of its own. The host sends them unchecked (people
picked them, and the check would turn down a meme template) and labels them
"Reference image N is what they shared with the request (a photo, meme or
template): use it as the request describes". In prod, "edit this meme" sent as a
reply to the meme, a linked photo of a pet, and a photo attached to the request
all failed to reach the image model, because the drawing used only the photos
Kiki remembered to name. A given photo that the history search finds again isn't
checked or sent twice. Shared images and `photos` include link previews' images
(a tweet's photo), which the history search skips: in prod, "replace Sneaky with
Reece" about a pasted tweet would have been drawn without the tweet's photo.
A given message's images are its attachments, then its linked post's photos from
the site's reader (all of a tweet's, in `read_link`'s order, downloaded only from
the site's media hosts), or Discord's previews when no reader gets any: Discord's
preview of an X post shows at most one photo, and a link in <...> none. Given
photos keep the order they were given, so an edit's photo is the first.

In prod, "replace the person in the twitter picture with reece" was refused, and
three follow-ups were drawn without the tweet: "no reece is a guy, do exactly as
instructed" replied to nothing, "you can do it" replied to Kiki's refusal, and
"yes the selfie one" replied to nothing. Kiki couldn't have named it either: the
tool took only messages with attachments, and the repost had link previews.

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
Use the subjects' reference images only for how they look, not for their backgrounds,
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
  deadline (`AGENT_HOST_TIMEOUT_MS`, 180 s). Each picker batch has 30 s; past it,
  that batch's candidates are dropped (the subject is drawn from the others, or
  from its description). Measured on prod data with DeepSeek V4.1 Flash: about
  2-4 s to search, 7-17 s to pick from 24 candidates in two batches, $0.001-0.003
  per subject. That runs after the ack, while the conversation carries on; the
  old single check of 8 took 2-7 s and about $0.0001-0.005.
- PNG/JPEG/WebP/GIF results up to 8 MiB, typed by their bytes (`media/fetch.py` `sniff`).

## Observability

Each `agent_host_images` row has the final `prompt`, `model`, `subjects`,
`reference_images` (in order: `message_id` and `attachment_id` or a link
preview's `url`, `avatar_of`, or `drawing_id`), `cost_usd` and `latency_ms`. Each `agent_image_jobs` row has its
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
  `/v1/images` and rejects the new tool arguments, and one without
  `Drawing.shared` rejects drawings that carry it. Without `DISCORD_BOT_TOKEN` on
  the host, history photos older than about a day can't be downloaded, so drawings
  fall back to avatars and descriptions.

## Trade-offs

- **Two messages.** The ack and the picture are separate, about 15–25 s apart in
  prod tests. The issue's alternative, reacting 🎨 on the request, is still
  available to Kiki as her ack (a reaction alone).
- **Likeness is as good as the references.** The picker judges from the
  photo and its caption whether it plausibly shows the person; it can't recognize
  faces, so a person's references are their own selfie-like posts. People who
  never post photos of themselves get an avatar (if it's a real photo) or a
  description.
- **Replay.** If the host dies after drawing but before saving the result, a retry
  draws again (billed again), as before. Delivery isn't retried.
- **Chat-style image models** (`openai/gpt-5-image`) aren't supported; use their
  Images API equivalents.
- **Moderation** is the provider's; Kiki relays its refusal in her note.
- **Duplicates are prevented in the prompt only.** A run sees pictures already
  queued, but two runs in different conversations of one channel that answer the
  same request at the same moment can both queue it (seen once in prod).

## Tests

Python unit tests cover the OpenRouter request (references in order as data URLs,
the 16 cap), queuing (the ack is the model's own text; lenient arguments; bad refs
and photos as tool errors), reference gathering against a mock Discord (ranking,
expired-link refresh, attachment ranking, the picker's clearest-first choice, its
anchors and its retry on a bad answer, memory-cited photos, avatars,
self-portraits, ambiguous names, IDs in names, Kiki's drawings refused as
photos), the final
prompt, saved metadata, and failure notes. Rust tests cover the image job contract
and retries. `tools/sim/scenarios/generate-image.yaml` drives it end to end:
fakecord's `/v1/images` stub records the reference images it got (it rejects links:
references must be inline bytes), its CDN serves avatars and expires old links until
`/attachments/refresh-urls` re-signs them, its chat stub answers the reference
picker with JSON verdicts (memes and screenshots are out, a "clear" photo ranks
first) and writes the failure note. `reference-picking.yaml` checks that the
clearest photo leads, ahead of a newer blurry one, and the meme is left out. The scenario
checks two different acks pass through verbatim, the follow-up replies to the
request with the caption and image, the references and prompt the image model got,
the stored metadata, a refusal followed by Kiki's note (not the fallback), and a
picture with no ack.
