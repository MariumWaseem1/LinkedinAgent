import os
import re
import json
import time
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import AsyncGenerator

import fitz
import requests
import httpx
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="REV3 LinkedIn Brand Optimizer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory rate limiting: 10 agent runs/hour per IP
_rate_limit: dict[str, list[float]] = defaultdict(list)

def check_rate_limit(ip: str) -> bool:
    now = time.time()
    window = 3600
    _rate_limit[ip] = [t for t in _rate_limit[ip] if now - t < window]
    if len(_rate_limit[ip]) >= 10:
        return False
    _rate_limit[ip].append(now)
    return True


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "REV3 <noreply@rev3.ai>")

# In-memory reminder store: email -> {name, plan, start_date, days_sent}
_reminders: dict[str, dict] = {}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _append_to_sheet(row: list):
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.info("Google Sheets not configured — skipping write.")
        return
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        gc = gspread.service_account_from_dict(creds_dict)
        gc.open_by_key(GOOGLE_SHEET_ID).sheet1.append_row(
            [str(v) for v in row], value_input_option="USER_ENTERED"
        )
        logger.info("Google Sheets row appended OK.")
    except Exception as e:
        logger.error(f"Google Sheets write failed: {e}")


# ─── Tool implementations ───────────────────────────────────────────────────

def search_news(query: str, industry: str) -> list[dict]:
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": f"{query} {industry}",
                "sortBy": "publishedAt",
                "pageSize": 5,
                "language": "en",
                "apiKey": NEWS_API_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "description": a.get("description", ""),
                "url": a.get("url", ""),
                "publishedAt": a.get("publishedAt", ""),
                "source": a.get("source", {}).get("name", ""),
            }
            for a in articles
        ]
    except Exception as e:
        logger.warning(f"search_news failed: {e}")
        return []


def search_web(query: str) -> list[dict]:
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [
            {
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "url": r.get("url", ""),
            }
            for r in results
        ]
    except Exception as e:
        logger.warning(f"search_web failed: {e}")
        return []


def get_trending_topics(keyword: str, geo: str = "GB") -> list[str]:
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-GB", tz=0)
        pt.build_payload([keyword], cat=0, timeframe="now 7-d", geo=geo)
        rq = pt.related_queries()
        rising = rq.get(keyword, {}).get("rising")
        if rising is not None and not rising.empty:
            return rising["query"].head(5).tolist()
        top = rq.get(keyword, {}).get("top")
        if top is not None and not top.empty:
            return top["query"].head(5).tolist()
        return []
    except Exception as e:
        logger.warning(f"get_trending_topics failed: {e}")
        return []


def search_reddit(subreddit: str, query: str) -> list[dict]:
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/search.json",
            params={"q": query, "sort": "hot", "limit": 5},
            headers={"User-Agent": "REV3-LinkedIn-Optimizer/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        posts = resp.json().get("data", {}).get("children", [])
        return [
            {
                "title": p["data"].get("title", ""),
                "selftext": p["data"].get("selftext", "")[:300],
                "score": p["data"].get("score", 0),
                "num_comments": p["data"].get("num_comments", 0),
                "url": "https://www.reddit.com" + p["data"].get("permalink", ""),
            }
            for p in posts
        ]
    except Exception as e:
        logger.warning(f"search_reddit failed: {e}")
        return []


def analyze_competitor_content(role: str, industry: str) -> list[dict]:
    try:
        results = search_web(f"LinkedIn thought leaders {role} {industry} 2026")
        output = []
        for r in results[:3]:
            output.append(
                {
                    "topic": r.get("title", ""),
                    "engagement_signal": r.get("content", "")[:200],
                    "gap_opportunity": f"Underexplored angle in: {r.get('title', '')}",
                    "url": r.get("url", ""),
                }
            )
        return output
    except Exception as e:
        logger.warning(f"analyze_competitor_content failed: {e}")
        return []


TOOL_DEFINITIONS = [
    {
        "name": "search_news",
        "description": "Search for the latest news articles about a topic in a given industry using NewsAPI.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "industry": {"type": "string", "description": "Industry context"},
            },
            "required": ["query", "industry"],
        },
    },
    {
        "name": "search_web",
        "description": "Search the web using Tavily for detailed information about a topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_trending_topics",
        "description": "Get trending search topics on Google Trends in the UK for a keyword.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Keyword to trend"},
                "geo": {"type": "string", "description": "Country code, default GB"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "search_reddit",
        "description": "Search Reddit for posts in a subreddit about a topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subreddit": {"type": "string", "description": "Subreddit name"},
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["subreddit", "query"],
        },
    },
    {
        "name": "analyze_competitor_content",
        "description": "Analyze competitor LinkedIn content to find gaps and opportunities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Professional role"},
                "industry": {"type": "string", "description": "Industry"},
            },
            "required": ["role", "industry"],
        },
    },
]

