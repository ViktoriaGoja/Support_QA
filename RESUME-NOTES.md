# CXone AI Chat Bot — Project Resume Notes
**Last updated: April 5, 2026**

## Overview
Building a Claude AI-powered chatbot for NICE CXone contact center at The Next Street driving school. The bot answers customer chats using Claude API, grounded in website content. Does NOT use Virtual Agent Hub — uses Studio scripts with REST API calls to a Flask webhook server.

## Architecture
- **Flask server** (`app.py`) on localhost:5000 with `/api/chat` endpoint
- **Cloudflare Tunnel** (`cloudflared`) exposes localhost:5000 to public internet (URL changes each restart)
- **CXone Studio script** ("Chat Bot Test") captures customer messages via Askcaller, sends to Flask server via Snippet + GetRESTProxy/MakeRestRequest, sends bot reply via Textmsg
- **Claude API** (model: claude-sonnet-4-20250514) generates responses grounded in knowledge_base.txt

## Project Files
All in: `cxone-faq-bot/` folder
- `app.py` — Flask server, /api/chat and /api/email endpoints
- `claude_engine.py` — Claude API integration for generating answers
- `faq_engine.py` — Keyword fallback engine
- `faq_data.json` — FAQ data
- `knowledge_base.txt` — Company knowledge for Claude grounding
- `cxone_client.py` — CXone API client (for future use)
- `scrape_website.py` — Website scraper to build knowledge base
- `.env` — API keys and config (ANTHROPIC_API_KEY, BOT_API_KEY=tns-bot-secret-2024, etc.)
- `requirements.txt` — Python dependencies
- `studio-scripts/` — Studio script documentation

## CXone Configuration
- **ACD Skill**: "Chat Bot Test" (ID: 35628172)
- **Original POC**: "Chat Bot Test" (ID: 56101067) - RATE LIMITED, don't use
- **Active POC**: "Chat Bot Test 2" (ID: 56101068)
  - GUID: 054e810e-12bf-454b-9795-a878279082dd
  - Business Unit: 4599573
- **Chat test URL**: `https://home-c35.nice-incontact.com/inContact/ChatClient/ChatClientPatron.aspx?poc=054e810e-12bf-454b-9795-a878279082dd&bu=4599573`
- **Chat interface**: V1 ASPX (legacy)
- **Script**: "Chat Bot Test" in CXone Studio
- **CXone instance**: na1.nice-incontact.com

## CXone Studio Script — Current State (8 actions)

### Flow
```
Begin → Reqagent (Default skill) → [dead end, just requests agent routing]

Onanswer (standalone event handler, fires when agent accepts) →
  Textmsg (welcome: "Hi there! Welcome to The Next Street. How can I help you today?") →
    Askcaller →
      Snippet (22 lines, calls bot API) →
        If (botReply = "") →
          True: Askcaller (loop back, wait for next message)
          False: Textmsg (sends botReply to customer) → Askcaller (loop back)
```

### Onanswer Behavior (IMPORTANT)
- Onanswer is an EVENT HANDLER — sits standalone on canvas, NOT connected inline from Reqagent
- Fires automatically when an agent accepts the chat
- All script execution before agent answer is TERMINATED; script resumes from Onanswer
- For digital/V2 chat, use ONASSIGNMENT instead of Onanswer

### Snippet Code (22 lines)
```
IF ContactTextMessage = ""
{
    ASSIGN botReply = ""
}
ELSE
{
    IF ContactTextMessage = lastProcessed
    {
        ASSIGN botReply = ""
    }
    ELSE
    {
        ASSIGN lastProcessed = ContactTextMessage
        ASSIGN reqBody = "{{""contactId"":""{ContactId}"",""message"":""{ContactTextMessage}""}}"
        ASSIGN proxy = GetRESTProxy()
        ASSIGN url = "<CLOUDFLARE_TUNNEL_URL>/api/chat?api_key=tns-bot-secret-2024"
        DYNAMIC result
        ASSIGN result = proxy.MakeRestRequest(url, reqBody, 1, "POST")
        ASSIGN botReply = "{result.reply}"
        ASSIGN shouldEscalate = "{result.escalate}"
    }
}
```

**IMPORTANT**: The URL in the Snippet must be updated every time Cloudflare tunnel restarts (quick tunnels get new random URLs).

### Textmsg Properties
- Welcome Textmsg (action 9): Target = "Caller from system", Text = "Hi there! Welcome to The Next Street. How can I help you today?"
- Reply Textmsg (action 6): Target = "Caller from system", Text = "{botReply}"

## CXone Studio Syntax Rules (CRITICAL)
- **No ELSE IF** — use nested `ELSE { IF ... }` blocks
- **No != operator** — only `=` for comparison
- **No AND/OR operators** — use nested IF blocks
- **String escaping**: Use double-double-quotes inside strings: `""key"":""value""`
- **Variables**: Use `{VariableName}` for substitution
- **GetRESTProxy()**: Returns proxy object for making HTTP calls
- **MakeRestRequest(url, body, contentType, method)**: contentType 1 = JSON

## Running the Bot Locally

### Start bot server
```bash
cd "C:\Users\ViktoriaGoja\OneDrive - The Next Street\Documents\Claude\cxone-faq-bot"
pip install -r requirements.txt
python app.py
```
Server runs on http://localhost:5000

