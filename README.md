# DHL Express tracking for Power Automate

A Render web service that opens the DHL Taiwan public tracking page in Chromium, clicks Track if needed, and returns the visible tracking data as JSON. It uses Playwright, without a DHL API credential or Power Automate Desktop.

## Publish and deploy

1. Push the files in this directory to the root of a GitHub repository. A private repository is sufficient. Do not commit the service token.
2. In Render, select **New → Blueprint** and connect the repository and branch. The included **render.yaml** defines a Docker web service, the /health probe and the secret. Alternatively, create a **Docker Web Service** from the repository and set its health check to /health.
3. Set **DHL_TRACK_TOKEN** in Render to a random secret at least 32 characters long. To generate one locally in PowerShell, run `[guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')`.
4. After deployment, open `https://YOUR-SERVICE.onrender.com/health` and confirm `{"ok":true}`. This verifies the HTTP service and token configuration. The first actual /track request verifies Chromium and DHL access from Render. On Render Free, open /health first to wake the service before testing the flow.

See [Docker on Render](https://render.com/docs/docker) and [Render Blueprints](https://render.com/docs/infrastructure-as-code).

## Power Automate

Create an instant cloud flow with a text input for the 10-digit DHL Express tracking number. Add a Premium **HTTP** action:

- Method: **POST**
- URI: `https://YOUR-SERVICE.onrender.com/track`
- Header **Content-Type**: `application/json`
- Header **Authorization**: `Bearer YOUR_TOKEN`
- Body: `{"tracking_number":"6093812465"}` for the first test. Then replace the example number with the trigger's text field.

Enable secure inputs and outputs for the HTTP action. In later actions, use `body('HTTP')?['status']` for the status and `body('HTTP')?['timeline']` or `body('HTTP')?['event_log']` for the detailed text. `shipment_summary`, `shipment_details` and `source_url` are also returned. Content is the DHL page's displayed text. Save the JSON in OneDrive or post the status and source link to Teams as needed.

HTTP 401 indicates a token mismatch, 400 indicates invalid input, 429 means another lookup is running, and 502 or 504 means lookup failed. An upstream block is reported as such. The service stops on blocked access and does not attempt to bypass verification. It provides tracking details; this version does not request or download POD.

## Local browser test

Install Python Playwright 1.58.0 and run `python -m playwright install --only-shell chromium`. Then run `python dhl_tracking.py 6093812465`. To use a locally installed Edge instead, add `--channel msedge`.
