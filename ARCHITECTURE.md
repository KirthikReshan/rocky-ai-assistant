# Rocky architecture and milestones

## Current: voice, conversation, desktop/web opening, reminders and approved code edits

Tkinter desktop UI → bounded message queue → voice worker → Assistant router.
The router handles basic greetings/time and deterministic desktop/web commands,
then sends conversation through the
Brain protocol to OllamaBrain at the fixed loopback endpoint. Ollama requests have
timeouts, bounded in-memory history and explicit error translation. Model text
cannot invoke tools. Voice capture pauses while generation and speech are running.
Typed immutable Action objects route explicit user commands into DesktopTools.
App names are allowlisted and resolved from trusted Windows directories,
registered Windows App Paths or fixed installation folders. CWD and PATH are
never searched for app executables. Folders must
exist; website URLs must use HTTP/HTTPS without embedded credentials. No shell
execution or AI-generated action execution is provided. YouTube metadata lookup
runs in a subprocess with a 30-second deadline and validates the returned video ID.
ReminderCommands runs before the desktop/chat router and writes parameterized
SQLite transactions through Store. Each thread gets its own short-lived connection.
The Tk loop polls UTC due times every second, displays modeless reminder popups,
persists notification acknowledgement, and queues spoken announcements separately
from user commands. Thus reminder text is never interpreted as a tool command.
CodeCommands routes explicit code requests before other commands. Workspace holds
an opt-in session project root; Project bounds reads/searches and excludes hidden
paths, known credentials, binaries, large files, dependencies and link traversal.
OllamaBrain.code_chat uses a separate ephemeral request with source snippets as
reference data. Code answers cannot invoke tools or write files. A dedicated Code
tab displays full output while speech announces a short completion message.
Reopening the database restores overdue pending alarms. Delivery is acknowledged
after popup creation; a crash in between may repeat a notification. App must be
running to deliver; no startup registration or background Windows service is installed.
Microphone → 16 kHz mono PCM → Vosk constrained wake recognizer →
tone → WebRTC speech/silence endpoint → local Whisper small.en CPU/int8 transcription
→ reply → local Piper (Windows SAPI fallback). Vosk command fallback remains available.
Command audio is held in memory, bounded to 30 seconds with 300 ms of pre-roll.
Endpointing waits 1.5 seconds of actual non-speech rather than partial-text stability.
After a reply, a 30-second follow-up window allows conversation without another
wake phrase. Silence or an explicit end phrase returns to wake mode. Pause resets
the session. Requests use a JSON reply schema and discard tagged reasoning before
display, speech and history. Qwen's thinking-off hints alone were insufficient with
the installed model template; a live regression reproduced the issue.
The microphone stream closes during speech to reduce self-triggering. No recordings
are saved or transmitted. Listening continues while the application is running;
Windows sleep, application exit, and reboot stop it. Startup registration is a later task.
The worker owns COM and audio; only the UI thread touches widgets.

## Next milestones, each verified before proceeding

1. Calibrate wake detection against the user's voice, microphone and background noise.
   Measure false activations and misses. Compare a trained openWakeWord model if needed.
   Replace baseline SAPI with local Piper for a more natural voice, checking voice licenses.
2. Implemented: brain provider interface and Ollama adapter (localhost, explicit timeouts).
   Start with Qwen3 4B and measure latency/VRAM on this laptop.
   Add optional NVIDIA NIM through the same interface; cloud requests are opt-in.
3. Typed tool requests and the expiring PC approval gate are implemented. Allow searches/YouTube
   without confirmation. Require explicit approval bound to the exact pending action
   for closing apps, Windows shutdown and code edits. Model output never executes shell.
4. Implemented app/folder/URL opening, Google searches, YouTube search and first-result
   watch-page opening with an autoplay request. Browser policies can prevent playback;
   playback is not falsely reported as verified. Network failures are surfaced.
5. Implemented user-selected project root, bounded read-only file listing/search,
   source explanation, code review and error explanation. No persistent code index.
   Implemented: EditService binds one exact immutable proposal to a project-selection
   generation, source file identity and original bytes. The model proposes old/new
   snippets only; validation builds the exact diff and rejects ambiguity/overlap.
   Only the preview UI approval callback calls apply; voice/text commands cannot
   approve. Apply opens a Windows-exclusive source handle (share mode 0), validates
   its identity/bytes/mode, durably backs up bytes and hashes, and writes/verifies
   the approved result through that same handle. Other writes/replaces are blocked.
   Caught write errors restore the original under the lock; failed rollback or
   process/power interruption may require manual backup restoration. Source file
   identity and ACLs are preserved. Python validation compiles without execution.
   Pending approval is
   one-shot, expires after ten minutes and does not survive restart. No automatic
   execution, file deletion/creation, multi-file edit or backup restore is provided.
   Do not index secrets, credentials or arbitrary laptop folders automatically.