TOOL_SOURCE_LABELS = {
    "search_news": "NewsAPI",
    "search_web": "Tavily",
    "get_trending_topics": "Google Trends",
    "search_reddit": "Reddit",
    "analyze_competitor_content": "Web Search",
}

TOOL_MESSAGES = {
    "search_news": "Scanning this week's industry news...",
    "search_web": "Stalking your competitors...",
    "get_trending_topics": "Checking Google Trends UK...",
    "search_reddit": "Finding what your audience is ranting about...",
    "analyze_competitor_content": "Finding the gaps they're missing...",
}


def dispatch_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "search_news":
        result = search_news(tool_input["query"], tool_input["industry"])
    elif tool_name == "search_web":
        result = search_web(tool_input["query"])
    elif tool_name == "get_trending_topics":
        result = get_trending_topics(tool_input["keyword"], tool_input.get("geo", "GB"))
    elif tool_name == "search_reddit":
        result = search_reddit(tool_input["subreddit"], tool_input["query"])
    elif tool_name == "analyze_competitor_content":
        result = analyze_competitor_content(tool_input["role"], tool_input["industry"])
    else:
        result = {"error": f"Unknown tool: {tool_name}"}
    return json.dumps(result)


# ─── Email reminders ─────────────────────────────────────────────────────────

async def _send_day_email(name: str, email: str, day_data: dict, day_num: int):
    if not RESEND_API_KEY:
        logger.info("RESEND_API_KEY not set — skipping email.")
        return
    day_label = day_data.get("day", f"Day {day_num}")
    topic = day_data.get("topic", "")
    hook = day_data.get("hook", "")
    post_type = day_data.get("postType", "")
    trending = day_data.get("trendingAngle", "")
    why = day_data.get("whyThisWorks", "")
    html_body = f"""
<div style="font-family:'Segoe UI',Arial,sans-serif;max-width:560px;margin:0 auto;color:#0F172A;">
  <div style="background:linear-gradient(135deg,#2563EB,#1D4ED8);padding:28px 32px;border-radius:16px 16px 0 0;">
    <div style="font-size:22px;font-weight:700;color:#fff;letter-spacing:-.02em;">REV3 Daily Plan</div>
    <div style="font-size:14px;color:rgba(255,255,255,.75);margin-top:4px;">Day {day_num} of 7 — {day_label}</div>
  </div>
  <div style="background:#fff;padding:28px 32px;border-radius:0 0 16px 16px;border:1px solid #E2E8F0;border-top:none;">
    <p style="font-size:15px;margin-bottom:20px;">Hi {name}, here's your LinkedIn action for today.</p>
    <div style="background:#EFF6FF;border-left:4px solid #2563EB;border-radius:0 10px 10px 0;padding:14px 18px;margin-bottom:20px;">
      <div style="font-size:11px;font-weight:700;color:#2563EB;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">{post_type}</div>
      <div style="font-size:17px;font-weight:700;color:#0F172A;margin-bottom:8px;">{topic}</div>
      <div style="font-size:14px;color:#475569;font-style:italic;">"{hook}"</div>
    </div>
    {"<div style='background:#F0FDF4;border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:13px;color:#166534;'><b>Trending angle:</b> " + trending + "</div>" if trending else ""}
    <div style="font-size:13px;color:#64748B;line-height:1.6;"><b>Why this works:</b> {why}</div>
    <hr style="border:none;border-top:1px solid #E2E8F0;margin:24px 0;"/>
    <p style="font-size:12px;color:#94A3B8;text-align:center;">You're receiving this because you scheduled REV3 daily reminders. {day_num} of 7 done.</p>
  </div>
</div>"""
    try:
        async with httpx.AsyncClient() as c:
            resp = await c.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from": RESEND_FROM_EMAIL,
                    "to": [email],
                    "subject": f"Day {day_num}: {topic} | Your REV3 LinkedIn Plan",
                    "html": html_body,
                },
                timeout=15,
            )
            if resp.status_code >= 400:
                logger.error(f"Resend error {resp.status_code}: {resp.text}")
            else:
                logger.info(f"Day {day_num} email sent to {email}")
    except Exception as e:
        logger.error(f"Email send failed: {e}")


