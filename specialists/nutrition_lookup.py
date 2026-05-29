"""Open Food Facts nutrition lookup with local JSON cache.

lookup_food(name)                 -> "Banana: 89 kcal, 23g carbs, ..." or ""
enrich_prompt_with_food_data(text) -> injection block for the Gemini prompt, or ""
"""

import json
import re
import subprocess
from pathlib import Path

_CACHE_PATH = Path(__file__).parent.parent / "data" / "food_lookup_cache.json"
_OFF_URL = "https://world.openfoodfacts.org/cgi/search.pl"

_FOOD_RE = re.compile(
    r"\b(?:banana|oats?|oatmeal|porridge|rice|pasta|bread|toast|"
    r"yoghurt|yogurt|chicken|egg|eggs|milk|"
    r"protein\s+shake|protein\s+bar|energy\s+gel|gel|gels|"
    r"dates?|cereal|muesli|granola|nuts|almonds|"
    r"peanut\s+butter|peanut|avocado|salmon|tuna|"
    r"cottage\s+cheese|sweet\s+potato|apple|orange|berries|"
    r"blueberries|strawberries|chocolate\s+milk|sports\s+drink|"
    r"coffee|espresso)\b",
    re.IGNORECASE,
)

_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_PATH.read_text()) if _CACHE_PATH.exists() else {}
        except (json.JSONDecodeError, OSError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is not None:
        try:
            _CACHE_PATH.write_text(json.dumps(_cache, indent=2))
        except OSError:
            pass


def _fetch_off(query: str) -> str:
    """Query Open Food Facts; return formatted macro string or ''."""
    params = (
        f"search_terms={query.replace(' ', '+')}"
        f"&action=process&json=true&page_size=5"
        f"&fields=product_name,nutriments"
    )
    result = subprocess.run(
        ["curl", "-s", "--max-time", "5", f"{_OFF_URL}?{params}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""

    for product in data.get("products", []):
        n = product.get("nutriments", {})
        kcal = n.get("energy-kcal_100g")
        carbs = n.get("carbohydrates_100g")
        protein = n.get("proteins_100g")
        fat = n.get("fat_100g")
        if all(v is not None for v in (kcal, carbs, protein, fat)):
            name = product.get("product_name") or query.title()
            return (
                f"{name}: {int(kcal)} kcal, "
                f"{carbs:.1f}g carbs, {protein:.1f}g protein, {fat:.1f}g fat "
                "(per 100g)"
            )
    return ""


def lookup_food(name: str) -> str:
    """Return a macro summary for name, cached. Returns '' if not found.

    Empty results (transient timeout or no match) are not written to the cache,
    so a temporary Open Food Facts outage does not permanently poison future lookups.
    """
    cache = _load_cache()
    key = name.lower().strip()
    if key in cache:
        return cache[key]
    result = _fetch_off(key)
    if result:
        cache[key] = result
        _save_cache()
    return result


def enrich_prompt_with_food_data(reply_text: str) -> str:
    """Scan reply_text for food mentions; return a nutrition data block for injection.

    Returns '' if no recognised foods found or all lookups return no data.
    """
    names = {m.group().lower().strip() for m in _FOOD_RE.finditer(reply_text)}
    if not names:
        return ""
    results = [r for name in sorted(names) if (r := lookup_food(name))]
    if not results:
        return ""
    return (
        "Nutritional reference data (Open Food Facts):\n"
        + "\n".join(f"  {r}" for r in results)
    )
