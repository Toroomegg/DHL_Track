import argparse
import json
import re

from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright

TRACKING_URL = "https://www.dhl.com/tw-zh/home/tracking.html"
SHIPMENT_MARKER = re.compile(r"處理此貨件由|Handled by", re.I)
ERROR_TEXT = re.compile(
    r"查無|找不到|無法追蹤|請稍後|no results|not found|no tracking|try again later", re.I
)
CHALLENGE = re.compile(r"verify you are human|access denied|驗證您是人類|captcha", re.I)


def track(tracking_number, channel=None):
    with sync_playwright() as playwright:
        browser = (
            playwright.chromium.launch(channel=channel)
            if channel else playwright.firefox.launch()
        )
        try:
            page = browser.new_page(locale="zh-TW")
            page.set_default_timeout(8_000)
            response = page.goto(
                f"{TRACKING_URL}?tracking-id={tracking_number}",
                wait_until="domcontentloaded", timeout=25_000,
            )
            if response and response.status in (403, 429):
                return {"outcome": "upstream_blocked"}

            cookies = page.get_by_role("button", name=re.compile("僅嚴格必要|Strictly Necessary"))
            if cookies.is_visible():
                cookies.click(timeout=3_000)

            marker = page.get_by_text(SHIPMENT_MARKER).first
            button = page.get_by_role("button", name=re.compile(r"^(追蹤|Track)$", re.I))
            if not marker.is_visible() and button.first.is_enabled(timeout=8_000):
                button.first.click()

            try:
                marker.wait_for(timeout=22_000)
            except BrowserTimeout:
                main = page.get_by_role("main")
                visible = main.inner_text() if main.count() else page.locator("body").inner_text()
                if CHALLENGE.search(visible):
                    return {"outcome": "upstream_blocked"}
                # Inspect actual alerts only. The page's FAQ mentions missing shipments too.
                alerts = page.get_by_role("alert").all_inner_texts()
                if any(ERROR_TEXT.search(alert) for alert in alerts):
                    return {"outcome": "upstream_message", "message": " ".join(alerts)}
                return {"outcome": "tracking_result_unavailable"}

            details_tab = page.get_by_role("tab", name=re.compile("貨物詳情|Shipment Details", re.I))
            panel = details_tab.get_attribute("aria-controls")
            details = page.locator(f'[id="{panel}"]').inner_text() if panel else ""
            status_heading = page.locator(
                'h2[id^="duil320-dhl-headline-"]'
            ).filter(visible=True).first
            status = status_heading.inner_text()
            summary = marker.evaluate("""element => {
                let section = element;
                for (let i = 0; section && i < 9; i++, section = section.parentElement) {
                    if (section.querySelector('h2') && section.querySelector('[role="tablist"]'))
                        return section.innerText;
                }
                return '';
            }""")
            timeline = page.get_by_role("tab", name=re.compile("貨物時間線|Shipment Timeline", re.I))
            timeline.click()
            timeline_panel = page.get_by_role("tabpanel").filter(visible=True)
            timeline_panel.wait_for()
            timeline_text = timeline_panel.inner_text()
            event_log = page.get_by_role("tab", name=re.compile("事件日誌|Event Log", re.I))
            event_text = ""
            if event_log.is_visible():
                event_log.click()
                event_text = page.get_by_role("tabpanel").filter(visible=True).inner_text()

            return {
                "outcome": "ok",
                "tracking_number": tracking_number,
                "status": status,
                "shipment_summary": summary.split("\n貨物詳情", 1)[0].strip(),
                "shipment_details": details.strip(),
                "timeline": timeline_text.strip(),
                "event_log": event_text.strip(),
                "source_url": f"{TRACKING_URL}?tracking-id={tracking_number}",
            }
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("tracking_number")
    parser.add_argument("--channel", choices=["chrome", "msedge"])
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{10}", args.tracking_number):
        parser.error("Use a 10-digit DHL Express waybill number.")
    print(json.dumps(track(args.tracking_number, args.channel)))

