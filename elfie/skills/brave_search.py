"""
Skill: Brave web search — much better results than the built-in fallback.

Reads BRAVE_API_KEY from the environment (free tier: 2,000 queries/month at
https://api.search.brave.com). When the key is missing the tool explains the
setup out loud instead of failing.

This file doubles as the reference example of the skill contract:
@function_tool async functions + module-level TOOLS list.
"""
import logging
import os

import httpx
from livekit.agents import RunContext, function_tool

logger = logging.getLogger("elfie.skills.brave_search")


@function_tool
async def brave_search(context: RunContext, query: str) -> str:
    """
    Searches the web with Brave Search — the PREFERRED search tool, with much
    better results than search_web. Call for news, facts, weather, prices,
    or anything time-sensitive the user asks about.
    """
    api_key = os.getenv("BRAVE_API_KEY", "")
    if not api_key:
        return "Web search isn't fully set up — add a BRAVE_API_KEY to the .env file."
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 4},
                headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            )
            r.raise_for_status()
            results = r.json().get("web", {}).get("results", [])
    except Exception as e:
        logger.warning(f"Brave search failed: {e}")
        return "I couldn't reach the search service right now."
    if not results:
        return "No results found — try rephrasing."
    lines = ["Brave Search results (cite these as the source; offer to open a URL):"]
    for item in results[:4]:
        desc = (item.get("description") or "").replace("<strong>", "").replace("</strong>", "")
        lines.append(f"- {item.get('title', '')}: {desc[:160]} (source: {item.get('url', '')})")
    return "\n".join(lines)


TOOLS = [brave_search]
