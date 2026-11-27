import argparse
import json
import re

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

TRACKING_URL = "https://www.dhl.com/tw-zh/home/tracking.html"
SHIPMENT_MARKER = re.compile(r"處理此貨件由|Handled by", re.I)
ERROR_TEXT = re.compile(
    r"查無|找不到|無法追蹤|請稍後|no results|not found|no tracking|try again later", re.I
)
CHALLENGE = re.compile(r"verify you are human|access denied|驗證您是人類|captcha", re.I)


def read_tab_panel(page, tab, *, select=False):
    if select:
        tab.click()
    panel_id = tab.get_attribute("aria-controls")
    panel = page.locator(f'[id="{panel_id}"]')
    panel.wait_for()
    return panel.inner_text().strip()


def track(tracking_number, channel=None):
    with sync_playwright() as playwright:
        browser = (
            playwright.chromium.launch(channel=channel)
            if channel else playwright.firefox.launch()
        )
        stage = "open_tracking_page"
        overlay = None
        try:
            page = browser.new_page(locale="zh-TW", viewport={"width": 1440, "height": 1000})
            cookies = page.locator("#onetrust-reject-all-handler")
            overlay = page.locator("#sec-overlay")

            def dismiss_cookies(button):
                if not overlay.is_visible():
                    button.click(timeout=4_000)

            page.add_locator_handler(cookies, dismiss_cookies, no_wait_after=True)
            page.set_default_timeout(8_000)
            response = page.goto(
                f"{TRACKING_URL}?tracking-id={tracking_number}",
                wait_until="domcontentloaded", timeout=25_000,
            )
            if response and response.status in (403, 429):
                return {"outcome": "upstream_blocked"}

            stage = "wait_for_page_overlay"
            overlay.wait_for(state="hidden", timeout=8_000)
            stage = "submit_tracking"
            marker = page.get_by_text(SHIPMENT_MARKER).first
            track_button = page.get_by_role(
                "button", name=re.compile(r"^(追蹤|Track)$", re.I)
            ).filter(visible=True).first
            marker.or_(track_button).first.wait_for(timeout=10_000)
            if not marker.is_visible() and track_button.is_enabled():
                track_button.click(timeout=12_000)

            stage = "wait_for_tracking_result"
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
                return {"outcome": "tracking_result_unavailable", "stage": stage}

            stage = "read_shipment_details"
            details_tab = page.get_by_role("tab", name=re.compile("貨物詳情|Shipment Details", re.I))
            details = read_tab_panel(page, details_tab)
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
            stage = "read_timeline"
            timeline = page.get_by_role("tab", name=re.compile("貨物時間線|Shipment Timeline", re.I))
            timeline_text = read_tab_panel(page, timeline, select=True)
            stage = "read_event_log"
            event_log = page.get_by_role("tab", name=re.compile("事件日誌|Event Log", re.I))
            event_text = ""
            if event_log.is_visible():
                event_text = read_tab_panel(page, event_log, select=True)

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
        except BrowserError as error:
            if isinstance(error, BrowserTimeout) and overlay is not None and overlay.is_visible():
                return {"outcome": "upstream_blocked", "stage": stage, "cause": "site_overlay"}
            error.tracking_stage = stage
            raise
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

