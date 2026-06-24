import os
import re
import time
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import sys

# Redefine print globally within this module to write to sys.stderr by default
# This prevents stdout pollution which corrupts the FastMCP JSON-RPC communication channel
def print(*args, **kwargs):
    kwargs.setdefault('file', sys.stderr)
    import builtins
    builtins.print(*args, **kwargs)

def smart_logout(page, base_url):
    """URL-based SAP session termination."""
    try:
        print("[INFO] 👋 Signing out...", file=sys.stderr)
        base_url = base_url.rstrip('/')
        page.goto(f"{base_url}/sap/public/bc/icf/logoff?sap-client=100", timeout=10000)
        print("[INFO] ✅ Session closed.", file=sys.stderr)
    except Exception as e:
        print(f"[WARNING] Logout failed: {e}", file=sys.stderr)

def sap_session_handler(func):
    """Decorator to manage Playwright startup, direct target navigation, login, and error capturing."""
    def wrapper(*args, **kwargs):
        load_dotenv()
        sap_url = os.getenv("SAP_URL")
        sap_email = os.getenv("SAP_EMAIL")
        sap_password = os.getenv("SAP_PASSWORD")

        if not all([sap_url, sap_email, sap_password]):
            raise Exception("Missing credentials in .env file.")

        base_url = sap_url.rstrip('/')
        # Direct Create Wizard URL
        target_url = base_url + "/ui#PMRPSimulation-create?%252Fh4screen=SchedPMRPSimuCreat&JobCatalogEntryName=SAP_SCM_PMRP_CREATE_WC%252CSAP_SCM_PMRP_CREATE_MAT%252CSAP_SCM_PMRP_CREATE_MATCOMP&/v4_JobRunCreate"

        headless = os.getenv("SAP_HEADLESS", "true").lower() == "true"
        print(f"[INFO] Launching browser (headless={headless}) to connect to target URL: {target_url}", file=sys.stderr)
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=[] if headless else ["--start-maximized"])
            context = browser.new_context() if headless else browser.new_context(no_viewport=True)
            page = context.new_page()
            page.on("console", lambda msg: print(f"[BROWSER] {msg.type}: {msg.text}", file=sys.stderr) if msg.type == "error" else None)
            try:
                print(f"[INFO] Navigating directly to application page...", file=sys.stderr)
                page.goto(target_url, wait_until="load", timeout=0)
                
                # Wait until either the login username input OR the application Create/Wizard elements are visible
                print("[INFO] Waiting for page determination...", file=sys.stderr)
                login_selector = page.locator("#j_username")
                app_selector = page.locator("#application-PMRPSimulation-create-component---JobRunList--appJobsOverviewAddJobButton-BDI-content, label:has-text('Job Template')").first
                
                try:
                    # Comma-separated selector waits for either to match
                    page.locator("#j_username, #application-PMRPSimulation-create-component---JobRunList--appJobsOverviewAddJobButton-BDI-content, label:has-text('Job Template')").first.wait_for(state="visible", timeout=60000)
                except Exception:
                    raise Exception("Target page failed to load (neither login screen nor SAP application elements appeared within 60 seconds).")
                
                if login_selector.count() > 0 and login_selector.is_visible():
                    print("[INFO] Redirected to login page. Authenticating...", file=sys.stderr)
                    login_selector.fill(sap_email)
                    page.locator("#j_password").fill(sap_password)
                    page.locator(".fn-button__text", has_text="Continue").click()

                    print("[INFO] Waiting for target application page to load...", file=sys.stderr)
                    app_selector.wait_for(state="visible", timeout=60000)
                elif app_selector.count() > 0 and app_selector.is_visible():
                    print("[INFO] Already logged in. Direct page entry.", file=sys.stderr)
                else:
                    raise Exception("Neither login field nor SAP application elements are visible on the page.")
                
                # Execute the wrapped function logic
                return func(page, sap_url, *args, **kwargs)
            except Exception as e:
                print(f"[ERROR] Automation failed: {e}", file=sys.stderr)
                try:
                    page.screenshot(path="automation_error.png")
                    print("[INFO] Saved failure screenshot as 'automation_error.png'", file=sys.stderr)
                except Exception as se:
                    print(f"[ERROR] Could not save screenshot: {se}", file=sys.stderr)
                return f"""# 📋 SAP pMRP Automation Report

### ❌ Automation Error Encountered
An error occurred during the execution of the browser automation script.

### 🔍 Error Details:
* **Error Message:** `{e}`

> [!IMPORTANT]
> The automation was stopped. A failure screenshot has been saved to the host directory as `automation_error.png` for diagnostic purposes."""
            finally:
                # Perform smart logout before closing browser
                smart_logout(page, sap_url)
                context.close()
                browser.close()
                print("[INFO] Browser closed.", file=sys.stderr)
    return wrapper