async def _reminder_loop():
    while True:
        await asyncio.sleep(3600)  # check hourly
        now = datetime.utcnow()
        for email, data in list(_reminders.items()):
            try:
                days_elapsed = (now - data["start_date"]).total_seconds() / 86400
                target_sent = min(int(days_elapsed) + 1, len(data["plan"]))
                while data["days_sent"] < target_sent:
                    idx = data["days_sent"]
                    await _send_day_email(data["name"], email, data["plan"][idx], idx + 1)
                    data["days_sent"] += 1
            except Exception as e:
                logger.error(f"Reminder loop error for {email}: {e}")


@app.on_event("startup")
async def start_reminder_scheduler():
    asyncio.create_task(_reminder_loop())


# ─── Endpoints ───────────────────────────────────────────────────────────────

def _strip_dashes(text: str) -> str:
    return text.replace("—", "-").replace("–", "-")


class LeadRequest(BaseModel):
    name: str
    email: str


class AnalyzeRequest(BaseModel):
    profile_text: str


class AgentRequest(BaseModel):
    profile_text: str
    audience_label: str
    audience_description: str
    profile_role: str = ""
    profile_industry: str = ""


class ResultsRequest(BaseModel):
    name: str
    email: str
    profile_name: str
    current_role: str
    industry: str
    profile_score: int
    audience: str
    content_pillars: list[str]
    seven_day_topics: list[str]
    key_gaps: list[str]
    competitive_edge: str


class ReminderRequest(BaseModel):
    name: str
    email: str
    plan: list[dict]


@app.post("/register")
async def register(request: Request, req: LeadRequest):
    name = req.name.strip()
    email = req.email.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required.")
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        raise HTTPException(status_code=400, detail="Invalid email.")
    ip = request.headers.get("x-forwarded-for", request.client.host or "").split(",")[0].strip()
    ua = request.headers.get("user-agent", "")
    device = "mobile" if any(x in ua.lower() for x in ["mobile", "android", "iphone"]) else "desktop"
    _append_to_sheet([datetime.utcnow().isoformat(), name, email, ip, device, "", "", "", "", "", "", "registered"])
    return {"ok": True}


@app.post("/save-results")
async def save_results(req: ResultsRequest):
    _append_to_sheet([
        datetime.utcnow().isoformat(),
        req.name,
        req.email,
        req.profile_name,
        req.current_role,
        req.industry,
        req.profile_score,
        req.audience,
        " | ".join(req.content_pillars),
        " | ".join(req.seven_day_topics),
        " | ".join(req.key_gaps),
        req.competitive_edge,
        "completed",
    ])
    return {"ok": True}


