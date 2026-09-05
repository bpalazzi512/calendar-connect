# Calendar Connect

An AI scheduler for your Google Calendar. Say it in plain English — *"dentist
next Tuesday 3pm"*, *"book club every Saturday 7pm"* — and the event lands on
your calendar, one-off or recurring. Say *"move the dentist to 4pm"* and it
finds that event and changes it. You get a confirmation with a link back.

Three ways in, all hitting the same function:

- **Telegram.** Message the bot. Works anywhere you're signed in.
- **A Mac hotkey.** Press it, type, Enter. Nothing opens, nothing to close.
- **Your iPhone.** Siri, the Action Button, or the share sheet.

See [initial-plan.md](initial-plan.md) for the design rationale. This file is
the build-and-run guide. [Step 7](#step-7--your-mac-and-your-iphone) sets up
the Mac and phone.

```
Telegram ──▶ POST /       ─┐
                           ├─▶ Cloud Function ──▶ LLM (parse to JSON)
Mac hotkey ─┐              │        │
iOS Shortcut├─▶ POST /event┘        ├──▶ new event?  Calendar insert
Siri       ─┘                       ├──▶ a change?   Calendar search ──▶ LLM
                                    │                (pick one) ──▶ patch
                                    └──▶ reply, with a link
```

Both front doors run the same handlers. All that differs is where the answer
goes: Telegram gets it pushed back over the Bot API, `/event` gets it in the
HTTP response.

## Repo layout

| Path | What it is |
| --- | --- |
| `src/main.py` | Entry point. Works out which channel a request belongs to, hands it over |
| `src/channels/telegram.py` | The Telegram channel: Bot API, HTML rendering, webhook |
| `src/channels/api.py` | The JSON channel: bearer token in, plain text out |
| `src/handlers.py` | What to do with a message. Knows nothing about either channel |
| `src/llm.py` | The prompts, and the two JSON round trips |
| `src/events.py` | Building and patching Calendar event resources. Pure, no network |
| `src/calendar_api.py` | Credentials, the Calendar client, finding candidate events |
| `src/receipts.py` | Turning an event into something to say |
| `src/messages.py` | The message vocabulary every channel renders from |
| `src/replies.py` | The seam between the handlers and whoever is listening |
| `src/config.py` | Reading the environment |
| `src/requirements.txt` | Python dependencies |
| `terraform/` | All the infrastructure |
| `terraform/terraform.tfvars.example` | Template for your settings |
| `scripts/api-token.sh` | Print the bearer token for `/event` |
| `scripts/set-webhook.sh` | Re-point Telegram at the function |
| `scripts/webhook-info.sh` | Ask Telegram if delivery is failing |
| `scripts/delete-webhook.sh` | Turn the bot off without destroying anything |
| `scripts/try-parse.py` | Run one message through the LLM locally, no deploy |

Terraform zips `src/` and deploys it, so `src/` holds only what the function
needs at runtime.

---

## How it's put together

One function, two front doors, one set of handlers:

```
channels/telegram.py ─┐                     ┌─ llm.py         parse, and pick
  webhook secret      │                     │                 an event to change
  + user-id check     │                     │
  renders HTML        ├─▶ handlers.py ──────┼─ calendar_api.py  find candidates
                      │   create or update  │                   insert, patch
channels/api.py      ─┘   never learns      │
  bearer token            which channel     ├─ events.py     dates, recurrence,
  renders plain text                        │                patches. no network
                                            └─ receipts.py   what to say about it
```

A channel owns exactly three things: how a caller proves who it is, how a
reply is rendered for that audience, and how the answer gets back. Everything
else is shared, which is why adding a third way in is a new file in
`channels/` and a line in `main.py`, not a fork of the logic.

**Plain text is the canonical form.** A handler builds a `Message` — a few
lines of spans, each marked plain, bold, code or strikethrough — and never
holds a string with a tag in it. `channels/telegram.py` adds the HTML on the
way out, and it is the only module in the project where HTML exists. The API
channel renders the same `Message` as plain text for a notification or for
Siri to read aloud.

That direction matters. Rendering Telegram's HTML everywhere and stripping it
back out for everyone else works, but it puts escaping in every handler, where
it can be forgotten or applied twice, and it makes the oldest channel the one
every other channel has to undo. Building up beats tearing down.

The practical payoff: when you change how a confirmation reads, you edit
`receipts.py` and every channel changes together.

---

## What Terraform builds

- Enables the required APIs (Cloud Functions, Run, Build, Artifact Registry,
  Secret Manager, IAM Credentials, **Calendar**, and friends).
- A **service account** the function runs as. This is the identity you share
  your calendar with.
- Four **Secret Manager** secrets — bot token, webhook secret, LLM API key, and
  a generated **API token** for `/event` — mounted into the function as
  environment variables. No secret is ever baked into the deployed source.
- A **GCS bucket** holding the zipped source.
- An **Artifact Registry repo** for the built container images, with a cleanup
  policy so old builds expire instead of accumulating.
- The **2nd-gen Cloud Function**, public (Telegram can't authenticate to IAM),
  protected by the shared-secret header and the allowed-user-id check on `/`,
  and by a bearer token on `/event`.
- A **$1/month budget alert**, if you supply a billing account id.
- The **Telegram webhook registration** itself, via `setWebhook`.

### A note on calendar auth

The plan called for a service-account JSON key. This build defaults to a
**keyless** variant of the same idea, because a key file is one more secret to
store and rotate.

The wrinkle: the token the function gets automatically from the metadata server
only carries the `cloud-platform` scope, and that scope does **not** cover
Calendar — Calendar is a Workspace API, not a Cloud API. So the service account
is granted `roles/iam.serviceAccountTokenCreator` *on itself*, and the code
mints its own token with the Calendar scope through the IAM Credentials API.
Same trust model as a key, nothing on disk.

If you'd rather use a real key, create one and set `calendar_sa_key_json` in
your tfvars (see [Appendix A](#appendix-a--using-a-json-key-instead)). The code
takes that path automatically when the variable is non-empty.

---

## Prerequisites

Install locally:

- [Terraform](https://developer.hashicorp.com/terraform/downloads) ≥ 1.5
- [gcloud CLI](https://cloud.google.com/sdk/docs/install)
- `curl`, `bash`, `python3` (all standard on macOS)

Accounts: a Telegram account, a Google account, and a card for the Google Cloud
billing account (required even on the free tier — you won't be charged at this
volume).

---

## Step 1 — Telegram

**1a. Create the bot.** In Telegram, message [@BotFather](https://t.me/BotFather):

```
/newbot
```

Give it a display name and a username ending in `bot`. BotFather replies with a
token like `1234567890:AAH...`. That's `telegram_bot_token`. Treat it as a
password — anyone holding it controls the bot.

**1b. Get your user id.** Message [@userinfobot](https://t.me/userinfobot). It
replies with your numeric `Id` — a 9–10 digit number. That's
`allowed_telegram_user_id`. Messages from any other id are dropped.

**1c. Make a webhook secret.** Any random string, 16–256 characters of
`A-Za-z0-9_-`:

```bash
openssl rand -hex 32
```

That's `telegram_webhook_secret`. Telegram sends it back in the
`X-Telegram-Bot-Api-Secret-Token` header on every call, and the function
rejects anything without it.

## Step 2 — LLM key

Sign up at [platform.deepseek.com](https://platform.deepseek.com), create an
API key. That's `llm_api_key`.

The call is a plain OpenAI-compatible `POST /chat/completions`, so switching
providers is exactly three settings:

| Provider | `llm_base_url` | `llm_model` |
| --- | --- | --- |
| DeepSeek (default) | `https://api.deepseek.com/v1` | `deepseek-v4-flash` |
| OpenAI | `https://api.openai.com/v1` | e.g. `gpt-4o-mini` |
| Groq | `https://api.groq.com/openai/v1` | e.g. `llama-3.3-70b-versatile` |
| Local Ollama | `http://localhost:11434/v1` | e.g. `llama3.2` |

> **Check the model name before you deploy.** The plan specifies
> `deepseek-v4-flash` and notes that `deepseek-chat` was retired on 2026-07-24.
> I could not verify either name against DeepSeek's live model list, so confirm
> it on their models page. If it's wrong you'll get a clear `LLM returned 400`
> message in Telegram, and the fix is a one-line change to `llm_model`. The
> provider must support `response_format: {"type": "json_object"}`.

## Step 3 — Google Cloud

**3a. Create a project and attach billing.**

```bash
gcloud auth login

PROJECT_ID=my-calendar-connect-$RANDOM     # must be globally unique
gcloud projects create "$PROJECT_ID"
gcloud config set project "$PROJECT_ID"

# Find your billing account id and link it
gcloud billing accounts list
gcloud billing projects link "$PROJECT_ID" --billing-account=XXXXXX-XXXXXX-XXXXXX
```

Keep that billing account id — it's also `billing_account_id` in step 4, which
is what creates the budget alert.

If you'd rather click: [console.cloud.google.com](https://console.cloud.google.com)
→ project picker → **New project**, then **Billing** → link an account.

Billing must be linked before Terraform runs; enabling Cloud Functions on an
unbilled project fails.

**3b. Give Terraform credentials.**

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT_ID"
```

Your account needs **Owner** on the project (or, at minimum, Project IAM Admin
+ Service Account Admin + Service Usage Admin + Secret Manager Admin +
Cloud Functions Admin + Service Account User). Owner is the easy answer for a
personal project.

**3c. Share your calendar with the service account.**

The service account's address is predictable, so you can do this now:

```
calendar-connect@PROJECT_ID.iam.gserviceaccount.com
```

(`calendar-connect` is the `function_name` variable; if you change that, change
this too. After `terraform apply`, `terraform output service_account_email`
prints the exact address.)

In Google Calendar on the web:

1. Hover your calendar in the left sidebar → **⋮** → **Settings and sharing**.
2. **Share with specific people or groups** → **Add people and groups**.
3. Paste the service account address.
4. Set permission to **Make changes to events**. ← not "See all event details"
5. **Send**. Service accounts don't accept invitations; access is immediate.
   Allow a couple of minutes for it to propagate.

While you're on that page, copy the **Calendar ID** from the *Integrate
calendar* section further down. For your default calendar it's your email
address. Use that literal value as `calendar_id` — **not** `primary`, which
resolves to the *service account's own* empty calendar and silently swallows
your events.

## Step 4 — Fill in your settings

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
```

| Variable | Notes |
| --- | --- |
| `project_id` | From step 3a |
| `region` | Keep a standard US region (`us-central1`, `us-west1`) for free-tier rules |
| `telegram_bot_token` | Step 1a |
| `telegram_webhook_secret` | Step 1c |
| `allowed_telegram_user_id` | Step 1b |
| `llm_api_key` / `llm_base_url` / `llm_model` | Step 2 |
| `calendar_id` | Step 3c — your email, not `primary` |
| `timezone` | IANA name, e.g. `America/New_York`. Every time you type is read in this zone |
| `billing_account_id` | Step 3a — creates the budget alert. Leave `""` to skip |

Optional: `default_event_minutes` (60), `min_instance_count` (0 — set to 1 to
kill cold starts for a small always-on cost), `max_instance_count` (3),
`register_webhook` (true), `function_name` (`calendar-connect`),
`budget_amount` (1), `budget_currency` (`USD`), `image_retention_days` (7),
`image_keep_count` (3).

> **Renaming a deployment that already exists.** `function_name` is the stem of
> the Cloud Function, the Cloud Run service, the service account, the secrets,
> the bucket and the image repo. Changing it on a live deployment replaces all
> of them, and the one that hurts is the service account: the new one has a new
> address, your calendar is still shared with the old one, and events stop
> being written with no error you'd notice. If you deployed this when it was
> called `calendar-bot`, pin `function_name = "calendar-bot"` in your tfvars
> and leave the infrastructure where it is — the name only ever appears in
> logs and URLs. If you do want the rename, re-share your calendar with the new
> address (step 3c) and update the URL in your Shortcut.

`terraform.tfvars` is gitignored. Don't commit it.

## Step 5 — Deploy

```bash
terraform init
terraform apply
```

First apply takes **5–10 minutes** — most of it is enabling APIs and the Cloud
Build step that containerizes the function. There's a deliberate 60-second
pause in the middle waiting for IAM grants to propagate; a fresh project that
skips it fails the build with a confusing permissions error.

On success:

```
function_url          = "https://calendar-connect-xxxxxxxxxx-uc.a.run.app"
event_url             = "https://calendar-connect-xxxxxxxxxx-uc.a.run.app/event"
service_account_email = "calendar-connect@my-project.iam.gserviceaccount.com"
```

The webhook is registered automatically. Confirm the function is up:

```bash
curl "$(terraform output -raw function_url)"     # -> calendar-connect is up
```

## Step 6 — Use it

Telegram is what's working at this point in the guide, so the examples below
use it. Nothing here is Telegram-specific — every phrasing behaves identically
from the Mac hotkey and the phone once [Step 7](#step-7--your-mac-and-your-iphone)
is done. Only the formatting differs: Telegram gets bold text and a tappable
link, everywhere else gets the same words in plain text.

Message your bot:

```
dentist next Tuesday 3pm
```

You should get back:

> ✅ **Dentist**
> 🗓 Tue, Aug 18, 2026 · 3:00 PM – 4:00 PM
> [Open in Calendar](#)

Other things it handles:

| You type | You get |
| --- | --- |
| `lunch with Sam Thursday 12:30 at Zuni` | Timed event with a location |
| `flight to Denver Oct 4` | All-day event (no time given → all-day) |
| `conference Oct 4 to Oct 8` | Multi-day all-day event |
| `standup tomorrow 9:15am for 15 minutes` | 15-minute event |
| `book club every Saturday 7pm` | Repeats weekly, forever |
| `gym Mon Wed Fri 6am for 8 weeks` | Repeats on three days, 24 occurrences |
| `1:1 every other Tuesday 10am` | Repeats fortnightly |
| `retro last Friday of the month 4pm` | Repeats monthly on the last Friday |
| `rent reminder monthly until Dec 20` | Repeats monthly with an end date |
| `move the dentist to 4pm` | Finds the dentist event and moves it |
| `push standup back 15 minutes` | Same event, 15 minutes later |
| `make book club an hour and a half` | Changes the length, not the start |
| `lunch with Sam is at Zuni now` | Sets the location |
| `/help` | The usage hint |
| `how are you` | 🤔 "That isn't a request to create an event" |

### Recurring events

Say it however you'd say it out loud — *"book club every Saturday at 7pm"* —
and you get one Google Calendar series, not a pile of copies. The reply tells
you what it understood:

> ✅ **Book club**
> 🗓 Sat, Aug 22, 2026 · 7:00 PM – 8:00 PM
> 🔁 Every Saturday

`DAILY`, `WEEKLY`, `MONTHLY` and `YEARLY` are supported, with an interval
(*every other*), specific weekdays (*Mon Wed Fri*), monthly ordinals (*first
Monday*, *last Friday*), and an end condition — either a count (*for 8 weeks*)
or a date (*until December 20*). Left open, it repeats indefinitely.

The LLM doesn't emit the recurrence rule directly; it fills in a small fixed
structure (`freq` / `interval` / `byday` / `count` / `until`) that
`events.py` validates and assembles into the RRULE. A model that invents something
unsupported gets you a specific complaint — `Unsupported repeat frequency:
'FORTNIGHTLY'` — instead of a cryptic 400 from Google.

Two details worth knowing:

- **The first occurrence is realigned.** A calendar series always includes its
  start date, even when that date doesn't fit the pattern — so if the model
  says "starts today" for a Saturday series, you'd get a stray event today plus
  the real series. Weekly patterns are snapped forward to the first matching
  weekday instead.
- **The clock time survives DST.** Times are stored as wall-clock plus a
  timezone, so a 7pm series stays at 7pm across a DST change rather than
  drifting to 6pm.

Moving a series is covered below. To cancel one, use Google Calendar.

### Changing an event

Talk about an event that already exists and Calendar Connect goes and finds
it — you
never give it an id, and you don't have to name it the way the calendar does:

```
move the dentist to 4pm
```

> ✏️ **Dentist**
> 🗓 Thu, Sep 3, 2026 · 4:00 PM – 5:00 PM
> ↩️ ~~Thu, Sep 3, 2026 · 3:00 PM – 4:00 PM~~
> [Open in Calendar](#)

The old time is struck through so you can see what actually moved. Start time,
end time, length, title, location and description are all fair game, and
switching an event to or from all-day works too.

It takes two LLM calls. The first decides whether the message creates something
or changes something, and for a change pulls out a search term — *"push
tomorrow's standup back 15 min"* gives `standup`. That searches the calendar.
The second call sees the real events that came back and picks the one you meant,
then works out the new values from what it can see. So the model never has to
guess an event's current time; it reads it.

A few behaviours worth knowing:

- **Moving keeps the length.** Change only the start and the event keeps its
  duration. Say *"make it two hours"* or *"until 5"* to change the length.
- **The search widens if it comes up empty.** A keyword search covers a month
  back and a year ahead. If it matches nothing — a title with no words in
  common with your message — it lists everything from a week back to two
  months ahead and lets the model read the titles itself.
- **One occurrence by default.** *"Move Thursday's standup to 4"* moves that
  Thursday. *"Move all my standups to 4"* — "every", "all", "from now on" —
  moves the series, and the reply says which it did (🔂 vs 🔁). A series move is
  re-derived from the series' own start date, so the whole thing shifts by the
  same amount rather than collapsing onto the occurrence you mentioned.
- **Wall-clock survives DST here too.** Same reasoning as for creation: a series
  moved to 8pm is at 8pm on both sides of a time change.

When the model can't find a plausible match, it says so rather than editing
something at random:

> 🤔 I couldn't find an event like that on your calendar.

The first message after an idle period takes a few seconds (cold start).
Telegram waits up to 60s, so there it just feels slow, never broken — Siri is
far less patient, which is why [latency](#latency-is-the-thing-to-watch) gets
its own section. A change is two LLM calls plus two calendar calls, so it runs
a little longer than a create.

---

## Step 7 — Your Mac and your iPhone

Telegram works from anywhere, but it costs you two app switches and a
conversation you have to find. This step puts the same scheduler behind a
keyboard shortcut on the Mac and behind Siri on the phone.

Both use the `/event` endpoint, which runs the same handlers with the reply
coming back in the HTTP response instead of over Telegram.

### The endpoint

```
POST https://…-uc.a.run.app/event
Authorization: Bearer <token from ./scripts/api-token.sh>
Content-Type: application/json

{"text": "dentist next Tuesday 3pm"}
```

```json
{
  "ok": true,
  "message": "✅ Dentist\n🗓 Tue, Sep 8, 2026 · 3:00 PM – 4:00 PM",
  "link": "https://calendar.google.com/calendar/event?eid=…"
}
```

`message` arrives **already formatted as plain text** — no HTML, no markdown,
newlines where you'd want them. That's deliberate: the client's whole job is to
show that string. When you want to change how confirmations read, you edit
`format_confirmation` in `src/receipts.py` and redeploy, and every channel
updates at once. Nothing downstream needs touching.

| Status | When |
| --- | --- |
| `200` | Calendar Connect handled it. Check `ok` — `false` means *"I couldn't find that event"*, which is an answer, not an error |
| `400` | Empty or missing `text` |
| `403` | Bad or missing bearer token |
| `405` | Not a POST |
| `503` | `API_TOKEN` isn't set on the function — `/event` fails closed rather than open |

A request that actually reached the handlers always comes back `200`, even
when it
couldn't do what you asked. Shortcuts treats any non-2xx as a failed action and
throws away the body, so a `422` here would replace *"🤔 I couldn't find an
event like that"* with a generic Shortcuts error. Non-2xx is reserved for
problems the caller has to fix.

### 7a — Check it works

```bash
terraform -chdir=terraform output -raw event_url    # the URL
./scripts/api-token.sh                              # the token
```

```bash
curl -sS -X POST "$(terraform -chdir=terraform output -raw event_url)" \
  -H "Authorization: Bearer $(./scripts/api-token.sh)" \
  -H "Content-Type: application/json" \
  -d '{"text": "dentist next Tuesday 3pm"}'
```

You should get JSON back and an event on your calendar. Time it — see
[latency](#latency-is-the-thing-to-watch) below.

### 7b — Build the Shortcut

Build this **once on the Mac**. With iCloud Shortcuts on, it appears on your
iPhone within a minute, and that's what gives you Siri.

Open **Shortcuts** → **+** → name it (see the warning below) → add six actions:

| # | Action | Settings |
| --- | --- | --- |
| 1 | **If** | `Shortcut Input` · *has any value* |
| 2 | ↳ **Set Variable** | `EventText` = `Shortcut Input` |
| 3 | **Otherwise** → **Ask for Input** | Type `Text`, prompt *"What's the event?"* — then **Set Variable** `EventText` to `Provided Input` |
| 4 | **Get Contents of URL** | URL = your `event_url`<br>Method `POST`<br>Headers: `Authorization` = `Bearer <token>`<br>Request Body `JSON`, one field: `text` = `EventText` |
| 5 | **Get Dictionary Value** | Get `Value` for `message` in `Contents of URL` |
| 6 | **Show Notification** | The dictionary value from step 5 |

The `If` in step 1 is what lets one shortcut serve both entry points: the share
sheet hands it text, the hotkey hands it nothing and so it asks.

Optionally add a step 7 — **Get Dictionary Value** for `link` → **Copy to
Clipboard** — so the calendar URL is on your clipboard afterwards.

> **Don't name it "Add Event" or "Add to Calendar".** Siri routes those phrases
> to its own built-in calendar feature and your shortcut never runs. Pick
> something with no native collision: **Calendar Connect**, **Quick Event**. The
> name *is* the Siri phrase, so make it something you don't mind saying.

Two settings in **Shortcut Details** (the ⓘ panel on the right):

- **Show in Share Sheet**, accepted input **Text** — enables the select-and-share flow.
- **Pin in Menu Bar** if you want a mouse-reachable fallback — worth ticking
  while you're getting a hotkey working, so you always have a way in.

### 7c — The Mac hotkey

Every method below ends up running the same one-line command, so pick whichever
matches what you already have installed — the choice doesn't affect anything
else in this guide:

```bash
open -g "shortcuts://run-shortcut?name=Calendar%20Connect"
```

URL-encode the name: `%20` for each space. If you named the shortcut something
else in 7b, use that name here, exactly, including case.

**Why the URL scheme and not `shortcuts run`.** The `shortcuts` CLI exists and
`shortcuts run "Calendar Connect"` looks tidier, but it has a history of
behaving oddly with shortcuts that present UI, which "Ask for Input" does. The
URL scheme goes through the Shortcuts app proper, so the dialog reliably
appears. It also works verbatim on iOS.

#### Option 1 — Shortcuts itself (nothing to install)

Shortcuts can register the hotkey on its own, so try this first:

1. Open the shortcut, then the **Shortcut Details** panel (the ⓘ on the right).
2. Tick **Use as Quick Action** and **Services Menu**.
3. Set **Receive** to **No Input** — a Quick Action that wants input is only
   offered when the frontmost app has some to give, which is exactly how these
   end up mysteriously dead in half your apps.
4. Click **Add Keyboard Shortcut** and press your combination.

No extra software, and it syncs with the shortcut. The catch is that
Services-menu hotkeys have a long history of going quiet — usually after an
update, or in a specific app that swallows the key. If yours stops firing,
untick and re-tick **Services Menu** to re-register it, and if it keeps
happening use one of the options below, which don't go through the Services
menu at all.

#### Option 2 — a launcher you already use

| Launcher | How |
| --- | --- |
| **Raycast** | It reads your Shortcuts library directly: search the shortcut by name, then ⌘K → **Configure Command** → assign a hotkey or an alias |
| **Alfred** (Powerpack) | New workflow → **Hotkey** trigger → **Run Script** action → paste the `open -g` command |
| **LaunchBar** | Index Shortcuts, then assign an abbreviation or hotkey to it |

#### Option 3 — a hotkey daemon or tiling WM

If you already run one of these, it owns your keyboard and is the most reliable
place to put this. All four run the same `open -g` command:

**AeroSpace** — one line in `~/.config/aerospace/aerospace.toml` under
`[mode.main.binding]`, then `aerospace reload-config`:

```toml
alt-shift-c = 'exec-and-forget open -g "shortcuts://run-shortcut?name=Calendar%20Connect"'
```

**skhd** — one line in `~/.config/skhd/skhdrc`, then `skhd --restart-service`:

```
alt + shift - c : open -g "shortcuts://run-shortcut?name=Calendar%20Connect"
```

**Hammerspoon** — in `~/.hammerspoon/init.lua`, then reload the config:

```lua
hs.hotkey.bind({"alt", "shift"}, "c", function()
  hs.execute('open -g "shortcuts://run-shortcut?name=Calendar%20Connect"')
end)
```

**Karabiner-Elements** — a complex modification whose `to` event is a
`shell_command` running the same line. Workable, but it's the heaviest way to
do this; prefer any of the above if you have them.

#### Option 4 — Automator, if you want zero dependencies and Option 1 failed

New **Quick Action**, set *Workflow receives* to **no input** in **any
application**, add **Run Shell Script** with the `open -g` line, save it as
*Calendar Connect*. Then **System Settings → Keyboard → Keyboard Shortcuts →
Services** and give it a key. This is the same Services plumbing as Option 1,
just built by hand, so it inherits the same flakiness — it's here because it
needs nothing installed and some people find it re-registers more reliably.

---

Whichever you picked, press the key and a small system dialog appears
mid-screen:

```
┌──────────────────────────────────┐
│  ⚙️  Calendar Connect             │
│                                  │
│  What's the event?               │
│  ┌────────────────────────────┐  │
│  │ dentist next tues 3pm      │  │
│  └────────────────────────────┘  │
│                                  │
│              [ Cancel ]  [ Done ]│
└──────────────────────────────────┘
```

Type, hit Enter, it vanishes. A few seconds later a notification slides in from
the top right with the confirmation. No app opens, no Dock icon, nothing to
close.

If the dialog never appears, run the `open -g` line straight in a terminal
first. That separates a broken shortcut from a broken key binding, which are
otherwise very hard to tell apart.

The dialog is Apple's, so you don't get to style it — it's a gray rounded box
with a Shortcuts gear, not a Spotlight-style bar, and it takes focus while it's
up. If that bothers you enough, this is the point at which a small
`LSUIElement` Swift app with `RegisterEventHotKey` and a floating `NSPanel`
becomes worth writing; the endpoint doesn't change.

**Skipping the prompt.** The URL scheme takes text directly, which is handy for
a second binding or a script that already has the text:

```bash
open -g "shortcuts://run-shortcut?name=Calendar%20Connect&input=text&text=dentist%20tuesday%203pm"
```

### 7d — The iPhone

The shortcut is already there via iCloud. Everything below is a setting, not
more building:

| How | Where to turn it on |
| --- | --- |
| **"Hey Siri, Calendar Connect"** | Nothing to do — the name is the phrase. Siri asks *"What's the event?"*, you dictate, it reads the confirmation back |
| **Action Button** | Settings → Action Button → Shortcut → Calendar Connect |
| **Back Tap** | Settings → Accessibility → Touch → Back Tap → Double Tap → Calendar Connect |
| **Share sheet** | Already on from 7b. Select text anywhere → Share → Calendar Connect |
| **Home/Lock Screen** | Long-press the shortcut in the app → Add to Home Screen; or the Shortcuts widget |

Siri is the one that actually replaces Telegram — it's hands-free, so you can
add an event while driving, which is often exactly when you find out about one.

The share sheet is the underrated one: an email says *"practice moved to
Thursday 6pm"*, you select that text, share it to Calendar Connect, and there's no
typing at all.

### Latency is the thing to watch

`min_instance_count` defaults to `0`, so an idle function cold-starts. On
Telegram nobody notices. Standing in a hallway holding your phone, or waiting
on Siri, you will — and Siri is the least patient surface of the three.

Time the `curl` in 7a twice: once after an idle hour, once immediately after.
The gap is your cold start. If it's bad enough to annoy you:

```hcl
min_instance_count = 1
```

That keeps one instance warm. It costs real money — an always-on instance is
outside the free tier — so measure before you reach for it. A change (*"move the
dentist"*) is two LLM calls plus two calendar calls, so it always runs longer
than a create.

### Two things the Shortcut can't do

**The token sits in the shortcut in plain text.** It syncs through iCloud, and
anyone with your unlocked device can open the shortcut and read it. There's no
clean fix that keeps iOS sync — Keychain access needs "Run Shell Script", which
is macOS-only. It's mitigated by what the token can actually do: create and
change events on one calendar, nothing else. Rotate it whenever you like:

```bash
terraform -chdir=terraform taint random_password.api_token
terraform -chdir=terraform apply
```

Then paste the new token into the shortcut.

**Notifications aren't clickable.** "Show Notification" can't open a URL on tap,
so the `link` field can't become a tappable notification. Copy it to the
clipboard (the optional step 7) or open Calendar yourself.

## Day-to-day

**Change the code.** Edit the module that owns it — see
[how it's put together](#how-its-put-together) — then `terraform apply`. The
zip's hash is part of the object name, so Terraform notices and redeploys.
`src/` is zipped whole, so a new subpackage needs its own `__pycache__` line in
the `excludes` in `terraform/main.tf`; stray bytecode changes the hash and
forces a rebuild that deploys nothing new.

**Change the model or timezone.** Edit `terraform.tfvars`, `terraform apply`.
These are plain environment variables — the redeploy is quick.

**Read the logs.** Everything the function logs, including full tracebacks:

```bash
gcloud beta run services logs tail calendar-connect --region us-central1
```

Or Console → Cloud Run → `calendar-connect` → **Logs**.

**Test a phrasing without deploying.** Hits the LLM only — never Telegram,
never your calendar:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r src/requirements.txt
export LLM_API_KEY=sk-...
./scripts/try-parse.py "brunch with mom the sunday after next at 11"
```

It prints the raw LLM JSON, the exact Calendar API body, and the reply you'd
get — the fastest way to see whether a miss is the model's fault or the code's.
For a message that changes an event it stops after the first call and prints
the search it would run, since matching needs calendar access this script
deliberately doesn't have.

**Rotate a secret.** Change it in `terraform.tfvars` and apply; a new secret
version is created and the function picks it up (it reads `latest`). For the
bot token or webhook secret the webhook is re-registered automatically.

The `/event` API token isn't in tfvars — Terraform generates it. Rotate it with
`terraform taint random_password.api_token && terraform apply`, then paste the
new value from `./scripts/api-token.sh` into your Shortcut. The old token stops
working the moment the function redeploys.

**Turn it off temporarily.** `./scripts/delete-webhook.sh` — Telegram stops
delivering; nothing is destroyed. `./scripts/set-webhook.sh` turns it back on.
That only closes the Telegram door; the hotkey and Shortcut keep working. To
close `/event` too, rotate the API token and don't tell anyone the new one.

**Tear it all down.**

```bash
./scripts/delete-webhook.sh
terraform destroy
```

The enabled APIs are deliberately left on. Revoke the calendar share by hand in
Calendar settings, and delete the bot via BotFather (`/deletebot`).

**Check what images are stored.** The cleanup policy runs on Google's schedule,
not instantly, so expect a lag of up to a day after a deploy:

```bash
gcloud artifacts docker images list \
  "$(terraform -chdir=terraform output -raw image_repository)" --include-tags
```

---

## Troubleshooting

Start here — it tells you whether Telegram is even reaching you:

```bash
./scripts/webhook-info.sh
```

`pending_update_count` climbing or a `last_error_message` means delivery is
failing. If `url` is empty, the webhook was never registered.

For the Mac hotkey or the Shortcut, start with the `curl` from
[7a](#7a--check-it-works) instead. It tells you in one shot whether the problem
is the endpoint or the client: if `curl` works and the Shortcut doesn't, the
bug is in the Shortcut.

| Symptom | Cause | Fix |
| --- | --- | --- |
| Bot totally silent, `webhook-info` shows no url | Webhook not registered | `./scripts/set-webhook.sh` |
| `last_error_message: Wrong response from the webhook: 403 Forbidden` | Secret mismatch between Telegram and the function | Re-apply, or `./scripts/set-webhook.sh` |
| Silent, but logs show `Ignoring message from unauthorized user id` | `allowed_telegram_user_id` is wrong | Re-check with @userinfobot; the logged id is the right one |
| ⚠️ `Couldn't add … 404 Not Found` | Calendar not shared with the service account, or wrong `calendar_id` | Redo step 3c; use your email, not `primary` |
| ⚠️ `Couldn't add … 403 forbidden` | Shared read-only | Change the share to **Make changes to events** |
| Events go somewhere you can't see | `calendar_id = "primary"` | That's the service account's own calendar. Use your email address |
| ⚠️ `LLM returned 400 … model` | Wrong `llm_model` | Check the provider's model list |
| ⚠️ `LLM returned 401` | Bad `llm_api_key` | Regenerate; re-apply |
| ⚠️ `LLM returned 402` | Out of credit | Top up the provider account |
| Times land an hour off around March/November | Wrong `timezone` | Use the IANA name for your zone; the code handles DST from that |
| Event on the wrong day | Model mis-resolved a relative date | Reproduce with `try-parse.py`; be more explicit, or tighten the prompt in `src/llm.py` |
| ⚠️ `Unsupported repeat frequency/day` | Model invented a recurrence field | Rephrase ("every other Tuesday" beats "biweekly"); check with `try-parse.py` |
| One-off event when you meant a series | Model didn't read it as recurring | Use the word "every" — "every Saturday", not "Saturdays" |
| Repeating event but the wrong pattern | Model's `byday`/`interval` was off | `try-parse.py` prints the RRULE; fix the series in Google Calendar |
| A change created a second event instead | Model read it as a new event | Lead with a verb — "move the dentist…", not "dentist at 4pm"; `try-parse.py` prints the intent it chose |
| 🤔 `I couldn't find an event like that` | Search matched nothing in range | Use a word from the event's actual title; say when it is ("last week's…") |
| It changed the wrong event | Several similar titles in range | Say which one — "the dentist on the 12th" |
| It moved one occurrence, you meant all | Scope defaulted to this occurrence | Say "every" or "all my …" |
| `terraform apply`: build fails with a permissions error | IAM hadn't propagated | Just run `terraform apply` again |
| `terraform apply`: `allUsers` policy rejected | Org policy `iam.allowedPolicyMemberDomains` | Deploy in a personal (non-org) project, or ask an admin to exempt it |
| `terraform apply`: `Permission denied ... actAs` | Missing Service Account User | Grant yourself Owner, or `roles/iam.serviceAccountUser` |
| `terraform apply`: billing error enabling APIs | Billing not linked | Step 3a |
| `terraform apply`: permission denied creating the budget | No `billing.costsManager` on the billing account | Grant it, or set `billing_account_id = ""` to skip |
| `Error creating Budget: 403 ... requires a quota project` | ADC user credentials with no quota project set | `gcloud auth application-default set-quota-project YOUR_PROJECT_ID` |
| Instances fail to start, image pull error | Artifact Registry read grant hadn't propagated | Re-run `terraform apply` |
| Duplicate events appear | Telegram retried a slow webhook | Known v1 gap — see below |
| **Mac/iPhone:** `curl` returns `Forbidden` | Wrong or stale bearer token | Re-read it with `./scripts/api-token.sh`; check the header is `Bearer <token>` |
| **Mac/iPhone:** `curl` returns `API is not enabled` | `API_TOKEN` didn't reach the function | `terraform apply`; confirm the secret exists in Secret Manager |
| Hotkey does nothing at all | Binder not reloaded, or the key is already taken | Reload it (`aerospace reload-config`, `skhd --restart-service`, …); check the combination isn't bound elsewhere, including by macOS itself |
| Hotkey worked, then stopped | Services-menu registration went stale (Options 1 and 4 only) | Untick and re-tick **Services Menu** in Shortcut Details; if it recurs, move to a launcher or hotkey daemon |
| Hotkey works in some apps, not others | Quick Action set to receive input rather than **No Input** | Shortcut Details → **Receive** → **No Input** |
| Hotkey fires nothing anywhere, but the menu bar item works | The `open -g` command or shortcut name is wrong | Run the `open -g` line in a terminal — it should raise the dialog on its own |
| Hotkey fires but no dialog appears | Shortcut name in the URL doesn't match, or spaces aren't encoded | Names are exact and case-sensitive; use `%20` for every space |
| Shortcut fails at "Get Dictionary Value" | Response wasn't parsed as JSON | The function sets `Content-Type: application/json`; if you proxied it through something, check that survived |
| Shortcut shows a generic error, no message | Non-2xx — auth, or not a POST | Only 403/405/503 do this; run the `curl` to see which |
| Siri answers instead of running the shortcut | Name collides with a built-in phrase | Rename it away from "Add Event"/"Add to Calendar" |
| Shortcut missing on the iPhone | iCloud Shortcuts sync off | Settings → your name → iCloud → check Shortcuts; give it a minute |
| Siri times out, Telegram is fine | Cold start plus LLM exceeded Siri's patience | See [latency](#latency-is-the-thing-to-watch); consider `min_instance_count = 1` |

---

## Cost

**$0/month on Google Cloud**, plus roughly **2¢/month** of DeepSeek tokens at 4
messages a day. Every piece sits inside a free tier with room to spare:

| Resource | Usage at ~120 msg/month | Free tier | Cost |
| --- | --- | --- | --- |
| Cloud Functions / Run | 120 req, ~90 GiB-s, ~60 vCPU-s | 2M req, 360K GiB-s, 180K vCPU-s | $0 |
| Secret Manager | 3 active versions, ~300 accesses | 6 versions, 10K access ops | $0 |
| Cloud Storage (source zip) | one ~10 KB object | 5 GB-months, US regions | $0 |
| Artifact Registry | 1–3 images, ~0.3–0.5 GB each | 0.5 GB | $0 with cleanup |
| Cloud Build | ~3 min per deploy | 2,500 build-min/month | $0 |
| Cloud Logging | a few hundred KB | 50 GiB ingest | $0 |
| Egress to DeepSeek + Telegram | ~1 MB | 1 GiB from N. America | $0 |
| Calendar API, IAM Credentials, Eventarc, Pub/Sub | enabled, ~unused | — | $0 |

That holds to roughly **1,000 messages a day**, well past the point where the
LLM bill dwarfs the infrastructure.

Three things could actually charge you, and two are now handled in Terraform:

- **Container images piling up.** Each deploy builds a ~300–500 MB image, and
  the 0.5 GB free tier fits about one. `terraform/registry.tf` owns the repo and
  expires images after `image_retention_days` (7), while always keeping the most
  recent `image_keep_count` (3) — `KEEP` rules beat `DELETE` rules in Artifact
  Registry, so the image your service is running can never be collected, however
  long you go between deploys. Without this it's maybe $0.15/month and growing.
- **`min_instance_count = 1`.** The one genuinely non-free setting: an always-on
  256 MiB instance runs about **$2–3/month** in idle CPU and memory. Leave it at
  0 unless the cold start bothers you.
- **Traffic to the public endpoint.** A request with a bad secret is rejected in
  milliseconds before any LLM or Calendar call, and `max_instance_count = 3`
  caps throughput, so a flood is bounded — but requests past 2M/month bill at
  ~$0.40/million. This is what the budget alert is for.

### The budget alert

Set `billing_account_id` and Terraform creates a **$1/month** budget scoped to
this project, emailing the billing account's admins at 50%, 90% and 100% of
actual spend, plus once if the month is merely *forecast* to go over. Since the
expected bill is $0, a 50¢ alert means something is wrong — and 50¢ is a much
better time to find out than $50.

**On the $300 free trial:** the budget deliberately ignores the trial credit
(`credit_types_treatment = "INCLUDE_SPECIFIED_CREDITS"` in `budget.tf`). If it
counted it, the credit would cancel out your costs, reported spend would sit at
$0 for the full 90 days, and the alert would stay silent no matter what the
stack was actually running up. As configured, it tracks what you'd be paying if
the trial weren't there — which is the number you want to see *before* the
credit runs out. Ordinary free-tier usage is still netted out, so normal traffic
won't trip it.

```
budget_alert = "USD 1/month, alerting at 50/90/100%"
```

If the output says `not created (billing_account_id is empty)`, either you left
it blank or you skipped it deliberately. It's optional because creating a budget
needs `roles/billing.costsManager` **on the billing account**, which project
Owner does not grant. Check with:

```bash
gcloud billing accounts get-iam-policy YOUR-BILLING-ACCOUNT-ID
```

Everything else in the stack deploys fine without it.

Two caveats on all of the above. These free tiers are **per billing account**,
not per project — if other projects already consume the Cloud Run or Secret
Manager allowance, this one bills at the margin. And the figures are list prices
as of early 2026; the structure is stable but confirm current rates if a dollar
matters.

## Known limitations

- **No deleting.** Calendar Connect creates and changes events; it won't
  cancel one.
  Do that in Google Calendar.
- **The repeat pattern is fixed once set.** A change can move a series or rename
  it, but not turn a weekly event into a monthly one. Asking gets you a 🤔
  rather than a surprise.
- **One event per message.** A message that changes two events at once picks
  one of them.

Two more are deliberate, per the plan:

- **No confirm step.** The event is added immediately. A "add this? 👍" flow
  spans two messages, so the pending event has to be stored in between.
- **No duplicate protection.** If a cold start plus a slow LLM call runs long,
  Telegram may retry the delivery and you get two events. (The function returns
  `200` on every path it can, including errors, specifically to make this rare.)
  This one is Telegram's alone — neither the Shortcut nor `curl` retries on its
  own, so the Mac and phone can only double up if you press the key twice.

The planned next step handles both at once: a small **Firestore** collection
holding a pending event, keyed by who is asking. The follow-up reads it back
and either inserts or discards — which is the confirm flow *and* the dedup key.
That key used to be the Telegram chat id; with two channels it has to be the
channel plus its own idea of identity, and the API channel has no identity
beyond "whoever holds the token".

---

## Appendix A — using a JSON key instead

If you prefer the classic service-account-key approach:

```bash
PROJECT_ID=$(terraform -chdir=terraform output -raw project_id)
SA=$(terraform -chdir=terraform output -raw service_account_email)

gcloud iam service-accounts keys create /tmp/sa-key.json \
  --iam-account="$SA" --project="$PROJECT_ID"
```

Add to `terraform.tfvars`:

```hcl
calendar_sa_key_json = file("/tmp/sa-key.json")
```

`terraform apply`, then **shred the local copy** (`rm -P /tmp/sa-key.json`) —
it's in Secret Manager now. The function prefers the key whenever
`GOOGLE_SA_KEY_JSON` is non-empty and ignores the impersonation path.

To go back to keyless, set the variable to `""` and apply.

## Appendix B — what protects a public endpoint

The function URL is world-reachable; Telegram has no way to hold Google IAM
credentials. Both front doors are checked before anything is parsed, and both
compare with `hmac.compare_digest` rather than `==`.

**On `/` (Telegram):**

1. **The secret header.** Every request must present
   `X-Telegram-Bot-Api-Secret-Token` matching your webhook secret. Failures
   return 403 before anything is parsed.
2. **The user-id check.** Even a valid-looking Telegram update is dropped unless
   `message.from.id` is exactly yours. This is what keeps strangers who find
   your bot from filling your calendar.

**On `/event` (the Mac and phone):**

3. **The bearer token.** A 48-character generated token, mounted from Secret
   Manager. No token, wrong token, or the wrong auth scheme is a 403.
4. **It fails closed.** If `API_TOKEN` somehow isn't set on the function,
   `/event` returns 503 rather than serving unauthenticated requests. There is
   no configuration in which the endpoint is open.

**On both:**

5. **The service account's reach.** It can write to exactly the one calendar you
   shared with it, and holds no other project permissions. This is the backstop:
   the worst a leaked token buys is the ability to make a mess of one calendar.

A GET to the URL returns `calendar-connect is up` and nothing else — it's there so
you can confirm a deploy.

The `/event` token is weaker than the Telegram path by design: it has no
equivalent of the user-id check, because a Shortcut has no identity to present.
It's a bearer token and whoever holds it is you. That's the trade for something
Siri can call. See [Two things the Shortcut can't do](#two-things-the-shortcut-cant-do)
for where it's
stored and how to rotate it.