def clear_existing_tokens(page, label_text):
    """Finds and clicks the close icon (x) on any existing tokens for a field."""
    label = page.locator("label").filter(has_text=re.compile(rf"^\s*\*?\s*{label_text}\s*\*?:?\s*$", re.IGNORECASE)).first
    for_id = label.get_attribute("for")
    if for_id:
        control_id = for_id.replace("-inner", "")
        tokens_x = page.locator(f"#{control_id} .sapMTokenIcon")
    else:
        tokens_x = label.locator("xpath=../..").locator(".sapMTokenIcon")
    
    count = tokens_x.count()
    if count > 0:
        print(f"[INFO] Clearing {count} existing token(s) for '{label_text}'...")
        for i in range(count):
            try:
                tokens_x.first.click()
                page.wait_for_timeout(300)
            except Exception as e:
                print(f"[WARNING] Could not clear token: {e}")

def get_field_elements(page, label_text):
    """Robust helper to find the input element and VHI icon associated with a label."""
    label = page.locator("label").filter(has_text=re.compile(rf"^\s*\*?\s*{label_text}\s*\*?:?\s*$", re.IGNORECASE)).first
    for_id = label.get_attribute("for")
    
    if for_id:
        inp = page.locator(f"#{for_id}")
        control_id = for_id.replace("-inner", "")
        vhi = page.locator(f"#{control_id}-vhi")
        if vhi.count() > 0:
            return inp, vhi
            
    # Sibling fallback: find the first input element following the label in document order
    inp = label.locator("xpath=following::input[1]")
    inp_id = inp.get_attribute("id")
    if inp_id:
        control_id = inp_id.replace("-inner", "")
        vhi = page.locator(f"#{control_id}-vhi")
    else:
        vhi = inp.locator("xpath=..").locator(".sapMInputValHelpIcon").first
        
    return inp, vhi

def fill_direct_field(page, label_text, value):
    """Fills a text or date field directly."""
    print(f"[INFO] Typing '{value}' directly into '{label_text}'...")
    inp, _ = get_field_elements(page, label_text)
    
    inp.wait_for(state="visible", timeout=15000)
    inp.focus()
    inp.fill(value)
    page.wait_for_timeout(100)
    inp.press("Enter")
    page.wait_for_timeout(100)
    inp.press("Tab")
    page.wait_for_timeout(100)

