import os
import re
import sys
import time
import json
from typing import Optional, Any
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext

# Redefine print globally within this module to write to sys.stderr by default
# This prevents stdout pollution which corrupts the FastMCP JSON-RPC communication channel
def print(*args, **kwargs):
    kwargs.setdefault('file', sys.stderr)
    import builtins
    builtins.print(*args, **kwargs)

# Central Configuration Settings
CONFIG = {
    "POLL_INTERVAL": 5,                 # Polling interval for simulation calculation (seconds)
    "MAX_SIMULATION_WAIT": 150,        # Max timeout for simulation calculation (seconds)
    "ERROR_RED_R": 150,                # Fallback red threshold for R channel
    "ERROR_RED_G": 50,                 # Fallback green threshold
    "ERROR_RED_B": 50,                 # Fallback blue threshold
    "MAX_PIR_WAIT": 90,                # Max wait time for PIR MD61 table (seconds)
    "MAX_RELEASE_WAIT": 60,            # Max wait time for release action (seconds)
}

def log_stage(stage_num: int, total_stages: int, message: str):
    print(f"\n[INFO] [{stage_num}/{total_stages}] {message}", file=sys.stderr)

def smart_logout(page, base_url):
    """URL-based SAP session termination."""
    try:
        print("[INFO] Signing out...", file=sys.stderr)
        base_url = base_url.rstrip('/')
        page.goto(f"{base_url}/sap/public/bc/icf/logoff?sap-client=100", timeout=10000)
        print("[INFO] Session closed.", file=sys.stderr)
    except Exception as e:
        print(f"[WARNING] Logout failed: {e}", file=sys.stderr)

def sap_session_handler(func):
    """Decorator to manage Playwright startup, direct target navigation, login, and error capturing."""
    def wrapper(*args, **kwargs):
        load_dotenv()
        sap_url = os.getenv("SAP_URL")
        
        # Check if an active page is passed to reuse browser context
        if "page" in kwargs and kwargs["page"] is not None:
            page = kwargs.pop("page")
            return func(page, sap_url, *args, **kwargs)
            
        sap_email = os.getenv("SAP_EMAIL")
        sap_password = os.getenv("SAP_PASSWORD")

        if not all([sap_url, sap_email, sap_password]):
            raise Exception("Missing credentials in .env file.")

        base_url = sap_url.rstrip('/')
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
                
                print("[INFO] Waiting for page determination...", file=sys.stderr)
                login_selector = page.locator("#j_username")
                app_selector = page.locator("#application-PMRPSimulation-create-component---JobRunList--appJobsOverviewAddJobButton-BDI-content, label:has-text('Job Template')").first
                
                try:
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
                
                return func(page, sap_url, *args, **kwargs)
            except Exception as e:
                print(f"[ERROR] Automation failed: {e}", file=sys.stderr)
                try:
                    page.screenshot(path="automation_error.png")
                    print("[INFO] Saved failure screenshot as 'automation_error.png'", file=sys.stderr)
                except Exception as se:
                    print(f"[ERROR] Could not save screenshot: {se}", file=sys.stderr)
                return f"""# SAP pMRP Automation Report

### Automation Error Encountered
An error occurred during the execution of the browser automation script.

### Error Details:
* **Error Message:** `{e}`

> [!IMPORTANT]
> The automation was stopped. A failure screenshot has been saved to the host directory as `automation_error.png` for diagnostic purposes."""
            finally:
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
    """Clicks F4 help button, searches for value, selects it, and closes dialog."""
    print(f"[INFO] Using F4 Help for '{label_text}' -> typing search query '{search_value}'...")
    
    _, vhi = get_field_elements(page, label_text)
    vhi.wait_for(state="visible", timeout=15000)
    
    dialog = page.locator("div.sapMDialog:visible").first
    
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
        
        page.wait_for_timeout(300)
    
    cell_selector = "[role='gridcell'], .sapMListTblCell"
    cell = dialog.locator(cell_selector).filter(has_text=search_value).first
    cell.wait_for(state="visible", timeout=15000)
        
    row = cell.locator("xpath=ancestor::tr[1] | ancestor::li[1]")
    checkbox = row.locator("div.sapMCb, [role='checkbox']").first
    
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

    if checkbox.count() > 0 and checkbox.is_visible():
        print(f"[INFO] Clicking selection checkbox for '{label_text}'...")
        checkbox.click()
    else:
        print(f"[INFO] Clicking cell directly for '{label_text}'...")
        cell.click()
        
    page.wait_for_timeout(500)

    ok_btn = dialog.locator("button:has-text('OK')").first
    if ok_btn.count() > 0 and ok_btn.is_visible():
        ok_btn.click()
        
    print(f"[SUCCESS] Handled F4 selection for '{label_text}'.")
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

    normalized_filters = {}
    for k, v in filter_labels.items():
        norm_key = str(k).lower().replace("_", "").replace(" ", "")
        mapped_key = LABEL_MAP.get(norm_key, k)
        normalized_filters[mapped_key] = str(v)
    filter_labels = normalized_filters

    if "Task List Usage" not in filter_labels:
        filter_labels["Task List Usage"] = "Description"

    normalized_fields = {}
    for k, v in fields.items():
        norm_key = str(k).lower().replace("_", "").replace(" ", "")
        mapped_key = LABEL_MAP.get(norm_key, k)
        
        if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            try:
                parts = v.split("-")
                v = f"{parts[2]}.{parts[1]}.{parts[0]}"
            except Exception:
                pass
        normalized_fields[mapped_key] = str(v)

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
    print(f"[INFO] run_automation resolved fields: ref_id={ref_id}, bucket_cat={bucket_cat_input}, start_date={start_date_input}, end_date={end_date_input}, sim_id={sim_id}, plant={plant}, material={material}", file=sys.stderr)

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

    def split_vals(val):
        return [p.strip() for p in str(val).split(",") if p.strip()]

    job_params = {"VALUES": []}

    def add_param(name, raw):
        parts = split_vals(raw)
        t_vals = [{"SIGN": "I", "OPTION": "EQ", "LOW": p, "HIGH": ""} for p in parts]
        job_params["VALUES"].append({"NAME": name, "T_VALUE": t_vals})

    add_param("P_REFID", ref_id)
    add_param("P_REFD", ref_desc)
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

    p_del_val = "X" if str(sorted_fields.get("Delete Existing pMRP Data", "")).strip().lower() in ["true", "x", "1"] else ""
    job_params["VALUES"].append({"NAME": "P_DEL", "T_VALUE": [{"SIGN": "I", "OPTION": "EQ", "LOW": p_del_val, "HIGH": ""}]})
    job_params["VALUES"].append({"NAME": "SO_MMSTA", "T_VALUE": []})
    job_params["VALUES"].append({"NAME": "SO_MSTAE", "T_VALUE": []})
    job_params["VALUES"].append({"NAME": "P_TOLER", "T_VALUE": [{"SIGN": "I", "OPTION": "EQ", "LOW": 2, "HIGH": ""}]})

    print("[INFO] Executing API check directly in browser context...", file=sys.stderr)
    import json
    
    js_code = f"""
    (async () => {{
        const jobParamsStr = '{json.dumps(job_params)}';
        const encodedParams = encodeURIComponent(jobParamsStr);
        
        const metadataRes = await fetch("/sap/opu/odata/sap/APJ_JOB_MANAGEMENT_SRV/$metadata?sap-client=100", {{
            headers: {{ "x-csrf-token": "fetch" }}
        }});
        const csrfToken = metadataRes.headers.get("x-csrf-token");
        const metadataXml = await metadataRes.clone().text();
        if (!csrfToken) {{
            return {{ success: false, error: "Failed to fetch CSRF token" }};
        }}
        
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
        const etag = templateData.d.results[0].__metadata.etag;
        
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

        const valSuccessful = !valText.includes('"SuccessfulInd":false');
        if (!valSuccessful) {{
            return {{ success: false, validationFailed: true, valResponse: valText, schedResponse: "", metadataXml: metadataXml }};
        }}

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
        error_details = []
        resp_text = sched_resp if sched_resp else val_resp
        
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
        
        try:
            page.screenshot(path="job_check_failed.png")
        except Exception:
            pass
            
        return f"""# SAP pMRP Automation Report

### SAP Validation Failed
The parameters were validated, but SAP returned validation errors.

### Validation Details:
{error_summary}

> [!WARNING]
> The job was not scheduled. Please adjust your parameters and try again."""

    print("[SUCCESS] Job created and scheduled successfully. Capturing confirmation screenshots...", file=sys.stderr)
    
    base_url = sap_url.rstrip('/')
    list_url = base_url + "/ui#PMRPSimulation-create"
    print(f"[INFO] Navigating to Application Jobs List: {list_url}", file=sys.stderr)
    page.goto(list_url, wait_until="load", timeout=0)
    page.wait_for_timeout(3000)
    page.screenshot(path="job_scheduled_success.png")
    
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

    return f"""# SAP pMRP Automation Report

### Execution Succeeded
The pMRP simulation job has been successfully created, validated, and scheduled in SAP.

