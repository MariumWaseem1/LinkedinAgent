import os
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

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


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


# ─── Endpoints ───────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    profile_text: str


class AgentRequest(BaseModel):
    profile_text: str
    audience_label: str
    audience_description: str


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
        doc.close()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse PDF: {e}")
    text = text.strip()
    if len(text) < 100:
        raise HTTPException(status_code=400, detail="PDF too short or not a LinkedIn export.")
    return {"text": text, "pages": doc.page_count if hasattr(doc, "page_count") else 1}


@app.post("/analyze-only")
async def analyze_only(req: AnalyzeRequest):
    system = (
        "You are a sharp LinkedIn strategist. Analyze this profile and return JSON:\n"
        "{\n"
        '  "name": "",\n'
        '  "currentRole": "",\n'
        '  "industry": "",\n'
        '  "profileScore": 0,\n'
        '  "scoreBreakdown": { "headline": 0, "about": 0, "experience": 0, "skills": 0, "completeness": 0 },\n'
        '  "topStrengths": ["", "", ""],\n'
        '  "keyGaps": ["", "", ""],\n'
        '  "firstImpression": "",\n'
        '  "competitiveEdge": "",\n'
        '  "hiddenOpportunities": ["", "", ""],\n'
        '  "suggestedAudiences": [\n'
        '    { "label": "", "description": "", "icon": "", "whyThisAudience": "" }\n'
        '  ]\n'
        "}\n"
        "Return only valid JSON. No markdown. Be brutally honest with scores."
    )
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": f"Profile text:\n\n{req.profile_text[:8000]}"}],
        )
        raw = msg.content[0].text.strip()
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

        research_log: list[dict] = []

        # Phase 1
        yield sse({"stage": "profile", "message": "Reading your profile. Give me a sec."})
        await asyncio.sleep(0.1)

        profile_summary = req.profile_text[:6000]

        # Phase 2 — agentic loop
        system_research = (
            "You are REV3, a sharp AI LinkedIn strategist with a London attitude. "
            "You have access to tools for searching live news, web content, Google Trends, "
            "Reddit and competitor analysis. You MUST call ALL FIVE tools at least once "
            "to build a comprehensive research base. Be thorough. Search the actual industry "
            "of this person, their role keywords, and topics relevant to their target audience. "
            "Use first-person when describing findings."
        )

        user_research_msg = (
            f"Research for this LinkedIn profile:\n\n{profile_summary}\n\n"
            f"Target audience: {req.audience_label} — {req.audience_description}\n\n"
            "Use ALL five tools to gather:\n"
            "1. Latest industry news (search_news)\n"
            "2. Thought leaders and competitor content (search_web + analyze_competitor_content)\n"
            "3. Trending topics UK (get_trending_topics)\n"
            "4. Audience pain points (search_reddit)\n"
            "Gather as much real, current data as possible."
        )

        messages = [{"role": "user", "content": user_research_msg}]
        tool_results_for_synthesis: list[dict] = []

        max_iterations = 15
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=system_research,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
            except Exception as e:
                yield sse({"stage": "error", "message": f"Agent error: {e}"})
                return

            # Collect assistant message content
            assistant_content = []
            for block in response.content:
                if hasattr(block, "text"):
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    tool_name = block.name
                    tool_input = block.input

                    msg_text = TOOL_MESSAGES.get(tool_name, f"Running {tool_name}...")
                    source_label = TOOL_SOURCE_LABELS.get(tool_name, tool_name)
                    yield sse({
                        "stage": "research",
                        "tool": tool_name,
                        "source": source_label,
                        "message": msg_text,
                    })
                    await asyncio.sleep(0.05)

                    try:
                        result_str = await asyncio.get_event_loop().run_in_executor(
                            None, dispatch_tool, tool_name, tool_input
                        )
                        result_data = json.loads(result_str)
                        log_entry = {
                            "tool": tool_name,
                            "query": str(tool_input),
                            "insight": (
                                result_data[0].get("title", str(result_data)[:100])
                                if isinstance(result_data, list) and result_data
                                else str(result_data)[:200]
                            ),
                            "status": "success",
                        }
                    except Exception as e:
                        result_str = json.dumps({"error": str(e)})
                        log_entry = {
                            "tool": tool_name,
                            "query": str(tool_input),
                            "insight": f"Tool failed: {e}",
                            "status": "failed",
                        }

                    research_log.append(log_entry)
                    tool_results_for_synthesis.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "result": result_str,
                    })

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

                messages.append({"role": "user", "content": tool_results})
            else:
                break

        # Phase 3 — synthesis
        yield sse({"stage": "synthesis", "message": "Connecting the dots. Nearly there."})
        await asyncio.sleep(0.1)

        research_dump = json.dumps(tool_results_for_synthesis, indent=2)[:12000]

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
      {{"topic": "", "source": "", "why_it_matters": ""}}
    ],
    "audiencePainPoints": [
      {{"pain": "", "source": "", "how_to_address": ""}}
    ],
    "competitorGaps": [
      {{"gap": "", "opportunity": "", "example_angle": ""}}
    ],
    "newsHooks": [
      {{"headline": "", "relevance": "", "post_angle": ""}}
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
- sevenDayPlan MUST have exactly 7 entries (Monday through Sunday)
- Every sevenDayPlan entry MUST reference actual news or trends from the research data
- profileScore must be 0-100, brutally honest
- scoreBreakdown values must sum to profileScore (each 0-20)
- contentPillars must have exactly 3 items
- profileTweaks must have exactly 4 items
- sixMonthMilestones must have exactly 3 items
- agentResearchLog will be filled in by the system, leave it as empty array []
- All text must use London voice: direct, cheeky, first-person from agent perspective
- Provide real, specific advice based on the actual research data gathered"""

        try:
            synth_msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                messages=[{"role": "user", "content": synthesis_prompt}],
            )
            raw = synth_msg.content[0].text.strip()
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else raw
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            result = json.loads(raw)
        except Exception as e:
            yield sse({"stage": "error", "message": f"Synthesis failed: {e}"})
            return

        result["agentResearchLog"] = research_log

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