@app.post("/schedule-reminders")
async def schedule_reminders(req: ReminderRequest):
    email = req.email.strip()
    if not req.plan:
        raise HTTPException(status_code=400, detail="No plan provided.")
    _reminders[email] = {
        "name": req.name,
        "plan": req.plan,
        "start_date": datetime.utcnow(),
        "days_sent": 0,
    }
    await _send_day_email(req.name, email, req.plan[0], 1)
    _reminders[email]["days_sent"] = 1
    return {"ok": True, "message": "Day 1 sent! Check your inbox. Days 2-7 will arrive each morning."}


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files accepted.")
    contents = await file.read()
    try:
        doc = fitz.open(stream=contents, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        page_count = doc.page_count
        doc.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse PDF: {e}")
    text = text.strip()
    if len(text) < 100:
        raise HTTPException(status_code=400, detail="PDF too short or not a LinkedIn export.")
    return {"text": text, "pages": page_count}


@app.post("/analyze-only")
async def analyze_only(req: AnalyzeRequest):
    system = (
        "You are a sharp LinkedIn strategist. Analyze this profile and return JSON.\n"
        "Be brutally honest with scores. Keep ALL text fields SHORT - see limits below.\n\n"
        "{\n"
        '  "name": "Full name only",\n'
        '  "currentRole": "Job title only, max 6 words",\n'
        '  "industry": "One or two words e.g. SaaS, Finance",\n'
        '  "profileScore": 0,\n'
        '  "scoreBreakdown": { "headline": 0, "about": 0, "experience": 0, "skills": 0, "completeness": 0 },\n'
        '  "topStrengths": ["max 8 words each", "", ""],\n'
        '  "keyGaps": ["max 8 words each", "", ""],\n'
        '  "firstImpression": "One sentence, max 15 words. What a stranger thinks in 8 seconds.",\n'
        '  "competitiveEdge": "One sentence, max 15 words. The one thing that sets them apart.",\n'
        '  "hiddenOpportunities": ["max 10 words each", "", ""],\n'
        '  "suggestedAudiences": [\n'
        '    { "label": "2-4 words", "description": "max 10 words", "icon": "", "whyThisAudience": "max 12 words" }\n'
        '  ]\n'
        "}\n"
        "Return only valid JSON. No markdown. No em dashes."
    )
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": f"Profile text:\n\n{req.profile_text[:4000]}"}],
        )
        raw = _strip_dashes(msg.content[0].text.strip())
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/run-agent")
async def run_agent(request: Request, req: AgentRequest):
    client_ip = request.client.host
    if not check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit: 10 agent runs per hour.")

    async def event_stream() -> AsyncGenerator[str, None]:
        def sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        # Phase 1
        yield sse({"stage": "profile", "message": "Reading your profile. Give me a sec."})
        await asyncio.sleep(0.1)

        profile_summary = req.profile_text[:6000]
        role     = req.profile_role or "professional"
        industry = req.profile_industry or "business"

        # Phase 2 — fire tools in parallel with a hard 8s timeout each
        tool_configs = [
            ("search_news",               {"query": f"{role} {industry}", "industry": industry}),
            ("search_web",                {"query": f"LinkedIn thought leaders {role} {industry} 2026"}),
            ("search_reddit",             {"subreddit": "linkedin", "query": f"{role} {industry} tips"}),
            ("analyze_competitor_content", {"role": role, "industry": industry}),
        ]

        for tool_name, _ in tool_configs:
            yield sse({
                "stage": "research",
                "tool": tool_name,
                "source": TOOL_SOURCE_LABELS.get(tool_name, tool_name),
                "message": TOOL_MESSAGES.get(tool_name, f"Running {tool_name}..."),
            })
            await asyncio.sleep(0.02)

        loop = asyncio.get_event_loop()

        async def run_tool_with_timeout(name, inp):
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(None, dispatch_tool, name, inp),
                    timeout=8.0,
                )
            except asyncio.TimeoutError:
                return json.dumps({"error": f"{name} timed out"})

        raw_results = await asyncio.gather(
            *[run_tool_with_timeout(name, inp) for name, inp in tool_configs],
            return_exceptions=True,
        )

        tool_results_for_synthesis: list[dict] = []
        for (tool_name, tool_input), raw in zip(tool_configs, raw_results):
            result_str = json.dumps({"error": str(raw)}) if isinstance(raw, Exception) else raw
            tool_results_for_synthesis.append({"tool": tool_name, "input": tool_input, "result": result_str})

        # Phase 3 — synthesis
        yield sse({"stage": "synthesis", "message": "Connecting the dots. Nearly there."})
        await asyncio.sleep(0.1)

        research_dump = json.dumps(tool_results_for_synthesis, indent=2)[:6000]

        synthesis_prompt = f"""You are REV3, an elite LinkedIn brand strategist with a London attitude.

Based on the profile and research data below, produce a comprehensive strategy.

PROFILE:
{profile_summary}

TARGET AUDIENCE: {req.audience_label} — {req.audience_description}

RESEARCH DATA (live data gathered by tools):
{research_dump}

Return ONLY valid JSON (no markdown, no commentary) in exactly this structure:
{{
  "profileIntelligence": {{
    "name": "",
    "currentRole": "",
    "industry": "",
    "profileScore": 0,
    "scoreBreakdown": {{"headline": 0, "about": 0, "experience": 0, "skills": 0, "completeness": 0}},
    "topStrengths": ["", "", ""],
    "keyGaps": ["", "", ""],
    "firstImpression": "",
    "competitiveEdge": "",
    "hiddenOpportunities": ["", "", ""]
  }},
  "marketIntelligence": {{
    "trendingTopicsThisWeek": [
      {{"topic": "", "source": "", "why_it_matters": "", "url": ""}}
    ],
    "audiencePainPoints": [
      {{"pain": "", "source": "", "how_to_address": "", "url": ""}}
    ],
    "competitorGaps": [
      {{"gap": "", "opportunity": "", "example_angle": "", "url": ""}}
    ],
    "newsHooks": [
      {{"headline": "", "relevance": "", "post_angle": "", "url": ""}}
    ]
  }},
  "strategy": {{
    "audienceSummary": "",
    "postingVoice": "",
    "weeklyRhythm": "",
    "contentPillars": [
      {{"pillar": "", "why": "", "exampleTopic": "", "contentFormat": ""}}
    ],
    "dos": ["", "", "", "", ""],
    "donts": ["", "", "", "", ""],
    "profileTweaks": [
      {{"section": "", "currentIssue": "", "suggestedFix": "", "impact": ""}}
    ],
    "sixMonthMilestones": [
      {{"month": "", "goal": "", "howToMeasure": ""}}
    ],
    "engagementTactics": ["", "", ""]
  }},
  "sevenDayPlan": [
    {{
      "day": "Monday",
      "postType": "",
      "topic": "",
      "hook": "",
      "whyThisWorks": "",
      "newsHook": "",
      "trendingAngle": ""
    }}
  ],
  "agentResearchLog": []
}}

CRITICAL REQUIREMENTS:
- Be CONCISE. Keep all text fields short (under 15 words per field). The JSON must fit within 8000 tokens.
- sevenDayPlan MUST have exactly 7 entries (Monday through Sunday)
- Every sevenDayPlan entry MUST reference actual news or trends from the research data
- profileScore must be 0-100, brutally honest
- scoreBreakdown values must sum to profileScore (each 0-20)
- contentPillars must have exactly 3 items
- profileTweaks must have exactly 4 items
- sixMonthMilestones must have exactly 3 items
- agentResearchLog will be filled in by the system, leave it as empty array []
- All text must use London voice: direct, cheeky, first-person from agent perspective
- Provide real, specific advice based on the actual research data gathered
- NEVER use em dashes (the character -) in any text. Use commas, colons, or plain hyphens instead.
- For every item in trendingTopicsThisWeek, audiencePainPoints, competitorGaps and newsHooks you MUST copy the exact url from the research data. If no url exists leave it as empty string."""

        try:
            synth_msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                messages=[{"role": "user", "content": synthesis_prompt}],
            )
            raw = _strip_dashes(synth_msg.content[0].text.strip())
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            # If truncated mid-JSON, try to salvage by closing open structures
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                # Count open braces/brackets and close them
                open_braces   = raw.count('{') - raw.count('}')
                open_brackets = raw.count('[') - raw.count(']')
                # Remove trailing partial string/value
                raw = raw.rstrip().rstrip(',')
                raw += ']' * open_brackets + '}' * open_braces
                result = json.loads(raw)
        except Exception as e:
            yield sse({"stage": "error", "message": f"Synthesis failed: {e}"})
            return

        result["agentResearchLog"] = []

        yield sse({
            "stage": "complete",
            "message": "Done. Here's everything I found.",
            "data": result,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
