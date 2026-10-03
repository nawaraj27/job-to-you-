# Interview — Manual Verification Checklist

These checks cannot be performed in the development environment used for the automated loop:

- **No JaaS account.** There are no JaaS credentials.
- **No real devices.** The in-app browser blocks real microphones and cameras. Playwright tests use fake devices.
- **No usable screen reader.** No screen reader could be operated and heard.

Each check lists what to do and what counts as a pass. Record the date, the device and browser version, and pass/fail for every line.

---

## A. Real JaaS video call — **BLOCKED until credentials exist**

### A1. Setup (once)

1. Create a JaaS account at 8x8 JaaS. Create an app and copy its **AppID** (`vpaas-magic-cookie-…`).
2. Generate an RSA key pair:

   ```
   openssl genrsa -out jaas.key 4096
   openssl rsa -in jaas.key -pubout -out jaas.pub
   ```

3. In the JaaS console, open **API Keys**, upload `jaas.pub`, and copy the **key id** (`vpaas-magic-cookie-…/abc123`).
4. In the JaaS console, turn on **authentication required**. Without it, a room URL with no token will let people in.
5. Set these on the server, then restart it:

   ```
   VIDEO_ROOM_PROVIDER=apps.interviews.providers.jitsi.JitsiRoomProvider
   JAAS_APP_ID=vpaas-magic-cookie-…
   JAAS_API_KEY_ID=vpaas-magic-cookie-…/abc123
   JAAS_PRIVATE_KEY_PATH=/path/to/jaas.key        # or JAAS_PRIVATE_KEY with \n escapes
   ```

6. Run `python manage.py check --deploy`. It must not report `interviews.E001` or `interviews.E002`.

### A2. Two-participant call

Use two different devices, or two different browser profiles: an **interviewer** (company member, invited to the panel) and a **candidate**.

| # | Step | Pass when |
|---|---|---|
| 1 | Employer publishes a time 20 minutes ahead; candidate books it | Booking confirmed; both receive a notification |
| 2 | 16+ minutes before the start, both open the interview | "Join" not offered; the join page says when the room opens |
| 3 | 15 minutes before, interviewer clicks **Join the room** | The 8x8 room opens with a `?jwt=` link. The interviewer's name is shown and they have moderator controls |
| 4 | Candidate clicks **Join** → **Join the room** | The candidate enters the **same** room and has no moderator controls |
| 5 | Both allow camera and microphone | Each sees and hears the other within 10 s. Note the join time for each side |
| 6 | Candidate closes the tab and rejoins from CareerAI | Back in the same room within 30 s |
| 7 | Interviewer switches Wi-Fi off for 20 s, then on | The call reconnects without leaving the room |
| 8 | Copy the candidate's room URL **without** `?jwt=…` into a private window | Refused by JaaS (authentication required) |
| 9 | Copy the full URL with the token into a private window **after** the join window closes (end + 15 min) | Refused (token expired) |
| 10 | A company member **not** on the panel opens the interview → Join | "You are not on this interview's panel"; no room opened |
| 11 | In the room, try to start recording or livestreaming | Not available (features disabled in the token) |
| 12 | Both leave; interviewer submits a scorecard and records the outcome | Interview shows **Completed** and appears under **Done** |

---

## B. Voice practice on real devices — **NOT VERIFIED**

Automated coverage used headless Chromium with a fake microphone fed from WAV files. Repeat the checks below in each of:

- Chrome (Windows)
- Edge (Windows)
- Firefox (Windows)
- Safari (macOS)
- Chrome (Android)
- Safari (iOS)

Use a real microphone and a real voice.

| # | Step | Pass when |
|---|---|---|
| 1 | Hub → "Answer aloud" → Start | The spoken interview opens with the "Answer aloud" panel |
| 2 | Expand "What is recorded, and what happens to it" | The text says audio is deleted, video never leaves the device, and the face is not analysed |
| 3 | **Turn on microphone and camera** → Allow | Camera preview shows; "Start answering" appears |
| 4 | Speak a 30–60 second answer → **Stop and transcribe** | The transcript appears in the answer box within about 10 s and is recognisably what was said |
| 5 | Fix a misheard word, then **Submit answer** | The next question appears. The result page later shows "How you spoke" with the pace and pause figures |
| 6 | Record, transcribe, then **reload the page** before submitting | The transcript and "We put back the answer you had started" are shown; after submitting, the result page still shows delivery figures for that answer |
| 7 | Deny the microphone in the browser prompt | "Your browser blocked the microphone" with steps to allow it; typing still works |
| 8 | Re-allow in site settings → **Try again** | Recording works |
| 9 | Record 5 s of silence → Stop | "No speech was heard. Check your microphone and record again." |
| 10 | Record in a noisy room (TV on) | The transcript is mostly correct, or the page asks you to record again |
| 11 | Start recording, switch to another tab for 10 s, come back | Recording stopped when the tab was hidden, and what was said is transcribed |
| 12 | Turn off the network, record, Stop | "The recording could not be sent…" with **Record again**; typing still works |
| 13 | Unplug or disable the microphone, then start | "No microphone could be used" |
| 14 | On a phone in portrait: complete steps 3–5 | No sideways scrolling; buttons reachable with a thumb; the keyboard does not cover the Submit button |
| 15 | Keyboard only (desktop): Tab to **Microphone only**, press Enter, Tab to **Start answering**, press Space, then Stop | Every control is reachable and shows a visible focus ring |
| 16 | Server: after a session, list the temp directory | No `.webm`/`.ogg`/`.m4a`/`.wav` files remain |

---

## C. Screen reader — **NOT VERIFIED**

Use NVDA + Firefox or Chrome on Windows, VoiceOver + Safari on macOS, and TalkBack on Android.

| # | Journey | Pass when |
|---|---|---|
| 1 | Candidate hub | The page title and "Interviews" heading are announced. "Coming up" and "Practise" are reachable by heading navigation. Role, answer-mode radio buttons and Style are announced with their labels |
| 2 | Typed interview question | The question is announced as the level 1 heading. The answer field announces its label and hint. The time left is announced as a timer, and the page does not announce every tick |
| 3 | Submit an empty answer | The error is announced immediately (alert) |
| 4 | Spoken interview | Each recorder state change is announced: permission requested, blocked, recording, transcribing, transcript ready. **Record again** and **Try again** are reachable |
| 5 | "Stop the interview" | The disclosure is announced as collapsed/expanded, and both options inside it are reachable |
| 6 | Result page | Headings for the outcome, each competency, "How you spoke" and "What you were asked" are navigable. Scores read as "3 / 5" |
| 7 | Booking a time | Each slot radio button announces its date and time; confirming announces the result |
| 8 | Employer interview detail | The scorecard competency, score and "Why" fields are labelled. "Join the room" is announced as a link. Messages after an action are announced |
| 9 | Employer "Publish times" | The "Start times" group and each time field are announced with their labels |

---

## D. Hosted model quality — **NOT VERIFIED**

The live AI benchmarks ran on local `qwen2.5:7b-instruct` on CPU. If a hosted model is configured (`OPENROUTER_API_KEY`), run:

```
python manage.py interview_benchmark --live --only grounding,followup --repeat 2
```

Compare band agreement, correction handling and latency with the local results in `Interview-Ecosystem.md`.
