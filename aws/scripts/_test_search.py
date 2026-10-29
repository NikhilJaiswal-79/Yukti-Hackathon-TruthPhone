import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")

from truthphone.amazon import search_amazon  # noqa: E402
from truthphone.browser import open_browser  # noqa: E402


async def main() -> None:
    message = sys.argv[1] if len(sys.argv) > 1 else "Infinix Hot 50 5G"
    async with open_browser("search-debug") as (_client, _browser, _context, page):
        phones, req = await search_amazon(page, message)
        print("\nQUERY:", message)
        print("REQ:", req)
        for phone in phones:
            print("->", phone.name, "|", phone.price, "|", phone.url)


if __name__ == "__main__":
    asyncio.run(main())
