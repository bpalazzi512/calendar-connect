# Calendar Bot

Text a Telegram bot in plain English — *"dentist next Tuesday 3pm"*, *"book club
every Saturday 7pm"* — and the event lands on your Google Calendar, one-off or
recurring. You get a confirmation with a link back.

See [initial-plan.md](initial-plan.md) for the design rationale. This file is
the build-and-run guide.

```
you ──▶ Telegram ──POST──▶ Cloud Function ──▶ LLM (parse to JSON)
                                │
                                └──▶ Google Calendar (insert)
                                └──▶ Telegram (reply with link)
```

## Repo layout

| Path | What it is |
| --- | --- |
| `src/main.py` | The whole function: verify → parse → insert → reply |
| `src/requirements.txt` | Python dependencies |
| `terraform/` | All the infrastructure |
| `terraform/terraform.tfvars.example` | Template for your settings |
| `scripts/set-webhook.sh` | Re-point Telegram at the function |
| `scripts/webhook-info.sh` | Ask Telegram if delivery is failing |
| `scripts/delete-webhook.sh` | Turn the bot off without destroying anything |
| `scripts/try-parse.py` | Run one message through the LLM locally, no deploy |

Terraform zips `src/` and deploys it, so `src/` holds only what the function
needs at runtime.

---

## What Terraform builds

- Enables the required APIs (Cloud Functions, Run, Build, Artifact Registry,
  Secret Manager, IAM Credentials, **Calendar**, and friends).
- A **service account** the function runs as. This is the identity you share
  your calendar with.
- Three **Secret Manager** secrets — bot token, webhook secret, LLM API key —
  mounted into the function as environment variables. No secret is ever
  baked into the deployed source.
- A **GCS bucket** holding the zipped source.
- An **Artifact Registry repo** for the built container images, with a cleanup
  policy so old builds expire instead of accumulating.
- The **2nd-gen Cloud Function**, public (Telegram can't authenticate to IAM),
  protected by the shared-secret header and the allowed-user-id check.
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

PROJECT_ID=my-calendar-bot-$RANDOM     # must be globally unique
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

**3c. Share your calendar with the bot.**

The service account's address is predictable, so you can do this now:

```
calendar-bot@PROJECT_ID.iam.gserviceaccount.com
```

(`calendar-bot` is the `function_name` variable; if you change that, change
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
`register_webhook` (true), `function_name` (`calendar-bot`),
`budget_amount` (1), `budget_currency` (`USD`), `image_retention_days` (7),
`image_keep_count` (3).

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
function_url          = "https://calendar-bot-xxxxxxxxxx-uc.a.run.app"
service_account_email = "calendar-bot@my-project.iam.gserviceaccount.com"
```

The webhook is registered automatically. Confirm the function is up:

```bash
curl "$(terraform output -raw function_url)"     # -> calendar-bot is up
```

## Step 6 — Use it

Message your bot in Telegram:

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
structure (`freq` / `interval` / `byday` / `count` / `until`) that `main.py`
validates and assembles into the RRULE. A model that invents something
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

To change or cancel a series, use Google Calendar — the bot only creates.

The first message after an idle period takes a few seconds (cold start).
Telegram waits up to 60s, so it just feels slow, never broken.

---

## Day-to-day

**Change the code.** Edit `src/main.py`, then `terraform apply`. The zip's hash
is part of the object name, so Terraform notices and redeploys.

**Change the model or timezone.** Edit `terraform.tfvars`, `terraform apply`.
These are plain environment variables — the redeploy is quick.

**Read the logs.** Everything the function logs, including full tracebacks:

```bash
gcloud beta run services logs tail calendar-bot --region us-central1
```

Or Console → Cloud Run → `calendar-bot` → **Logs**.

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

**Rotate a secret.** Change it in `terraform.tfvars` and apply; a new secret
version is created and the function picks it up (it reads `latest`). For the
bot token or webhook secret the webhook is re-registered automatically.

**Turn it off temporarily.** `./scripts/delete-webhook.sh` — Telegram stops
delivering; nothing is destroyed. `./scripts/set-webhook.sh` turns it back on.

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
| Event on the wrong day | Model mis-resolved a relative date | Reproduce with `try-parse.py`; be more explicit, or tighten the prompt in `src/main.py` |
| ⚠️ `Unsupported repeat frequency/day` | Model invented a recurrence field | Rephrase ("every other Tuesday" beats "biweekly"); check with `try-parse.py` |
| One-off event when you meant a series | Model didn't read it as recurring | Use the word "every" — "every Saturday", not "Saturdays" |
| Repeating event but the wrong pattern | Model's `byday`/`interval` was off | `try-parse.py` prints the RRULE; fix the series in Google Calendar |
| `terraform apply`: build fails with a permissions error | IAM hadn't propagated | Just run `terraform apply` again |
| `terraform apply`: `allUsers` policy rejected | Org policy `iam.allowedPolicyMemberDomains` | Deploy in a personal (non-org) project, or ask an admin to exempt it |
| `terraform apply`: `Permission denied ... actAs` | Missing Service Account User | Grant yourself Owner, or `roles/iam.serviceAccountUser` |
| `terraform apply`: billing error enabling APIs | Billing not linked | Step 3a |
| `terraform apply`: permission denied creating the budget | No `billing.costsManager` on the billing account | Grant it, or set `billing_account_id = ""` to skip |
| `Error creating Budget: 403 ... requires a quota project` | ADC user credentials with no quota project set | `gcloud auth application-default set-quota-project YOUR_PROJECT_ID` |
| Instances fail to start, image pull error | Artifact Registry read grant hadn't propagated | Re-run `terraform apply` |
| Duplicate events appear | Telegram retried a slow webhook | Known v1 gap — see below |

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

- **Create only.** The bot adds events (including recurring series); it can't
  edit, move, or delete them. Do that in Google Calendar.

Two more are deliberate, per the plan:

- **No confirm step.** The event is added immediately. A "add this? 👍" flow
  spans two messages, so the pending event has to be stored in between.
- **No duplicate protection.** If a cold start plus a slow LLM call runs long,
  Telegram may retry the delivery and you get two events. (The function returns
  `200` on every path it can, including errors, specifically to make this rare.)

The planned next step handles both at once: a small **Firestore** collection
holding a pending event keyed by chat id. The second webhook call reads it back
and either inserts or discards — which is the confirm flow *and* the dedup key.

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
credentials. Three things stand between the internet and your calendar:

1. **The secret header.** Every request must present
   `X-Telegram-Bot-Api-Secret-Token` matching your webhook secret, compared with
   `hmac.compare_digest`. Failures return 403 before anything is parsed.
2. **The user-id check.** Even a valid-looking Telegram update is dropped unless
   `message.from.id` is exactly yours. This is what keeps strangers who find
   your bot from filling your calendar.
3. **The service account's reach.** It can write to exactly the one calendar you
   shared with it, and holds no other project permissions.

A GET to the URL returns `calendar-bot is up` and nothing else — it's there so
you can confirm a deploy.
