# AI Overlay Helper

A Windows desktop overlay that lets you fire defined prompts templates at any AI provider — with optional screenshot context — from anywhere, via a global hotkey.

## Requirements

- **Windows 10 / 11** (global hotkeys + tray; untested on other OSes).
- **Python 3.10+** (PyQt6 + modern type-hint syntax).
- An API key for at least one supported provider — or a local Ollama install.

## Quick start

```powershell
# 1. Create + activate a venv (optional but recommended)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install deps
pip install -r requirements.txt

# 3. Set the API key for whichever provider you'll use
$env:OPENAI_API_KEY = "sk-..."
# or: $env:ANTHROPIC_API_KEY = "sk-ant-..."
# or: $env:GEMINI_API_KEY = "..."

# 4. Run
python main.py
```

The app runs in the background with a system tray icon (left-click to summon the overlay, right-click for Settings/Quit). Trigger it with hotkeys or the tray.

## Providers & models

Four providers are supported. Pick one in **Settings → AI provider**: choose the provider from the dropdown, then type (or pick from suggestions) the exact model identifier. The provider/model pair is saved to `settings.yaml` and used for every Send until you change it.

| Provider    | Env var             | Example models                                                                  | Cost shown? |
| ----------- | ------------------- | ------------------------------------------------------------------------------- | ----------- |
| `openai`    | `OPENAI_API_KEY`    | `gpt-4o-mini`, `gpt-4o`, `gpt-4.1-mini`, `gpt-4.1`                              | yes         |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-7`                      | yes         |
| `gemini`    | `GEMINI_API_KEY`    | `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-2.0-flash`                        | yes         |
| `ollama`    | — (local)           | any model you've `ollama pull`ed (e.g. `llama3.2`, `qwen2.5-vl`)                | always $0   |

Keys live in environment variables only — never in `settings.yaml`. A `.env` file at the project root is loaded automatically (see `.env.example`). "Cost shown?" means the usage strip at the bottom of the overlay displays an estimated USD figure; models not in the built-in rate table still work, the dollar value is just hidden. Vision (screenshot) support depends on the model — pick a vision-capable one if you plan to use Extract / Analyze.

## Default hotkeys

| Hotkey             | Scope   | Action                                                                |
| ------------------ | ------- | --------------------------------------------------------------------- |
| `Ctrl+Alt+Space`   | global  | Summon the overlay — or hide it if it's already visible (toggle)      |
| `Ctrl+Alt+,`       | global  | Open settings                                                         |
| `Ctrl+Enter`       | overlay | Send the current prompt                                               |
| `Ctrl+Alt+→`       | overlay | Cycle to the next template                                            |
| `Ctrl+Alt+←`       | overlay | Cycle to the previous template                                        |

**Scope:** *global* hotkeys fire from anywhere on the desktop; *overlay* hotkeys only fire while the overlay window has focus. All five are changeable in **Settings → Hotkeys** — click **Edit** on a row, press the desired key combination, then **Done** to commit (or **Cancel** to discard). **Default** restores the built-in binding for that row.

**Per-template hotkey.** In **Settings → Templates** each template has its own optional global hotkey row. Pressing it from anywhere selects that template and summons the overlay — a "speed dial" for prompts you reach for often. Leave it empty for no binding.

**Duplicate-hotkey detection.** Settings refuses to bind the same combo in two places. When you commit a combo that's already used (whether by another row on the Hotkeys tab, or by a per-template hotkey), a dialog lists every conflicting row and offers **Overwrite** (clears the others) or **Cancel** (reverts your capture). Save / Apply re-runs the same check as a belt-and-suspenders pass.

## How templates work

A template is a string with `{variable}` placeholders. When you pick a template in the overlay, an input field is generated for each variable. Example:

```yaml
- name: Explain Code
  text: "Explain this code for a {level} developer. Focus on {aspect}."
  include_screenshot: true
