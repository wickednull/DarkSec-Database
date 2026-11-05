"""NewsAPI integration for real-time global news aggregation.

Fetches headlines from 70+ news sources worldwide with focus on:
- War/conflict zones
- Natural disasters
- Security incidents
- Emergency situations

Free tier: 100 requests/day with API key
"""
from __future__ import annotations

import time

import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

NEWSAPI_BASE_URL = "https://newsapi.org/v2"

# Conflict/disaster results are cached for an hour so automatic syncing and
# page refreshes do not burn the free-tier request budget.
_cache: dict = {"conflict": None, "disaster": None, "fetched_at": 0.0}
TTL_SECONDS = 3600


def _cache_valid() -> bool:
    return time.monotonic() - _cache["fetched_at"] <= TTL_SECONDS


def fetch_news(
    query: str = "war OR conflict OR disaster OR emergency OR attack",
    category: str = "general",
    language: str = "en",
    page_size: int = 20,
    api_key: str = ""
) -> list[dict]:
    """
    Fetch news headlines from NewsAPI.
    
    Args:
        query: Search query (supports boolean operators)
        category: News category (general, world, business, technology, entertainment, sports, science, health)
        language: Language code (en, es, fr, de, etc.)
        page_size: Number of results (max 100)
        api_key: NewsAPI key (free tier available)
    
    Returns:
        List of news articles
    """
    if not api_key:
        # Try to get from settings
        try:
            from .config import config
            api_key = config.get("newsapi_key", "")
        except:
            api_key = ""
    
    if not api_key:
        return []
    
    try:
        # Calculate date range (last 7 days)
        from_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        params = {
            "q": query,
            "from": from_date,
            "to": to_date,
            "language": language,
            "pageSize": min(page_size, 100),
            "sortBy": "relevancy",
            "apiKey": api_key,
        }
        
        resp = requests.get(
            f"{NEWSAPI_BASE_URL}/everything",
            params=params,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("status") != "ok":
            return []
        
        articles = []
        for article in data.get("articles", []):
            # Calculate relevance score based on keywords
            title = article.get("title", "").lower()
            description = article.get("description", "").lower()
            content = f"{title} {description}"
            
            relevance = calculate_news_relevance(content)
            
            articles.append({
                "title": article.get("title", "Untitled"),
                "description": article.get("description", ""),
                "url": article.get("url", ""),
                "source": article.get("source", {}).get("name", "Unknown"),
                "published_at": article.get("publishedAt", ""),
                "relevance_score": relevance,
                "severity": get_news_severity(relevance),
                "summary": article.get("description", "")[:400],
                "image_url": article.get("urlToImage", ""),
                "author": article.get("author", ""),
            })
        
        # Sort by relevance
        articles.sort(key=lambda x: x["relevance_score"], reverse=True)
        return articles
        
    except Exception as e:
        print(f"NewsAPI fetch error: {e}")
        return []


def calculate_news_relevance(content: str) -> int:
    """
    Calculate relevance score 0-100 based on keywords.
    Higher score = more critical for survival monitoring.
    """
    score = 0
    
    # Critical keywords (high weight)
    critical_keywords = [
        "war", "attack", "bombing", "explosion", "massacre", "genocide",
        "nuclear", "chemical", "biological", "radiation", "contamination",
        "earthquake", "tsunami", "hurricane", "tornado", "flood", "wildfire",
        "pandemic", "epidemic", "outbreak", "plague",
        "terrorism", "shooting", "massacre", "hostage",
    ]
    
    # High priority keywords
    high_keywords = [
        "conflict", "military", "troops", "invasion", "strike",
        "disaster", "emergency", "evacuation", "shelter", "refugee",
        "casualties", "deaths", "injuries", "missing",
        "radiation", "leak", "spill", "toxic",
        "warning", "alert", "danger", "threat",
    ]
    
    # Medium priority keywords
    medium_keywords = [
        "crisis", "tension", "unrest", "protest", "riot",
        "supply", "shortage", "rationing", "famine",
        "infrastructure", "power outage", "blackout",
        "quarantine", "lockdown", "curfew",
    ]
    
    content_lower = content.lower()
    
    for keyword in critical_keywords:
        if keyword in content_lower:
            score += 15
    
    for keyword in high_keywords:
        if keyword in content_lower:
            score += 8
    
    for keyword in medium_keywords:
        if keyword in content_lower:
            score += 3
    
    return min(score, 100)


def get_news_severity(score: int) -> str:
    """Map relevance score to severity level."""
    if score >= 70:
        return "CRITICAL"
    elif score >= 50:
        return "SEVERE"
    elif score >= 30:
        return "ALERT"
    else:
        return "INFO"


def fetch_conflict_news(api_key: str = "", page_size: int = 10) -> list[dict]:
    """Fetch war/conflict specific news (cached for one hour)."""
    if _cache["conflict"] is not None and _cache_valid():
        return _cache["conflict"]
    result = fetch_news(
        query="war OR conflict OR military OR attack OR bombing OR invasion",
        api_key=api_key,
        page_size=page_size
    )
    if result:
        _cache["conflict"] = result
        _cache["fetched_at"] = time.monotonic()
    return result or (_cache["conflict"] or [])


def fetch_disaster_news(api_key: str = "", page_size: int = 10) -> list[dict]:
    """Fetch natural disaster news (cached for one hour)."""
    if _cache["disaster"] is not None and _cache_valid():
        return _cache["disaster"]
    result = fetch_news(
        query="earthquake OR tsunami OR hurricane OR tornado OR flood OR wildfire OR volcano",
        api_key=api_key,
        page_size=page_size
    )
    if result:
        _cache["disaster"] = result
        _cache["fetched_at"] = time.monotonic()
    return result or (_cache["disaster"] or [])


def cached_conflict_news() -> list[dict]:
    return _cache["conflict"] or []


def cached_disaster_news() -> list[dict]:
    return _cache["disaster"] or []


def get_news_summary(api_key: str = "") -> dict:
    """Get summary statistics for news dashboard."""
    articles = fetch_news(api_key=api_key, page_size=30)
    
    if not articles:
        return {
            "total": 0,
            "critical": 0,
            "severe": 0,
            "sources": [],
        }
    
    critical = sum(1 for a in articles if a["severity"] == "CRITICAL")
    severe = sum(1 for a in articles if a["severity"] == "SEVERE")
    
    # Get unique sources
    sources = list(set(a["source"] for a in articles if a.get("source")))
    
    return {
        "total": len(articles),
        "critical": critical,
        "severe": severe,
        "sources": sources[:10],  # Top 10 sources
        "updated": datetime.now(timezone.utc).isoformat(),
    }
