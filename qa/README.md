# QA Service

The support team's QA tools: weekly QA screening assignments and per-agent
metrics, pulled from NICE CXone.

It is a **standalone service**, separate from the email/chat bot in the repo
root:

- It runs as its own Render service, `tns-qa-service`, with `rootDir: qa`.
- It has its own key, `QA_API_KEY`. The bot's `BOT_API_KEY` does not work
  here, and this key does not work on the bot.
- It doesn't use Claude or any of the bot's code. Suspending, pausing or
  redeploying the bot has no effect on it.

## Endpoints

Every endpoint except `/health` needs one of two kinds of sign-in:

- **Microsoft sign-in (used by the QA web page).** The page sends the
  signed-in person's Microsoft ID token as `Authorization: Bearer <token>`.
  The service checks it was issued by the company's Microsoft tenant
  (`MS_TENANT_ID`) for the QA page's app (`MS_CLIENT_ID`) and hasn't expired.
  No secret is stored in the page.
- **`X-API-Key: <QA_API_KEY>` (scripts and manual testing only).** Never
  put this key in the web page, because anything in a public page can be
  read by anyone.

If neither is configured, every request gets a 503 ("fails closed").

| Method | Path | What it does |
|---|---|---|
| POST | `/api/assignments/generate` | Builds QA assignments for evaluator–agent pairs |
| GET | `/api/assignments/debug-contacts` | Sample of raw CXone contact fields |
| GET | `/api/metrics/agents` | Active CXone agents for the agent picker |
| GET | `/api/metrics/productivity?startDate=&endDate=` | Contacts by channel, handle time, refused contacts |
| GET | `/api/metrics/unavailable?startDate=&endDate=` | Unavailable time by reason |
| GET | `/health` | Liveness check; doesn't call CXone |

## Run locally

```bash
cd qa
pip install -r requirements.txt
cp .env.example .env   # fill in QA_API_KEY and the CXone keys
python app.py          # http://localhost:5001
python test_qa.py      # tests; no CXone access needed
```

## Deploy on Render

1. Create the service. If the bot was set up from `render.yaml` (a Render
   Blueprint), sync the Blueprint and the `tns-qa-service` entry is created.
   Otherwise create it by hand: **New → Web Service**, choose this repo, and
   set Root Directory to `qa`. Use the build and start commands from
   `render.yaml`.
2. Set `MS_TENANT_ID` and `MS_CLIENT_ID` to the same values as `tenant` and
   `clientId` in the web page's `src/authConfig.js`. Set the CXone access key
   ID, secret and tenant ID. `QA_API_KEY` is optional and only needed for
   scripts.
3. In the QA web page, point the API address at the new service URL. The
   page sends the Microsoft sign-in automatically.

Use the paid always-on instance for this service. On the free tier it goes to
sleep after 15 minutes idle and wakes up slowly.