```

…will show two text fields (`level`, `aspect`) and auto-attach a screenshot when you send.

**Per-variable options.** Each variable can also declare `default` (pre-filled value), `default_on` (whether its checkbox starts ticked in the overlay), and `prefix` / `suffix` — literal text inserted **before** and **after** the value when the variable is included. Toggling a variable off drops the whole `prefix + value + suffix` clause, so the surrounding sentence stays grammatical. Example:

```yaml
- name: Explain Code
  text: "Explain this code{level}{aspect}."
  variables:
    - name: level
      prefix: " for a "
      suffix: " developer"
      default: "senior"
      default_on: true
    - name: aspect
      prefix: ". Focus on "
      default: ""
      default_on: false
```

## Screenshots

Click **Take screenshot** in the overlay to drag-select a region of the screen; the captured image attaches to the next Send. Once a screenshot is attached the button becomes **Retake screenshot**, and two AI-assisted buttons appear next to it:

### Use globally

The checkbox under the **Take screenshot** button controls how captured screenshots are scoped:

- **Off (default)** — each screenshot is tied to the template that was active when it was taken. Switching to another template hides the preview (so the wrong image can't be sent by accident) and switching back restores it. Useful when you want to keep separate captures per template.
- **On** — a single shared screenshot is reused across every template, the original behavior. Useful when you want to run the same image through several different templates without re-capturing.

Flipping the toggle carries the **currently visible** screenshot with it so the preview never blanks out: checking it lifts the active template's shot into the shared slot, and unchecking it drops the shared shot back onto whichever template you're viewing at that moment. Other templates' stored shots are left alone.

### Keep attached

The second checkbox under **Take screenshot** controls whether a captured screenshot survives a Send. **On by default** — the screenshot is baked into the user turn and stays in the preview, so the next Send reuses the same image without forcing you to recapture. Clear it manually with the ✕ button or replace it with **Retake screenshot**. **Off** — the original behavior: each Send detaches the screenshot. Scope follows **Use globally**: the kept screenshot lives in the active template's slot by default, or in the shared slot when **Use globally** is on.

### Extract values

Visible only when the active template has `{variables}` **and** a screenshot is attached. Sends the image to the AI with the template's text as context and asks it to fill in each variable. Returned values are written straight into the input fields, so you can review/edit them before hitting Send.

Use it when you've built a ready template a want AI to help you fill the variables  — Extract reads them off the image so you don't have to type them.

### Analyze image…

Always visible while a screenshot is attached, even for templates with no variables (or with no template selected). Asks the AI to **design a brand-new template from scratch** based on what's in the image: a name, prompt text with `{placeholder}` markers, one variable per placeholder (with extracted value, prefix, and suffix), and whether the template should auto-attach a screenshot at use time.

The result opens in a **Review proposed template** dialog where you can edit every field before clicking **Save as new template**. The new template is written into `settings.yaml` and shows up in the picker immediately.

Use it when you see something on screen (an error, a UI, a chart, a snippet) and want a reusable prompt for that *class* of thing — without designing the template by hand.

Both calls run through the provider's structured-output path (OpenAI/Gemini `response_format`, Anthropic forced tool use, Ollama JSON schema), so the result is schema-validated before it reaches the UI. While either call is in flight the button shows **Extracting…** / **Analyzing…** and Send is disabled.

## Memory

The app has **two independent layers of memory**:

1. **Conversation history** — the default. Plain per-template chat thread you see in the response area, persisted to disk as JSON. Always on, no setup.
2. **Long-term memory** — optional. A ChromaDB-backed vector store (RAG): distills past exchanges and AI-generated summaries into embeddings, then injects the most relevant ones into future Sends. Requires extra packages and a per-template opt-in.

You can use the app fine with just layer 1. Layer 2 is for when you want the app to recall things across many sessions or threads.

Throughout the UI the optional layer is labeled **long-term memory**; in this document and in code comments you'll also see it called the "vector store" or "ChromaDB store" — same thing.

### Conversation history (default behavior)

Each template has its **own conversation thread**. When you send a follow-up question, the full thread for the current template (all prior user + assistant turns) is included in the API call, so the model has context. Switching to a different template shows that template's thread instead — threads don't cross-pollinate.

History is **persisted to disk** at `settings/conversations.json` and survives app restarts, so reopening the app brings you back to where you left off. The file is plain-text JSON; you can hand-edit or delete it.

**Screenshots are NOT persisted** — only a "with screenshot" marker is kept for display. The raw image was already sent to the model on the original turn, and we don't re-send it from disk. If a follow-up turn after a restart needs visual context, re-attach a fresh screenshot.

**Controlling the thread:**

- **New thread** button in the overlay — wipes the current template's history (asks for confirmation). Does **not** touch long-term memory.
- **Curate…** button — open the per-turn editor to delete/edit individual turns, or distill selected turns into long-term memory (if enabled).
- **Send** appends a new user turn (with whatever screenshot is currently attached). After Send, the live screenshot is auto-cleared so the next turn doesn't accidentally re-include it.

Cost note: each follow-up sends the *full* thread to the API, so token usage grows turn by turn. Hit **New thread** when you want to start fresh.

### Long-term memory (optional)

This is the **opt-in** RAG layer, persisted as a ChromaDB vector store at `settings/memory/`. When enabled for a template, each successful Send is embedded and saved; on subsequent Sends, the top-K most semantically similar past entries are pulled and prepended to the outgoing messages. The app remembers what *worked*, not your raw scrollback.

Two kinds of entries live in the same store and are searched together:

- **Raw exchanges** (`kind: exchange`) — saved automatically on every successful Send for memory-enabled templates.
- **Summaries** (`kind: summary`) — saved only when you explicitly use **Summarize → Memory…** in the Curate dialog (see below). These are the distilled lessons, not the literal turns.

#### Installation

Long-term memory deps are **optional** so the base install stays lean. Add them with:

```powershell
pip install -r requirements-memory.txt
```

That installs `chromadb` and `sentence-transformers`. You only actually need `sentence-transformers` if you pick the **local** backend below.

#### Two configuration steps

1. **Global switch.** In **Settings → Memory**, tick **Enable long-term memory**. This is the master toggle; flipping it off disables the whole subsystem. The app probes `chromadb` the instant you tick the box — if the package isn't installed you'll see an explicit error with the install command and the box will be auto-unchecked (no risk of silently broken state).
2. **Per-template opt-in.** In the template editor, check **Use long-term memory** for each template that should participate. Templates without it checked save/retrieve nothing — even with the global switch on.

Memory never crosses templates: your "Explain Code" entries won't leak into "Quick Q&A".

#### Embedding backends

Pick one in **Settings → Memory → Embedding backend**:

| Backend  | What it uses                                                    | When to pick it                                              |
| -------- | --------------------------------------------------------------- | ------------------------------------------------------------ |
| `gemini` | Gemini `gemini-embedding-001` via your existing `GEMINI_API_KEY` | **Default.** Same key as your AI provider, tiny per-call cost (~$0.00001), best quality. |
| `openai` | OpenAI `text-embedding-3-small` via `OPENAI_API_KEY`            | If you already use OpenAI for chat — same key, similar tiny cost. |
| `local`  | `sentence-transformers/all-MiniLM-L6-v2`                        | Free, offline. Downloads ~80MB on first run, slightly worse quality, no API key needed. |

The model field is an **editable dropdown** with curated suggestions per backend — pick one, or type a custom name (e.g. another sentence-transformers ID, or a fine-tuned model). Changing the backend resets the model to that backend's default since they use different model names and embedding dimensions.

**Verify before you commit.** Click **Test embedding** in **Settings → Memory** to run a real round-trip embed against the current backend + model. It catches: deprecated models, missing API keys, network issues. For the `local` backend it also triggers the one-time ~80MB download right then, so the first real Send isn't a silent 60-second hang.

**Note**: ChromaDB pins the embedding dimensionality per collection. If you switch backends after building up history, the new embedding won't match the existing store. Use **Settings → Memory → Clear all memory** to start over with the new backend.

**Error visibility.** When a Send's retrieve or save fails, the usage strip at the bottom of the overlay shows `⚠ memory error`; hover for the full error message and a hint about the likely cause.

#### Summarize → Memory

In the **Curate…** dialog, check one or more turns and click **Summarize → Memory…** to ask the AI to distill them into a concise lesson. You can edit the summary before it's saved.

The summary is then written into **ChromaDB** (the same vector store as raw exchanges, but tagged `kind: summary`) — *not* into `conversations.json`. Practical consequences:

- The original Q/A turns in your thread are **not** modified or deleted by summarization. The thread keeps showing what you actually said and got back.
- The summary **survives "New thread"** — once distilled, it lives in long-term memory independent of any conversation history. You can wipe your thread freely without losing curated knowledge.
- Summaries are searched together with raw exchanges on future retrievals; the most semantically similar entries win regardless of kind.
- To delete summaries, use **Settings → Memory → Clear all memory** (this also clears raw exchanges — currently the only "wipe" path exposed in the UI). Deleting `settings/memory/` from disk has the same effect.

#### Where data lives

- Vector store: `settings/memory/` (a ChromaDB persistent directory; managed by chromadb itself).
- Configuration: `settings/settings.yaml` under the `memory:` key.

You can delete `settings/memory/` at any time to wipe everything.

#### Retrieval indicator

When a Send used long-term memory, the usage strip at the bottom of the overlay shows e.g. `🔍 3 memories` for that call.

What that number counts:

- It's the number of **ChromaDB entries** prepended to the outgoing messages on that Send — a mix of raw exchanges and/or summaries, whichever were most similar to your new prompt. So `🔍 3 memories` might be 3 exchanges, 3 summaries, or any combination.
- It does **not** count your current chat thread. The thread is sent separately as real message turns and isn't subject to similarity ranking.

If you don't see the indicator, either: the template doesn't have **Use long-term memory** checked, the global switch is off, the store had nothing relevant to retrieve, or chromadb isn't installed.

## Project layout

```
main.py                       # entry point: wires tray, hotkeys, overlay, settings
settings/settings.yaml        # user config (auto-created on first run)
settings/conversations.json   # per-template chat history (auto-created)
settings/memory/              # ChromaDB vector store (only if long-term memory enabled)
.env / .env.example           # API keys (loaded via python-dotenv)
requirements.txt              # base deps
requirements-memory.txt       # optional: chromadb + sentence-transformers
src/
├── ai_client.py              # provider-agnostic chat + structured-output client
├── config.py                 # settings.yaml load/save + defaults
├── conversations.py          # per-template chat thread persistence
├── hotkeys.py                # global hotkey registration (pynput ↔ Qt)
├── memory.py                 # ChromaDB-backed long-term memory (RAG)
├── screenshot.py             # mss-based region capture
├── templates.py              # template parsing + variable rendering
├── variable_resolver.py      # Extract values + Analyze image AI calls
├── worker.py                 # QThread helpers for streaming + one-shot AI calls
├── logger.py                 # logging config
└── ui/
    ├── overlay.py            # main translucent overlay window
    ├── settings_window.py    # Settings dialog (provider, hotkeys, memory, …)
    ├── tray.py               # system tray icon + menu
    ├── region_selector.py    # drag-rectangle screenshot picker
    ├── proposal_dialog.py    # Review-and-save dialog for Analyze image
    └── curate_dialog.py      # per-turn history editor + Summarize → Memory
```

## Debug logging

Logging is configured in `settings/settings.yaml`:

```yaml
logging:
  enabled: true        # set false to silence ALL log output
  level: INFO          # DEBUG | INFO | WARNING | ERROR | CRITICAL
```

Edit it in the Settings window, or directly in the YAML file. Main entry points (startup, hotkey fired, send, settings save) log at `INFO` and are tagged with `[main]`, `[startup]`, `[hotkey]`, `[send]`, `[settings]` — so the default `INFO` level shows app activity, and `DEBUG` adds fine-grained detail. Set `enabled: false` to silence everything.

## Stopping the app

Right-click the tray icon → **Quit**.
