import argparse
import json
import re
from urllib.parse import urlsplit

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

TRACKING_URL = "https://www.dhl.com/tw-zh/home/tracking.html"
DETAILS_TAB = re.compile(r"貨物詳情|Shipment Details", re.I)
ERROR_TEXT = re.compile(
    r"查無|找不到|無法追蹤|請稍後|no results|not found|no tracking|try again later", re.I
)
CHALLENGE = re.compile(r"verify you are human|access denied|驗證您是人類|captcha", re.I)


def read_tab_panel(page, tab):
    if tab.get_attribute("aria-selected") != "true":
        tab.click()
    panel_id = tab.get_attribute("aria-controls")
    panel = page.locator(f'[id="{panel_id}"]')
    panel.wait_for()
    return panel.evaluate("""panel => {
        const visible = el => el.getClientRects().length > 0 &&
            getComputedStyle(el).visibility === 'visible';
        const text = el => el.innerText.trim();
        const rows = [...panel.querySelectorAll('tr, [role="row"]')]
            .filter(visible)
            .map(row => [...row.children].filter(visible).map(text))
            .filter(cells => cells.length);
        const links = [...panel.querySelectorAll('a[href]')]
            .filter(link => visible(link) && /^https?:/.test(link.href))
            .map(link => ({text: text(link), url: link.href}));
        return {text: text(panel), rows, links};
    }""")


def observe_dhl_requests(page):
    calls = {}
    tracked = {}
    preconditions = []

    def on_request(request):
        url = urlsplit(request.url)
        host = url.hostname or ""
        if (request.resource_type in ("fetch", "xhr")
                and (host == "dhl.com" or host.endswith(".dhl.com"))
                and len(calls) < 50):
            calls[request] = "pending"
            if re.search(r"track|shipment", url.path, re.I):
                tracked[request] = request.method

    def on_response(response):
        if response.request in calls:
            calls[response.request] = response.status
            if response.status == 428:
                url = urlsplit(response.url)
                preconditions.append({
                    "host": url.hostname,
                    "tracking_path": response.request in tracked,
                    "security_path": bool(re.search(r"security|challenge|captcha|akam", url.path, re.I)),
                    "content_type": response.headers.get("content-type", "").split(";", 1)[0],
                })

    def on_failure(request):
        if request in calls:
            failure = request.failure or ""
            code = re.search(r"(?:NS_ERROR|ERR)_[A-Z0-9_]+", failure)
            calls[request] = code.group(0) if code else "request_failed"

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_failure)

    def snapshot():
        counts = {}
        for result in calls.values():
            key = str(result)
            counts[key] = counts.get(key, 0) + 1
        return {
            "observed_requests": len(calls),
            "results": counts,
            "preconditions": preconditions,
            "tracking_path_responses": [
                {"method": method, "result": calls[request]}
                for request, method in tracked.items()
            ],
        }

    return snapshot


def submit_tracking(page, tracking_number):
    field = page.get_by_role(
        "textbox", name=re.compile(r"輸入您的追蹤碼|Enter your tracking", re.I)
    ).filter(visible=True).first
    field.fill(tracking_number, timeout=12_000)
    button = page.get_by_role(
        "button", name=re.compile(r"^(追蹤|Track)$", re.I)
    ).filter(visible=True).first
    # Playwright waits until the site's input validation enables the button.
    button.click(timeout=12_000)


def track(tracking_number, channel=None):
    with sync_playwright() as playwright:
        browser = (
            playwright.chromium.launch(channel=channel)
            if channel else playwright.firefox.launch()
        )
        stage = "open_tracking_page"
        overlay = None
        track_clicked = False
        try:
            page = browser.new_page(locale="zh-TW", viewport={"width": 1440, "height": 1000})
            cookies = page.locator("#onetrust-reject-all-handler").filter(visible=True).first
            overlay = page.locator("#sec-overlay").filter(visible=True).first

            def dismiss_cookies(button):
                if not overlay.is_visible():
                    button.click(timeout=4_000)

            page.add_locator_handler(cookies, dismiss_cookies, times=2, no_wait_after=True)
            page.set_default_timeout(8_000)
            response = page.goto(
                TRACKING_URL,
                wait_until="domcontentloaded", timeout=25_000,
            )
            if response and response.status in (403, 429):
                return {"outcome": "upstream_blocked"}

            stage = "wait_for_page_overlay"
            overlay.wait_for(state="hidden", timeout=8_000)
            stage = "submit_tracking"
            details_tab = page.get_by_role("tab", name=DETAILS_TAB).filter(visible=True).first
            network_snapshot = observe_dhl_requests(page)
            submit_tracking(page, tracking_number)
            track_clicked = True

            stage = "wait_for_tracking_result"
            try:
                details_tab.wait_for(timeout=22_000)
            except BrowserTimeout:
                main = page.get_by_role("main")
                visible = main.inner_text() if main.count() else page.locator("body").inner_text()
                if CHALLENGE.search(visible):
                    return {"outcome": "upstream_blocked"}
                # Inspect actual alerts only. The page's FAQ mentions missing shipments too.
                alerts = page.get_by_role("alert").all_inner_texts()
                if any(ERROR_TEXT.search(alert) for alert in alerts):
                    return {"outcome": "upstream_message", "message": " ".join(alerts)}
                return {
                    "outcome": "tracking_result_unavailable",
                    "stage": stage,
                    "track_clicked": track_clicked,
                    "dhl_network": network_snapshot(),
                    "visible_tabs": page.get_by_role("tab").filter(visible=True).count(),
                    "site_overlay_visible": overlay.is_visible(),
                }

            stage = "read_shipment_details"
            details = read_tab_panel(page, details_tab)
            status_heading = page.locator(
                'h2[id^="duil320-dhl-headline-"]'
            ).filter(visible=True).first
            status = status_heading.inner_text()
            summary = details_tab.evaluate("""element => {
                let section = element;
                for (let i = 0; section && i < 9; i++, section = section.parentElement) {
                    if (section.querySelector('h2') && section.querySelector('[role="tablist"]'))
                        return section.innerText;
                }
                return '';
            }""")
            stage = "read_timeline"
            timeline = page.get_by_role("tab", name=re.compile("貨物時間線|Shipment Timeline", re.I))
            timeline_details = read_tab_panel(page, timeline)
            stage = "read_event_log"
            event_log = page.get_by_role("tab", name=re.compile("事件日誌|Event Log", re.I))
            events = read_tab_panel(page, event_log)

            return {
                "outcome": "ok",
                "tracking_number": tracking_number,
                "status": status,
                "track_clicked": track_clicked,
                "shipment_summary": re.split(r"\n(?:貨物詳情|Shipment Details)", summary, maxsplit=1)[0].strip(),
                "shipment_details": details["text"],
                "timeline": timeline_details["text"],
                "event_log": events["text"],
                "tabs": {
                    "shipment_details": details,
                    "shipment_timeline": timeline_details,
                    "event_log": events,
                },
                "source_url": f"{TRACKING_URL}?tracking-id={tracking_number}",
            }
        except BrowserError as error:
            if isinstance(error, BrowserTimeout) and overlay is not None and overlay.is_visible():
                return {"outcome": "upstream_blocked", "stage": stage,
                        "cause": "site_overlay", "track_clicked": track_clicked}
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

