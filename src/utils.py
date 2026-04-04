"""Shared utilities for the litigation tracker pipeline."""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw" / "courtlistener_responses"
ENRICHED_DIR = DATA_DIR / "enriched"
SEED_DIR = DATA_DIR / "seed"
DB_PATH = ENRICHED_DIR / "cases.db"

COURTLISTENER_BASE_URL = "https://www.courtlistener.com/api/rest/v4"
COURTLISTENER_TOKEN = os.getenv("COURTLISTENER_TOKEN")

# Rate limiting: 5000/hour = ~1 per 0.72 seconds, use 1 second for safety
REQUEST_DELAY = 1.0

# Ensure directories exist
RAW_DIR.mkdir(parents=True, exist_ok=True)
ENRICHED_DIR.mkdir(parents=True, exist_ok=True)
SEED_DIR.mkdir(parents=True, exist_ok=True)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Error log file
error_log_path = DATA_DIR / "pipeline_errors.log"


def log_error(error: Exception, context: str = "") -> None:
    """Log an error to both console and file."""
    from datetime import datetime

    message = f"{context}: {str(error)}" if context else str(error)
    logger.error(message)
    with open(error_log_path, "a") as f:
        f.write(f"{datetime.now().isoformat()} - ERROR - {message}\n")


def get_cache_key(endpoint: str, query_params: dict) -> str:
    """Generate a cache key from endpoint and query parameters."""
    key_str = endpoint + "|" + json.dumps(query_params, sort_keys=True)
    return hashlib.md5(key_str.encode()).hexdigest()


def check_cache(cache_key: str) -> Optional[dict]:
    """Check if a response is cached and return it."""
    cache_path = RAW_DIR / f"{cache_key}.json"
    if cache_path.exists():
        with open(cache_path, "r") as f:
            return json.load(f)
    return None


def save_cache(cache_key: str, data: dict) -> None:
    """Save a response to cache."""
    cache_path = RAW_DIR / f"{cache_key}.json"
    with open(cache_path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def make_api_call(endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
    """
    Make a rate-limited API call to CourtListener with caching.

    Args:
        endpoint: The API endpoint (without base URL)
        params: Query parameters

    Returns:
        JSON response or None if failed
    """
    if not COURTLISTENER_TOKEN:
        logger.error("COURTLISTENER_TOKEN not set in .env file")
        return None

    # Check cache first
    cache_key = get_cache_key(endpoint, params or {})
    cached = check_cache(cache_key)
    if cached:
        logger.info(f"Cache hit: {endpoint}")
        return cached

    url = f"{COURTLISTENER_BASE_URL}/{endpoint}"
    headers = {"Authorization": f"Token {COURTLISTENER_TOKEN}"}

    try:
        # Rate limiting delay
        time.sleep(REQUEST_DELAY)

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        # Cache the response
        save_cache(cache_key, data)
        return data

    except requests.exceptions.RequestException as e:
        log_error(e, f"API call to {endpoint}")
        return None


def normalize_case_name(case_name: str) -> str:
    """
    Normalize a case name for matching.

    - Strip "v." variations
    - Trim whitespace
    - Convert to lowercase
    - Remove extra punctuation
    """
    import re

    # Convert to lowercase
    name = case_name.lower().strip()

    # Remove various "v." patterns (including "vs", "versus", etc.)
    name = re.sub(r"\s+v\.\s+", " ", name)
    name = re.sub(r"\s+vs\.\s+", " ", name)
    name = re.sub(r"\s+versus\s+", " ", name)

    # Remove extra punctuation
    name = re.sub(r"[^\w\s\-]", "", name)

    # Normalize whitespace
    name = re.sub(r"\s+", " ", name).strip()

    return name


def extract_case_parts(case_name: str) -> tuple[str, str]:
    """
    Extract plaintiff and defendant from a case name.

    Returns:
        Tuple of (plaintiff, defendant)
    """
    import re

    # Try various separators
    for sep in [r"\s+v\.\s+", r"\s+vs\.\s+", r"\s+versus\s+", r"\s+v\s+"]:
        parts = re.split(sep, case_name, flags=re.IGNORECASE)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()

    # If no separator found, return whole thing as plaintiff
    return case_name.strip(), ""


def truncate_name_for_search(name: str, max_length: int = 50) -> str:
    """Truncate case name for API search to avoid URL length limits."""
    if len(name) <= max_length:
        return name

    # Try to keep the beginning and end
    mid = "..."
    start_len = (max_length - len(mid)) // 2
    return name[:start_len] + mid + name[-start_len:]
