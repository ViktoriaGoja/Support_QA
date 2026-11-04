# CXone Claude Bot — Chat & Email

A Python bot that answers customer **chats and emails** in NICE inContact (CXone) using Claude AI, grounded in your website content. Works via **Studio scripts** — no Virtual Agent Hub needed.

---

## How It Works

```
Customer sends a chat or email
        ↓
CXone Studio script calls your bot server (REST API action)
        ↓
Claude reads your knowledge base + the message
        ↓
Can answer?  →  Studio sends the reply to the customer
Can't answer? →  Studio routes to a human agent
```

## QA tools live in `qa/` (separate service)

The QA assignments generator and agent metrics are **not** part of this bot.
They run as their own Render service (`tns-qa-service`) from the `qa/`
folder, with their own key (`QA_API_KEY`) and their own CXone credentials.
Suspending or redeploying the bot never affects them. See `qa/README.md`.

## Turning auto-replies on/off

Auto-replies are **off** unless the `AUTO_REPLY_ENABLED` environment variable
is `true`. While off, `/api/email` and `/api/chat` escalate every contact to a
human agent without calling Claude. Emails are still logged to the Google
Sheet and the dashboard as "Auto-reply disabled". The dashboard and the QA
tools are unaffected. `GET /health` reports the current setting as
`auto_reply`.

To turn auto-replies back on, set `AUTO_REPLY_ENABLED=true` in the Render
dashboard. Render redeploys the service when you save.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY and COMPANY_NAME

# 3. Build your knowledge base (scrape your website)
python scrape_website.py https://www.thenextstreet.com

# 4. Test in your terminal
python app.py --test

# 5. Run the server
python app.py
```

## Getting an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an account or sign in
3. Go to **API Keys** → create a new key
4. Paste it into `.env` as `ANTHROPIC_API_KEY`
5. Add credits under **Billing** (~$0.003 per chat, ~$0.006 per email)

## Setting Up in CXone Studio

Since you have ACD + Studio, you'll create two scripts — one for chat, one for email. Each script uses the **REST API** action to call your bot.

### Chat Script
See `studio-scripts/CHAT_BOT_SCRIPT.txt` for step-by-step instructions.

Key flow: **BEGIN → SNIPPET (build request) → REST API (call bot) → IF (escalate?) → REPLY or REQAGENT**

### Email Script
See `studio-scripts/EMAIL_BOT_SCRIPT.txt` for step-by-step instructions.

Key flow: **BEGIN → SNIPPET (extract email) → REST API (call bot) → IF (escalate?) → REPLY or REQAGENT**

### What You Need in Studio
- **REST API action** — calls `POST https://your-server.com/api/chat` or `/api/email`
- **SNIPPET action** — builds the JSON request and parses the response
- **IF action** — checks if the bot wants to escalate
- **REQAGENT action** — routes to a human agent queue when needed
- **REPLY action** (email only) — sends the bot's email reply

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/chat` | POST | Handle a chat message |
| `/api/email` | POST | Handle an inbound email |
| `/health` | GET | Health check |

### Chat Request/Response
```json
// Request
{ "contactId": "abc-123", "message": "What are your hours?" }

// Response
{ "reply": "We're open Mon-Fri 9-6 ET.", "escalate": false, "confidence": 90 }
```

### Email Request/Response
```json
// Request
{
  "contactId": "email-456",
  "from": "customer@example.com",
  "subject": "Question about pricing",
  "body": "Hi, how much does it cost?"
}

// Response
{
  "reply": "Dear Customer,\n\nThank you for reaching out!...",
  "escalate": false,
  "confidence": 90,
  "subject": "Re: Question about pricing"
}
```

## Hosting the Bot

You need a server with a public URL. Options:

- **Azure App Service** or **AWS Elastic Beanstalk** — managed, easy
- **Any VPS** (DigitalOcean, Linode, etc.) — $5-10/mo
- **ngrok** — for testing only (creates a public URL to your laptop)

For production, run with gunicorn:
```bash
gunicorn app:app --bind 0.0.0.0:5000 --workers 4
```

## Project Structure

```
cxone-faq-bot/
├── app.py                  # Flask server (main entry point)
├── claude_engine.py        # Claude-powered chat & email engine
├── faq_engine.py           # Keyword matching fallback (no API needed)
├── cxone_client.py         # CXone API client
├── scrape_website.py       # Scrape your site → knowledge_base.txt
├── knowledge_base.txt      # Your company info (auto-generated)
├── faq_data.json           # FAQ entries (keyword fallback)
├── studio-scripts/
│   ├── CHAT_BOT_SCRIPT.txt # Step-by-step Studio script for chat
│   └── EMAIL_BOT_SCRIPT.txt# Step-by-step Studio script for email
├── test_bot.py             # Automated tests
├── requirements.txt
├── .env.example
└── README.md
```

## Cost Estimate

Claude Sonnet pricing per message (approximate):
- Chat messages: ~$0.003 each
- Email replies: ~$0.006 each (longer responses)
- 1,000 chats/day ≈ $90/month
