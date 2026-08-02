# Calendar Bot — The Plan

Turn plain-text messages into Google Calendar events. You text a Telegram bot in
natural language ("dentist next Tuesday 3pm"); it parses the date/time with an LLM
and adds the event to your calendar, then replies with a confirmation and a link.

## How it works

There are three moving parts:

1. **Telegram client** — the app where you type the message.
2. **Telegram's servers** — receive your message and, because a webhook is
   registered, immediately POST it (as JSON) to your function's URL.
3. **Google Cloud Function** — the code that runs on each message.

"Webhook" isn't a separate component — it's just the arrangement where Telegram
pushes each message to a URL you gave it. That URL *is* the Cloud Function's
endpoint. Nothing of yours runs until a message arrives.

## The flow, per message

1. You message the bot.
2. Telegram POSTs the message to the Cloud Function.
3. The function checks it's really Telegram (a secret header) and really you
   (your Telegram user id — this keeps strangers who find the bot out).
4. It sends your text, plus the current time and your timezone, to the LLM and
   asks for a strict JSON event (title, start, end, all-day, location).
5. It inserts that event into Google Calendar via a service account.
6. It replies on Telegram with the event details and a link.

## Key decisions

- **Serverless (Google Cloud Functions).** Runs only when a message arrives, so
  there's nothing to keep alive. Same ecosystem as Calendar.
- **LLM is provider-agnostic.** The call uses the standard OpenAI-compatible
  format, so switching providers is just three settings (base URL, model, key).
  Default provider: **DeepSeek**, model **`deepseek-v4-flash`**
  (note: the old `deepseek-chat` name was retired on 2026-07-24).
- **Calendar auth via a service account**, not the usual OAuth login flow. You
  share your calendar with the service account's email once, and there are no
  refresh tokens to manage in a stateless function.
- **Single user, single timezone.** Simplifies everything; only you can use it.

## Cost

Effectively free for personal use.

- **Cloud Functions** — the "always free" tier covers ~1.5–2 million invocations
  a month (one invocation = one message). A billing account (a card on file) is
  required even on the free tier, but at this volume you won't be charged. Deploy
  to a standard US region (e.g. `us-central1` / `us-west1`) so free-tier rules apply.
- **DeepSeek** — V4-Flash is $0.14 per million input tokens, $0.28 per million
  output. Each call is a few hundred tokens, so ~$0.0001 per call. At 4 calls a
  day that's roughly **2 cents a month**. New accounts also get a free token grant
  that covers a year or two of this at that rate.
- **Cold starts** — a few seconds of extra latency on the first message after
  idle. Harmless here (Telegram waits up to 60s). Optional `min-instances=1`
  removes it but adds a small always-on cost, so it's usually not worth it.

## One-time setup (high level)

1. **Telegram** — create the bot with @BotFather (get the token); get your own
   user id from @userinfobot; pick a random webhook secret.
2. **LLM** — get a DeepSeek API key; note the base URL and model name.
3. **Google** — create a Cloud project, enable the Calendar API, make a service
   account with a JSON key, and share your calendar with the service account's
   email ("Make changes to events"). Use your real calendar id (your email),
   **not** "primary".
4. **Deploy** — push the function to Cloud Functions with all the above as
   environment variables.
5. **Webhook** — tell Telegram to point your bot at the function's URL.

Then message the bot and watch the event appear.

## Current limitations (deliberate, to keep v1 small)

- **No confirm-before-adding.** It adds the event immediately and sends a receipt
  with a link, rather than asking "add this? 👍" and waiting. A real confirm step
  spans two separate messages, so the pending event has to be stored in between.
- **No duplicate protection.** If processing is slow, Telegram may retry the
  webhook and create a double event. Deduping needs a little stored state.

## Planned next step

Add a small **Firestore** store (free tier is ample, and you're already in Google
Cloud) to hold a pending event keyed by your chat. That single addition delivers
both the **"confirm with 👍 before adding"** flow and **duplicate protection** at
once — the second webhook call reads the pending event back and either inserts it
or discards it.

## Files

- `main.py` — the Cloud Function (the whole loop).
- `requirements.txt` — Python dependencies.
- `README.md` — detailed step-by-step setup and deploy instructions.