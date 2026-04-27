# NemoClaw + Hermes + Omni: Zero-to-Hero Cookbook

This guide takes you from a fresh machine to a working multimodal agent demo. By the end, a **Hermes Agent** inside a NemoClaw sandbox will analyze video, audio, and PDF documents with **Nemotron 3 Nano Omni 30B**, look up definitions on Wikipedia — and a deny-by-default policy will block every other website.

The setup connects three components:

- **NemoClaw** — creates the sandbox, applies the network policy, enforces the L7 egress filter
- **Hermes Agent** (Nous Research) — orchestrates skills and tracks context across turns
- **Nemotron 3 Nano Omni 30B** — multimodal model (video, audio, image, text, reasoning) served from the NVIDIA cloud

> **No GPU required.** Omni is served by NVIDIA's hosted endpoint.

## What you'll be able to do at the end

| Modality | Example prompt |
|---|---|
| Short video (≤ 2 min) | *"Analyze /tmp/clip.mp4 — what's happening?"* |
| Long video (any length) | *"Analyze /tmp/long-talk-chunks — give me three takeaways"* |
| Audio | *"Transcribe /tmp/podcast.mp3 and tell me the speaker's tone"* |
| PDF document | *"Read /tmp/paper-pages — what's the main argument?"* |
| Image | *"Describe what's in /tmp/screenshot.png"* |
| Jargon lookup | *"Look up 'unit vector' on Wikipedia with physics context"* |
| Policy demo | *"Try to fetch https://google.com — let's see NemoClaw block it"* |