def handle_f4_select(page, label_text, search_value, filter_label=None):
    """Clicks F4 help button, searches for value, selects it, and closes dialog.
       Auto-detects checkbox (in standard & parallel tables) and OK button presence.
       If filter_label is provided, searches in that specific filter field inside the dialog."""
    print(f"[INFO] Using F4 Help for '{label_text}' -> typing search query '{search_value}'...")
    
    # Locate the value help icon (VHI) using unified helper
    _, vhi = get_field_elements(page, label_text)
    
    vhi.wait_for(state="visible", timeout=15000)
    
    # Wait for the Dialog to appear
    dialog = page.locator("div.sapMDialog:visible").first
    
    # Click VHI with retry logic in case event listener is not yet bound
    for attempt in range(4):
        try:
            vhi.click()
            dialog.wait_for(state="visible", timeout=4000)
            break
        except Exception:
            print(f"[WARNING] F4 Dialog did not open for '{label_text}'. Retrying click (Attempt {attempt+1})...")
            page.wait_for_timeout(1000)
    else:
        raise Exception(f"Failed to open F4 dialog for '{label_text}'")
    
    # Try finding the specific input if filter_label is specified
    has_search = True
    search_input = None
    
    if filter_label:
        dialog_label = dialog.locator("label").filter(has_text=re.compile(rf"^\s*\*?\s*{filter_label}\s*\*?:?\s*$", re.IGNORECASE)).first
        try:
            dialog_label.wait_for(state="visible", timeout=3000)
            for_id = dialog_label.get_attribute("for")
            if for_id:
                search_input = dialog.locator(f"#{for_id}")
            else:
                search_input = dialog_label.locator("xpath=../..").locator("input").first
            print(f"[INFO] Found specific filter input for '{filter_label}' inside dialog.")
        except Exception:
            print(f"[WARNING] Specific filter label '{filter_label}' not found. Falling back to default search input...")
            filter_label = None

    if not filter_label or search_input is None:
        # Target only the Search Field input to prevent opening any select-view dropdowns
        search_input = dialog.locator(".sapMSF input, input[type='search'].sapMSFI").first
        try:
            search_input.wait_for(state="visible", timeout=2000)
        except Exception:
            search_input = dialog.locator("input").first
            try:
                search_input.wait_for(state="visible", timeout=2000)
            except Exception:
                has_search = False
                print("[INFO] No search input found in F4 dialog. Proceeding to select directly from list...")
        
    if has_search and search_input is not None:
        # Close any warning message popovers that could steal focus
        msg_close = page.locator(".sapMPopover:visible button[id*='close'], button.sapMPopoverCloseButton:visible, button[id*='messagePopover-close']:visible").first
        if msg_close.count() > 0 and msg_close.is_visible():
            try:
                print("[INFO] Closing blocking message popover...")
                msg_close.click()
                page.wait_for_timeout(500)
            except Exception as pe:
                print(f"[WARNING] Could not close popover: {pe}")

        search_input.click()
        search_input.focus()
        search_input.fill(search_value)
        page.wait_for_timeout(100)
        search_input.press("Enter")
        
        # Click search / Go button
        go_btn = dialog.locator(
            "button[id*='search']:visible, "
            "button[id*='go']:visible, "
            "button:has-text('Go'):visible, "
            "button:has-text('Search'):visible, "
            "button.sapMSFBtn:visible"
        ).first
        
        if go_btn.count() > 0:
            try:
                go_btn.click()
            except Exception:
                pass
        
        # Wait for search results to begin updating
        page.wait_for_timeout(300)
    
    # Locate matching cell in the results table body (using role="gridcell" to avoid headers)
    cell_selector = "[role='gridcell'], .sapMListTblCell"
    cell = dialog.locator(cell_selector).filter(has_text=search_value).first
    
    # Wait for the exact matching cell to load and become visible
    cell.wait_for(state="visible", timeout=15000)
        
    # Check if a checkbox exists for selection
    # 1. Try finding checkbox in the same row (for sap.m.Table)
    row = cell.locator("xpath=ancestor::tr[1] | ancestor::li[1]")
    checkbox = row.locator("div.sapMCb, [role='checkbox']").first
    
    # 2. Try parallel table selector mapping using DOM row index (for sap.ui.table.Table)
    if not (checkbox.count() > 0 and checkbox.is_visible()):
        try:
            row_index = row.evaluate("""el => {
                const trs = Array.from(el.parentNode.querySelectorAll('tr[role="row"]'));
                return trs.indexOf(el);
            }""")
            print(f"[DEBUG] Row index calculated via DOM: {row_index}")
            if row_index >= 0:
                row_header = dialog.locator(".sapUiTableRowHdr, .sapUiTableRowSelectionCell").nth(row_index)
                if row_header.count() > 0 and row_header.is_visible():
                    checkbox = row_header
                    print(f"[DEBUG] Mapped checkbox to parallel row header at index {row_index}")
        except Exception as re_err:
            print(f"[WARNING] Failed to calculate parallel row index: {re_err}")

    # Click the checkbox if found, otherwise click cell directly
    if checkbox.count() > 0 and checkbox.is_visible():
        print(f"[INFO] Clicking selection checkbox for '{label_text}'...")
        checkbox.click()
    else:
        print(f"[INFO] Clicking cell directly for '{label_text}'...")
        cell.click()
        
    page.wait_for_timeout(500) # Give state a moment to sync

    # Check if an OK button exists at the bottom of the dialog and click it
    ok_btn = dialog.locator("button:has-text('OK')").first
    if ok_btn.count() > 0 and ok_btn.is_visible():
        ok_btn.click()
        
    print(f"[SUCCESS] Handled F4 selection for '{label_text}'.")
    # Wait for dialog to close
    dialog.wait_for(state="hidden", timeout=10000)

