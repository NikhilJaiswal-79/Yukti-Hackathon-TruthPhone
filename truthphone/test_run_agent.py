import sys
sys.path.append('.')
from agent import _run_playwright_search_sync

try:
    print("Running scraper...")
    result = _run_playwright_search_sync("samsung galaxy s23")
    print("--- RESULT ---")
    print(result)
except Exception as e:
    print("ERROR:", e)