All of it runs inside the same sandbox, with the same agent, behind the same policy. The optional [Web UI](#part-13-optional-the-web-ui) wraps the same stack with drag-and-drop, voice input, and a live policy ticker.

## Prerequisites

| Requirement | Details |
|---|---|
| Linux machine | Brev instance, DGX, or any Docker-capable host. No GPU required. |
| Docker | Installed and running. |
| NVIDIA API key | Starts with `nvapi-`, with Omni access. Get one at [build.nvidia.com](https://build.nvidia.com). |
| `ffmpeg` on the host | Needed for the synthetic test video and for long-video chunking. `apt install -y ffmpeg`. |
| `poppler-utils` on the host | Needed for PDF rendering (`pdftoppm`). `apt install -y poppler-utils`. |
| Node 20+ and `pnpm` (or `npm`) | Only if you want to run the optional Web UI in Part 13. |

## Part 1: Install NemoClaw

```bash
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
source ~/.bashrc
```

Verify:

```bash
nemoclaw --version
openshell --version
```

> This step only installs the CLI binaries. The onboarding wizard runs once in Part 2.

## Part 2: Onboard with the Hermes Agent

NemoClaw ships a first-class Hermes agent — pass `--agent hermes` during onboarding:

```bash
nemoclaw onboard --agent hermes
```

When prompted:

1. **Inference**: Choose `1` (NVIDIA Endpoints)
2. **API Key**: Paste your NVIDIA API key (`nvapi-...`)
3. **Model**: Choose `1` (Nemotron 3 Super 120B). You'll swap this to Omni in the next step — the onboard menu doesn't offer Omni directly.
4. **Sandbox name**: Enter a name — this guide uses `my-hermes`
5. **Policy presets**: Accept the suggested presets with `Y`

You should see:

```
✓ Sandbox 'my-hermes' created
✓ Hermes gateway launched inside sandbox
```

Verify:

```bash
nemoclaw my-hermes status
```

You should see `Phase: Ready` and the Hermes gateway listening on port 8642.

### Switch the gateway to Nemotron Omni

You picked Super 120B during onboarding because that's what the menu offers, but this cookbook needs **Omni** as the primary model so Hermes can handle video, audio, images, and PDFs. Swap the gateway's inference route now:

```bash
openshell inference set \
  --provider nvidia-prod \
  --model private/nvidia/nemotron-3-nano-omni-reasoning-30b-a3b
```

Verify:

```bash
openshell inference get
```

You should see `Model: private/nvidia/nemotron-3-nano-omni-reasoning-30b-a3b`.

### Update the Hermes display label too

The OpenShell gateway routes every call to whatever you just set, but Hermes's *own* config still says it's calling Super 120B and will print that label in its TUI banner. Fix it so the display matches reality:

```bash
openshell sandbox exec -n my-hermes -- bash -c "sed -i 's|nvidia/nemotron-3-super-120b-a12b|nvidia/nemotron-3-nano-omni-reasoning-30b-a3b|' /sandbox/.hermes-data/config.yaml"
```

If you skip this, the model name shown in the Hermes prompt header will be wrong even though the actual inference is hitting Omni — confusing during a demo.

## Part 3: Set Variables

```bash
export SANDBOX=my-hermes                       # whatever you named it in Part 2
```

The NVIDIA API key only needs to exist where you ran `nemoclaw onboard` — it lives in the OpenShell gateway's credential store from that point on. Scripts inside the sandbox reach Omni through the gateway and never handle the key directly.

Clone this cookbook (if you haven't already):

```bash
git clone https://github.com/PicoNVIDIA/vlmdemo.git
cd vlmdemo/hermes-omni-demo
```

## Part 4: Add the Knowledge-Lookup Policy Blocks

The baseline Hermes policy already allows the NVIDIA Omni API, PyPI, and a few Nous endpoints. We add two more whitelists — Wikipedia's summary API and the Free Dictionary API — so the jargon-lookup skill can do its job. `openshell policy set` replaces the full policy, so we export the current one, append the new blocks, and re-apply.

### 4a. Export the current policy

```bash
openshell policy get $SANDBOX --full > /tmp/raw-policy.txt
sed -n '8,$p' /tmp/raw-policy.txt > /tmp/current-policy.yaml
```

The first few lines of `raw-policy.txt` are a status header; line 8 onward is the YAML.

### 4b. Append the two new blocks

`policy/hermes-omni-lookup.yaml` in this repo contains the two blocks ready-to-paste (already indented to sit under `network_policies:`). Append them:

```bash
cat policy/hermes-omni-lookup.yaml >> /tmp/current-policy.yaml
```

### 4c. Apply the updated policy

```bash
openshell policy set --policy /tmp/current-policy.yaml $SANDBOX
```

You should see `✓ Policy version N submitted (hash: ...)`.

Verify the additions made it in:

```bash
openshell policy get $SANDBOX -v | grep -E "wikipedia|dictionary"
```

### What this policy enforces

| Destination | Method / path | Allowed binaries | Notes |
|---|---|---|---|
| `en.wikipedia.org` | `GET /api/rest_v1/page/summary/**`, `GET /w/api.php` | `python3.11` only | No `/wiki/` pages, no POSTs |
| `api.dictionaryapi.dev` | `GET /api/v2/entries/**` | `python3.11` only | Everything else denied |

`curl`, `wget`, and `browser_*` tools **cannot** reach either site. Anything outside these endpoints returns `403 Forbidden` at the L7 proxy.

## Part 5: Install the Skills

Hermes uses "skills" — self-contained `SKILL.md` manifests — to decide which tool to run for a given user request. We ship two:

- **video-analyze** — wraps `omni-video-analyze.py`. Handles **video, audio, image, PDF-pages, and chunk directories** (single skill, multiple input shapes — see [Part 11 / 12 / 10](#part-10-long-videos--chunk-and-synthesize)).
- **jargon-lookup** — wraps `lookup-jargon.py`. Wikipedia + Free Dictionary fallback.

Install both:

```bash
nemoclaw $SANDBOX skill install skills/video-analyze
nemoclaw $SANDBOX skill install skills/jargon-lookup
```

Verify:

```bash
openshell sandbox exec -n $SANDBOX -- hermes skills list
```

You should see both skills listed under `general`.

## Part 6: Upload the Scripts and SOUL.md

Upload the Python scripts the skills reference. Use a **trailing slash** on the destination — that puts the file *into* the directory instead of creating a nested folder:

```bash
openshell sandbox upload $SANDBOX scripts/omni-video-analyze.py /sandbox/.hermes-data/workspace/
openshell sandbox upload $SANDBOX scripts/lookup-jargon.py /sandbox/.hermes-data/workspace/
```

Make them executable:

```bash
openshell sandbox exec -n $SANDBOX -- chmod +x \
    /sandbox/.hermes-data/workspace/omni-video-analyze.py \
    /sandbox/.hermes-data/workspace/lookup-jargon.py
```

### Drop in SOUL.md

Hermes reads `SOUL.md` to decide which tool to reach for. Our SOUL tells Hermes:

- Use the `terminal` tool (not `execute_code`) to run the scripts
- Never try `browser_navigate` or `curl` for Wikipedia — call `lookup-jargon.py`
- Re-run the video script when the user asks a follow-up, instead of answering from memory

Hermes reads SOUL.md from **two paths** — keep both in sync:

```bash
openshell sandbox upload $SANDBOX memories/SOUL.md /sandbox/.hermes-data/memories/
openshell sandbox upload $SANDBOX memories/SOUL.md /sandbox/.hermes-data/
```

The script routes its Omni requests through the OpenShell gateway at `https://inference.local/v1/chat/completions` — the gateway proxies out to NVIDIA and injects the API key on the way. Nothing inside the sandbox needs to know the key.

## Part 7: Smoke Test with a Short Video

Omni needs a video file **inside the sandbox** to analyze.

### Generate a synthetic test clip with ffmpeg

```bash
ffmpeg -y \
  -f lavfi -i "testsrc=duration=20:size=320x240:rate=15" \
  -f lavfi -i "sine=frequency=440:duration=20" \
  -c:v libx264 -pix_fmt yuv420p -shortest \
  /tmp/test-video.mp4
```

(Or bring your own MP4 — keep it under ~2 minutes / ~9 MB for a single-call demo. Longer videos need [Part 10](#part-10-long-videos--chunk-and-synthesize).)

### Upload to the sandbox

The sandbox has its own filesystem — `/tmp/foo.mp4` on the host is **not** visible at `/tmp/foo.mp4` in the sandbox until you upload it.

```bash
openshell sandbox upload $SANDBOX /tmp/test-video.mp4 /tmp/
```

> Trailing slash on `/tmp/` matters — without it, `openshell sandbox upload` creates a directory named `test-video.mp4` and puts your file inside.

Verify:

```bash
openshell sandbox exec -n $SANDBOX -- ls -la /tmp/test-video.mp4
```

### Smoke-test the analyzer before touching Hermes

```bash
openshell sandbox exec -n $SANDBOX -- \
  python3 /sandbox/.hermes-data/workspace/omni-video-analyze.py /tmp/test-video.mp4 "What is in this video?"
```

You should see Omni describe what it sees plus a line like `[5878 tokens, 350KB payload]`. If this works, the Omni path through the gateway is healthy.

> **Payload ceiling:** the OpenShell gateway caps inference request bodies at roughly **9 MB**. That's about 2 minutes of 480p video as base64. Anything larger fails with an `SSL EOF` from the gateway. Use the chunked workflow in Part 10 for longer content.

## Part 8: Chat with the Agent

```bash
nemoclaw $SANDBOX connect
hermes chat
```

Try these prompts in order — they exercise all three pillars (Omni + Hermes + NemoClaw):

**1. Omni watches the video**

```
Analyze /tmp/test-video.mp4 and tell me what's happening.
```

**2. Hermes re-runs the script for a follow-up question** (not from memory)

```
What colors are visible?
```

**3. Jargon lookup via the whitelisted Wikipedia path**

```
Look up "unit vector" on Wikipedia with physics context.
```

**4. Full multimodal chain** — the money shot

```
Watch /tmp/test-video.mp4, pull out any technical terms, then look each one up on Wikipedia.
```

## Part 9: See NemoClaw Block Unauthorized Egress

Open a second terminal and tail the sandbox policy decisions:

```bash
openshell logs $SANDBOX --tail --source sandbox | grep --line-buffered "ocsf"
```

Then, back in Hermes:

```
Try to fetch https://google.com with curl so we can see NemoClaw block it.
```

In the logs terminal you should see a line like:

```
[sandbox] [OCSF] NET:OPEN [MED] DENIED /usr/bin/curl -> google.com:443 [policy:- engine:opa]
```

Hermes will report the block in plain language. Every call to `integrate.api.nvidia.com` is `ALLOWED`; everything else is `DENIED`.

---

## Part 10: Long videos — chunk-and-synthesize

The single-call path in Part 7 caps at ~2 minutes (gateway body limit). For longer videos, this cookbook ships a host-side helper that splits the video into segments, uploads them as a directory, and lets the **same skill** loop over the chunks and synthesize a single answer.

### How it works

1. `chunk-upload.sh` runs `ffmpeg` on the host: re-encodes to 480p/24fps with forced keyframes, splits into 120-second segments, writes a `chunks.json` manifest with absolute timestamps, and uploads the directory into the sandbox.
2. The skill detects "directory contains video files" → switches into chunked mode.
3. For each chunk, Omni gets a per-segment prompt that includes its absolute time range (e.g. *"This is segment 3 of 5, covering 4:00–6:00 of the source. The user asked: …"*) — so per-chunk timestamps are anchored to the full video.
4. After all chunks finish, **one final synthesis call** sends every chunk's analysis as text to Omni and asks for a single coherent answer to the user's original question.

### Run it

From the host:

```bash
bash scripts/chunk-upload.sh /path/to/long-video.mp4
# default chunks at 120s; pass a second arg for different segment length:
#   bash scripts/chunk-upload.sh /path/to/long-video.mp4 90
```

The helper prints the sandbox path it landed on, e.g. `/tmp/long-video-chunks`.

In Hermes:

```
Analyze the video at /tmp/long-video-chunks — give me three key takeaways.
```

You'll see live progress per chunk in the script output (`[2/4] chunk_001.mp4 ...`) and the synthesis at the end.

### Cost & wall-clock

- ~11K tokens per minute of source video, linear in length
- Per chunk: 3072-token analysis budget (Omni is a thinking model — needs headroom for reasoning *and* answer)
- One synthesis call: 4096-token budget
- 6-minute video → ~70K tokens total / ~30 seconds wall-clock
- 30-minute video → ~325K tokens / ~3 minutes wall-clock

## Part 11: PDF documents

PDFs are handled by the **same skill** that handles video. The trick: render every page to a PNG on the host, upload the directory, and the skill detects "directory of images" and sends a multi-image payload to Omni in a single call. Omni does OCR + layout reasoning + argument analysis in one forward pass — no chunking, no RAG.

### Run it

From the host:

```bash
bash scripts/pdf-upload.sh /path/to/document.pdf
```

The helper renders pages at 150 dpi, uploads the directory (e.g. `/tmp/document-pages`) into the sandbox, and prints the sandbox path.

In Hermes:

```
Read the document at /tmp/document-pages — what's the main argument and what's the weakest claim?
```

### Why this beats chunk-and-embed RAG for short documents

- One Omni call, all pages → no chunk-level inference loss
- Layout, figures, and tables stay together
- Cheap when the document fits in context (≤ ~50 pages comfortably)

For very long documents, fall back to chunking the PDF (e.g. 20 pages at a time) using the same pattern.

## Part 12: Audio

Audio files are handled by the **same skill** too — the script detects `.mp3` / `.wav` / `.m4a` extensions and sends an `input_audio` content block to Omni instead of a video. Omni hears tone, pacing, and content natively.

### Run it

```bash
openshell sandbox upload $SANDBOX /path/to/podcast.mp3 /tmp/
```

(For browser-recorded audio in `.webm` or `.opus`, transcode first:
`ffmpeg -i input.webm -c:a libmp3lame -b:a 96k input.mp3` — Omni's `input_audio` only accepts `.mp3` / `.wav`.)

In Hermes:

```
Listen to /tmp/podcast.mp3 — summarize in three bullets and tell me the speaker's tone.
```

## How modalities flow through the stack

```
                                ┌─────── OpenShell sandbox ───────┐
                                │                                 │
  HOST           UPLOAD HELPER  │  HERMES (the agent)             │   GATEWAY            NVIDIA CLOUD
  ────           ─────────────  │  ───────────────────            │   ───────            ───────────
  short MP4 ───► sandbox upload ─► /tmp/clip.mp4 ──┐              │                     ┌─►  Omni
                                │                  ├─► video-analyze ──► inference.local ┤
  long MP4 ───► chunk-upload.sh ─► /tmp/talk-chunks ┘  (one skill, │     (gateway        └─►  (rewrites
                (ffmpeg + segment │   detects shape: video?         │      injects          model →
                 + chunks.json)   │   chunks dir? audio?            │      API key)         Omni regardless)
                                │   image dir? image?)             │
  PDF ────────► pdf-upload.sh ──► /tmp/doc-pages   ─┤              │
                (pdftoppm 150dpi  │   (multi-image  │              │
                 + sandbox upload)│    payload)     │              │
                                │                  │              │
  audio ──────► sandbox upload ──► /tmp/clip.mp3 ──┘              │
  (mp3/wav)                       │                                │
                                └─────────────────────────────────┘
```

Same agent, same sandbox, same policy wall — one skill routes by filesystem shape and file extension. The gateway rewrites every model call to Omni, so the script never has to know which model it's talking to.

---

## Part 13: (Optional) The Web UI

Everything above runs from the CLI. This repo also ships a small web UI that wraps the same stack — drag-and-drop uploads, live policy ticker, voice input, memory drawer, hot-swap policy toggles — so you can demo the system to non-technical audiences.

The UI does **not** add capability — it talks to the same sandbox, the same Hermes, the same skills. It just provides a nicer surface.

### Architecture

```
   browser  ◄──── React UI (Vite)
              │
              ├── /api/upload         → server transcodes + sandbox uploads
              ├── /api/chat (SSE)     → server invokes hermes chat
              ├── /api/policy/stream  → tails openshell logs, parses OCSF events
              ├── /api/policy/toggle  → hot-swaps a named policy block
              └── /api/memory/summary → Hermes session index

   FastAPI server (host)
              │
              └── shells out to:  openshell, ffmpeg, pdftoppm, hermes
```

### Run it

```bash
# 1. install server deps
cd server
pip install -r requirements.txt
export OMNI_SANDBOX=$SANDBOX
uvicorn server:app --host 0.0.0.0 --port 8088

# 2. in a second terminal, install + run the UI
cd ../ui
pnpm install      # or: npm install
pnpm dev          # → http://localhost:5173
```

The UI proxies `/api/*` to the FastAPI server on port 8088 (configured in `vite.config.ts`).

### What's in `ui/src/components/`

| Component | What it does |
|---|---|
| `ChatPanel.tsx` | Streaming chat, file attachment, voice input, react-markdown rendering |
| `FlowDiagram.tsx` | Live `You → Sandbox { Hermes } → Omni` visualization that lights up nodes per turn |
| `PolicyDrawer.tsx` | Hot-swap toggles for individual policy blocks; live security check runner |
| `MemoryDrawer.tsx` | Session index — top tools, attachments, recent conversations |
| `PolicyTicker.tsx` | Bottom-of-screen ticker of OCSF allow/deny events parsed from sandbox logs |
| `AudioRecorder.tsx` | Browser MediaRecorder → backend `/api/transcribe` → Hermes |

### Long videos in the UI

The UI's drag-and-drop path goes through `/api/upload`, which is built for ≤ 9 MB single-call payloads. **For long videos, use `chunk-upload.sh` from a host shell** (Part 10) — the chunk directory ends up in the sandbox at `/tmp/<name>-chunks/`, and you can ask Hermes about it through the UI chat normally.

---

## How it all fits together

```
┌───────────────────────────────────────────────────────────────────┐
│  User                                                             │
│    │                                                              │
│    │  CLI (hermes chat)  ── or ──  Web UI (React → FastAPI)       │
│    ▼                                                              │
│  ┌──────────────────────────── OpenShell sandbox ────────────────┐│
│  │                                                               ││
│  │  Hermes Agent (Nous Research)                                 ││
│  │    reads SOUL.md + skills                                     ││
│  │    routes:  video/PDF/audio/image  → video-analyze skill      ││
│  │             definitions             → jargon-lookup skill     ││
│  │                                                               ││
│  │      ┌─ omni-video-analyze.py    ──────►  inference.local ───┼──►  Omni
│  │      │   (one script, every                  │   (gateway        (NVIDIA cloud)
│  │      │    modality — routes by               │   injects key)
│  │      │    filesystem shape +                 │
│  │      │    extension)                         │
│  │      │                                       │
│  │      └─ lookup-jargon.py ──► L7 proxy (allow: wikipedia /api/rest_v1/**,
│  │                                          api.dictionaryapi.dev /api/v2/**;
│  │                                          deny everything else → 403)
│  │                                       │
│  └────────────────────────────────────────┼──────────────────────┘│
│                                           ▼                        │
│                                       Wikipedia / Dictionary       │
└───────────────────────────────────────────────────────────────────┘
```

## Troubleshooting

| Issue | Fix |
|---|---|
| Hermes TUI banner shows the wrong model name (Super 120B) | Sandbox config never updated. Re-run the `sed` command in Part 2's "Update the Hermes display label too" step, then `/exit` and restart `hermes chat`. |
| `SSL EOF occurred in violation of protocol` from the script | Video payload exceeded the gateway's ~9 MB cap. Use `chunk-upload.sh` (Part 10), or trim with `ffmpeg -i big.mp4 -t 120 -c copy smaller.mp4`. |
| `'NoneType' object has no attribute 'strip'` mid-chunked-run | Omni used all chunk-call tokens reasoning and returned `content=null`. The shipped script falls back to `reasoning_content`; if you see this, you're running an older copy — re-upload `scripts/omni-video-analyze.py`. |
| `Connection refused` or DNS failure on `inference.local` | Sandbox lost its gateway route. Run `openshell inference get` to verify; re-run `openshell inference set ...` from Part 2 if not. |
| Hermes says "I don't have the ability to browse the web" | SOUL.md didn't load or didn't override the stale one at `/sandbox/.hermes-data/SOUL.md`. Re-run Part 6 — there are **two** SOUL files and both must match. Restart `hermes chat`. |
| Hermes calls `browser_navigate` or `curl` for Wikipedia | Same root cause: SOUL isn't steering. Confirm `grep "lookup-jargon" /sandbox/.hermes-data/SOUL.md` returns lines, restart chat. |
| `exit 126` when Hermes runs a script | Script lost its executable bit. `chmod +x` it inside the sandbox. |
| `No such file or directory: 'ffprobe'` when the sandbox analyzes video | The script has a pure-Python MP4 duration fallback — make sure `scripts/omni-video-analyze.py` is the v3 one shipped here, not an older copy. |
| Hermes uses `execute_code` and gets a network error | Wrong tool. SOUL.md says use `terminal`. Re-prompt: *"Use the terminal tool and run: python3 /sandbox/.hermes-data/workspace/omni-video-analyze.py /tmp/foo.mp4"* |
| Hermes hallucinates a name for the speaker on a long video | Omni has no face/voice grounding. Open the recording with a self-introduction, or add `Refer to the narrator as "the narrator" — do not assign a name unless they introduce themselves` to the prompt. |
| Hermes summarizes a follow-up from memory instead of running the script | Click "New chat" in the UI (or `/exit` and restart `hermes chat`) to clear session memory, then re-prompt. |
| `openshell sandbox upload DEST` creates a directory instead of a file | Known behavior — `DEST` is treated as a folder. Use a trailing slash on the parent dir, or upload + flatten as in Part 6. |

## Tailing logs

```bash
# Sandbox-side (policy decisions, OCSF-formatted ALLOW/DENY events)
openshell logs $SANDBOX --tail --source sandbox

# Gateway-side (OpenShell tunnel + command execution)
openshell logs $SANDBOX --tail --source gateway

# Demo-friendly view — only the policy verdicts
openshell logs $SANDBOX --tail --source sandbox | grep --line-buffered -E "ALLOWED|DENIED"
```

## Starting over

```bash
nemoclaw $SANDBOX destroy --yes
nemoclaw onboard --agent hermes
# Repeat Parts 3–8
```

To snapshot the sandbox before testing destructive changes:

```bash
nemoclaw $SANDBOX snapshot create
```

## Repo layout

```
hermes-omni-demo/
├── hermes-omni-guide.md        ← this file
├── policy/
│   └── hermes-omni-lookup.yaml   network-policy blocks for Wikipedia + Dictionary
├── memories/
│   └── SOUL.md                   Hermes identity / steering
├── skills/
│   ├── video-analyze/SKILL.md    routes video, audio, image, PDF-pages, chunked dirs
│   └── jargon-lookup/SKILL.md    Wikipedia + Free Dictionary
├── scripts/
│   ├── omni-video-analyze.py     v3 — single-file, multi-input, chunked w/ synthesis
│   ├── lookup-jargon.py          Wikipedia summary + dictionary fallback
│   ├── chunk-upload.sh           HOST helper — long video → chunks dir → upload
│   └── pdf-upload.sh             HOST helper — PDF → page PNGs → upload
├── server/                       Optional FastAPI backend for the Web UI
│   ├── server.py
│   └── requirements.txt
└── ui/                           Optional React + Vite + Tailwind frontend
    ├── package.json
    ├── vite.config.ts
    ├── tailwind.config.js
    ├── index.html
    └── src/
        ├── App.tsx
        ├── api/client.ts
        ├── components/{ChatPanel,FlowDiagram,PolicyDrawer,...}.tsx
        └── styles/index.css
```