LABEL_MAP = {
    "bomusage": "BOM Usage",
    "bom_usage": "BOM Usage",
    "bucketcategory": "Bucket Category",
    "bucket_category": "Bucket Category",
    "refdataid": "ID for Reference Data",
    "ref_data_id": "ID for Reference Data",
    "referencedataid": "ID for Reference Data",
    "reference_data_id": "ID for Reference Data",
    "referenceid": "ID for Reference Data",
    "reference_id": "ID for Reference Data",
    "refdatadesc": "Reference Description",
    "ref_data_desc": "Reference Description",
    "referencedescription": "Reference Description",
    "reference_description": "Reference Description",
    "refstartdate": "Start Date of Reference",
    "ref_start_date": "Start Date of Reference",
    "startdateofreference": "Start Date of Reference",
    "start_date_of_reference": "Start Date of Reference",
    "startdate": "Start Date of Reference",
    "start_date": "Start Date of Reference",
    "referencestartdate": "Start Date of Reference",
    "reference_start_date": "Start Date of Reference",
    "startdatereference": "Start Date of Reference",
    "start_date_reference": "Start Date of Reference",
    "refenddate": "End Date of Reference",
    "ref_end_date": "End Date of Reference",
    "enddateofreference": "End Date of Reference",
    "end_date_of_reference": "End Date of Reference",
    "enddate": "End Date of Reference",
    "end_date": "End Date of Reference",
    "referenceenddate": "End Date of Reference",
    "reference_end_date": "End Date of Reference",
    "enddatereference": "End Date of Reference",
    "end_date_reference": "End Date of Reference",
    "simid": "Simulation ID",
    "sim_id": "Simulation ID",
    "simulationid": "Simulation ID",
    "simulation_id": "Simulation ID",
    "simdesc": "Simulation Description",
    "sim_desc": "Simulation Description",
    "simulationdescription": "Simulation Description",
    "simulation_description": "Simulation Description",
    "tasklistusage": "Task List Usage",
    "task_list_usage": "Task List Usage",
    "plant": "Plant",
    "material": "Material"
}

