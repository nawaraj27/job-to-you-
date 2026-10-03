/**
 * Shared front-end behaviour.
 *
 * Deliberately small: Alpine handles per-component state in markup, HTMX handles
 * partial updates from Django. Anything global lives here.
 */

(function () {
  "use strict";

  /* --- Theme -------------------------------------------------------- */
  // Persisted in a cookie (not localStorage) so Django renders the correct
  // theme on the very first response — no flash, no client-side correction.
  const THEME_COOKIE = "theme";

  function setCookie(name, value, days) {
    const expires = new Date(Date.now() + days * 864e5).toUTCString();
    document.cookie = `${name}=${value}; expires=${expires}; path=/; SameSite=Lax`;
  }

  function currentTheme() {
    return document.documentElement.classList.contains("dark") ? "dark" : "light";
  }

  const systemQuery = window.matchMedia("(prefers-color-scheme: dark)");

  // Three states, not two. "system" is a real choice — it means "keep
  // following the OS, including when it flips at sunset" — so it is stored as
  // itself rather than resolved once and frozen into light or dark.
  // The theme is stored twice — a cookie so it applies before the first paint,
  // and the account record so Settings can show it. Both are written here, by
  // whichever control changed it. Writing only the cookie left the header
  // switch and Settings › Appearance disagreeing about what the theme was.
  function persistTheme(theme) {
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    if (!token) return;
    fetch("/settings/appearance/act/", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": token,
      },
      body: "command=theme&value=" + encodeURIComponent(theme),
      keepalive: true,
    }).catch(() => { /* the cookie already applied it; the record catches up */ });
  }

  function setTheme(theme, { persist = true } = {}) {
    const root = document.documentElement;
    const resolved = theme === "system"
      ? (systemQuery.matches ? "dark" : "light")
      : theme;
    root.classList.toggle("dark", resolved === "dark");
    root.dataset.themeSource = theme;
    setCookie(THEME_COOKIE, theme, 365);
    if (persist) persistTheme(theme);
    document.dispatchEvent(new CustomEvent("theme:changed", {
      detail: { theme: resolved, source: theme },
    }));
  }

  systemQuery.addEventListener("change", () => {
    if (document.documentElement.dataset.themeSource === "system") setTheme("system");
  });

  window.CareerAI = window.CareerAI || {};
  window.CareerAI.toggleTheme = function () {
    setTheme(currentTheme() === "dark" ? "light" : "dark");
  };
  window.CareerAI.setTheme = setTheme;
  window.CareerAI.currentTheme = currentTheme;

  /* --- Announcements ------------------------------------------------- */
  // One polite live region for the whole page. Confirmations that are obvious
  // to a sighted user — an icon filling in, a row disappearing — are silent
  // otherwise. The text is cleared first so repeating the same action twice
  // is still announced twice.
  window.CareerAI.announce = function (message) {
    const region = document.getElementById("announcer");
    if (!region) return;
    region.textContent = "";
    window.setTimeout(() => {
      region.textContent = message;
    }, 60);
  };

  // Anything marked `data-announce` is read out when it appears, on a full
  // page load and after every htmx swap alike. Confirmations used to be
  // silent: a line of green text saying "Version saved" tells a screen reader
  // nothing, and this is the region built for them.
  function announceSwappedIn(root) {
    if (!root || !root.querySelectorAll) return;
    const marked = root.matches && root.matches("[data-announce]")
      ? [root]
      : Array.from(root.querySelectorAll("[data-announce]"));
    const message = marked
      .map((el) => (el.getAttribute("data-announce") || "").trim())
      .filter(Boolean)
      .join(". ");
    if (message) window.CareerAI.announce(message);
  }

  document.addEventListener("DOMContentLoaded", () => announceSwappedIn(document.body));
  // `detail.target` is the element the response was swapped into;
  // `detail.elt` is the control that fired the request, which is not it.
  document.addEventListener("htmx:afterSwap", (event) =>
    announceSwappedIn(event.detail.target || event.target)
  );

  /* --- Alpine components -------------------------------------------- */
  document.addEventListener("alpine:init", () => {
    const Alpine = window.Alpine;

    // Header: dropdown menus, mobile drawer, scroll state.
    Alpine.data("siteHeader", () => ({
      openMenu: null,
      mobileOpen: false,
      scrolled: false,

      init() {
        this.onScroll();
        window.addEventListener("scroll", () => this.onScroll(), { passive: true });
      },
      onScroll() {
        this.scrolled = window.scrollY > 4;
      },
      toggleMenu(name) {
        this.openMenu = this.openMenu === name ? null : name;
      },
      isOpen(name) {
        return this.openMenu === name;
      },
      closeMenus() {
        this.openMenu = null;
      },
      toggleMobile() {
        this.mobileOpen = !this.mobileOpen;
        document.body.classList.toggle("overflow-hidden", this.mobileOpen);
      },
      closeMobile() {
        this.mobileOpen = false;
        document.body.classList.remove("overflow-hidden");
      },

      // One method rather than `closeMenus(); closeMobile()` in the template:
      // the CSP build of Alpine parses an expression, not a statement list, and
      // two calls separated by a semicolon are two statements (ADR-A31).
      closeEverything() {
        this.closeMenus();
        this.closeMobile();
      },
    }));

    /**
     * One interview question: the timer, and the integrity counters (P16).
     *
     * The counters are declared on the page in plain words rather than
     * collected silently — a candidate is entitled to know what is recorded
     * about them, and what is recorded is three integers.
     *
     * The timer never submits the form. Running over a time box is recorded and
     * is explicitly not a scoring input (FR-10.4), so a countdown that cut
     * somebody off mid-sentence would be enforcing a rule the product does not
     * have.
     *
     * `seconds` is what is *left*, worked out by the server from when the
     * question was first shown. It used to be the whole allowance, so a
     * refresh gave somebody a fresh four minutes on the same question.
     *
     * The draft is kept in this browser as it is typed, keyed by the question,
     * and put back after a refresh or a dropped connection. Losing three
     * minutes of typing to a reload is the likeliest way to lose a candidate.
     * It is cleared on submit and never leaves the browser.
     *
     *   <div x-data="interviewTurn(180, 'interview-draft-42')">
     */
    Alpine.data("interviewTurn", (seconds = 240, draftKey = "") => ({
      seconds,
      draftKey,
      pastes: 0,
      blurEvents: 0,
      keystrokes: 0,
      restored: false,
      submitting: false,

      init() {
        this.ticker = setInterval(() => {
          if (this.seconds > 0) this.seconds--;
        }, 1000);

        // A question page brought back by the Back button from the browser's
        // page cache is a picture of a question that may already be answered.
        // Asking the server again shows whatever is being asked now. This is
        // courtesy only: the server refuses an answer to anything but the
        // current question whatever the page shows (engine.StaleQuestion).
        this.onRestore = (event) => {
          if (event.persisted) window.location.reload();
        };
        window.addEventListener("pageshow", this.onRestore);

        const box = this.$refs.answer;
        if (box && this.draftKey && !box.value) {
          try {
            const saved = window.localStorage.getItem(this.draftKey);
            if (saved) {
              box.value = saved;
              this.restored = true;
            }
          } catch (error) {
            // Storage blocked (private window, policy). The draft is a
            // convenience; the interview works without it.
          }
        }
      },

      saveDraft(event) {
        this.keystrokes++;
        if (!this.draftKey) return;
        try {
          window.localStorage.setItem(this.draftKey, event.target.value);
        } catch (error) {
          // See init().
        }
      },

      /**
       * The next question can take a while: a follow-up is written by a model
       * after reading this answer, and on a local model that measured 14-18
       * seconds. The button said nothing during that wait, which reads as
       * broken and invites a second click. It says what is happening now.
       */
      onSubmit(event) {
        // A second click while the first is on its way would post the same
        // answer again. The server treats that as the answer it already has;
        // not sending it is simply quieter.
        if (this.submitting) {
          event.preventDefault();
          return;
        }
        this.submitting = true;
        this.clearDraft();
      },

      clearDraft() {
        if (!this.draftKey) return;
        try {
          window.localStorage.removeItem(this.draftKey);
        } catch (error) {
          // See init().
        }
      },

      destroy() {
        clearInterval(this.ticker);
        window.removeEventListener("pageshow", this.onRestore);
      },

      clock() {
        const minutes = Math.floor(this.seconds / 60);
        const rest = String(this.seconds % 60).padStart(2, "0");
        return `${minutes}:${rest}`;
      },

      payload() {
        return JSON.stringify({
          pastes: this.pastes,
          blur_events: this.blurEvents,
          keystrokes: this.keystrokes,
        });
      },
    }));

    /**
     * Answering one question aloud (spoken practice).
     *
     * Records the **microphone track only** — the camera stream feeds a local
     * preview and is never given to the recorder or sent anywhere. The audio
     * goes to the server once, comes back as text in the answer box, and the
     * candidate edits and submits it like a typed answer.
     *
     * Every failure lands in a named state with words on the page (see
     * templates/interviews/_voice_answer.html), and typing works in all of
     * them. Leaving the tab mid-answer stops the recording and transcribes what
     * was said, rather than recording a silent tab for four minutes.
     *
     *   <section x-data="voiceAnswer(url, turnId, maxSeconds, speechAvailable)">
     */
    Alpine.data("voiceAnswer", (url = "", turn = 0, maxSeconds = 240, speechAvailable = true) => ({
      url,
      turn,
      maxSeconds: Math.max(Number(maxSeconds) || 0, 30),
      speechAvailable,
      state: "idle",
      message: "",
      notes: [],
      hasVideo: false,
      elapsed: 0,
      stream: null,
      recorder: null,
      chunks: [],
      mime: "",

      init() {
        if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) || typeof window.MediaRecorder === "undefined") {
          this.state = "unavailable";
        }
        // The sealed timings came back with the transcript and lived only in a
        // hidden field, so a reload kept the text (the draft) and silently
        // dropped the measurements: the answer was submitted with no delivery
        // data. They are kept beside the draft now, per question.
        try {
          const kept = window.localStorage.getItem(this.sealedKey());
          const field = document.getElementById("delivery-sealed");
          if (kept && field && !field.value) field.value = kept;
        } catch (error) {
          // Storage blocked. The answer still submits; only the timings are lost.
        }
        const form = document.getElementById("answer") && document.getElementById("answer").form;
        if (form) form.addEventListener("submit", () => this.forgetSealed());
      },

      sealedKey() {
        return "interview-delivery-" + String(this.turn);
      },

      forgetSealed() {
        try {
          window.localStorage.removeItem(this.sealedKey());
        } catch (error) {
          // See init().
        }
      },

      destroy() {
        clearInterval(this.ticker);
        this.release();
      },

      release() {
        if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
        this.stream = null;
        this.hasVideo = false;
      },

      startWithCamera() {
        this.open(true);
      },

      startAudioOnly() {
        this.open(false);
      },

      async open(withVideo) {
        this.state = "requesting";
        try {
          this.stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: withVideo });
        } catch (error) {
          const name = error && error.name;
          if (withVideo && (name === "NotFoundError" || name === "NotReadableError" || name === "OverconstrainedError")) {
            // No usable camera is not a reason to stop somebody answering aloud.
            return this.open(false);
          }
          this.state = name === "NotAllowedError" || name === "SecurityError" ? "denied" : "unavailable";
          return;
        }
        this.hasVideo = this.stream.getVideoTracks().length > 0;
        if (this.hasVideo && this.$refs.preview) this.$refs.preview.srcObject = this.stream;
        this.state = this.speechAvailable ? "ready" : "nospeech";
      },

      retry() {
        this.message = "";
        this.state = this.stream ? (this.speechAvailable ? "ready" : "nospeech") : "idle";
      },

      record() {
        if (!this.stream) {
          this.state = "idle";
          return;
        }
        const audioOnly = new MediaStream(this.stream.getAudioTracks());
        const types = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
        this.mime = types.find((type) => window.MediaRecorder.isTypeSupported(type)) || "";
        this.chunks = [];
        try {
          this.recorder = this.mime ? new window.MediaRecorder(audioOnly, { mimeType: this.mime }) : new window.MediaRecorder(audioOnly);
        } catch (error) {
          this.state = "unavailable";
          return;
        }
        this.recorder.ondataavailable = (event) => {
          if (event.data && event.data.size) this.chunks.push(event.data);
        };
        this.recorder.onstop = () => this.upload();
        this.recorder.start(1000);
        this.elapsed = 0;
        this.state = "recording";
        this.ticker = setInterval(() => {
          this.elapsed++;
          if (this.elapsed >= this.maxSeconds) this.stop();
        }, 1000);
      },

      stop() {
        clearInterval(this.ticker);
        if (this.recorder && this.recorder.state !== "inactive") {
          this.state = "transcribing";
          this.recorder.stop();
        }
      },

      onHidden() {
        if (document.hidden && this.state === "recording") this.stop();
      },

      recordingClock() {
        const minutes = Math.floor(this.elapsed / 60);
        return minutes + ":" + String(this.elapsed % 60).padStart(2, "0");
      },

      async upload() {
        const type = (this.recorder && this.recorder.mimeType) || this.mime || "audio/webm";
        const blob = new Blob(this.chunks, { type });
        this.chunks = [];
        if (!blob.size) {
          this.fail("Nothing was recorded. Check your microphone and record again.");
          return;
        }
        const extension = type.indexOf("mp4") >= 0 ? "m4a" : type.indexOf("ogg") >= 0 ? "ogg" : "webm";
        const form = new FormData();
        form.append("turn", String(this.turn));
        form.append("audio", blob, "answer." + extension);
        const token = document.querySelector("input[name=csrfmiddlewaretoken]");

        let response;
        try {
          response = await fetch(this.url, {
            method: "POST",
            body: form,
            credentials: "same-origin",
            headers: { "X-CSRFToken": token ? token.value : "" },
          });
        } catch (error) {
          this.fail("The recording could not be sent. Check your connection and record again, or type your answer.");
          return;
        }

        let data = {};
        try {
          data = await response.json();
        } catch (error) {
          data = {};
        }
        if (!response.ok) {
          this.fail(data.error || "Transcription failed. Record again, or type your answer.");
          return;
        }

        const box = document.getElementById("answer");
        if (box) {
          box.value = data.text || "";
          box.dispatchEvent(new Event("input", { bubbles: true }));
          box.focus();
        }
        const sealed = document.getElementById("delivery-sealed");
        if (sealed) sealed.value = data.sealed || "";
        try {
          if (data.sealed) window.localStorage.setItem(this.sealedKey(), data.sealed);
        } catch (error) {
          // See init().
        }
        this.notes = Array.isArray(data.feedback) ? data.feedback : [];
        this.state = "review";
      },

      fail(message) {
        this.message = message;
        this.state = "error";
      },
    }));

    // Generic disclosure, used by collapsible report sections and FAQs.
    Alpine.data("disclosure", (open = false) => ({
      open,
      toggle() {
        this.open = !this.open;
      },
    }));

    /**
     * Modal dialog: focus trap, Escape, and focus restore.
     *
     * A dialog that does not manage focus is a keyboard trap in reverse —
     * Tab walks straight out of it into the page behind, which is still
     * scrollable and still clickable, and the user has no way to tell where
     * they are. WCAG 2.4.3 (focus order) and 2.1.2 (no keyboard trap) both
     * land here, and it is the one interaction on the site that genuinely
     * needs JavaScript to be correct.
     *
     *   <div x-data="dialog" @keydown.escape.window="close()">
     *     <button @click="open()">Filters</button>
     *     <div x-show="isOpen" x-ref="panel" role="dialog" aria-modal="true">…</div>
     */
    /**
     * The "/" shortcut on a search page.
     *
     * A component rather than an inline `if` in the template, because the CSP
     * build evaluates an *expression* and this is three statements and a
     * regular expression (ADR-A31). Behaviour is unchanged: slash focuses the
     * search box, and is ignored while somebody is typing so it cannot swallow
     * a slash mid-word.
     *
     *   <div x-data="searchShortcut('job-search')" @keydown.window="onKey($event)">
     */
    Alpine.data("searchShortcut", (targetId) => ({
      onKey(event) {
        if (event.key !== "/") return;
        const tag = (document.activeElement && document.activeElement.tagName) || "";
        if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;

        const field = document.getElementById(targetId);
        if (!field) return;
        event.preventDefault();
        field.focus();
      },
    }));

    Alpine.data("dialog", () => ({
      isOpen: false,
      _returnTo: null,

      open() {
        // Remember where focus came from so it can be handed back exactly.
        this._returnTo = document.activeElement;
        this.isOpen = true;
        document.body.classList.add("overflow-hidden");
        this.$nextTick(() => {
          const first = this._focusable()[0] || this.$refs.panel;
          if (first) first.focus();
        });
      },

      close() {
        if (!this.isOpen) return;
        this.isOpen = false;
        document.body.classList.remove("overflow-hidden");
        if (this._returnTo && this._returnTo.focus) this._returnTo.focus();
      },

      _focusable() {
        if (!this.$refs.panel) return [];
        return Array.from(
          this.$refs.panel.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
          )
        ).filter((el) => el.offsetParent !== null);
      },

      // Bound with @keydown.tab.prevent / @keydown.shift.tab.prevent so the
      // cycle stays inside the panel in both directions.
      trap(event) {
        const items = this._focusable();
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        const active = document.activeElement;

        if (event.shiftKey) {
          (active === first || !this.$refs.panel.contains(active) ? last : items[items.indexOf(active) - 1]).focus();
        } else {
          (active === last || !this.$refs.panel.contains(active) ? first : items[items.indexOf(active) + 1]).focus();
        }
      },
    }));

    /**
     * CV upload.
     *
     * A real multipart POST driven by XMLHttpRequest, because `fetch` still
     * cannot report upload progress — and a progress bar that is animated on a
     * timer rather than driven by bytes is the definition of a fake
     * interaction. Every state a file upload actually has is represented:
     * idle, validating, uploading with a real percentage, cancelled, failed
     * with the server's reason, and done.
     *
     * Client-side validation exists to fail fast and say why; the server
     * validates the same rules again and its answer is the one that counts.
     */
    Alpine.data("uploader", (config) => ({
      endpoint: config.endpoint,
      maxBytes: config.maxBytes,
      extensions: config.extensions,
      //: Optional selector to replace with the response, for uploads whose
      //: result changes the surrounding page rather than a panel beside it.
      target: config.target || null,
      //: The multipart field name the endpoint reads. Was hard-coded to "cv",
      //: which silently sent nothing to every other upload endpoint — the
      //: server saw an empty request and answered "no file was received".
      field: config.field || "cv",

      file: null,
      phase: "idle",        // idle | uploading | done | error
      progress: 0,
      error: "",
      over: false,
      request: null,

      get busy() {
        return this.phase === "uploading";
      },

      choose(event) {
        const file = event.target.files?.[0];
        if (file) this.accept(file);
      },

      drop(event) {
        this.over = false;
        const file = event.dataTransfer?.files?.[0];
        if (file) this.accept(file);
      },

      accept(file) {
        this.error = "";
        const problem = this.check(file);
        if (problem) {
          this.file = file;
          this.phase = "error";
          this.error = problem;
          window.CareerAI.announce(problem);
          return;
        }
        this.file = file;
        this.start();
      },

      check(file) {
        const name = (file.name || "").toLowerCase();
        if (!this.extensions.some((ext) => name.endsWith(ext))) {
          // Derived from the accepted list rather than hard-coded: this
          // component serves CVs, logos and registration documents, and the
          // fixed "upload a PDF or Word file" told someone choosing a logo to
          // do the opposite of what the endpoint accepts.
          return `That file type won't work here. Accepted: ${this.extensions.join(", ")}.`;
        }
        if (file.size <= 0) return "That file is empty. Check it opens on your machine, then try again.";
        if (file.size > this.maxBytes) {
          return `That file is ${(file.size / 1048576).toFixed(1)} MB and the limit is ${(this.maxBytes / 1048576).toFixed(0)} MB.`;
        }
        return null;
      },

      start() {
        this.phase = "uploading";
        this.progress = 0;
        this.error = "";

        const body = new FormData();
        body.append(this.field, this.file);

        const xhr = new XMLHttpRequest();
        this.request = xhr;
        xhr.open("POST", this.endpoint);
        const token = document.querySelector('meta[name="csrf-token"]');
        if (token) xhr.setRequestHeader("X-CSRFToken", token.content);

        xhr.upload.addEventListener("progress", (event) => {
          if (event.lengthComputable) {
            this.progress = Math.round((event.loaded / event.total) * 100);
          }
        });

        xhr.addEventListener("load", () => {
          this.request = null;
          const okay = xhr.status >= 200 && xhr.status < 300;

          // Two response shapes. Without `target` the body is a small panel
          // that sits next to the control (the CV upload). With one, the body
          // is a re-rendered region and replaces it — which is what an upload
          // that changes the page around it needs, such as a logo that has to
          // appear in three places at once.
          const region = this.target && document.querySelector(this.target);
          if (region) {
            region.outerHTML = xhr.responseText;
          } else if (this.$refs.result) {
            this.$refs.result.innerHTML = xhr.responseText;
          }

          this.phase = okay ? "done" : "error";
          this.progress = okay ? 100 : this.progress;
          if (!okay) this.error = "";   // the server's message is in the swapped region

          // Prefer the server's wording — it is written where the decision was
          // made and says which rule was applied.
          let message = okay ? `${this.file.name} uploaded` : "Upload failed";
          try {
            const trigger = JSON.parse(xhr.getResponseHeader("HX-Trigger") || "{}");
            if (trigger["builder:announce"]) message = trigger["builder:announce"];
          } catch { /* header absent or not JSON — keep the default */ }
          window.CareerAI.announce(message);
        });

        xhr.addEventListener("error", () => {
          this.request = null;
          this.phase = "error";
          this.error = "The connection dropped before the file finished uploading. Nothing was saved — try again.";
        });

        xhr.addEventListener("abort", () => {
          this.request = null;
          this.phase = "idle";
          this.progress = 0;
          window.CareerAI.announce("Upload cancelled");
        });

        xhr.send(body);
      },

      cancel() {
        if (this.request) this.request.abort();
      },

      retry() {
        if (this.file) this.start();
      },

      clear() {
        this.cancel();
        this.file = null;
        this.phase = "idle";
        this.progress = 0;
        this.error = "";
        this.$refs.result.innerHTML = "";
        if (this.$refs.input) this.$refs.input.value = "";
      },
    }));

    /**
     * Share a link.
     *
     * The native share sheet where the browser has one — on a phone that is
     * the list of apps people already send links through, and a row of brand
     * buttons would be a worse copy of it. Clipboard everywhere else, which is
     * what desktop actually wants.
     *
     * A cancelled share sheet is not an error: `AbortError` means the user
     * changed their mind, and reporting it as a failure would be the interface
     * arguing with them.
     */
    Alpine.data("share", (title) => ({
      copied: false,
      label: navigator.share ? "Share this role" : "Copy link",

      async go() {
        const url = window.location.href;
        if (navigator.share) {
          try {
            await navigator.share({ title, url });
            return;
          } catch (error) {
            if (error.name === "AbortError") return;
          }
        }
        try {
          await navigator.clipboard.writeText(url);
          this.copied = true;
          window.CareerAI.announce("Link copied to clipboard");
          window.setTimeout(() => (this.copied = false), 2500);
        } catch {
          // Clipboard blocked (insecure origin, or refused). Select the URL so
          // copying by hand is one keystroke rather than a manual retype.
          window.prompt("Copy this link", url);
        }
      },
    }));

    /**
     * The describe-it composer.
     *
     * Quick-answer chips fill the box instead of sending immediately: a
     * one-tap answer is still an answer someone may want to amend before it
     * goes, and a chip that sends on click makes a mis-tap unrecoverable.
     */
    Alpine.data("assistant", () => ({
      draft: "",

      suggest(text) {
        this.draft = text;
        this.$nextTick(() => {
          const box = this.$refs.composer;
          if (!box) return;
          box.focus();
          box.setSelectionRange(box.value.length, box.value.length);
        });
      },

      sent() {
        this.draft = "";
        // Keep the newest turn in view without yanking the page mid-read.
        this.$nextTick(() => {
          this.$refs.composer?.focus();
          const thread = document.getElementById("assistant-thread");
          thread?.lastElementChild?.scrollIntoView({ block: "nearest" });
        });
      },
    }));

    /**
     * CV builder: autosave, undo/redo and the keyboard shortcuts.
     *
     * The document lives on the server, so every one of these is the same
     * POST the buttons make — the shortcut is a second way to reach one
     * endpoint rather than a parallel client-side implementation that could
     * disagree with it.
     *
     * Autosave is debounced after the last change settles. A save on every
     * keystroke would be a request per character; a save only on blur loses
     * work when a tab closes.
     */
    Alpine.data("builder", () => ({
      timer: null,

      command(name) {
        // Endpoint and target come from the element: the guided builder and
        // the full editor swap different regions, and hard-coding the
        // editor's target here is what left autosave dead on the guided page.
        window.htmx.ajax("POST", this.$el.dataset.actUrl, {
          target: this.$el.dataset.actTarget,
          swap: "outerHTML",
          values: { command: name },
        });
      },

      scheduleSave() {
        window.clearTimeout(this.timer);
        this.timer = window.setTimeout(() => this.save(), 1400);
      },

      save() {
        window.clearTimeout(this.timer);
        this.command("save");
      },

      undo() { this.command("undo"); },
      redo() { this.command("redo"); },
    }));

    // Theme switch, so the button can show the right icon reactively.
    Alpine.data("themeToggle", () => ({
      theme: currentTheme(),
      init() {
        document.addEventListener("theme:changed", (e) => {
          this.theme = e.detail.theme;
        });
      },
      toggle() {
        window.CareerAI.toggleTheme();
      },
    }));
  });

  /* --- Collapsed sections addressed by a link ------------------------- */
  // A link to #skills is useless if #skills is a closed <details>: the browser
  // scrolls to a summary line and the reader thinks the link is broken. Open
  // the target first, then let the jump happen.
  function revealTarget(hash) {
    if (!hash || hash.length < 2) return;
    let node = document.getElementById(hash.slice(1));
    while (node) {
      if (node.tagName === "DETAILS") node.open = true;
      node = node.parentElement;
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (link) revealTarget(link.getAttribute("href"));
  });
  window.addEventListener("hashchange", () => revealTarget(window.location.hash));
  revealTarget(window.location.hash);

  /* --- Ordinary forms in flight ---------------------------------------
   *
   * htmx marks the control it fired from, and `.btn.htmx-request` styles it.
   * A plain `<form method="post">` gets none of that — the browser navigates,
   * and until the response arrives the page looks exactly as it did before the
   * click. On the screens that call a model that is up to half a minute of
   * nothing, which is how one click becomes four.
   *
   * So a submit that is going to navigate marks itself. Applied on `submit`
   * rather than on `click`, so a form that fails validation never leaves a
   * button stuck in a spinner.
   */
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    // htmx handles its own; marking it here would fight the swap.
    if (form.hasAttribute("hx-post") || form.hasAttribute("hx-get")) return;
    if (event.defaultPrevented) return;

    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (!button || button.classList.contains("is-busy")) return;

    button.classList.add("is-busy");
    button.setAttribute("aria-busy", "true");

    // Disabled after the event loop turn, because a disabled button is not a
    // successful control and the browser would drop its name from the payload
    // — which is how a form with two submit buttons stops knowing which one
    // was pressed.
    window.setTimeout(() => {
      button.disabled = true;
    }, 0);
  });

  // Coming back through the history cache leaves the old DOM in place, spinner
  // and all. `pageshow` fires on that path where `load` does not.
  window.addEventListener("pageshow", () => {
    document.querySelectorAll("button.is-busy").forEach((button) => {
      button.classList.remove("is-busy");
      button.removeAttribute("aria-busy");
      button.disabled = false;
    });
  });

  /* --- Drag to reorder ------------------------------------------------
   *
   * Used by the CV builder's section list. Deliberately not a library: the
   * whole behaviour is one drop handler and a comparison of midpoints, and a
   * dependency for that is a dependency to keep patched forever.
   *
   * Three things it has to get right:
   *
   *   * **The list must stay usable without it.** Every card also carries
   *     move-up and move-down buttons that post the same command, so keyboard
   *     and screen-reader users lose nothing. This is an accelerant, not the
   *     mechanism.
   *   * **Dragging must not fight the fields.** The cards are full of inputs,
   *     and an element marked `draggable` swallows text selection inside it.
   *     So `draggable` is switched on only while the pointer is held on the
   *     grip, and off again the moment the drag ends.
   *   * **The server decides.** The drop reorders the DOM for the fraction of
   *     a second before the swap arrives, then posts the whole new order and
   *     lets the response replace the list. A local reorder that disagreed
   *     with the saved one would be a CV that looks right and exports wrong.
   */
  (() => {
    let dragging = null;

    const list = (el) => el && el.closest("[data-sortable]");
    const item = (el) => el && el.closest("[data-sortable-item]");

    document.addEventListener("pointerdown", (event) => {
      const handle = event.target.closest("[data-sortable-handle]");
      if (!handle) return;
      const card = item(handle);
      if (card) card.setAttribute("draggable", "true");
    });

    document.addEventListener("pointerup", clearDraggable);
    document.addEventListener("dragend", clearDraggable);

    function clearDraggable() {
      document
        .querySelectorAll('[data-sortable-item][draggable="true"]')
        .forEach((el) => el.removeAttribute("draggable"));
      if (dragging) dragging.classList.remove("is-dragging");
      dragging = null;
    }

    document.addEventListener("dragstart", (event) => {
      const card = item(event.target);
      if (!card || card.getAttribute("draggable") !== "true") return;
      dragging = card;
      card.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      // Firefox will not start a drag without payload on the transfer.
      event.dataTransfer.setData("text/plain", card.dataset.sectionId || "");
    });

    document.addEventListener("dragover", (event) => {
      if (!dragging) return;
      const container = list(event.target);
      if (!container || container !== list(dragging)) return;

      event.preventDefault();
      const after = nextSibling(container, event.clientY);
      if (after === dragging) return;
      if (after === null) container.appendChild(dragging);
      else container.insertBefore(dragging, after);
    });

    document.addEventListener("drop", (event) => {
      const container = list(event.target);
      if (!dragging || !container) return;
      event.preventDefault();
      const order = Array.from(
        container.querySelectorAll("[data-sortable-item]"),
        (el) => el.dataset.sectionId
      );
      clearDraggable();
      submit(container, order);
    });

    /* The first card whose midpoint is below the pointer. Midpoints rather
       than edges, so a card swaps when the pointer passes its centre and not
       when it merely touches it — which is what makes the list feel settled
       rather than twitchy. */
    function nextSibling(container, y) {
      const others = Array.from(container.querySelectorAll("[data-sortable-item]")).filter(
        (el) => el !== dragging
      );
      return (
        others.find((el) => {
          const box = el.getBoundingClientRect();
          return y < box.top + box.height / 2;
        }) || null
      );
    }

    function submit(container, order) {
      const url = container.dataset.sortableUrl;
      const target = container.dataset.sortableTarget;
      if (!url || !window.htmx) return;

      // The command and its scope come from the list, so one drag handler
      // serves both the section list and the entries inside each section.
      const values = {
        command: container.dataset.sortableCommand || "reorder_sections",
        order,
      };
      if (container.dataset.sortableSection) {
        values.section = container.dataset.sortableSection;
      }

      window.htmx.ajax("POST", url, { target: target || container, swap: "outerHTML", values });
    }
  })();

  /* --- HTMX defaults ------------------------------------------------- */
  document.addEventListener("htmx:configRequest", (event) => {
    const token = document.querySelector('meta[name="csrf-token"]');
    if (token) {
      event.detail.headers["X-CSRFToken"] = token.content;
    }
  });

  // Announce loading to assistive technology. The skeleton itself is
  // aria-hidden — a screen reader should hear "busy", then the result, not a
  // description of eleven grey rectangles. `aria-busy` on the live region
  // also stops it announcing the half-swapped DOM mid-update.
  // How long a request may run before it is worth showing a skeleton. Under
  // this, the swap reads as instant and any placeholder is just a flicker;
  // over it, the user needs to know the click registered.
  const SKELETON_DELAY = 300;

  document.addEventListener("htmx:beforeRequest", (event) => {
    const selector = event.detail.elt.getAttribute("hx-indicator");
    const region = selector && document.querySelector(selector);
    if (!region) return;
    region.setAttribute("aria-busy", "true");
    region._skeletonTimer = window.setTimeout(
      () => region.classList.add("is-loading"),
      SKELETON_DELAY
    );
  });

  // Cleared by querying the DOM rather than by re-deriving the element from
  // the event: by settle time `detail.elt` can be the swapped-in content,
  // which carries no `hx-indicator`, and the region was being left permanently
  // marked busy — a live region that never stops being busy stops announcing.
  document.addEventListener("htmx:afterRequest", () => {
    document.querySelectorAll('[aria-busy="true"]').forEach((region) => {
      window.clearTimeout(region._skeletonTimer);
      region.classList.remove("is-loading");
      region.removeAttribute("aria-busy");
    });
  });

  // Server-sent confirmations. A view raises `HX-Trigger: {"builder:announce":
  // "Section moved"}` and it reaches the live region — so the message that
  // describes a change is written where the change is made, not duplicated in
  // the template that triggered it.
  document.body.addEventListener("builder:announce", (event) => {
    const message = typeof event.detail === "string" ? event.detail : event.detail?.value;
    if (message) window.CareerAI.announce(message);
  });

  // Reader preferences live on <html>, which is outside every swap target, so
  // the settings view reports them and this applies them. The same values are
  // rendered server-side on the next request — this is only what keeps the
  // current page honest between now and then.
  document.body.addEventListener("prefs:changed", (event) => {
    const detail = event.detail || {};
    const root = document.documentElement;

    if (typeof detail.classes === "string") {
      [...root.classList]
        .filter((name) => name.startsWith("prefs-"))
        .forEach((name) => root.classList.remove(name));
      detail.classes.split(/\s+/).filter(Boolean)
        .forEach((name) => root.classList.add(name));
    }

    // The settings view has already stored this one — applying it again is
    // all that is needed, and posting it back would be a second write.
    if (detail.theme) window.CareerAI.setTheme(detail.theme, { persist: false });
  });

  // A failed swap must not leave the user staring at stale results with no
  // explanation. Replace the region with something that says what happened
  // and offers the one action that fixes it.
  /* --- Confirmation ---------------------------------------------------- */
  //
  // Every `hx-confirm` in the product is answered by the app's own dialog
  // instead of the browser's `confirm()`. HTMX raises `htmx:confirm` before it
  // issues a request; preventing the default and calling `issueRequest()` later
  // substitutes any dialog without touching a single call site.
  //
  // The native prompt was doing the job, but it opened in the operating
  // system's chrome at the top of the window, in the operating system's type,
  // and read as though a different program had asked. Two mechanisms for one
  // decision is one too many.
  const confirmDialog = (() => {
    let root = null;
    let onAccept = null;
    let returnTo = null;

    const parts = () => ({
      panel: root.querySelector("[data-confirm-panel]"),
      heading: root.querySelector("[data-confirm-heading]"),
      message: root.querySelector("[data-confirm-message]"),
      accept: root.querySelector("[data-confirm-accept]"),
      cancel: root.querySelector("[data-confirm-cancel]"),
    });

    function focusable() {
      const { panel } = parts();
      return Array.from(
        panel.querySelectorAll('button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')
      ).filter((el) => el.offsetParent !== null);
    }

    // Hand focus back to whatever opened the dialog — but several of these
    // triggers sit inside an overflow menu that closes on the same click, so
    // by now the element is hidden and `.focus()` on it silently does nothing,
    // dropping a keyboard user at the top of the document. Walk out to the
    // nearest control still on screen instead.
    const FOCUSABLE = 'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

    function restoreFocus() {
      if (returnTo && returnTo.offsetParent !== null) {
        returnTo.focus();
        return;
      }

      // The trigger is gone from view. Climb to the closest container that is
      // still visible and focus the first control inside it — for a menu item
      // that is the button which opened the menu, which is where the reader
      // was a moment ago. Ancestors alone are not enough: the menu button is
      // a sibling of the panel, not a parent of it.
      let container = returnTo && returnTo.parentElement;
      while (container && container.offsetParent === null) container = container.parentElement;

      const nearby = container
        ? Array.from(container.querySelectorAll(FOCUSABLE)).find((el) => el.offsetParent !== null)
        : null;

      const target = nearby || document.getElementById("main");
      if (target && target.focus) target.focus();
    }

    function close() {
      if (!root || root.hidden) return;
      root.hidden = true;
      document.body.classList.remove("overflow-hidden");
      onAccept = null;
      restoreFocus();
      returnTo = null;
    }

    function accept() {
      const run = onAccept;
      close();
      if (run) run();
    }

    function trap(event) {
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    function init() {
      root = document.getElementById("app-confirm");
      if (!root) return;
      const { accept: acceptBtn, cancel } = parts();
      acceptBtn.addEventListener("click", accept);
      cancel.addEventListener("click", close);

      // Only a click on the overlay itself dismisses. Without the identity
      // check, a click that started inside the panel and drifted out — a
      // selection drag over the message — would close the dialog.
      root.querySelector("[data-confirm-overlay]").addEventListener("click", (event) => {
        if (event.target === event.currentTarget) close();
      });
      root.addEventListener("keydown", (event) => {
        if (event.key === "Escape") close();
        trap(event);
      });
    }

    function open({ question, title, label, tone, trigger }, run) {
      if (!root) return window.confirm(question) && run();
      const { panel, heading, message, accept: acceptBtn } = parts();

      heading.textContent = title || "Are you sure?";
      message.textContent = question;
      acceptBtn.textContent = label || "Continue";
      acceptBtn.className = "btn " + (tone === "danger" ? "btn-danger" : "btn-primary");

      onAccept = run;
      // The element that carries `hx-confirm`, not `document.activeElement`:
      // menu items close their own menu on click, so by the time we get here
      // focus has already moved off and the active element is the document.
      returnTo = trigger || document.activeElement;
      root.hidden = false;
      document.body.classList.add("overflow-hidden");

      // Focus lands on Cancel, not on the confirming button: a stray Enter
      // should not complete a destructive action the reader has not read yet.
      parts().cancel.focus();
      void panel;
    }

    return { init, open };
  })();

  confirmDialog.init();

  document.addEventListener("htmx:confirm", (event) => {
    const question = event.detail.question;
    if (!question) return;   // every htmx request raises this; only ours has text

    event.preventDefault();
    const source = event.detail.elt;
    confirmDialog.open(
      {
        question,
        title: source.getAttribute("data-confirm-title"),
        label: source.getAttribute("data-confirm-label"),
        tone: source.getAttribute("data-confirm-tone"),
        trigger: source,
      },
      () => event.detail.issueRequest(true)
    );
  });

  // The same dialog for a plain form: a submit button carrying `data-confirm`
  // asks first, and the form is submitted with that button once accepted.
  // Registered in the capture phase so it runs before the busy-state handler.
  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      const button = event.submitter;
      if (!button || !button.hasAttribute("data-confirm")) return;
      if (form.dataset.confirmed === "1") {
        delete form.dataset.confirmed;
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      confirmDialog.open(
        {
          question: button.getAttribute("data-confirm"),
          title: button.getAttribute("data-confirm-title"),
          label: button.getAttribute("data-confirm-label"),
          tone: button.getAttribute("data-confirm-tone"),
          trigger: button,
        },
        () => {
          form.dataset.confirmed = "1";
          form.requestSubmit(button);
        }
      );
    },
    true
  );

  // An expected refusal — a plan limit, a permission, a state that does not
  // allow it yet — comes back with its real 4xx status and an
  // `X-Restriction` header, carrying the design system's warning alert as
  // its body. htmx does not swap error statuses by default, so without this
  // the body was thrown away and `handleFailure` below painted "The
  // connection dropped" over what was only a "not on your plan". Real
  // failures carry no such header and still reach `handleFailure`.
  document.addEventListener("htmx:beforeSwap", (event) => {
    const xhr = event.detail.xhr;
    if (xhr && xhr.status >= 400 && xhr.getResponseHeader("X-Restriction")) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  document.addEventListener("htmx:responseError", handleFailure);
  document.addEventListener("htmx:sendError", handleFailure);

  function handleFailure(event) {
    const target = event.detail.target;
    if (!target) return;
    target.innerHTML = `
      <div class="alert alert-error" role="alert">
        <div>
          <p class="alert-title">Couldn't load those results</p>
          <p class="mt-0.5 text-content-muted">
            The connection dropped or the server didn't answer. Your filters are
            still set — try again.
          </p>
          <button type="button" class="btn btn-secondary btn-sm mt-3"
                  onclick="window.location.reload()">Try again</button>
        </div>
      </div>`;
  }
})();

/* --- Payment hand-off ---------------------------------------------- */
// The page that sends a customer to a payment gateway (billing/handoff.html)
// works without script: it is a form with a visible button. With script, it
// continues on its own once, so nobody has to press "continue" twice.
(function () {
  "use strict";
  function handoff() {
    const form = document.querySelector("form[data-autosubmit]");
    if (form && !form.dataset.submitted) {
      form.dataset.submitted = "1";
      form.submit();
      return;
    }
    const link = document.querySelector("a[data-autofollow]");
    if (link) window.location.assign(link.href);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", handoff);
  } else {
    handoff();
  }
})();

/* --- Prompt examples ------------------------------------------------ */
// A button with `data-fill="#field"` and `data-fill-text` puts its example
// into that field, ready to edit (Assisted CV). Delegated, so it also works on
// content htmx swaps in.
(function () {
  "use strict";
  document.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-fill]");
    if (!chip) return;
    const field = document.querySelector(chip.getAttribute("data-fill"));
    if (!field) return;
    field.value = chip.getAttribute("data-fill-text") || "";
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
  });
})();
