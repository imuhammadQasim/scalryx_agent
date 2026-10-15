# Scalryx sales agent

The AI behind the chat on scalryx.com. "Scout" answers visitors' questions from
the site's own facts, works out what their business needs, scores the lead
in plain code, books the free 30 minute growth call through Cal.com right in
the chat, emails a project brief, and alerts the team when a hot lead turns
up.

```
Browser (AskAI widget)
   │  POST /api/chat  (server-sent events)
   ▼
Next.js route  src/app/api/chat/route.js   ← falls back to FAQ answers if the agent is down
   │  X-Agent-Key, X-Visitor-IP
   ▼
FastAPI  app/main.py ──► app/agent.py  (OpenAI Responses API + tool loop, streams events)
                           ├─ app/tools.py    update_lead · calculate_roi · check_availability
                           │                  book_meeting · send_project_brief · handoff_to_human
                           ├─ app/scoring.py  score, tier and next move, in plain code
                           ├─ integrations/   Cal.com v2 · SMTP
                           └─ app/db.py       SQLite (or Postgres): conversations, messages, leads, bookings
```

Events the widget receives: `session`, `text`, `brief` (what the agent has
understood so far, without the score), `slots` (bookable times), `roi`,
`booking`, `done`.

## Run it locally

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # then fill in OPENAI_API_KEY and AGENT_API_KEY at least
uvicorn app.main:app --reload --port 8000
pytest                            # tests use a fake OpenAI client, no key needed
```

Then point the website at it. In `scalryx.com/.env.local`:

```
AGENT_API_URL=http://127.0.0.1:8000
AGENT_API_KEY=<same value as in the agent's .env>
```

`GET /healthz` shows which integrations are switched on, and the startup log
warns about anything missing.

## Keep the facts in sync

The agent answers only from `knowledge/scalryx.json`, which is exported from
the website's `src/data`. After changing prices, services or FAQs:

```bash
cd ../scalryx.com && npm run export:knowledge
# then restart the agent (on the server: sudo systemctl restart scalryx-agent)
```

The system prompt is built once per process and must not change during a
conversation, which is why the facts load at startup rather than per request.

## Cal.com (in-chat booking)

1. Create an event type, for example "Free growth call", 30 minutes, with
   your availability and Cal Video (or Google Meet) as the location.
2. Note its numeric id (in the event type's URL in the dashboard).
3. Settings → Developer → API keys → create a key.
4. Set `CAL_API_KEY`, `CAL_EVENT_TYPE_ID` and `CAL_BOOKING_URL` (the public
   booking page, used as a fallback).

Cal.com sends the visitor the calendar invite. The agent only books times it
has just offered, so it can't book a made-up slot.

Time zones: visitors see times in their own browser time zone; when that is
unknown, the agent uses `DEFAULT_TIMEZONE` (America/New_York). Team alerts use
`OWNER_TIMEZONE`. The calendar is open 24/7, so the agent only suggests times
between 8 AM and 9 PM in the visitor's zone; the booker on /contact shows
every open time.

## Email (contact form, alerts, briefs)

All email goes out from this service over SMTP: the team alert and the
visitor's confirmation for every contact form, hot-lead, booking and handoff
alerts from the chat, and project briefs.

**Gmail (quickest):** in `.env`, set only

```
SMTP_USER=you@gmail.com
SMTP_PASSWORD=abcd efgh ijkl mnop
```

The password must be a Gmail **App Password**, not your normal password:
turn on 2-Step Verification, then create one at
myaccount.google.com/apppasswords. Mail is sent as `SMTP_USER` and alerts go
to `SMTP_USER`; set `SMTP_FROM` or `OWNER_EMAIL` to change either. Then check
it works:

```bash
python -m app.integrations.mailer              # sends a test email to yourself
python -m app.integrations.mailer you@else.com # or to any address
```

**OCI Email Delivery (for more volume):** verify your domain (SPF and DKIM)
and create an Approved Sender under Developer Services > Email Delivery,
generate SMTP Credentials under your user, then set `SMTP_HOST` (e.g.
`smtp.email.<region>.oci.oraclecloud.com`), `SMTP_USER`, `SMTP_PASSWORD` and
`SMTP_FROM` (the approved sender). Any other SMTP server works the same way.

Each address gets at most one form confirmation an hour, so the form can't
be used to flood someone's inbox.

## Deploy on an OCI VM

On an Always Free Ubuntu or Oracle Linux VM:

```bash
sudo useradd -r -m -d /opt/scalryx_agent scalryx
sudo -u scalryx git clone <this repo> /opt/scalryx_agent    # or copy the folder
cd /opt/scalryx_agent
sudo -u scalryx python3 -m venv .venv
sudo -u scalryx .venv/bin/pip install -r requirements.txt
sudo -u scalryx cp .env.example .env && sudo -u scalryx nano .env
sudo -u scalryx mkdir -p data

sudo cp deploy/scalryx-agent.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now scalryx-agent

# HTTPS in front of it
sudo apt install caddy          # or see caddyserver.com/docs/install for Oracle Linux
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # edit the domain first
sudo systemctl reload caddy
```

Open ports 80 and 443 in **both** the VCN security list (OCI Console →
Networking) and the VM's own firewall (`sudo firewall-cmd --add-service={http,https} --permanent && sudo firewall-cmd --reload`
on Oracle Linux, or the iptables rules on Ubuntu images). Then set
`AGENT_API_URL=https://agent.scalryx.com` and `AGENT_API_KEY` in the
website's hosting environment and redeploy it.

Run one uvicorn worker: rate limits and per-conversation locks are kept in
memory.

## See your leads

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" https://agent.scalryx.com/v1/admin/leads?tier=hot
curl -H "Authorization: Bearer $ADMIN_TOKEN" https://agent.scalryx.com/v1/admin/conversations/<conversation_id>
```

## Model and cost

The agent uses the OpenAI Responses API with `gpt-5.4-mini` at low reasoning
effort: in testing, first words arrive in about 1.5 to 3.5 seconds and a
whole conversation costs a few cents. Change `OPENAI_MODEL` and
`OPENAI_REASONING_EFFORT` in `.env` (any model your key lists works; leave
the effort empty for non-reasoning models such as `gpt-4.1-mini`).

Requests are stateless (`store=False`): OpenAI keeps nothing, the
conversation lives in this database and is replayed each turn. Reasoning
comes back encrypted and is replayed as received, so the model keeps its
reasoning across tool calls. The history is append-only and the
instructions and tools never change mid-conversation, which keeps OpenAI's
prompt cache warm (`prompt_cache_key` groups every chat on one cache).

If the model ever echoes a tool result as text (it happens rarely),
`EchoGuard` in `app/agent.py` drops it before the visitor sees it, and the
stored message is cleaned too.