### Execution Details:
{details_summary}
* **Redirection URL:** Navigated to pMRP Simulation Dashboard successfully."""

def ensure_logged_in(page: Page, context: BrowserContext, target_url: str):
    load_dotenv()
    sap_email = os.getenv("SAP_EMAIL")
    sap_password = os.getenv("SAP_PASSWORD")
    state_path = "sap_session_state.json"
    
    print(f"[INFO] Navigating to {target_url}...", file=sys.stderr)
    page.goto(target_url, wait_until="domcontentloaded", timeout=0)
    
    print("[INFO] Waiting for session/page determination...", file=sys.stderr)
    # Wait for either username input, shell header, user settings button or iframe
    locator = page.locator("#j_username, #shell-header, #userActionsMenuHeaderButton, iframe[src*='webgui'], [id*='shell-header']")
    try:
        locator.first.wait_for(state="visible", timeout=45000)
    except Exception as e:
        print(f"[WARNING] Settle timeout reached: {e}", file=sys.stderr)
    
    # Check explicitly which elements are visible to decide path
    is_login = page.locator("#j_username").first.is_visible()
    is_logged_in = page.locator("#shell-header, #userActionsMenuHeaderButton, [id*='shell-header']").first.is_visible()
    
    if is_login:
        print("[INFO] Session expired or invalid. Performing login...", file=sys.stderr)
        page.locator("#j_username").fill(sap_email)
        page.locator("#j_password").fill(sap_password)
        page.locator(".fn-button__text", has_text="Continue").click()
        print("[INFO] Clicked login Continue button. Waiting for shell header...", file=sys.stderr)
        
        shell_header_selector = "#shell-header, #userActionsMenuHeaderButton, [id*='shell-header']"
        page.locator(shell_header_selector).first.wait_for(state="visible", timeout=60000)
        
        context.storage_state(path=state_path)
        print(f"[SUCCESS] Login successful! Session state saved to {state_path}.", file=sys.stderr)
        page.wait_for_timeout(5000)
    elif is_logged_in:
        print("[INFO] Active session detected. Proceeding...", file=sys.stderr)
    else:
        # Neither is visible, meaning page failed to load or has major connection issues
        raise Exception("Failed to load page. Neither login screen nor SAP application elements are visible. Title: " + page.title())

def ensure_session(force=False):
    load_dotenv()
    sap_url = os.getenv("SAP_URL")
    state_path = "sap_session_state.json"
    
    if not force and os.path.exists(state_path):
        print("[INFO] Session state already exists. Skipping pre-warming.", file=sys.stderr)
        return
        
    print("[INFO] Pre-warming session state...", file=sys.stderr)
    target_url = sap_url.rstrip('/') + "/ui"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            ensure_logged_in(page, context, target_url)
            print("[SUCCESS] Session pre-warmed successfully.", file=sys.stderr)
        except Exception as e:
            print(f"[WARNING] Session pre-warming failed: {e}", file=sys.stderr)
        finally:
            browser.close()

def parse_period_string(date_str: str, planning_period: str) -> str:
    import datetime
    date_str = str(date_str).strip()
    parsed_date = None
    
    # 1. MM.YYYY or M.YYYY
    m = re.match(r'^(\d{1,2})\.(\d{4})$', date_str)
    if m:
        parsed_date = datetime.date(int(m.group(2)), int(m.group(1)), 1)
    
    # 2. YYYY-MM-DD
    if not parsed_date:
        m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', date_str)
        if m:
            parsed_date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            
    # 3. MM/YYYY or M/YYYY
    if not parsed_date:
        m = re.match(r'^(\d{1,2})/(\d{4})$', date_str)
        if m:
            parsed_date = datetime.date(int(m.group(2)), int(m.group(1)), 1)
            
    # 4. Words like "June 2026" or "Jun 2026"
    if not parsed_date:
        for fmt in ["%B %Y", "%b %Y", "%B, %Y", "%b, %Y"]:
            try:
                dt = datetime.datetime.strptime(date_str, fmt)
                parsed_date = dt.date()
                break
            except Exception:
                pass
                
    if parsed_date:
        if planning_period == "M":
            return f"M {parsed_date.month:02d}.{parsed_date.year}"
        elif planning_period == "W":
            iso_yr, iso_wk, _ = parsed_date.isocalendar()
            return f"W {iso_wk:02d}.{iso_yr}"
        else: # Day
            return f"D {parsed_date.day:02d}.{parsed_date.month:02d}.{parsed_date.year}"
            
    # 5. Direct week like WW.YYYY or WW/YYYY or W28 2026
    m = re.match(r'^(?:W)?(\d{1,2})[-/.](\d{4})$', date_str, re.IGNORECASE)
    if m and planning_period == "W":
        return f"W {int(m.group(1)):02d}.{m.group(2)}"
        
    return date_str.upper()

def create_pir_automation(
    material: str,
    plant: str,
    requirements_plan: str = "",
    mrp_area: str = "",
    version: str = "00",
    planning_period: str = "M",
    start_date: str = "",
    end_date: str = "",
    demands: list = None,
    page = None
) -> str:
    load_dotenv()
    sap_url = os.getenv("SAP_URL")
    target_url = sap_url.rstrip('/') + "/ui#ForecastDemand-create?sap-ui-tech-hint=GUI"
    state_path = "sap_session_state.json"
    
    if not demands:
        demands = []
        
    print(f"[INFO] Running create_pir_automation for material {material}, plant {plant}...", file=sys.stderr)
    
    planning_period = str(planning_period).upper().strip()
    
    if not start_date or not end_date:
        import datetime
        parsed_dates = []
        for d in demands:
            date_str = d.get("date") or d.get("period") or d.get("month") or d.get("week") or d.get("day")
            if not date_str:
                continue
            date_str = str(date_str).strip()
            
            m = re.match(r'^(\d{1,2})\.(\d{4})$', date_str)
            if m:
                parsed_dates.append(datetime.date(int(m.group(2)), int(m.group(1)), 1))
                continue
            m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', date_str)
            if m:
                parsed_dates.append(datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
                continue
            m = re.match(r'^(\d{1,2})/(\d{4})$', date_str)
            if m:
                parsed_dates.append(datetime.date(int(m.group(2)), int(m.group(1)), 1))
                continue
            try:
                dt = datetime.datetime.strptime(date_str, "%B %Y")
                parsed_dates.append(dt.date())
                continue
            except Exception:
                pass
            try:
                dt = datetime.datetime.strptime(date_str, "%b %Y")
                parsed_dates.append(dt.date())
                continue
            except Exception:
                pass
                
        if parsed_dates:
            min_date = min(parsed_dates)
            max_date = max(parsed_dates)
            if not start_date:
                start_date = f"01.{min_date.month:02d}.{min_date.year}"
            if not end_date:
                import calendar
                last_day = calendar.monthrange(max_date.year, max_date.month)[1]
                end_date = f"{last_day:02d}.{max_date.month:02d}.{max_date.year}"
        else:
            if not start_date:
                start_date = "01.06.2026"
            if not end_date:
                end_date = "31.12.2026"
                
    print(f"[INFO] Calculated dates: start_date={start_date}, end_date={end_date}", file=sys.stderr)
    
    def run_with_page(active_page):
        ensure_logged_in(active_page, active_page.context, target_url)
        
        frame_obj = None
        for attempt in range(25):
            for f in active_page.frames:
                if "webgui" in f.url:
                    frame_obj = f
                    break
            if frame_obj:
                break
            active_page.wait_for_timeout(2000)
            
        if not frame_obj:
            frame_urls = [f.url for f in active_page.frames]
            raise Exception(f"WebGUI frame not found! Available frame URLs: {frame_urls}")
            
        print("[INFO] Filling initial screen fields...", file=sys.stderr)
        material_input = frame_obj.locator('input[title="Material Number"]').first
        material_input.wait_for(state="visible", timeout=90000)
        material_input.fill(material)
        frame_obj.locator('input[title="Plant"]').fill(plant)
        
        req_plan_input = frame_obj.locator('input[title="Requirements Plan"]')
        if requirements_plan and req_plan_input.count() > 0:
            req_plan_input.fill(requirements_plan)
            
        mrp_area_input = frame_obj.locator('input[title="MRP Area"]')
        if mrp_area and mrp_area_input.count() > 0:
            mrp_area_input.fill(mrp_area)
            
        version_input = frame_obj.locator('input[title="Version"]')
        if version and version_input.count() > 0:
            version_input.fill(version)
            
        frame_obj.locator('input[title="Start of the Period to Be Evaluated"]').fill(start_date)
        frame_obj.locator('input[title="End of the Period of Examination"]').fill(end_date)
        frame_obj.locator('input[title="Period indicator (day, week, month, posting period)"]').fill(planning_period)
        
        print("[INFO] Navigating to Planning Table...", file=sys.stderr)
        frame_obj.locator('input[title="Material Number"]').press("Enter")
        active_page.wait_for_timeout(5000)
        
        for f in active_page.frames:
            if "webgui" in f.url:
                frame_obj = f
                break
        
        status_text = ""
        status_el = frame_obj.locator('#wnd\\[0\\]\\/sbar_msg-txt, .lsStatusBar-text').first
        if status_el.count() > 0:
            status_text = status_el.inner_text()
            print(f"[INFO] Status bar text: '{status_text}'", file=sys.stderr)
            if "blocked" in status_text.lower() or "locked" in status_text.lower():
                if os.path.exists(state_path):
                    try:
                        os.remove(state_path)
                        print("[INFO] Deleted cached session state due to lock/block.", file=sys.stderr)
                    except Exception:
                        pass
                raise Exception(f"Material or requirement is blocked/locked: {status_text}")
        
        prefix_info = frame_obj.evaluate("""
        () => {
            const cells = Array.from(document.querySelectorAll('[id*="[1,1]_c"]'));
            if (cells.length > 0) {
                return cells[0].id.split('[')[0];
            }
            const any_cell = Array.from(document.querySelectorAll('[id*="[1,"]'));
            if (any_cell.length > 0) {
                const match = any_cell[0].id.match(/(M0:[^\[]+)\[/);
                if (match) return match[1];
            }
            return null;
        }
        """)
        
        if not prefix_info:
            raise Exception("Table cells not found. Cannot determine table prefix.")
            
        headers_map = frame_obj.evaluate("""
        (prefix) => {
            const map = {};
            const headers = Array.from(document.querySelectorAll('[id^="' + prefix + '[0,"]'));
            headers.forEach(h => {
                const id = h.id;
                const parts = id.split('[0,');
                if (parts.length > 1) {
                    const col_idx = parts[1].split(']')[0];
                    const text = h.textContent.trim();
                    const match = text.match(/([MWD]\\s+\\d{2}\\.\\d{2,4})/i) || text.match(/(\\d{2}\\.\\d{2}\\.\\d{4})/);
                    if (match) {
                        map[match[0].toUpperCase()] = col_idx;
                    } else {
                        map[text.toUpperCase()] = col_idx;
                    }
                }
            });
            return map;
        }
        """, prefix_info)
        
        print(f"[INFO] Extracted Headers map: {headers_map}", file=sys.stderr)
        
        for item in demands:
            date_val = item.get("date") or item.get("period") or item.get("month") or item.get("week") or item.get("day")
            qty = item.get("quantity") or item.get("qty")
            if date_val is None or qty is None:
                continue
            
            period_key = parse_period_string(date_val, planning_period).upper()
            
            matched_col = None
            for h_key, col_idx in headers_map.items():
                h_key_clean = h_key.strip().upper()
                if h_key_clean and (period_key in h_key_clean or h_key_clean in period_key):
                    matched_col = col_idx
                    break
            
            if matched_col is None:
                print(f"[WARNING] Period key '{period_key}' not found in headers map. Skipping...", file=sys.stderr)
                continue
                
            cell_id = f"{prefix_info}[1,{matched_col}]_c"
            print(f"[INFO] Entering quantity {qty} into cell {cell_id} for period {period_key}...", file=sys.stderr)
            
            entered = False
            for attempt in range(5):
                try:
                    for f in active_page.frames:
                        if "webgui" in f.url:
                            frame_obj = f
                            break
                    
                    cell_locator = frame_obj.locator(f'[id="{cell_id}"]').first
                    cell_locator.scroll_into_view_if_needed(timeout=5000)
                    cell_locator.click(force=True, timeout=5000)
                    active_page.wait_for_timeout(300)
                    
                    active_page.keyboard.press("Control+A")
                    active_page.wait_for_timeout(100)
                    active_page.keyboard.press("Backspace")
                    active_page.wait_for_timeout(100)
                    active_page.keyboard.type(str(qty))
                    active_page.wait_for_timeout(100)
                    active_page.keyboard.press("Enter")
                    active_page.wait_for_timeout(1500)
                    entered = True
                    break
                except Exception as cell_err:
                    print(f"[WARNING] Cell entry failed (attempt {attempt+1}): {cell_err}. Retrying...", file=sys.stderr)
                    active_page.wait_for_timeout(2000)
                    
            if not entered:
                raise Exception(f"Failed to enter quantity into cell {cell_id} after 5 attempts.")
            
        active_page.screenshot(path="md61_grid_populated.png")
        
        print("[INFO] Saving planning table...", file=sys.stderr)
        save_locator = frame_obj.locator('div[title=" (Ctrl+S)"], [id$="btn[11]"], [id$="btn[11]-r"]').first
        save_locator.click(force=True)
        active_page.wait_for_timeout(5000)
        
        final_status = ""
        if status_el.count() > 0:
            final_status = status_el.inner_text()
        print(f"[SUCCESS] Final status message: '{final_status}'", file=sys.stderr)
        active_page.screenshot(path="md61_grid_saved.png")
        
        if "saved" in final_status.lower() or "success" in final_status.lower() or final_status == "":
            return f"PIR created/updated successfully. Status: {final_status}"
        else:
            raise Exception(f"Failed to save PIR: {final_status}")

    # Reusing page
    if page is not None:
        try:
            return run_with_page(page)
        except Exception as e:
            print(f"[ERROR] create_pir_automation failed: {e}", file=sys.stderr)
            try:
                page.screenshot(path="md61_error.png")
                for f in page.frames:
                    if "webgui" in f.url:
                        cancel_btn = f.locator('[id$="btn[12]"], [title="Cancel (F12)"]').first
                        if cancel_btn.count() > 0 and cancel_btn.is_visible():
                            cancel_btn.click(force=True)
                            page.wait_for_timeout(2000)
            except Exception:
                pass
            raise e

    # Default standalone run (spins up its own browser)
    with sync_playwright() as p:
        headless = os.getenv("SAP_HEADLESS", "true").lower() == "true"
        browser = p.chromium.launch(headless=headless, args=[] if headless else ["--start-maximized"])
        context_args = {}
        if os.path.exists(state_path):
            context_args["storage_state"] = state_path
        if not headless:
            context_args["no_viewport"] = True
        context = browser.new_context(**context_args)
        page_local = context.new_page()
        try:
            return run_with_page(page_local)
        except Exception as e:
            print(f"[ERROR] create_pir_automation failed: {e}", file=sys.stderr)
            try:
                page_local.screenshot(path="md61_error.png")
                for f in page_local.frames:
                    if "webgui" in f.url:
                        cancel_btn = f.locator('[id$="btn[12]"], [title="Cancel (F12)"]').first
                        if cancel_btn.count() > 0 and cancel_btn.is_visible():
                            cancel_btn.click(force=True)
                            page_local.wait_for_timeout(2000)
            except Exception:
                pass
            raise e
        finally:
            try:
                smart_logout(page_local, target_url)
            except Exception:
                pass
            browser.close()

def dismiss_communication_error(page: Page):
    try:
        dialog = page.locator('div[role="dialog"]').first
        if dialog.is_visible():
            close_btn = dialog.locator('button:has-text("Close")').first
            if close_btn.is_visible():
                print("[INFO] Dismissing SAP Communication error dialog...", file=sys.stderr)
                close_btn.click(force=True)
                page.wait_for_timeout(2000)
    except Exception:
        pass

def get_detailed_issues(page: Page) -> list[dict]:
    issues_list = []
    import sys
    try:
        # Navigate directly using the switcher menu
        header_btn = page.locator('[id="simulationViewMenuButtonId-internalBtn"]')
        table_select = page.locator('[id*="perspectiveSwitcherId-labelText"], [id*="perspectiveSwitcherId"]')
        
        switcher = None
        is_select = False
        
        if header_btn.count() > 0 and header_btn.is_visible():
            switcher = header_btn
            is_select = False
        elif table_select.count() > 0 and table_select.first.is_visible():
            switcher = table_select.first
            is_select = True
            
        if switcher:
            switcher.click(force=True)
            page.wait_for_timeout(300)
            
            if is_select:
                issue_item = page.locator('.sapMSelectListItemText:has-text("Issue Worklist"), .sapMSelectListItemText:has-text("Issue List"), .sapMSelectListItem:has-text("Issue Worklist"), li:has-text("Issue Worklist")').first
            else:
                issue_item = page.locator('.sapMMenuItem:has-text("Issue Worklist"), .sapMMenuItemText:has-text("Issue Worklist"), .sapMMenuItem:has-text("Issue List")').first
                
            try:
                issue_item.wait_for(state="visible", timeout=4000)
            except Exception:
                # Fallback to search both
                issue_item = page.locator('.sapMMenuItem:has-text("Issue Worklist"), .sapMMenuItemText:has-text("Issue Worklist"), .sapMSelectListItemText:has-text("Issue Worklist"), li:has-text("Issue Worklist"), .sapMMenuItem:has-text("Issue List")').first
                issue_item.wait_for(state="visible", timeout=6000)
                
            print("[INFO] Navigating to Issue Worklist to extract detailed issues...", file=sys.stderr)
            issue_item.click(force=True)
            
            # Wait dynamically for Issue Worklist table/rows
            table_row = page.locator('tbody.sapMListItems tr.sapMListTblRow, tr.sapMListTblRow').first
            table_row.wait_for(state="attached", timeout=12000)
            page.wait_for_timeout(500)
            
            # Extract all issues
            rows = page.locator('tbody.sapMListItems tr.sapMListTblRow, tr.sapMListTblRow').all()
            print(f"[INFO] Found {len(rows)} issue rows in Issue Worklist", file=sys.stderr)
            for r in rows:
                cells = r.locator('td.sapMListTblCell').all()
                if len(cells) >= 6:
                    cat = cells[0].text_content().strip()
                    obj = cells[1].text_content().strip().replace('\n', ' ')
                    months = cells[2].text_content().strip()
                    demands = cells[3].text_content().strip()
                    score = cells[4].text_content().strip()
                    overload = cells[5].text_content().strip()
                    cause = cells[6].text_content().strip() if len(cells) > 6 else ""
                    
                    issues_list.append({
                        "Category": cat,
                        "Object": obj,
                        "Affected Months": months,
                        "Affected Demands": demands,
                        "Impact Score": score,
                        "Overload %": overload,
                        "Cause": cause
                    })
            
            # Switch back to Demand Plan Simulation (or Simulation)
            if switcher:
                switcher.click(force=True)
                page.wait_for_timeout(300)
                
                if is_select:
                    sim_item = page.locator('.sapMSelectListItemText:has-text("Demand Plan Simulation"), .sapMSelectListItemText:has-text("Simulation"), .sapMSelectListItem:has-text("Demand Plan Simulation"), li:has-text("Demand Plan Simulation")').first
                else:
                    sim_item = page.locator('.sapMMenuItem:has-text("Demand Plan Simulation"), .sapMMenuItemText:has-text("Demand Plan Simulation"), .sapMMenuItem:has-text("Simulation")').first
                    
                try:
                    sim_item.wait_for(state="visible", timeout=4000)
                except Exception:
                    sim_item = page.locator('.sapMMenuItem:has-text("Demand Plan Simulation"), .sapMMenuItemText:has-text("Demand Plan Simulation"), .sapMSelectListItemText:has-text("Demand Plan Simulation"), .sapMSelectListItem:has-text("Demand Plan Simulation"), li:has-text("Demand Plan Simulation"), .sapMMenuItem:has-text("Simulation")').first
                    sim_item.wait_for(state="visible", timeout=6000)
                    
                sim_item.click(force=True)
            
            # Wait for main simulation table
            page.locator('[id*="simulationTableId"]').first.wait_for(state="attached", timeout=12000)
            page.wait_for_timeout(500)
    except Exception as e:
        print(f"[WARNING] Failed to extract detailed issues: {e}", file=sys.stderr)
    return issues_list


def get_simulation_kpis(page: Page, wait_for_load: bool = True, extract_details: bool = True) -> dict:
    import re
    if wait_for_load:
        # Wait for the first KPI to be attached in the DOM
        page.locator('[id*="Capacity_Issues::NumberOfCapacityIssues::Value"]').first.wait_for(state="attached", timeout=30000)
        
        # Wait for value to load (not empty)
        for _ in range(15):
            val = page.locator('[id*="Capacity_Issues::NumberOfCapacityIssues::Value"]').first.text_content()
            if val and val.strip() != "":
                break
            page.wait_for_timeout(1000)
    
    kpis = {}
    selectors = {
        "Capacity Issues": '[id*="Capacity_Issues::NumberOfCapacityIssues::Value"]',
        "Delivery Performance": '[id*="Delivery_Performance::TopLvlDmndUndrdelivRatioInPct::Value"]',
        "Invalid Source": '[id*="Invalid_SoS::NumberOfProductsWithMissingSOS::Value"]',
        "Violated Constraints": '[id*="Supplier_Issues::PMRPNumberOfConstraintIssues::Value"]'
    }
    for key, selector in selectors.items():
        try:
            val = page.locator(selector).first.text_content()
            kpis[key] = val.strip() if val else "0"
        except Exception:
            kpis[key] = "unknown"
            
    # Count visible red inputs
    try:
        red_count = page.locator(".sapMInputBaseContentWrapperError input").count()
        kpis["Red Input Cells"] = str(red_count)
    except Exception:
        kpis["Red Input Cells"] = "0"
        
    # Extract any visible alert or message strips on the page
    page_msgs = []
    try:
        selectors_msg = [
            ".sapMMessageStripText", 
            "div[role='alert']", 
            ".sapMMessageToast", 
            ".sapMessageBarText",
            ".sapMBarChildText"
        ]
        for sel in selectors_msg:
            locs = page.locator(sel)
            for i in range(locs.count()):
                txt = locs.nth(i).text_content()
                if txt and txt.strip():
                    cleaned = txt.strip().replace("\n", " ")
                    if cleaned not in page_msgs:
                        page_msgs.append(cleaned)
    except Exception:
        pass
    kpis["Page Messages"] = page_msgs
        
    # Extract detailed issues if there are capacity issues
    kpis["Detailed Issues"] = []
    try:
        cap_val = int(re.sub(r'\D', '', kpis.get("Capacity Issues", "0")))
    except Exception:
        cap_val = 0
        
    if cap_val > 0 and extract_details:
        kpis["Detailed Issues"] = get_detailed_issues(page)
        
    return kpis

def wait_for_simulation_ready(page: Page, sim_url: str, fast_kpi_only: bool = False):
    print("[INFO] Waiting for simulation data to be fully calculated and loaded...", file=sys.stderr)
    poll_interval = CONFIG["POLL_INTERVAL"]
    max_wait = CONFIG["MAX_SIMULATION_WAIT"]
    attempts = max(1, int(max_wait / poll_interval))
    
    for attempt in range(attempts):
        if attempt > 0:
            print(f"[INFO] Simulation is still processing (attempt {attempt+1}/{attempts}). Waiting {poll_interval} seconds...", file=sys.stderr)
            page.wait_for_timeout(poll_interval * 1000)
            page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
            page.wait_for_timeout(2000)
        else:
            # First attempt: page was just loaded by the caller, wait a bit for initial rendering
            page.wait_for_timeout(2000)
        
        try:
            page.locator('[id*="Capacity_Issues::NumberOfCapacityIssues::Value"]').first.wait_for(state="attached", timeout=5000)
            val = page.locator('[id*="Capacity_Issues::NumberOfCapacityIssues::Value"]').first.text_content()
            if val and val.strip() != "" and "Empty" not in val and "empty" not in val.lower():
                if fast_kpi_only:
                    print(f"[INFO] Simulation header ready (fast mode)! Capacity Issues: {val.strip()}", file=sys.stderr)
                    return
                print("[INFO] Simulation header ready. Waiting for table data...", file=sys.stderr)
                for _ in range(15):
                    try:
                        is_busy = page.locator('[id*="simulationTableId"]').first.evaluate("el => el.getAttribute('aria-busy') === 'true' || el.classList.contains('sapUiTableBusy')")
                        if not is_busy:
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                for _ in range(15):
                    try:
                        input_val = page.locator('.sapMInputBaseInner').first.evaluate("el => el.value")
                        if input_val and input_val.strip() != "":
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                page.wait_for_timeout(2000)
                print(f"[INFO] Simulation data and table are fully loaded! Capacity Issues: {val.strip()}", file=sys.stderr)
                return
        except Exception:
            pass
        
    raise Exception(f"Simulation data failed to calculate/load within {max_wait} seconds.")

def run_demand_shifting(page: Page) -> bool:
    print("[INFO] Skipping demand shifting as requested by user.", file=sys.stderr)
    return False

# def run_capacity_adaptation(page: Page):


#     print("[INFO] Running capacity adaptation flow from main dashboard...", file=sys.stderr)
#     sim_url = page.url
#     print(f"[INFO] Main simulation URL stored: {sim_url}", file=sys.stderr)
    
#     for iteration in range(3):
#         # Scroll the main dashboard table to the right to make sure November/December columns render!
#         print("[INFO] Scrolling main dashboard table to the right...", file=sys.stderr)
#         page.evaluate("""
#         () => {
#             const scrollables = Array.from(document.querySelectorAll('*')).filter(el => el.scrollWidth > el.clientWidth);
#             scrollables.forEach(el => el.scrollLeft = el.scrollWidth);
#         }
#         """)
#         page.wait_for_timeout(200)
        
#         # 1. Find red input or status cells on the main dashboard
#         red_inputs = page.locator(".sapMObjStatusError, .sapMValueStateError, .sapMInputBaseContentWrapperError input")
#         count = red_inputs.count()
#         print(f"[INFO] Iteration {iteration+1}: Found {count} red elements on main dashboard", file=sys.stderr)
#         if count == 0:
#             print("[INFO] No more red elements found on main dashboard. Adaptation complete.", file=sys.stderr)
#             break
            
#         # Navigate directly using the switcher menu if not already on the Capacity Plan Simulation view
#         tbl = page.locator('[id*="unGroupTableId"]:visible').first
#         if tbl.count() == 0:
#             print("[INFO] Capacity Plan table not visible. Navigating via switcher...", file=sys.stderr)
#             switcher = page.locator('[id="simulationViewMenuButtonId-internalBtn"], [id*="perspectiveSwitcherId-labelText"], [id*="perspectiveSwitcherId"]').first
#             switcher.wait_for(state="visible", timeout=15000)
#             switcher.click(force=True)
#             page.wait_for_timeout(300)
            
#             sim_item = page.locator('.sapMMenuItem:has-text("Capacity Plan Simulation"), .sapMMenuItemText:has-text("Capacity Plan Simulation"), .sapMSelectListItemText:has-text("Capacity Plan Simulation"), li:has-text("Capacity Plan Simulation")').first
#             sim_item.wait_for(state="visible", timeout=15000)
#             sim_item.click(force=True)
#         else:
#             print("[INFO] Already on Capacity Plan Simulation view.", file=sys.stderr)
            
#         # Wait dynamically for the Capacity Plan table to load
#         page.locator('[id*="unGroupTableId"]:visible').first.wait_for(state="visible", timeout=20000)
#         page.wait_for_timeout(1500)
        
#         # Define table scan JS function using direct SAP UI5 Element registry lookup
#         scan_js = """
#         (cfg) => {
#             // Find the visible unGroupTableId container
#             const visibleTable = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
#                 const style = window.getComputedStyle(el);
#                 return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetHeight > 0;
#             });
#             if (!visibleTable) return { found: false, rowIndex: 0, months: [], error: "No unGroupTableId visible" };

#             // Scrollable header area table (IDs end with -header)
#             const scrollHeader = visibleTable.querySelector('[id$="-header"]');
#             if (!scrollHeader) return { found: false, rowIndex: 0, months: [], error: "No scroll header found" };
#             const headerElements = Array.from(scrollHeader.querySelectorAll('.sapUiTableHeaderCell'));
#             const headers = headerElements.map(h => {
#                 const lbl = h.querySelector('.sapMLabel, .sapUiTableColCell > span, span');
#                 return lbl ? lbl.textContent.trim() : h.textContent.trim();
#             });

#             // Scrollable body area table (IDs end with -table)
#             const scrollBody = visibleTable.querySelector('[id$="-table"]');
#             if (!scrollBody) return { found: false, rowIndex: 0, months: [], error: "No scroll body found" };
#             const rows = Array.from(scrollBody.querySelectorAll('.sapUiTableTr'));

#             let targetRowIndex = -1;
#             const redMonths = [];

#             // Helper to check if a color is red
#             const isRedColor = (color) => {
#                 const c = color.replace(/\\s+/g, "").toLowerCase();
#                 if (c.includes("rgb(170,8,8)") || c.includes("rgb(170, 8, 8)") || c.includes("rgb(166,25,25)") || c.includes("rgb(187,0,0)")) {
#                     return true;
#                 }
#                 const match = c.match(/rgb\\((\\d+),(\\d+),(\\d+)\\)/);
#                 if (match) {
#                     const r = parseInt(match[1], 10);
#                     const g = parseInt(match[2], 10);
#                     const b = parseInt(match[3], 10);
#                     const rMin = cfg && cfg.errorRedR ? cfg.errorRedR : 150;
#                     const gMax = cfg && cfg.errorRedG ? cfg.errorRedG : 80;
#                     const bMax = cfg && cfg.errorRedB ? cfg.errorRedB : 80;
#                     return r > rMin && g < gMax && b < bMax;
#                 }
#                 return false;
#             };

#             for (let rIdx = 0; rIdx < rows.length; rIdx++) {
#                 const row = rows[rIdx];
                
#                 // Skip hidden rows
#                 if (row.classList.contains('sapUiTableRowHidden')) continue;

#                 // In the scrollable row, the cells are direct children of the row element in the DOM
#                 const cells = Array.from(row.children);
#                 const rowRedMonths = [];

#                 cells.forEach((cell, cIdx) => {
#                     const colText = headers[cIdx];
#                     if (!colText) return;

#                     // A column is a month column if it looks like "Month YYYY"
#                     const isMonthCol = /^[A-Za-z]+ \\d{4}$/.test(colText);
#                     if (!isMonthCol) return;

#                     // Check if cell contains error state
#                     let isError = false;
#                     if (cell.querySelector('.sapMObjStatusError, .sapMValueStateError') !== null || cell.classList.contains('sapMObjStatusError') || cell.classList.contains('sapMValueStateError')) {
#                         isError = true;
#                     } else {
#                         const textEls = Array.from(cell.querySelectorAll('*'));
#                         textEls.push(cell);
#                         const hasRed = textEls.some(el => {
#                             const style = window.getComputedStyle(el);
#                             return isRedColor(style.color || "");
#                         });
#                         if (hasRed) {
#                             isError = true;
#                         }
#                     }

#                     if (isError && !rowRedMonths.includes(colText.trim())) {
#                         rowRedMonths.push(colText.trim());
#                     }
#                 });

#                 if (rowRedMonths.length > 0) {
#                     targetRowIndex = rIdx;
#                     rowRedMonths.forEach(m => {
#                         if (!redMonths.includes(m)) redMonths.push(m);
#                     });
#                     break; // Stop at first row with issues
#                 }
#             }

#             return {
#                 found: targetRowIndex !== -1,
#                 rowIndex: targetRowIndex === -1 ? 0 : targetRowIndex,
#                 months: redMonths
#             };
#         }
#         """

#         cfg = {
#             "rMin": CONFIG["ERROR_RED_R"],
#             "gMax": CONFIG["ERROR_RED_G"],
#             "bMax": CONFIG["ERROR_RED_B"]
#         }

#         # Scroll RIGHT -> Nov/Dec visible -> scan
#         print("[INFO] Scrolling Capacity Plan Simulation table to the right...", file=sys.stderr)
#         page.evaluate("""
#         () => {
#             const tbl = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
#                 const s = window.getComputedStyle(el);
#                 return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetHeight > 0;
#             });
#             if (tbl) {
#                 const scrollables = Array.from(tbl.querySelectorAll('*')).filter(el => el.scrollWidth > el.clientWidth);
#                 scrollables.forEach(el => el.scrollLeft = el.scrollWidth);
#             }
#         }
#         """)
#         page.wait_for_timeout(1200)
#         print("[INFO] Scanning right-side table columns for overloaded periods...", file=sys.stderr)
#         scan_right = page.evaluate(scan_js, cfg)
#         print(f"[INFO] Scan right completed: {scan_right}", file=sys.stderr)

#         # Scroll LEFT -> Jul/Aug visible -> scan
#         print("[INFO] Scrolling Capacity Plan Simulation table to the left...", file=sys.stderr)
#         page.evaluate("""
#         () => {
#             const tbl = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
#                 const s = window.getComputedStyle(el);
#                 return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetHeight > 0;
#             });
#             if (tbl) {
#                 const scrollables = Array.from(tbl.querySelectorAll('*')).filter(el => el.scrollWidth > el.clientWidth);
#                 scrollables.forEach(el => el.scrollLeft = 0);
#             }
#         }
#         """)
#         page.wait_for_timeout(800)
#         print("[INFO] Scanning left-side table columns for overloaded periods...", file=sys.stderr)
#         scan_left = page.evaluate(scan_js, cfg)
#         print(f"[INFO] Scan left completed: {scan_left}", file=sys.stderr)

#         # Merge scan results
#         found = scan_right["found"] or scan_left["found"]
#         row_index = scan_right["rowIndex"] if scan_right["found"] else scan_left["rowIndex"]
#         months = list(set(scan_right["months"] + scan_left["months"]))
        
#         # Propagate merged variables back to the browser context
#         import json
#         page.evaluate(f"() => {{ window._pmrpRedMonths = {json.dumps(months)}; window._pmrpTargetRowIndex = {row_index}; }}")
        
#         scan_result = {
#             "found": found,
#             "rowIndex": row_index,
#             "months": months
#         }
#         print(f"[INFO] Merged scan results: {scan_result}", file=sys.stderr)
#         print(f"[INFO] Detected overloaded periods scan result: {scan_result}", file=sys.stderr)
        
#         # 3. Select the resolved row via UI5 setSelectedIndex on the resolved visible table
#         print(f"[INFO] Selecting row {scan_result.get('rowIndex', 0)} using UI5 setSelectedIndex and firing selection change...", file=sys.stderr)
#         selected = page.evaluate("""
#         () => {
#             const visibleTable = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
#                 const style = window.getComputedStyle(el);
#                 return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetHeight > 0;
#             });
#             if (!visibleTable) return "no_visible_table";
            
#             const getControl = (el) => {
#                 if (!el) return null;
#                 try {
#                     if (sap.ui.core.Element && sap.ui.core.Element.closestTo) {
#                         const c = sap.ui.core.Element.closestTo(el);
#                         if (c) return c;
#                     }
#                 } catch(e) {}
#                 try {
#                     let current = el;
#                     while (current) {
#                         if (current.id) {
#                             let ctrl = null;
#                             if (sap.ui.core.Element && sap.ui.core.Element.registry) {
#                                 ctrl = sap.ui.core.Element.registry.get(current.id);
#                             }
#                             if (!ctrl && sap.ui.getCore) {
#                                 ctrl = sap.ui.getCore().byId(current.id);
#                             }
#                             if (ctrl && ctrl.getMetadata) {
#                                 return ctrl;
#                             }
#                         }
#                         current = current.parentElement;
#                     }
#                 } catch(e) {}
#                 return null;
#             };
            
#             const table = getControl(visibleTable);
#             if (table) {
#                 const targetIdx = window._pmrpTargetRowIndex || 0;
#                 if (typeof table.setSelectedIndex === "function") {
#                     table.setSelectedIndex(targetIdx);
#                 } else if (typeof table.setSelectionInterval === "function") {
#                     table.setSelectionInterval(targetIdx, targetIdx);
#                 } else if (typeof table.setSelectedIndices === "function") {
#                     table.setSelectedIndices([targetIdx]);
#                 }
#                 try {
#                     table.fireRowSelectionChange({
#                         rowIndex: targetIdx,
#                         rowContext: table.getContextByIndex(targetIdx),
#                         selectAll: false
#                     });
#                 } catch(e) {}
#                 return table.getMetadata().getName() + " selected row " + targetIdx;
#             }
#             return "control_not_resolved";
#         }
#         """)
#         print(f"[INFO] Row selection success: {selected}", file=sys.stderr)
#         page.wait_for_timeout(200)
        
#         btn = page.locator('button:has-text("Change Available Capacity")').first
#         is_disabled = btn.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true'")
#         if is_disabled:
#             print("[WARNING] Change Available Capacity button is still disabled. Force enabling selection via backup clicks...", file=sys.stderr)
#             clicked_backup = page.evaluate("""
#             () => {
#                 const visibleTable = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
#                     const style = window.getComputedStyle(el);
#                     return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetHeight > 0;
#                 });
#                 if (!visibleTable) return false;
                
#                 // Click the first non-empty text cell in the first data row
#                 const firstRow = visibleTable.querySelector('.sapUiTableTr, tr.sapUiTableTr, tbody tr');
#                 if (firstRow) {
#                     const cells = Array.from(firstRow.querySelectorAll('td, .sapUiTableCell, .sapUiTableCellInner'));
#                     const textCell = cells.find(c => c.textContent.trim().length > 0);
#                     if (textCell) {
#                         textCell.click();
#                         return "text_cell_" + textCell.textContent.trim();
#                     }
#                 }
                
#                 // Fallback to rowsel0
#                 const rowsel = visibleTable.querySelector('[id*="rowsel0"]');
#                 if (rowsel && rowsel.offsetWidth > 0) {
#                     rowsel.click();
#                     return "rowsel0";
#                 }
#                 return false;
#             }
#             """)
#             print(f"[INFO] Backup selection click result: {clicked_backup}", file=sys.stderr)
#             page.wait_for_timeout(1000)
                
#         # 4. Click "Change Available Capacity" button
#         print("[INFO] Clicking Change Available Capacity button...", file=sys.stderr)
#         btn.click(force=True)
        
#         # 5. Wait for Change Capacity Limit dialog/popover
#         dialog_selector = 'div[role="dialog"]:has-text("Change Capacity Limit"), .sapMDialog:has-text("Change Capacity Limit")'
#         page.locator(dialog_selector).first.wait_for(state="visible", timeout=25000)
#         dialog = page.locator(dialog_selector).first
#         page.wait_for_timeout(200)
        
#         # 6. Select only overloaded buckets via high-performance UI5 API call
#         print("[INFO] Selecting only overloaded periods programmatically...", file=sys.stderr)
#         page.evaluate("""
#         () => {
#             let cb = sap.ui.getCore().byId("bucketSelection");
#             if (!cb) {
#                 const el = document.getElementById("bucketSelection");
#                 if (el) {
#                     cb = sap.ui.getCore().byId(el.id);
#                 }
#             }
#             if (!cb) {
#                 try {
#                     const registry = sap.ui.getCore().elementRegistry || (sap.ui.core.Element && sap.ui.core.Element.registry);
#                     if (registry) {
#                         const mcb = registry.filter(c => c.getMetadata().getName() === "sap.m.MultiComboBox");
#                         if (mcb.length > 0) cb = mcb[0];
#                     }
#                 } catch(e) {}
#             }
#             if (cb) {
#                 cb.setSelectedKeys([]);
#                 const items = cb.getItems();
#                 const selectedKeys = [];
#                 const targetMonths = window._pmrpRedMonths || [];
#                 items.forEach(item => {
#                     const itemText = item.getText().trim();
#                     const shouldSelect = targetMonths.length === 0 || targetMonths.some(m => itemText.includes(m) || m.includes(itemText));
#                     if (shouldSelect) {
#                         selectedKeys.push(item.getKey());
#                         cb.setSelectedKeys([...selectedKeys]);
#                         cb.fireSelectionChange({ changedItem: item, selected: true });
#                     }
#                 });
#                 cb.fireSelectionFinish({ selectedItems: cb.getSelectedItems() });
#             }
#         }
#         """)
#         page.wait_for_timeout(200)
        
#         # Save screenshot of selected dropdown
#         page.screenshot(path="change_capacity_dialog_debug.png")
        
#         # 7. Click Adopt Proposal inside the dialog
#         print("[INFO] Clicking Adopt Proposal button inside dialog...", file=sys.stderr)
#         adopted = page.evaluate("""
#         () => {
#             let adoptCtrl = null;
#             try {
#                 const reg = sap.ui.core.Element && sap.ui.core.Element.registry;
#                 if (reg) {
#                     reg.forEach(ctrl => {
#                         if (adoptCtrl) return;
#                         try {
#                             const dom = ctrl.getDomRef && ctrl.getDomRef();
#                             if (dom && dom.offsetParent !== null && dom.textContent.trim() === 'Adopt Proposal') {
#                                 adoptCtrl = ctrl;
#                             }
#                         } catch(e) {}
#                     });
#                 }
#             } catch(e) {}

#             if (adoptCtrl) {
#                 try { adoptCtrl.firePress(); return 'firePress:' + adoptCtrl.getMetadata().getName(); } catch(e) {}
#                 try { adoptCtrl.getDomRef().click(); return 'domClick:' + adoptCtrl.getMetadata().getName(); } catch(e) {}
#             }

#             const all = Array.from(document.querySelectorAll('a, button, span, bdi'));
#             const el = all.find(e => e.textContent.trim() === 'Adopt Proposal' && e.offsetParent !== null);
#             if (el) { el.click(); return 'domFallback:' + el.tagName; }

#             return 'not_found';
#         }
#         """)
#         print(f"[INFO] Adopt Proposal: {adopted}", file=sys.stderr)
#         page.wait_for_timeout(2500)
        
#         # 8. Click Apply inside the dialog
#         print("[INFO] Clicking Apply button inside dialog...", file=sys.stderr)
#         dialog.locator('button:has-text("Apply")').first.click(force=True)
        
#         print("[INFO] Waiting for simulation to reconcile...", file=sys.stderr)
#         # Wait dynamically for the dialog to disappear
#         page.locator(dialog_selector).first.wait_for(state="hidden", timeout=15000)
#         page.wait_for_timeout(200)
        
#         # 9. Return to main dashboard (Demand Plan Simulation) by reloading the dashboard directly
#         print("[INFO] Reloading main dashboard to refresh KPIs...", file=sys.stderr)
#         page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
#         wait_for_simulation_ready(page, sim_url)

SCAN_CAPACITY_PLAN_JS = """
(cfg) => {
    // Find the visible unGroupTableId container
    const visibleTable = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetHeight > 0;
    });
    if (!visibleTable) return { found: false, rowIndex: 0, months: [], error: "No unGroupTableId visible" };

    // Scrollable header area table (IDs end with -header)
    const scrollHeader = visibleTable.querySelector('[id$="-header"]');
    if (!scrollHeader) return { found: false, rowIndex: 0, months: [], error: "No scroll header found" };
    const headerElements = Array.from(scrollHeader.querySelectorAll('.sapUiTableHeaderCell'));
    const headers = headerElements.map(h => {
        const lbl = h.querySelector('.sapMLabel, .sapUiTableColCell > span, span');
        return lbl ? lbl.textContent.trim() : h.textContent.trim();
    });

    // Scrollable body area table (IDs end with -table)
    const scrollBody = visibleTable.querySelector('[id$="-table"]');
    if (!scrollBody) return { found: false, rowIndex: 0, months: [], error: "No scroll body found" };
    const rows = Array.from(scrollBody.querySelectorAll('.sapUiTableTr'));

    let targetRowIndex = -1;
    const redMonths = [];

    // Helper to check if a color is red
    const isRedColor = (color) => {
        const c = color.replace(/\\s+/g, "").toLowerCase();
        if (c.includes("rgb(170,8,8)") || c.includes("rgb(170, 8, 8)") || c.includes("rgb(166,25,25)") || c.includes("rgb(187,0,0)")) {
            return true;
        }
        const match = c.match(/rgb\\((\\d+),(\\d+),(\\d+)\\)/);
        if (match) {
            const r = parseInt(match[1], 10);
            const g = parseInt(match[2], 10);
            const b = parseInt(match[3], 10);
            const rMin = cfg && cfg.rMin ? cfg.rMin : 150;
            const gMax = cfg && cfg.gMax ? cfg.gMax : 80;
            const bMax = cfg && cfg.bMax ? cfg.bMax : 80;
            return r > rMin && g < gMax && b < bMax;
        }
        return false;
    };

    for (let rIdx = 0; rIdx < rows.length; rIdx++) {
        const row = rows[rIdx];
        
        // Skip hidden rows
        if (row.classList.contains('sapUiTableRowHidden')) continue;

        // In the scrollable row, the cells are direct children of the row element in the DOM
        const cells = Array.from(row.children);
        const rowRedMonths = [];

        cells.forEach((cell, cIdx) => {
            const colText = headers[cIdx];
            if (!colText) return;

            // A column is a month column if it looks like "Month YYYY"
            const isMonthCol = /^[A-Za-z]+ \\d{4}$/.test(colText);
            if (!isMonthCol) return;

            // Check if cell contains error state
            let isError = false;
            if (cell.querySelector('.sapMObjStatusError, .sapMValueStateError') !== null || cell.classList.contains('sapMObjStatusError') || cell.classList.contains('sapMValueStateError')) {
                isError = true;
            } else {
                const textEls = Array.from(cell.querySelectorAll('*'));
                textEls.push(cell);
                const hasRed = textEls.some(el => {
                    const style = window.getComputedStyle(el);
                    return isRedColor(style.color || "");
                });
                if (hasRed) {
                    isError = true;
                }
            }

            if (isError && !rowRedMonths.includes(colText)) {
                rowRedMonths.push(colText);
            }
        });

        if (rowRedMonths.length > 0) {
            targetRowIndex = rIdx;
            rowRedMonths.forEach(m => {
                if (!redMonths.includes(m)) redMonths.push(m);
            });
            break; // Stop at first row with issues
        }
    }

    return {
        found: targetRowIndex !== -1,
        rowIndex: targetRowIndex === -1 ? 0 : targetRowIndex,
        months: redMonths
    };
}
"""



def run_capacity_adaptation(page):
    sim_url = page.url
    print("\n=================== STARTING TIMED CAPACITY REMEDIATION ===================", file=sys.stderr)
    start = time.time()

    # ── STEP 1: Confirm capacity issues exist on main dashboard ───────────────
    t0 = time.time()
    red_count = page.locator(".sapMObjStatusError, .sapMValueStateError").count()
    print(f"[TIMED] Main dashboard check: {red_count} red elements found in {time.time() - t0:.3f}s", file=sys.stderr)
    if red_count == 0:
        print("[INFO] No capacity issues detected. Nothing to do.", file=sys.stderr)
        return

    # ── STEP 2: Switch to "Capacity Plan Simulation" view ────────────────────
    t0 = time.time()
    print("[TIMED] Opening view switcher...", file=sys.stderr)
    header_btn = page.locator('[id="simulationViewMenuButtonId-internalBtn"]')
    header_btn.wait_for(state="visible", timeout=8000)
    header_btn.click(force=True)
    page.wait_for_timeout(300)

    menu_item = page.locator(
        '.sapMMenuItem:has-text("Capacity Plan Simulation"), '
        '.sapMMenuItemText:has-text("Capacity Plan Simulation"), '
        'li:has-text("Capacity Plan Simulation")'
    ).first
    menu_item.wait_for(state="visible", timeout=8000)
    menu_item.click(force=True)
    print(f"[TIMED] Switched to Capacity Plan Simulation in {time.time() - t0:.3f}s", file=sys.stderr)

    # ── STEP 3: Wait for the Capacity Plan table to load ─────────────────────
    t0 = time.time()
    page.locator('[id*="unGroupTableId"]:visible').first.wait_for(state="visible", timeout=20000)
    page.wait_for_timeout(1500)
    print(f"[TIMED] Capacity Plan table loaded in {time.time() - t0:.3f}s", file=sys.stderr)

    # ── STEP 4: Scan for red months ─────────────────────────────────────────
    # Scroll RIGHT first so Nov/Dec columns (which have the real issues) are in DOM
    t0 = time.time()
    cfg = {"rMin": 150, "gMax": 80, "bMax": 80}

    scroll_js = """
    (direction) => {
        const tbl = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
            const s = window.getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetHeight > 0;
        });
        if (tbl) Array.from(tbl.querySelectorAll('*'))
            .filter(el => el.scrollWidth > el.clientWidth)
            .forEach(el => el.scrollLeft = direction === 'right' ? el.scrollWidth : 0);
    }
    """

    # Scroll RIGHT → Nov/Dec visible → scan
    page.evaluate(scroll_js, "right")
    page.wait_for_timeout(1200)
    scan_right = page.evaluate(SCAN_CAPACITY_PLAN_JS, cfg)
    print(f"[TIMED] Scan right (Nov/Dec): {scan_right}", file=sys.stderr)

    # Scroll LEFT → Jul/Aug visible → scan
    page.evaluate(scroll_js, "left")
    page.wait_for_timeout(800)
    scan_left = page.evaluate(SCAN_CAPACITY_PLAN_JS, cfg)
    print(f"[TIMED] Scan left (Jul/Aug): {scan_left}", file=sys.stderr)

    # Merge — prefer the RIGHT scan for row_index since Nov/Dec issues are real overloads
    found = scan_right["found"] or scan_left["found"]
    row_index = scan_right["rowIndex"] if scan_right["found"] else scan_left["rowIndex"]
    months = list(set(scan_right["months"] + scan_left["months"]))
    print(f"[TIMED] Merged: found={found}, row={row_index}, months={months}  [{time.time()-t0:.3f}s]", file=sys.stderr)


    if not months:
        print("[WARNING] No red months detected in Capacity Plan Simulation table!", file=sys.stderr)
        print("[INFO]    This may mean the issues cleared already, or the colour check needs tuning.", file=sys.stderr)

    # Propagate to browser context for the dialog step
    page.evaluate(f"() => {{ window._pmrpRedMonths = {json.dumps(months)}; window._pmrpTargetRowIndex = {row_index}; }}")

    # ── STEP 5: Select the target row via UI5 registry ───────────────────────
    t0 = time.time()
    selected = page.evaluate("""
    () => {
        const visibleTable = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
            const s = window.getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetHeight > 0;
        });
        if (!visibleTable) return "no_table";

        const getCtrl = (el) => {
            try {
                const c = sap.ui.core.Element.closestTo && sap.ui.core.Element.closestTo(el);
                if (c && c.getColumns) return c;
            } catch(e) {}
            try {
                let cur = el;
                while (cur) {
                    let ctrl = (sap.ui.core.Element.registry && sap.ui.core.Element.registry.get(cur.id)) ||
                               (sap.ui.getCore && sap.ui.getCore().byId(cur.id));
                    if (ctrl && ctrl.getColumns) return ctrl;
                    cur = cur.parentElement;
                }
            } catch(e) {}
            return null;
        };

        const table = getCtrl(visibleTable);
        if (!table) return "no_ctrl";

        const idx = window._pmrpTargetRowIndex || 0;
        if (table.setSelectedIndex) table.setSelectedIndex(idx);
        else if (table.setSelectionInterval) table.setSelectionInterval(idx, idx);
        try {
            table.fireRowSelectionChange({
                rowIndex: idx,
                rowContext: table.getContextByIndex(idx),
                selectAll: false
            });
        } catch(e) {}
        return table.getMetadata().getName() + " row=" + idx;
    }
    """)
    print(f"[TIMED] Row selected ({selected}) in {time.time()-t0:.3f}s", file=sys.stderr)
    page.wait_for_timeout(200)

    # ── STEP 6: Click "Change Available Capacity" ─────────────────────────────
    t0 = time.time()
    btn = page.locator('button:has-text("Change Available Capacity")').first
    is_disabled = btn.evaluate("el => el.disabled || el.getAttribute('aria-disabled') === 'true'")
    if is_disabled:
        print("[WARNING] Button disabled — clicking first row cell as fallback...", file=sys.stderr)
        page.evaluate("""
        () => {
            const tbl = Array.from(document.querySelectorAll('[id*="unGroupTableId"]')).find(el => {
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetHeight > 0;
            });
            if (tbl) {
                const row = tbl.querySelector('.sapUiTableTr:not(.sapUiTableEmptyRow)');
                if (row) {
                    const cell = row.querySelector('.sapUiTableCell, td');
                    if (cell) cell.click();
                }
            }
        }
        """)
        page.wait_for_timeout(800)
    btn.click(force=True)
    print(f"[TIMED] Clicked 'Change Available Capacity' in {time.time()-t0:.3f}s", file=sys.stderr)

    # ── STEP 7: Wait for "Change Capacity Limit" dialog ──────────────────────
    t0 = time.time()
    dlg_sel = 'div[role="dialog"]:has-text("Change Capacity Limit"), .sapMDialog:has-text("Change Capacity Limit")'
    page.locator(dlg_sel).first.wait_for(state="visible", timeout=25000)
    dialog = page.locator(dlg_sel).first
    page.wait_for_timeout(200)
    print(f"[TIMED] Dialog visible in {time.time()-t0:.3f}s", file=sys.stderr)

    # ── STEP 8: Select months in the MultiComboBox ───────────────────────────
    # Select ONLY the detected red months (passed via window._pmrpRedMonths).
    t0 = time.time()
    page.evaluate("""
    () => {
        let cb = null;
        try {
            const reg = sap.ui.core.Element && sap.ui.core.Element.registry;
            if (reg) reg.forEach(ctrl => {
                if (!cb && ctrl.getMetadata && ctrl.getMetadata().getName() === "sap.m.MultiComboBox") cb = ctrl;
            });
        } catch(e) {}
        if (!cb) { try { cb = sap.ui.getCore().byId("bucketSelection"); } catch(e) {} }
        if (!cb) return;

        const items = cb.getItems();
        const selectedKeys = [];
        const targetMonths = window._pmrpRedMonths || [];
        
        items.forEach(item => {
            const itemText = item.getText().trim();
            const shouldSelect = targetMonths.length === 0 || targetMonths.some(m => itemText.includes(m) || m.includes(itemText));
            if (shouldSelect) {
                selectedKeys.push(item.getKey());
                cb.setSelectedKeys([...selectedKeys]);
                cb.fireSelectionChange({ changedItem: item, selected: true });
            }
        });
        cb.fireSelectionFinish({ selectedItems: cb.getSelectedItems() });
    }
    """)
    page.wait_for_timeout(300)
    print(f"[TIMED] MultiComboBox: selected target months in {time.time()-t0:.3f}s", file=sys.stderr)


    # ── STEP 9: Click "Adopt Proposal" ───────────────────────────────────────
    # NOTE: "Adopt Proposal" is a LINK inside the dialog body (not a footer button)
    # It populates the capacity table with proposed values
    t0 = time.time()
    adopted = page.evaluate("""
    () => {
        // Find Adopt Proposal via UI5 registry (works for Link, Button, any pressable control)
        let adoptCtrl = null;
        try {
            const reg = sap.ui.core.Element && sap.ui.core.Element.registry;
            if (reg) {
                reg.forEach(ctrl => {
                    if (adoptCtrl) return;
                    try {
                        const dom = ctrl.getDomRef && ctrl.getDomRef();
                        if (dom && dom.offsetParent !== null && dom.textContent.trim() === 'Adopt Proposal') {
                            adoptCtrl = ctrl;
                        }
                    } catch(e) {}
                });
            }
        } catch(e) {}

        if (adoptCtrl) {
            try { adoptCtrl.firePress(); return 'firePress:' + adoptCtrl.getMetadata().getName(); } catch(e) {}
            try { adoptCtrl.getDomRef().click(); return 'domClick:' + adoptCtrl.getMetadata().getName(); } catch(e) {}
        }

        // Fallback: direct DOM text search
        const all = Array.from(document.querySelectorAll('a, button, span, bdi'));
        const el = all.find(e => e.textContent.trim() === 'Adopt Proposal' && e.offsetParent !== null);
        if (el) { el.click(); return 'domFallback:' + el.tagName; }

        return 'not_found';
    }
    """)
    print(f"[TIMED] Adopt Proposal: {adopted}", file=sys.stderr)
    # Wait for capacity rows to populate (the table loads data asynchronously)
    page.wait_for_timeout(2500)
    print(f"[TIMED] Adopt Proposal + data wait done in {time.time()-t0:.3f}s", file=sys.stderr)

    # ── STEP 10: Click "Apply" (footer button) ───────────────────────────────
    t0 = time.time()
    apply_btn = page.locator('button:has-text("Apply"), .sapMBtn:has-text("Apply")').first
    apply_btn.wait_for(state="visible", timeout=8000)
    apply_btn.click(force=True)
    page.wait_for_timeout(500)
    print(f"[TIMED] Clicked 'Apply' in {time.time()-t0:.3f}s", file=sys.stderr)

    # ── STEP 11: Wait for dialog close ───────────────────────────────────────
    t0 = time.time()
    page.locator(dlg_sel).first.wait_for(state="hidden", timeout=30000)
    page.wait_for_timeout(200)
    print(f"[TIMED] Dialog closed in {time.time()-t0:.3f}s", file=sys.stderr)



    # ── STEP 12: Reload main dashboard ───────────────────────────────────────
    t0 = time.time()
    print("[TIMED] Reloading main dashboard...", file=sys.stderr)
    page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
    wait_for_simulation_ready(page, sim_url)
    print(f"[TIMED] Dashboard reloaded & recalculated in {time.time()-t0:.3f}s", file=sys.stderr)

    print(f"=================== DONE in {time.time()-start:.3f}s ===================\n", file=sys.stderr)

def run_release_simulation(
    page: Page, 
    sap_url: str, 
    simulation_id: str = "SIM_PIR",
    ddmrp_components: Optional[bool] = None,
    ddmrp_req_version: Optional[str] = None,
    ddmrp_version_active: Optional[bool] = None,
    top_level_materials: Optional[bool] = None,
    top_level_req_version: Optional[str] = None,
    top_level_version_active: Optional[bool] = None,
    subassembly_components: Optional[bool] = None,
    non_mrp_kanban: Optional[bool] = None,
    selected_non_mrp: Optional[bool] = None,
    selected_non_mrp_type: Optional[str] = None,
    selected_non_mrp_req_version: Optional[str] = None,
    selected_non_mrp_version_active: Optional[bool] = None,
    capacity_change_proposals: Optional[bool] = None
):
    list_url = sap_url.rstrip('/') + "/ui#PMRPSimulation-process"
    print(f"[INFO] Navigating to simulations list URL: {list_url}", file=sys.stderr)
    page.goto(list_url, wait_until="domcontentloaded", timeout=0)
    
    print(f"[INFO] Waiting for simulations table to load and find '{simulation_id}'...", file=sys.stderr)
    page.locator(f'text={simulation_id}').first.wait_for(state="visible", timeout=30000)
    page.wait_for_timeout(500)
    
    print(f"[INFO] Checking if {simulation_id} Release button is active...", file=sys.stderr)
    release_btn = page.locator(f'tr:has-text("{simulation_id}")').locator('button:has-text("Release")').first
    
    if release_btn.is_disabled():
        print(f"[INFO] {simulation_id} Release button is disabled. The simulation is already released or in progress.", file=sys.stderr)
        return
        
    print(f"[INFO] Clicking 'Release' button in the {simulation_id} row...", file=sys.stderr)
    release_btn.click(force=True)
    
    print("[INFO] Waiting for Release dialog...", file=sys.stderr)
    page.locator('[id="idReleaseSimulationButton"]').first.wait_for(state="visible", timeout=15000)
    page.wait_for_timeout(500)
    
    # Setup helpers for checking/filling/toggling
    def set_checkbox_state(checkbox_id: str, checked: Optional[bool]):
        if checked is None:
            return
        cb = page.locator(f'[id="{checkbox_id}"]')
        if cb.count() > 0:
            is_checked = cb.evaluate("el => el.classList.contains('sapMCbMarkChecked') || el.getAttribute('aria-checked') === 'true'")
            if is_checked != checked:
                cb.click(force=True)
                page.wait_for_timeout(500)
                
    def set_combobox_value(input_id: str, value: Optional[str]):
        if not value:
            return
        inp = page.locator(f'[id="{input_id}"]')
        if inp.count() > 0:
            inp.click(force=True)
            page.wait_for_timeout(300)
            inp.fill("")
            page.wait_for_timeout(200)
            inp.fill(value)
            page.wait_for_timeout(600)
            
            # Look for suggestions in the open listbox popover
            suggestions = page.locator('li[role="option"], .sapMListBoxItem, .sapMPopOver li')
            matched = False
            count = suggestions.count()
            for i in range(count):
                item = suggestions.nth(i)
                if item.is_visible():
                    text = item.inner_text().strip()
                    # Check if suggestion starts with value or contains it
                    if text.lower().startswith(value.lower()) or value.lower() in text.lower():
                        print(f"[INFO] Selecting suggestion matching '{value}': '{text}'", file=sys.stderr)
                        item.click(force=True)
                        matched = True
                        break
            
            if not matched:
                print(f"[INFO] No explicit suggestion matching '{value}' found. Pressing Enter as fallback.", file=sys.stderr)
                inp.press("Enter")
            page.wait_for_timeout(800)
            
    def set_switch_state(switch_id: str, enabled: Optional[bool]):
        if enabled is None:
            return
        sw = page.locator(f'[id="{switch_id}"]')
        if sw.count() > 0:
            is_on = sw.evaluate("el => el.classList.contains('sapMSwtOn') || el.getAttribute('aria-checked') === 'true'")
            if is_on != enabled:
                sw.click(force=True)
                page.wait_for_timeout(500)

    print("[INFO] Applying Release configuration options...", file=sys.stderr)
    set_checkbox_state("idChkBoxDDMRP", ddmrp_components)
    set_checkbox_state("idChkBoxTopMat", top_level_materials)
    set_checkbox_state("idChkBoxSubAsm", subassembly_components)
    set_checkbox_state("idChkBoxMRPKanban", non_mrp_kanban)
    set_checkbox_state("idChkBoxNonMRPMAT", selected_non_mrp)
    set_checkbox_state("idChkBoxCapacity", capacity_change_proposals)
    
    # Subfields are filled only if they are explicitly specified
    set_combobox_value("ddmrpversionCombo-inner", ddmrp_req_version)
    set_switch_state("ddmrpfieldGroupSwitch-switch", ddmrp_version_active)
        
    set_combobox_value("topMatversionCombo-inner", top_level_req_version)
    set_switch_state("topMatfieldGroupSwitch-switch", top_level_version_active)
        
    set_combobox_value("MRPTypCombo-inner", selected_non_mrp_type)
    set_combobox_value("NonMRPMatversionCombo-inner", selected_non_mrp_req_version)
    set_switch_state("NonMRPMatfieldGroupSwitch-switch", selected_non_mrp_version_active)
    
    print("[INFO] Clicking dialog 'Release' button...", file=sys.stderr)
    page.locator('[id="idReleaseSimulationButton"]').click(force=True)
    
    print("[INFO] Waiting for release success toast...", file=sys.stderr)
    try:
        page.locator('.sapMMessageToast, div[role="alert"]').first.wait_for(state="visible", timeout=8000)
        print("[SUCCESS] Found release success confirmation toast!", file=sys.stderr)
    except Exception:
        print("[INFO] Toast not detected. Performing fallback 2-second wait...", file=sys.stderr)
        page.wait_for_timeout(2000)
        
    print("[SUCCESS] Simulation release flow completed successfully!", file=sys.stderr)

def main():
    load_dotenv()
    sap_url = os.getenv("SAP_URL")
    state_path = "sap_session_state.json"
    sim_url = sap_url.rstrip('/') + "/ui#PMRPSimulation-process&/PmrpSimulation('SIM_PIR')"
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context_args = {}
        if os.path.exists(state_path):
            context_args["storage_state"] = state_path
            
        context = browser.new_context(**context_args)
        page = context.new_page()
        
        try:
            ensure_logged_in(page, context, sim_url)
            run_capacity_adaptation(page)
            run_release_simulation(page, sap_url)
        except Exception as e:
            print(f"[ERROR] Automation run failed: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            browser.close()

if __name__ == "__main__":
    main()