### Start Cloudflare tunnel
```bash
cloudflared tunnel --url http://localhost:5000
```
Note the random URL it generates (e.g., https://xxx-xxx-xxx-xxx.trycloudflare.com)
Then update the `url` variable in the CXone Studio Snippet action with the new URL.

## Current Status (Updated April 5, 2026)

### What works
- Flask server runs correctly, Claude API integration works
- Health check endpoint works through Cloudflare tunnel
- CXone chat connects when agent accepts, customer can type (Onanswer fix)
- Welcome Textmsg configured with greeting text
- Script flow is logically correct
- knowledge_base.txt is comprehensive (600 lines, all MA + CT products)
- Render.com deployment files ready (render.yaml, .gitignore)

### Resolved blocker: Root cause identified
The Askcaller action does NOT work for bot flows on V1 ASPX chat. Root cause:
- Regular `Askcaller` was designed for voice IVR, not chat bots
- V1 ASPX chat doesn't properly establish sessions for automated Askcaller
- `contacttextmessage` is always empty, "Not a valid chat session" error
- This is a fundamental limitation of ACD Chat + regular Askcaller

### Fix: Two paths forward (scripts written, ready to implement)

**Path A — Digital Chat (BEST, requires Digital Experience DX)**
- Use CXone Digital chat channel + Digital Studio script
- Uses Askcaller Digital + Textmsg Digital (purpose-built for chat bots)
- Uses OnAssignment instead of OnAnswer
- Script guide: `studio-scripts/DIGITAL_CHAT_BOT_SCRIPT.txt`
- STATUS: Check with NICE account manager if DX is included in plan

**Path B — V2 HTML5 + Wait Loop (workaround, no DX needed)**
- Switch Chat Profile from V1 ASPX to V2 HTML5
- Replace Askcaller with Wait(3) + ContactTextMessage polling loop
- Script guide: `studio-scripts/V2_CHAT_BOT_SCRIPT.txt`
- STATUS: Ready to try immediately — no CXone plan changes needed

### Next steps (in priority order)
1. **Deploy to Render.com** — push code to GitHub, connect to Render for permanent URL
2. **Try Path B first** — switch to V2 HTML5, create new script per V2_CHAT_BOT_SCRIPT.txt
3. **If ContactTextMessage is still empty on V2** — need DX (Path A)
4. **Ask NICE account manager** if Digital Experience can be enabled

## Issues Solved So Far
- REST API Action never worked → switched to GetRESTProxy() in Snippet
- Headers with hyphens broke script save → moved API key to query param
- ELSE IF not supported → nested IF/ELSE
- != and AND operators not supported → use only = and nested IF/ELSE
- Rapid looping caused rate limiting → added Wait action + message tracking
- Rate-limited POC → created new POC "Chat Bot Test 2"
- "Waiting" status (customer can't type) → added Reqagent for agent acceptance
- Empty welcome message → moved Textmsg after Onanswer, configured text
- "Not a valid chat session" with Askcaller → ROOT CAUSE: V1 ASPX + regular Askcaller is incompatible with bot flows. Fix: switch to Digital chat (Path A) or V2 HTML5 + Wait loop (Path B)
- .env not loading API key → system env var was overriding; fixed with load_dotenv(override=True)
- anthropic SDK warning on Python 3.14 → upgraded from 0.49 to 0.89+

## Reference: Working "Chat IB Prod" Script (24 actions)
The production chat script that works for human agents:
```
Main flow: Begin → Snippet (Set Skill Vars) → If (Test Chat?) →
  False: Hours → (Emergency/Closed/Holiday branches) → Wait → Countagents →
    Getqueue → Textmsg (Thank you) → Reqagent (Skill: {ChatSkill}) →
      Textmsg (Customer info) → [waits for agent]

Event handlers (standalone, not connected to main flow):
  Onanswer → Setlabel (Agent Name) → Setlabel (Contact Name) →
    Textmsg (Agent Joined) → Textmsg (How can I assist?)
  Onrelease → Wait → Stop
```

## Deployment — Render.com Setup
1. Push code to GitHub (private repo)
2. Sign up at render.com, connect GitHub repo
3. Render auto-detects render.yaml config
4. Set secret env vars in Render dashboard: ANTHROPIC_API_KEY, BOT_API_KEY
5. Deploy — get permanent URL like https://cxone-faq-bot.onrender.com
6. Update Studio Snippet URL with the Render URL (one time, never changes)
Note: Render free tier spins down after 15 min idle, first request takes ~30s.
Paid plan ($7/mo) keeps it always on.

## Future Work (After Bot Works)
- Set up email bot script (app.py already has /api/email endpoint)
- Prices should NOT be quoted; direct customers to enrollment page
- Scrape website content periodically to keep knowledge_base.txt up to date
- Add conversation history (multi-turn context) to Claude calls
- Analytics: track common questions, escalation rate, response quality

## User Info
- Name: Vicky (viktoria.goja@thenextstreet.com)
- Company: The Next Street (driving school)
- CXone instance: na1.nice-incontact.com
- Has ACD + Studio, does NOT have Virtual Agent Hub
