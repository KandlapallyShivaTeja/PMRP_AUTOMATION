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

        print(f"[INFO] Launching browser to connect to target URL: {target_url}", file=sys.stderr)
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--start-maximized"])
            context = browser.new_context(no_viewport=True)
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

    # Wait for Create button (already navigated by decorator)
    # Wait for either Create button OR Step 1 field (Job Template)
    print("[INFO] Waiting for page entry (Create button or Job Template field)...", file=sys.stderr)
    create_btn = page.locator("#application-PMRPSimulation-create-component---JobRunList--appJobsOverviewAddJobButton-BDI-content").first
    step1_label = page.locator("#application-PMRPSimulation-create-component---JobRunCreate--appJobsCreateLabelTemplateName, label:has-text('Job Template')").first
    
    try:
        page.locator("#application-PMRPSimulation-create-component---JobRunList--appJobsOverviewAddJobButton-BDI-content, #application-PMRPSimulation-create-component---JobRunCreate--appJobsCreateLabelTemplateName, label:has-text('Job Template')").first.wait_for(state="visible", timeout=45000)
    except Exception:
        raise Exception("Failed to load page: neither the 'Create' button nor the 'Job Template' field appeared.")
        
    if create_btn.count() > 0 and create_btn.is_visible():
        print("[INFO] List page loaded. Clicking Create button...", file=sys.stderr)
        page.wait_for_timeout(1000)
        create_btn.click()
        
    # Wait for Step 1 fields to be visible
    print("[INFO] Waiting for Step 1 fields...", file=sys.stderr)
    try:
        step1_label.wait_for(state="visible", timeout=15000)
    except Exception:
        if create_btn.count() > 0 and create_btn.is_visible():
            print("[WARNING] Step 1 did not load. Retrying click on Create button...", file=sys.stderr)
            create_btn.click()
            step1_label.wait_for(state="visible", timeout=15000)
        else:
            raise
        
    print("[INFO] Step 1 fields are visible.", file=sys.stderr)

    # Click Step 2
    print("[INFO] Clicking Step 2 button...")
    step2_btn = page.locator("button:has-text('Step 2'), button[id*='SelectTemplateStep-nextButton'], #application-PMRPSimulation-create-component---JobRunCreate--SelectTemplateStep-nextButton-BDI-content").first
    step2_btn.wait_for(state="visible", timeout=15000)
    
    # Click Step 2 with retry loop until Step 2 field is visible
    step2_field = page.locator("label[title='Start Immediately']:visible, label:has-text('Start Immediately'):visible, #application-PMRPSimulation-create-component---JobRunCreate--immediateCheckbox:visible").first
    for attempt in range(5):
        try:
            step2_btn.click()
            step2_field.wait_for(state="visible", timeout=4000)
            break
        except Exception:
            print(f"[WARNING] Step 2 transition did not occur. Retrying click (Attempt {attempt+1})...")
            page.wait_for_timeout(1000)
    else:
        raise Exception("Failed to transition to Step 2 after multiple clicks.")
    print("[INFO] Step 2 fields are visible.")

    # Click Step 3
    print("[INFO] Clicking Step 3 button...")
    step3_btn = page.locator("button:has-text('Step 3'), button[id*='CreateSchedulingOptionsStep-nextButton'], #application-PMRPSimulation-create-component---JobRunCreate--CreateSchedulingOptionsStep-nextButton-BDI-content").first
    step3_btn.wait_for(state="visible", timeout=15000)
    
    # Click Step 3 with retry loop until Step 3 section is visible
    step3_section = page.locator("label:has-text('ID for Reference Data'):visible, label:has-text('Simulation ID'):visible, label:has-text('Plant'):visible").first
    for attempt in range(5):
        try:
            step3_btn.click()
            step3_section.wait_for(state="visible", timeout=4000)
            break
        except Exception:
            print(f"[WARNING] Step 3 transition did not occur. Retrying click (Attempt {attempt+1})...")
            page.wait_for_timeout(1000)
    else:
        raise Exception("Failed to transition to Step 3 after multiple clicks.")
    print("[INFO] Step 3 Parameters page loaded. Settling...")
    page.wait_for_timeout(1000) # Settle delay to let event listeners bind

    # Dynamically fill all supplied fields!
    for label_text, value in fields.items():
        print(f"[INFO] Processing field '{label_text}' with value '{value}'...")
        
        # Locate the field's label
        label = page.locator("label").filter(has_text=re.compile(rf"^\s*\*?\s*{label_text}\s*\*?:?\s*$", re.IGNORECASE)).first
        try:
            label.wait_for(state="visible", timeout=5000)
        except Exception:
            print(f"[WARNING] Label '{label_text}' not found on Step 3 page. Skipping...")
            continue
            
        # Determine if it has an F4 help icon associated with it
        for_id = label.get_attribute("for")
        has_f4 = False
        if for_id:
            control_id = for_id.replace("-inner", "")
            # Check if there is an F4 help icon inside the container or matching ID
            vhi = page.locator(f"#{control_id}-vhi, #{control_id} .sapMInputValHelpIcon").first
            if vhi.count() > 0 and vhi.is_visible():
                has_f4 = True
        if not has_f4:
            # Locate the input sibling and check its container specifically (never search whole form row)
            try:
                if for_id:
                    inp = page.locator(f"#{for_id}")
                else:
                    inp = label.locator("xpath=../..").locator("input").first
                
                vhi = inp.locator("xpath=..").locator(".sapMInputValHelpIcon").first
                if vhi.count() > 0 and vhi.is_visible():
                    has_f4 = True
            except Exception:
                pass
                
        # Double-layer safety: explicitly exclude fields known to be direct input/selects
        direct_only_fields = [
            "id for reference data", "reference description",
            "start date of reference", "end date of reference",
            "simulation id", "simulation description"
        ]
        if label_text.lower() in direct_only_fields:
            has_f4 = False
                
        if has_f4:
            print(f"[INFO] Field '{label_text}' identified as F4 Help input.")
            # Clear existing tokens first if any exist
            clear_existing_tokens(page, label_text)
            
            # Fetch filter label if provided
            filter_label = filter_labels.get(label_text)
            
            # Execute F4 select
            handle_f4_select(page, label_text, value, filter_label=filter_label)
        else:
            print(f"[INFO] Field '{label_text}' identified as direct input field.")
            fill_direct_field(page, label_text, value)
            
        page.wait_for_timeout(500) # Settle after filling

    # Save screenshot of filled form
    print("[INFO] Dynamic parameters entered. Saving verification screenshot...")
    page.screenshot(path="sap_parameters_filled.png")

    # Step 4: Verification Check
    print("[INFO] Clicking 'Check' button...")
    check_btn = page.locator("#application-PMRPSimulation-create-component---JobRunCreate--checkButton-BDI-content, button[id*='checkButton']").first
    check_btn.click()
    page.wait_for_timeout(3000) # Give check result time to compute

    # Open/Inspect Message Popover in footer (bottom-left)
    print("[INFO] Checking if Message Popover is visible...")
    popover = page.locator(".sapMPopover:visible, .sapMMessagePopover:visible, .sapMMessageView:visible, div[id*='messagePopover']:visible").first
    
    # Check if the popover is visible automatically
    popover_visible = False
    try:
        popover.wait_for(state="visible", timeout=4000)
        popover_visible = True
        print("[INFO] Message Popover is already open.")
    except Exception:
        pass
    
    if not popover_visible:
        # Find the footer message button
        msg_popover_btn = page.locator(
            "button[id*='JobRunCreate--messageButton']:visible, "
            "button[id*='messageButton']:visible, "
            ".sapMMessagePopoverBtn:visible, "
            ".sapUiMessagePopoverBtn:visible"
        ).first
        
        if msg_popover_btn.count() > 0:
            is_enabled = msg_popover_btn.is_enabled()
            print(f"[INFO] Message button enabled state: {is_enabled}")
            if is_enabled:
                print("[INFO] Clicking message button to open popover...")
                msg_popover_btn.click()
                popover.wait_for(state="visible", timeout=5000)
                popover_visible = True
            else:
                print("[INFO] Message button is disabled. No validation errors returned.")
        else:
            print("[INFO] Message button not found in footer.")

    is_success = True
    popover_text = ""
    if popover_visible:
        # Cleanly extract list items or titles inside the message view to avoid interface noise
        items = popover.locator(".sapMMessageViewItem, .sapMMessageListItem, .sapMMsgViewItemTitle, .sapMMessagePopoverContent, .sapMMessagePopoverItem").all()
        if items:
            messages = []
            for item in items:
                txt = item.text_content()
                if txt and txt.strip():
                    cleaned = " ".join(txt.split())
                    # Deduplicate and filter out utility words
                    if cleaned not in messages:
                        messages.append(cleaned)
            popover_text = " | ".join(messages)
        else:
            popover_text = " ".join((popover.text_content() or "").split())
            
        print(f"[INFO] Scraped SAP Popover Messages: '{popover_text}'")
        
        success_msg = "You can go ahead and schedule the job."
        is_success = success_msg in popover_text

    if is_success:
        print("[SUCCESS] Validation succeeded. Clicking 'Schedule' button...")
        schedule_btn = page.locator("#application-PMRPSimulation-create-component---JobRunCreate--scheduleButton-BDI-content, button[id*='scheduleButton'], button:has-text('Schedule')").first
        schedule_btn.click()
        print(f"[INFO] Clicked Schedule button. Waiting 2 seconds to capture success state...")
        page.wait_for_timeout(2000)
        page.screenshot(path="job_scheduled_success.png")

        # Navigate to the pMRP Simulation URL
        sim_url = "https://my401292.s4hana.cloud.sap/ui#PMRPSimulation-simulate&/?sap-iapp-state=AS99TB9M6NIPS212WLIDXZAZFMU5EJOUSCH0YFU4&sap-iapp-state--history=TAS52YK80GBQW9L57V0V096OXAXDS5RO3QH89PFSA"
        print(f"[INFO] Navigating directly to pMRP Simulation URL: {sim_url}")
        page.goto(sim_url, wait_until="load", timeout=0)
        
        print("[INFO] Waiting exactly 10 seconds on the pMRP Simulation dashboard...")
        page.wait_for_timeout(10000)
            
        page.screenshot(path="job_simulated_dashboard.png")
        print("[SUCCESS] Navigated to pMRP Simulation dashboard successfully.")
        return f"""# 📋 SAP pMRP Automation Report

### ✅ Execution Succeeded
The pMRP simulation job has been successfully created, checked, and scheduled in SAP.

### 🔍 Execution Details:
* **SAP Message / Popover:** `{popover_text.strip()}`
* **Action Performed:** Parameters were entered, validation passed, and the job was scheduled.
* **Redirection URL:** Navigated to pMRP Simulation Dashboard successfully."""
    else:
        cleaned_msg = popover_text.replace("MessagesCloseMessages", "").replace("Errors, please check job parameters", "").strip()
        cleaned_msg = re.sub(r'\s*\|\s*\|+', ' |', cleaned_msg).strip(" |")
        print(f"[WARNING] SAP Validation Failed: {cleaned_msg}")
        page.screenshot(path="job_check_failed.png")
        return f"""# 📋 SAP pMRP Automation Report

### ⚠️ SAP Validation Failed
The parameters were filled, but SAP returned validation errors during the check phase.

### 🔍 Validation Details:
* **SAP Message / Popover:** `{cleaned_msg}`

> [!WARNING]
> The job was not scheduled. A screenshot of the validation failure was saved as `job_check_failed.png`."""

if __name__ == "__main__":
    test_fields = {
        "ID for Reference Data": "REF_JUNE",
        "Reference Description": "REFERENCE JUNE",
        "Bucket Category": "Month",
        "Start Date of Reference": "01.07.2026",
        "End Date of Reference": "01.10.2026",
        "Simulation ID": "SIM_JUNE09",
        "Simulation Description": "PLANNING TEST",
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
