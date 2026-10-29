import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from truthphone.browser import open_browser  # noqa: E402


async def main() -> None:
    async with open_browser("tls-check") as (_client, _browser, _context, page):
        await page.goto("https://example.com", wait_until="domcontentloaded")
        print("title:", await page.title())


if __name__ == "__main__":
    asyncio.run(main())
