"""Throwaway check for the Sonnet claim planner + judge."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from truthphone import agents  # noqa: E402
from truthphone.models import PhoneCandidate  # noqa: E402

LISTING = """
Samsung Galaxy M34 5G (Midnight Blue, 8GB RAM, 128GB Storage)
- 16 GB RAM with RAM Plus (8 GB physical + 8 GB virtual)
- 33 W fast charging support
- 6000 mAh battery
- 6.5 inch sAMOLED 120 Hz display
- 50 MP triple camera
- 5G SA/NSA support
- Exynos 1280 octa-core processor
- IP67 rated
- 3 years of OS updates
"""

EVIDENCE = """
### RETAILER LISTING (Amazon.in)
Samsung Galaxy M34 5G (Midnight Blue, 8GB RAM, 128GB Storage)
16 GB RAM with RAM Plus (8GB physical + 8GB virtual)
33 W fast charging support
6000 mAh battery
6.5 inch sAMOLED 120 Hz display
50 MP triple camera
5G support
Exynos 1280 octa-core processor
IP67
3 years of OS updates

### GSMArena SPECIFICATION SHEET
Body: IP67 dust/water resistant
Display: Super AMOLED, 120Hz, 1000 nits (HBM)
Platform: Exynos 1280 (5nm), octa-core (2x2.4 GHz Cortex-A78 & 6x2.0 GHz Cortex-A55)
Memory: 128GB 6GB RAM, 128GB 8GB RAM, 256GB 8GB RAM; UFS 2.2
Main Camera: 50 MP, f/1.8, OIS; 8 MP ultrawide; 2 MP macro
Battery: 6000 mAh, 25W wired charging
5G bands: SA/NSA
Charger: not included in the box in India

### BOX CONTENTS / BUNDLED CHARGER (search results)
Review: "Samsung does not bundle a charger in the M34 5G box. You get the phone,
a USB-C cable and a SIM tool. The phone supports 25W charging, and you need to
buy a 25W PD adapter separately. The '33W' figure in some listings is wrong."

### 5G BANDS AND CARRIER SUPPORT (search results)
The M34 5G supports both SA and NSA 5G, including Airtel and Jio bands n78/n1/n3.
"""


async def main() -> None:
    phone = PhoneCandidate(
        name="Samsung Galaxy M34 5G",
        price="Rs 16,999",
        specs="8GB RAM, 128GB, 6000mAh",
        url="https://www.amazon.in/dp/TEST",
        details=LISTING,
        asin="TEST",
    )

    claims = await agents.plan_claims(phone)
    print(f"\n=== {len(claims)} claims ===")
    for claim in claims:
        print(f"- [{claim.category}] {claim.claim}   risk={claim.risk!r}")

    results = await agents.judge_claims(
        phone,
        claims,
        EVIDENCE,
        {
            "Retailer listing": phone.url,
            "GSMArena": "https://www.gsmarena.com/test.php",
            "Box contents search": "https://duckduckgo.com/?q=box",
            "5G bands search": "https://duckduckgo.com/?q=5g",
        },
    )

    print(f"\n=== {len(results)} verdicts ===")
    for result in results:
        print(
            json.dumps(
                {
                    "claim": result.claim,
                    "verdict": result.verdict,
                    "severity": result.severity,
                    "reality": result.reality,
                    "hidden_catch": result.hidden_catch,
                    "evidence": [e.source for e in result.evidence],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