6. Implemented SQLite notes/reminders in the Windows user data directory, UTC due
   timestamps, original command, timezone and notification state. Ambiguous times
   request clarification; pending reminders survive restart and overdue alarms are
   shown on reopening. Next: startup/tray support for delivery after login;
   no reminder can sound while the laptop is powered off.

## Document workspace (0.12.0)

`DocumentsPage` lives inside the main Tk window. Its background task communicates
through the existing event queue; only the main thread updates widgets. `Documents`
owns a separate Ollama client, a selected-folder identity, a bounded extraction
cache and a single-task lock. Folder changes invalidate results. Cancellation
stops extraction and prevents pending model results from reaching the viewer.

`document_io` decodes PDF, DOCX, XLSX, PPTX, UTF-8 text/Markdown, CSV, HTML and
images in an isolated process. Native PDF rendering and RapidOCR run locally.
File size, expansion, page, text, row and runtime limits bound extraction.
The frozen executable dispatches the same worker without opening a console.
Text blocks retain page, paragraph, line, slide or table-row references.

Exact and keyword lookup use extracted text. Semantic lookup accepts only source
identifiers and quotes verified against the text. Summaries and Q&A process source
chunks with stable citation identifiers; longer results are combined in bounded
groups. Source text is reference data and cannot execute actions. Study tools use
structured question/answer/source records. Translation is model-generated and
requires review; the installed 4B model is unreliable for some languages.

`document_export` writes derived TXT, Markdown, DOCX and CSV results atomically to
the chosen destination and refuses to overwrite source documents. Original
documents remain read only. Export publication is exclusive: an existing file,
including one created during generation, cannot be replaced. The packaged
`--documents-check` exercises real
Office extraction, image/PDF OCR, export and the embedded viewer using temporary
synthetic files. No user document is needed for release verification.

## Models by task (0.13.0)

`TaskBrain` extends the Ollama provider with immutable per-launch settings and an
independent document client. Chat/history, code and document operations retain
existing validation and approval services. Specialist translation receives plain
source text and appends references in deterministic code. A shared local lock
serializes inference; switching releases the previous main model used by Rocky.
Embeddings use CPU. Cancellation bypasses fallback; a partially streamed answer
is never replayed on a second model.

`DocumentIndex` now uses a bounded RAM cache; the old vectors-only SQLite cache is
retired. Keys hash source text/locations and the embedding model digest. Preparation
must finish before normal document Q&A. Summaries retain complete chunk coverage.

`ModelSettingsPage` persists validated model IDs and opt-in NIM flags in Store;
keys use a database-scoped Windows Credential Manager target. The NIM adapter
allows only NVIDIA's fixed TLS host and disallows redirects. It shares no chat
history, emits no tools and never logs request headers or response error bodies.
Only enabled code/document operations use NIM. Failure falls back to local
inference; file edits still require EditService's exact-diff approval.

## Document sessions and evidence (0.14.0)

`DocumentIndex.prepare` caches read-only normalized arrays and source chunks in an
LRU within this process. Closing or clearing increments its generation, drops all
entries and invalidates in-flight publication. Opening a file starts preparation
in the existing background worker. Short rows/paragraphs are packed for efficiency;
physical PDF pages keep separate references. Hybrid ranking fuses lexical BM25
and dense similarity; nearby passages supply cross-sentence context.

`GroundedQA` extracts source-labelled quotations and matches them against the
extracted text. Enclosing sentences and qualifying exceptions are retained.
Only verified evidence reaches answer composition. The response must have valid
citations and supported numbers; a separate request checks support/contradictions.
Failure produces an extractive answer or explicit not-found result. Large evidence
sets are shown completely rather than truncated for a short synthesis.

`Documents.clear_memory` cancels work, invalidates workspace identity and clears
the parsed cache and index. `DocumentText` weakly references the session generation
so queued old output cannot be displayed/spoken. Document messages are separately
tagged in the chat log and removed on clear; reminders are preserved. Close calls
the same cleanup. Recognised legacy caches are removed only after checking that
they contain the expected vectors-only schema and are not links.

## Acceptance criteria

Use a recorded test ledger: at least 50 representative commands in a quiet room
and normal background noise, reporting successful intent+argument recognition / total.
Target >=80% for each condition; this is not a current measured claim. Separately
measure wake recall, false wakes per hour, response latency and CPU/RAM consumption.
Exercise pause/resume, device removal, repeated speech, shutdown, offline startup,
reminder recovery, and allow/deny paths for every sensitive action as features arrive.