@sap_session_handler
def run_automation(page, sap_url, fields, filter_labels=None):
    if filter_labels is None:
        filter_labels = {}

    # Normalize filter_labels (map camelCase/short names to exact SAP labels)
    normalized_filters = {}
    for k, v in filter_labels.items():
        norm_key = str(k).lower().replace("_", "").replace(" ", "")
        mapped_key = LABEL_MAP.get(norm_key, k)
        normalized_filters[mapped_key] = str(v)
    filter_labels = normalized_filters

    # Default filters if not provided
    if "Task List Usage" not in filter_labels:
        filter_labels["Task List Usage"] = "Description"

    # Normalize fields (map camelCase/short names to exact SAP labels, and auto-convert YYYY-MM-DD dates)
    normalized_fields = {}
    for k, v in fields.items():
        norm_key = str(k).lower().replace("_", "").replace(" ", "")
        mapped_key = LABEL_MAP.get(norm_key, k)
        
        # Auto-convert dates from YYYY-MM-DD to DD.MM.YYYY
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            try:
                parts = v.split("-")
                v = f"{parts[2]}.{parts[1]}.{parts[0]}"
            except Exception:
                pass
        normalized_fields[mapped_key] = str(v)

    # Sort fields strictly to match the exact top-to-bottom UI sequence
    ordered_keys = [
        "ID for Reference Data",
        "Reference Description",
        "Bucket Category",
        "Start Date of Reference",
        "End Date of Reference",
        "Simulation ID",
        "Simulation Description",
        "BOM Usage",
        "Task List Usage",
        "Plant",
        "Material"
    ]
    sorted_fields = {}
    for key in ordered_keys:
        if key in normalized_fields:
            sorted_fields[key] = normalized_fields[key]
    for key, val in normalized_fields.items():
        if key not in sorted_fields:
            sorted_fields[key] = val
    fields = sorted_fields

    # Normalize and map date inputs and dropdown keys to SAP OData format
    ref_id = sorted_fields.get("ID for Reference Data", "")
    ref_desc = sorted_fields.get("Reference Description", "")
    bucket_cat_input = sorted_fields.get("Bucket Category", "Month")
    start_date_input = sorted_fields.get("Start Date of Reference", "")
    end_date_input = sorted_fields.get("End Date of Reference", "")
    sim_id = sorted_fields.get("Simulation ID", "")
    sim_desc = sorted_fields.get("Simulation Description", "")
    bom_use_input = sorted_fields.get("BOM Usage", "1")
    task_list_input = sorted_fields.get("Task List Usage", "Production")
    plant = sorted_fields.get("Plant", "")
    material = sorted_fields.get("Material", "")

    # Mappings
    bucket_cat = "M" if "week" not in bucket_cat_input.lower() else "W"
    
    def convert_date(d_str):
        if not d_str:
            return ""
        if "." in d_str:
            parts = d_str.split(".")
            if len(parts) == 3:
                return f"{parts[2]}{parts[1]}{parts[0]}"
        elif "-" in d_str:
            parts = d_str.split("-")
            if len(parts) == 3:
                return f"{parts[0]}{parts[1]}{parts[2]}"
        return d_str

    start_date = convert_date(start_date_input)
    end_date = convert_date(end_date_input)

    bom_use = "1" if "production" in bom_use_input.lower() or bom_use_input == "1" else bom_use_input
    task_list = "1" if "production" in task_list_input.lower() or task_list_input == "1" else task_list_input

    # Construct the JSON select-options parameters
    # Dynamically build job_params to support comma‑separated multi‑value fields
    def split_vals(val):
        # Split on commas and trim whitespace
        return [p.strip() for p in str(val).split(",") if p.strip()]

    job_params = {"VALUES": []}

    # Helper to add a parameter entry
    def add_param(name, raw):
        parts = split_vals(raw)
        t_vals = [{"SIGN": "I", "OPTION": "EQ", "LOW": p, "HIGH": ""} for p in parts]
        job_params["VALUES"].append({"NAME": name, "T_VALUE": t_vals})

    # Populate parameters
    add_param("P_REFID", ref_id)
    add_param("P_REFD", ref_desc)
    # Bucket category expects M or W – convert if needed
    bucket_val = "M" if "week" not in bucket_cat.lower() else "W"
    add_param("P_BUCK", bucket_val)
    add_param("P_START", start_date)
    add_param("P_END", end_date)
    add_param("P_SIMID", sim_id)
    add_param("P_SIMD", sim_desc)
    add_param("SO_WERKS", plant)
    add_param("SO_MATNR", material)
    add_param("SO_STLAN", bom_use)
    add_param("SO_PLANV", task_list)

    # Constant / empty parameters required by the template
    job_params["VALUES"].append({"NAME": "P_DEL", "T_VALUE": [{"SIGN": "I", "OPTION": "EQ", "LOW": "", "HIGH": ""}]})
    job_params["VALUES"].append({"NAME": "SO_MMSTA", "T_VALUE": []})
    job_params["VALUES"].append({"NAME": "SO_MSTAE", "T_VALUE": []})
    job_params["VALUES"].append({"NAME": "P_TOLER", "T_VALUE": [{"SIGN": "I", "OPTION": "EQ", "LOW": 2, "HIGH": ""}]})

    print("[INFO] Executing API check directly in browser context...", file=sys.stderr)
    import json
    
    js_code = f"""
    (async () => {{
        const jobParamsStr = '{json.dumps(job_params)}';
        const encodedParams = encodeURIComponent(jobParamsStr);
        
        // 1. Fetch CSRF token
        const metadataRes = await fetch("/sap/opu/odata/sap/APJ_JOB_MANAGEMENT_SRV/$metadata?sap-client=100", {{
            headers: {{ "x-csrf-token": "fetch" }}
        }});
        const csrfToken = metadataRes.headers.get("x-csrf-token");
        const metadataXml = await metadataRes.clone().text();
        if (!csrfToken) {{
            return {{ success: false, error: "Failed to fetch CSRF token" }};
        }}
        
        // 2. Fetch Job Template details to get ETag
        const templateRes = await fetch("/sap/opu/odata/sap/APJ_JOB_MANAGEMENT_SRV/JobTemplateSet?sap-client=100&$filter=JobTemplateName eq 'SAP_SCM_PMRP_CREATE_C_DEFAULT'", {{
            headers: {{
                "Accept": "application/json",
                "x-csrf-token": csrfToken
            }}
        }});
        const templateData = await templateRes.json();
        if (!templateData.d || !templateData.d.results || templateData.d.results.length === 0) {{
            return {{ success: false, error: "Failed to fetch Job Template data" }};
        }}
        console.error("METADATA_PARAMS:" + templateData.d.results[0].JobParameterValues);
        const etag = templateData.d.results[0].__metadata.etag;
        
        // 3. Prepare Validation multipart request
        const valBoundary = "batch_val_check";
        const valChangeset = "changeset_val_check";
        const valBody = [
            `--${{valBoundary}}`,
            `Content-Type: multipart/mixed; boundary=${{valChangeset}}`,
            "",
            `--${{valChangeset}}`,
            "Content-Type: application/http",
            "Content-Transfer-Encoding: binary",
            "",
            `POST CheckScheduleJob?sap-client=100&CheckPhase='CANDA'&JobParameterValues='${{encodedParams}}'&JobTemplateName='SAP_SCM_PMRP_CREATE_C_DEFAULT'&ParameterKey='' HTTP/1.1`,
            "sap-cancel-on-close: false",
            "sap-contextid-accept: header",
            "Accept: application/json",
            "Accept-Language: en-US",
            "DataServiceVersion: 2.0",
            "MaxDataServiceVersion: 2.0",
            "X-Requested-With: XMLHttpRequest",
            `x-csrf-token: ${{csrfToken}}`,
            "Content-Type: application/json",
            "Content-ID: id-1",
            "",
            "",
            `--${{valChangeset}}--`,
            `--${{valBoundary}}--`,
            ""
        ].join("\\r\\n");

        const valRes = await fetch("/sap/opu/odata/sap/APJ_JOB_MANAGEMENT_SRV/$batch?sap-client=100", {{
            method: "POST",
            headers: {{
                "Content-Type": `multipart/mixed; boundary=${{valBoundary}}`,
                "x-csrf-token": csrfToken,
                "Accept": "application/json"
            }},
            body: valBody
        }});
        const valText = await valRes.text();

        // 4. Check validation success
        const valSuccessful = !valText.includes('"SuccessfulInd":false');
        if (!valSuccessful) {{
            return {{ success: false, validationFailed: true, valResponse: valText, schedResponse: "", metadataXml: metadataXml }};
        }}

        // 5. If validation passes, construct and execute ScheduleJob PUT batch call
        const schedBoundary = "batch_sched_run";
        const schedChangeset = "changeset_sched_run";
        const now = new Date();
        const timestampStr = now.toISOString().replace("Z", "");
        
        const schedBody = [
            `--${{schedBoundary}}`,
            `Content-Type: multipart/mixed; boundary=${{schedChangeset}}`,
            "",
            `--${{schedChangeset}}`,
            "Content-Type: application/http",
            "Content-Transfer-Encoding: binary",
            "",
            `PUT ScheduleJob?sap-client=100&CalendarId='01'&EndDateTime=datetime'1970-01-01T00%3A00%3A00'&EndMaxIterations=10&EndTypeC=''&JobCatalogEntryName='SAP_SCM_PMRP_CREATE_MATCOMP'&JobTemplateName='SAP_SCM_PMRP_CREATE_C_DEFAULT'&JobText='Creation%20of%20pMRP%20Data%20via%20Components'&PeriodicGranularity=''&PeriodicValue=0&ScheduleTypeCode='I'&StartDateTime=datetime'${{timestampStr}}'&StartRestrictionCode=''&MonthOnlyWorkdaysInd=false&MonthDay=0&WeekDayInfo=''&MonthDayShiftDirection='0'&SchedulingTimezone='INDIA'&WeekNumber=0&SkipCheckAndAdjustInd=true&JobParameterValues='${{encodedParams}}' HTTP/1.1`,
            "sap-cancel-on-close: false",
            "sap-contextid-accept: header",
            "Accept: application/json",
            "Accept-Language: en-US",
            "DataServiceVersion: 2.0",
            "MaxDataServiceVersion: 2.0",
            "X-Requested-With: XMLHttpRequest",
            `x-csrf-token: ${{csrfToken}}`,
            `If-Match: ${{etag}}`,
            "Content-Type: application/json",
            "Content-ID: id-1",
            "",
            "",
            `--${{schedChangeset}}--`,
            `--${{schedBoundary}}--`,
            ""
        ].join("\\r\\n");

        const schedRes = await fetch("/sap/opu/odata/sap/APJ_JOB_MANAGEMENT_SRV/$batch?sap-client=100", {{
            method: "POST",
            headers: {{
                "Content-Type": `multipart/mixed; boundary=${{schedBoundary}}`,
                "x-csrf-token": csrfToken,
                "Accept": "application/json"
            }},
            body: schedBody
        }});
        const schedText = await schedRes.text();
        
        const schedSuccessful = !schedText.includes('"SuccessfulInd":false') && !schedText.includes('"severity":"error"');
        
        return {{ 
            success: schedSuccessful, 
            validationFailed: false,
            valResponse: valText,
            schedResponse: schedText,
            metadataXml: metadataXml
        }};
    }})()
    """
    
    result = page.evaluate(js_code)
    
    metadata_xml = result.get("metadataXml", "")
    if metadata_xml:
        try:
            with open("sap_metadata.xml", "w", encoding="utf-8") as f:
                f.write(metadata_xml)
            print("[INFO] Saved OData metadata to sap_metadata.xml", file=sys.stderr)
        except Exception as e:
            print(f"[WARNING] Failed to save metadata: {e}", file=sys.stderr)

    is_success = result.get("success", False)
    val_resp = result.get("valResponse", "")
    sched_resp = result.get("schedResponse", "")
    
    if not is_success:
        # Handle Validation or Scheduling Failure
        error_details = []
        resp_text = sched_resp if sched_resp else val_resp
        
        # Match sap-message header JSON
        sap_msg_str = ""
        msg_match = re.search(r'sap-message:\s*({.*?})(?:\r?\n|$)', resp_text)
        if msg_match:
            sap_msg_str = msg_match.group(1)
            
        if sap_msg_str:
            try:
                import json
                msg_data = json.loads(sap_msg_str)
                if msg_data.get("message"):
                    error_details.append(f"- **Error:** {msg_data.get('message')}")
                for detail in msg_data.get("details", []):
                    if detail.get("message"):
                        error_details.append(f"- **Detail:** {detail.get('message')}")
            except Exception:
                error_details.append(f"- **Message:** {sap_msg_str}")
        else:
            error_details.append("- **Message:** Validation check failed, but no specific message was returned.")
            
        error_summary = "\n".join(error_details)
        print(f"[WARNING] SAP Action Failed:\n{error_summary}", file=sys.stderr)
        
        # Take verification failure screenshot
        try:
            page.screenshot(path="job_check_failed.png")
        except Exception:
            pass
            
        return f"""# 📋 SAP pMRP Automation Report

### ⚠️ SAP Validation Failed
The parameters were validated, but SAP returned validation errors.

### 🔍 Validation Details:
{error_summary}

> [!WARNING]
> The job was not scheduled. Please adjust your parameters and try again."""

    # Success Flow
    print("[SUCCESS] Job created and scheduled successfully. Capturing confirmation screenshots...", file=sys.stderr)
    
    # 1. Take screenshot of the Application Jobs list showing the scheduled job
    base_url = sap_url.rstrip('/')
    list_url = base_url + "/ui#PMRPSimulation-create"
    print(f"[INFO] Navigating to Application Jobs List: {list_url}", file=sys.stderr)
    page.goto(list_url, wait_until="load", timeout=0)
    page.wait_for_timeout(3000) # Let the table/log records load
    page.screenshot(path="job_scheduled_success.png")
    
    # 2. Take screenshot of the pMRP Simulation Dashboard
    sim_url = "https://my401292.s4hana.cloud.sap/ui#PMRPSimulation-simulate&/?sap-iapp-state=AS99TB9M6NIPS212WLIDXZAZFMU5EJOUSCH0YFU4&sap-iapp-state--history=TAS52YK80GBQW9L57V0V096OXAXDS5RO3QH89PFSA"
    print(f"[INFO] Navigating directly to pMRP Simulation URL: {sim_url}", file=sys.stderr)
    page.goto(sim_url, wait_until="load", timeout=0)
    print("[INFO] Waiting exactly 10 seconds on the pMRP Simulation dashboard...", file=sys.stderr)
    page.wait_for_timeout(10000)
    page.screenshot(path="job_simulated_dashboard.png")
    print("[SUCCESS] Navigated to pMRP Simulation dashboard successfully.", file=sys.stderr)
    
    details_lines = []
    for field_name, field_val in fields.items():
        details_lines.append(f"* **{field_name}:** `{field_val}`")
    details_summary = "\n".join(details_lines)

    return f"""# 📋 SAP pMRP Automation Report

### ✅ Execution Succeeded
The pMRP simulation job has been successfully created, validated, and scheduled in SAP.

### 🔍 Execution Details:
{details_summary}
* **Redirection URL:** Navigated to pMRP Simulation Dashboard successfully."""

if __name__ == "__main__":
    import datetime
    suffix = datetime.datetime.now().strftime("%m%d%H%M%S")
    test_fields = {
        "ID for Reference Data": f"REF_{suffix}",
        "Reference Description": f"REFERENCE {suffix}",
        "Bucket Category": "Month",
        "Start Date of Reference": "01.07.2026",
        "End Date of Reference": "01.10.2026",
        "Simulation ID": f"SIM_{suffix}",
        "Simulation Description": f"PLANNING TEST {suffix}",
        "BOM Usage": "1",
        "Task List Usage": "Production",
        "Plant": "1001",
        "Material": "3381"
    }
    test_filters = {
        "Task List Usage": "Description"
    }
    res = run_automation(fields=test_fields, filter_labels=test_filters)
    print(f"\n[RESULT] {res}")
