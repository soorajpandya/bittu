from playwright.sync_api import sync_playwright
import json
import time


def scrape_meesho():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            slow_mo=100  # human-like delay
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-IN"
        )

        page = context.new_page()

        # 👇 Basic stealth (no dependency)
        page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
        """)

        results = []

        # 🔥 Capture API response
        def handle_response(response):
            try:
                if "/api/v1/feed" in response.url:
                    data = response.json()

                    catalogs = data.get("data", {}).get("catalogs", [])

                    for item in catalogs:
                        product = {
                            "id": item.get("catalog_id"),
                            "title": item.get("title"),
                            "price": item.get("price"),
                            "image": item.get("image")
                        }

                        results.append(product)

                    print(f"Captured: {len(results)}")

            except Exception as e:
                pass

        page.on("response", handle_response)

        # 🔥 Step 1: Open homepage first (important)
        page.goto("https://www.meesho.com/")
        page.wait_for_timeout(5000)

        # simulate human behavior
        page.mouse.move(200, 300)
        page.wait_for_timeout(2000)

        # 🔥 Step 2: Open category page
        page.goto("https://www.meesho.com/women-ethnicwear/pl/3tq")
        page.wait_for_load_state("networkidle")

        print("Page title:", page.title())

        # 🔥 Step 3: Scroll to trigger API
        for i in range(10):
            page.mouse.wheel(0, 8000)
            page.wait_for_timeout(2000)

        # wait for final API calls
        page.wait_for_timeout(5000)

        browser.close()

        return results


if __name__ == "__main__":
    data = scrape_meesho()

    print("\nSample Data:\n")
    print(json.dumps(data[:10], indent=2))

    print(f"\nTotal Products Scraped: {len(data)}")