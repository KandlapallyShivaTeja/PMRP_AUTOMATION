import os
import sys
from typing import Optional, Any
from fastmcp import FastMCP
from sap_automation import run_automation, ensure_session, create_pir_automation
import time

# Ensure crucial Windows system environment variables are present under minimal environments (like IDEs or services)
if os.name == "nt":
    for var in ["SystemRoot", "windir", "PATH", "TEMP", "TMP"]:
        if var not in os.environ:
            if var == "SystemRoot" or var == "windir":
                os.environ[var] = r"C:\Windows"
            elif var == "PATH":
                os.environ[var] = r"C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem"
            elif var == "TEMP" or var == "TMP":
                os.environ[var] = r"C:\Windows\TEMP"

# Set the active working directory to the directory containing this script
os.chdir(os.path.dirname(os.path.abspath(__file__)))

mcp = FastMCP(
    name="SAP Automation",
    instructions="""
    Provides SAP OData-first API automation capabilities for scheduling pMRP jobs.
    """,
    version="1.1.0",
)

def log_mcp_call(tool_name: str, duration: float, status: str, details: str = ""):
    import os
    from datetime import datetime
    log_file_path = "mcp_execution.log"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] Tool: {tool_name} | Duration: {duration:.2f}s | Status: {status} | Details: {details}\n"
    try:
        with open(log_file_path, "a") as f:
            f.write(log_entry)
    except Exception as e:
        import sys
        print(f"[WARNING] Failed to write to log file: {e}", file=sys.stderr)


import atexit
import threading
import queue

_task_queue = queue.Queue()
_init_event = threading.Event()
_playwright = None
_browser = None
_context = None
_page = None

def _playwright_worker():
    global _playwright, _browser, _context, _page
    from playwright.sync_api import sync_playwright
    import os
    import sys
    from dotenv import load_dotenv
    from sap_automation import ensure_logged_in
    
    load_dotenv()
    sap_url = os.getenv("SAP_URL")
    state_path = "sap_session_state.json"
    headless = os.getenv("SAP_HEADLESS", "true").lower() == "true"
    
    print("[INFO] PlaywrightWorker: Launching persistent browser and logging in...", file=sys.stderr)
    try:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(headless=headless, args=[] if headless else ["--start-maximized"])
        
        context_args = {}
        if os.path.exists(state_path):
            context_args["storage_state"] = state_path
        if not headless:
            context_args["no_viewport"] = True
            
        _context = _browser.new_context(**context_args)
        _page = _context.new_page()
        
        ensure_logged_in(_page, _context, sap_url)
        print("[SUCCESS] PlaywrightWorker: Browser session is ready and logged in!", file=sys.stderr)
    except Exception as e:
        print(f"[ERROR] PlaywrightWorker: Startup pre-warm failed: {e}", file=sys.stderr)
    finally:
        _init_event.set()
        
    while True:
        task = _task_queue.get()
        if task is None:
            break
        
        func, args, kwargs, resp_queue = task
        try:
            # Check if page is closed or not logged in/on logon page
            is_logged_out = False
            try:
                if _page is None or _page.is_closed():
                    is_logged_out = True
                else:
                    curr_url = _page.url
                    if "login" in curr_url or "logon" in curr_url or "identity" in curr_url or curr_url == "about:blank":
                        is_logged_out = True
                    elif _page.locator("#j_username").first.is_visible():
                        is_logged_out = True
            except Exception:
                is_logged_out = True

            if is_logged_out:
                print("[INFO] PlaywrightWorker: Browser session offline or logged out. Re-logging in...", file=sys.stderr)
                if _page is None or _page.is_closed():
                    if _context:
                        _page = _context.new_page()
                    else:
                        _playwright = sync_playwright().start()
                        _browser = _playwright.chromium.launch(headless=headless, args=[] if headless else ["--start-maximized"])
                        _context = _browser.new_context(**context_args)
                        _page = _context.new_page()
                ensure_logged_in(_page, _context, sap_url)
                
            res = func(_page, *args, **kwargs)
            resp_queue.put(("SUCCESS", res))
        except Exception as ex:
            import traceback
            traceback.print_exc(file=sys.stderr)
            resp_queue.put(("ERROR", ex))
        finally:
            _task_queue.task_done()

# Start the dedicated worker thread immediately
_worker_thread = threading.Thread(target=_playwright_worker, daemon=True, name="PlaywrightWorker")
_worker_thread.start()

def run_on_worker(func, *args, **kwargs):
    resp_queue = queue.Queue()
    _task_queue.put((func, args, kwargs, resp_queue))
    status, result = resp_queue.get()
    if status == "ERROR":
        raise result
    return result

def get_shared_page():
    return _page

def cleanup_shared_browser():
    print("[INFO] Sending shutdown signal to worker thread...", file=sys.stderr)
    try:
        _task_queue.put(None)
        _worker_thread.join(timeout=5)
    except Exception:
        pass

atexit.register(cleanup_shared_browser)


@mcp.tool(timeout=600)
def create_pmrp_simulation(
    simulation_id: Optional[str] = "",
    plant: Optional[str] = "",
    reference_id: Optional[str] = "",
    material: Optional[str] = "",
    delete_existing_data: Optional[bool] = True,
    bucket_category: Optional[str] = "Month",
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    reference_description: Optional[str] = "Simulation Reference",
    simulation_description: Optional[str] = "Simulation Run",
    bom_usage: Optional[str] = "1",
    task_list_usage: Optional[str] = "Production",
    batch_simulations: Optional[list[dict[str, Any]]] = None,
) -> str:
    """
    Execute an SAP OData-first API automation to schedule a pMRP job. Supports both single simulation and batch runs.

    Parameters:
    - simulation_id: The Simulation ID (e.g. "SIM_PIR").
    - plant: The plant code or comma-separated list of plants (e.g. "1001").
    - reference_id: The ID for Reference Data (e.g. "REF_PIR").
    - material: Optional material number.
    - batch_simulations: Optional list of dicts for bulk/batch scheduling. E.g.:
                         [{"simulation_id": "SIM1", "plant": "1001", "reference_id": "REF1"}, ...]
    """
    import time
    start_time = time.time()
    status = "SUCCESS"
    details = ""
    try:
        def run_logic(page):
            sap_url = os.getenv("SAP_URL")
            base_ui_url = sap_url.rstrip('/') + "/ui"
            if not page.url.startswith(sap_url.rstrip('/')):
                print(f"[INFO] Page is at {page.url}. Navigating to SAP launchpad base UI first...", file=sys.stderr)
                page.goto(base_ui_url, wait_until="domcontentloaded", timeout=0)
                page.wait_for_timeout(3000)

            if batch_simulations:
                details_local = f"Batch of {len(batch_simulations)} simulations"
                results = []
                for idx, sim in enumerate(batch_simulations):
                    s_id = sim.get("simulation_id")
                    plt = sim.get("plant")
                    ref_id = sim.get("reference_id")
                    mat = sim.get("material", "")
                    del_existing = sim.get("delete_existing_data", True)
                    bucket = sim.get("bucket_category", "Month")
                    start = sim.get("start_date", "")
                    if not start:
                        start = "01.07.2026"
                    end = sim.get("end_date", "")
                    if not end:
                        end = "31.12.2026"
                    ref_desc = sim.get("reference_description", "Simulation Reference")
                    sim_desc = sim.get("simulation_description", "Simulation Run")
                    bom = sim.get("bom_usage", "1")
                    task = sim.get("task_list_usage", "Production")
                    
                    print(f"[INFO] Scheduling batch simulation {idx+1}/{len(batch_simulations)}: {s_id}...", file=sys.stderr)
                    fields = {
                        "Simulation ID": s_id,
                        "Plant": plt,
                        "ID for Reference Data": ref_id,
                        "Material": mat,
                        "Delete Existing pMRP Data": del_existing,
                        "Bucket Category": bucket,
                        "Start Date of Reference": start,
                        "End Date of Reference": end,
                        "Reference Description": ref_desc,
                        "Simulation Description": sim_desc,
                        "BOM Usage": bom,
                        "Task List Usage": task
                    }
                    res = run_automation(fields=fields, page=page)
                    results.append(f"Simulation {s_id}: {res}")
                return "\n".join(results)
            else:
                if not simulation_id or not plant or not reference_id:
                    raise Exception("Missing mandatory parameters: simulation_id, plant, and reference_id are required for single simulation.")
                details_local = f"Sim ID: {simulation_id}, Plant: {plant}"
                start = start_date
                if not start:
                    start = "01.07.2026"
                end = end_date
                if not end:
                    end = "31.12.2026"
                fields = {
                    "Simulation ID": simulation_id,
                    "Plant": plant,
                    "ID for Reference Data": reference_id,
                    "Material": material,
                    "Delete Existing pMRP Data": delete_existing_data,
                    "Bucket Category": bucket_category,
                    "Start Date of Reference": start,
                    "End Date of Reference": end,
                    "Reference Description": reference_description,
                    "Simulation Description": simulation_description,
                    "BOM Usage": bom_usage,
                    "Task List Usage": task_list_usage
                }
                res = run_automation(fields=fields, page=page)
                return res
                
        res_str = run_on_worker(run_logic)
        if "ERROR" in str(res_str) or "Failed" in str(res_str):
            status = "ERROR"
            details += f" | {res_str}"
        return res_str
    except Exception as e:
        status = "ERROR"
        details += f" | {e}"
        return f"ERROR: {e}"
    finally:
        log_mcp_call("create_pmrp_simulation", time.time() - start_time, status, details)


@mcp.tool(timeout=600)
def create_sap_pir(
    material: Optional[str] = "",
    plant: Optional[str] = "",
    requirements_plan: Optional[str] = "",
    mrp_area: Optional[str] = "",
    version: Optional[str] = "00",
    planning_period: Optional[str] = "M",
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    demands: Optional[list[dict[str, Any]]] = None,
    batch_demands: Optional[list[dict[str, Any]]] = None,
) -> str:
    """
    Automate Planned Independent Requirements (PIR) creation in SAP S/4HANA via MD61 transaction. Supports single and batch runs.

    Parameters:
    - material: The material number/code (e.g. "3381").
    - plant: The plant code (e.g. "1001").
    - demands: A list of dicts specifying target planning periods and quantity. E.g.:
               [{"date": "07.2026", "quantity": 250}, {"date": "08.2026", "quantity": 600}]
    - batch_demands: Optional list of dicts for bulk/batch PIR creation. E.g.:
                     [{"material": "3381", "plant": "1001", "demands": [...]}, ...]
    """
    import time
    start_time = time.time()
    status = "SUCCESS"
    details = ""
    try:
        def run_logic(page):
            if batch_demands:
                details_local = f"Batch of {len(batch_demands)} PIRs"
                results = []
                for idx, item in enumerate(batch_demands):
                    mat = item.get("material")
                    plt = item.get("plant")
                    req_plan = item.get("requirements_plan", requirements_plan)
                    area = item.get("mrp_area", mrp_area)
                    ver = item.get("version", version)
                    period = item.get("planning_period", planning_period)
                    start = item.get("start_date", start_date)
                    end = item.get("end_date", end_date)
                    dems = item.get("demands")
                    
                    print(f"[INFO] Creating batch PIR {idx+1}/{len(batch_demands)}: material {mat}, plant {plt}...", file=sys.stderr)
                    res = create_pir_automation(
                        material=mat,
                        plant=plt,
                        requirements_plan=req_plan,
                        mrp_area=area,
                        version=ver,
                        planning_period=period,
                        start_date=start,
                        end_date=end,
                        demands=dems,
                        page=page
                    )
                    results.append(f"Material {mat} at Plant {plt}: {res}")
                return "\n".join(results)
            else:
                if not material or not plant or not demands:
                    raise Exception("Missing mandatory parameters: material, plant, and demands are required for single PIR creation.")
                details_local = f"Material: {material}, Plant: {plant}"
                res = create_pir_automation(
                    material=material,
                    plant=plant,
                    requirements_plan=requirements_plan,
                    mrp_area=mrp_area,
                    version=version,
                    planning_period=planning_period,
                    start_date=start_date,
                    end_date=end_date,
                    demands=demands,
                    page=page
                )
                return res
                
        res_str = run_on_worker(run_logic)
        if "ERROR" in str(res_str):
            status = "ERROR"
            details += f" | {res_str}"
        return res_str
    except Exception as e:
        status = "ERROR"
        details += f" | {e}"
        return f"ERROR: {e}"
    finally:
        log_mcp_call("create_sap_pir", time.time() - start_time, status, details)


@mcp.tool(timeout=600)
def check_pmrp_simulation_kpis(
    simulation_id: Optional[str] = "",
    batch_simulations: Optional[list[str]] = None,
) -> str:
    """
    Check the status and details of pMRP Simulation KPIs. Supports single and batch checks.

    Parameters:
    - simulation_id: The Simulation ID to check (defaults to "SIM_PIR").
    - batch_simulations: Optional list of Simulation IDs to check in bulk. E.g.: ["SIM1", "SIM2"]
    """
    from sap_automation import ensure_logged_in, get_simulation_kpis, wait_for_simulation_ready
    import sys
    import time
    
    sap_url = os.getenv("SAP_URL")
    
    start_time = time.time()
    status = "SUCCESS"
    details = ""
    
    try:
        def run_logic(page):
            if batch_simulations:
                details_local = f"Batch of {len(batch_simulations)} simulation KPIs"
                reports = []
                for s_id in batch_simulations:
                    sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{s_id}')"
                    page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
                    wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                    kpis = get_simulation_kpis(page, wait_for_load=False)
                    
                    report = f"""### Simulation KPI Status: {s_id}
| KPI Metric | Value |
|------------|-------|
| **Capacity Issues** | {kpis.get('Capacity Issues', 'unknown')} |
| **Delivery Performance** | {kpis.get('Delivery Performance', 'unknown')} |
| **Materials with Invalid Source** | {kpis.get('Invalid Source', 'unknown')} |
| **Violated Constraints** | {kpis.get('Violated Constraints', 'unknown')} |
| **Red (Overloaded) Input Cells** | {kpis.get('Red Input Cells', '0')} |
"""
                    if kpis.get("Detailed Issues"):
                        report += "\n#### Detailed Capacity Issues:\n| Issue Category | Affected Objects | Affected Months | Affected Demands | Impact Score | Overload % | Cause |\n|---|---|---|---|---|---|---|\n"
                        for issue in kpis["Detailed Issues"]:
                            report += f"| {issue.get('Category')} | {issue.get('Object')} | {issue.get('Affected Months')} | {issue.get('Affected Demands')} | {issue.get('Impact Score')} | {issue.get('Overload %')} | {issue.get('Cause')} |\n"
                    
                    if kpis.get("Page Messages"):
                        report += "\n#### System Messages:\n" + "\n".join([f"- {msg}" for msg in kpis["Page Messages"]]) + "\n"
                    
                    reports.append(report)
                return "\n\n".join(reports)
            else:
                sim_id = simulation_id if simulation_id else "SIM_PIR"
                details_local = f"Sim ID: {sim_id}"
                sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{sim_id}')"
                page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
                wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                kpis = get_simulation_kpis(page, wait_for_load=False)
                
                report = f"""# SAP pMRP Simulation KPI Status: {sim_id}
                
| KPI Metric | Value |
|------------|-------|
| **Capacity Issues** | {kpis.get('Capacity Issues', 'unknown')} |
| **Delivery Performance** | {kpis.get('Delivery Performance', 'unknown')} |
| **Materials with Invalid Source** | {kpis.get('Invalid Source', 'unknown')} |
| **Violated Constraints** | {kpis.get('Violated Constraints', 'unknown')} |
| **Red (Overloaded) Input Cells** | {kpis.get('Red Input Cells', '0')} |
"""
                if kpis.get("Detailed Issues"):
                    report += "\n### Detailed Capacity Issues:\n| Issue Category | Affected Objects | Affected Months | Affected Demands | Impact Score | Overload % | Cause |\n|---|---|---|---|---|---|---|\n"
                    for issue in kpis["Detailed Issues"]:
                        report += f"| {issue.get('Category')} | {issue.get('Object')} | {issue.get('Affected Months')} | {issue.get('Affected Demands')} | {issue.get('Impact Score')} | {issue.get('Overload %')} | {issue.get('Cause')} |\n"
                
                if kpis.get("Page Messages"):
                    report += "\n### System Messages:\n" + "\n".join([f"- {msg}" for msg in kpis["Page Messages"]]) + "\n"
                    
                return report
                
        return run_on_worker(run_logic)
    except Exception as e:
        status = "ERROR"
        details += f" | {e}"
        return f"ERROR: Failed to extract KPIs: {e}"
    finally:
        log_mcp_call("check_pmrp_simulation_kpis", time.time() - start_time, status, details)


@mcp.tool(timeout=400)
def remediate_pmrp_simulation(
    simulation_id: Optional[str] = "",
    batch_simulations: Optional[list[str]] = None,
) -> str:
    """
    Run capacity adaptation/remediation for a specific pMRP simulation to resolve all capacity overloads.
    Supports single and batch runs.
    """
    from sap_automation import ensure_logged_in, get_simulation_kpis, wait_for_simulation_ready, run_capacity_adaptation
    import sys
    import re
    import time
    
    sap_url = os.getenv("SAP_URL")
    start_time = time.time()
    status = "SUCCESS"
    details = ""
    
    try:
        def run_logic(page):
            if batch_simulations:
                details_local = f"Batch of {len(batch_simulations)} simulations"
                results = []
                for s_id in batch_simulations:
                    sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{s_id}')"
                    page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
                    wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                    
                    kpis_start = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                    try:
                        issues_val = int(re.sub(r'\D', '', kpis_start.get("Capacity Issues", "0")))
                    except Exception:
                        issues_val = 0
                        
                    if issues_val > 0:
                        print(f"[INFO] Capacity issues detected: {issues_val}. Running Capacity Adaptation for {s_id}...", file=sys.stderr)
                        run_capacity_adaptation(page)
                        wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                    
                    kpis_final = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                    results.append(f"Simulation {s_id} remediated. Final KPIs: {kpis_final}")
                return "\n".join(results)
            else:
                sim_id = simulation_id if simulation_id else "SIM_PIR"
                details_local = f"Sim ID: {sim_id}"
                sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{sim_id}')"
                page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
                wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                
                kpis_start = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                print(f"[INFO] Initial KPIs: {kpis_start}", file=sys.stderr)
                
                try:
                    issues_val = int(re.sub(r'\D', '', kpis_start.get("Capacity Issues", "0")))
                except Exception:
                    issues_val = 0
                    
                if issues_val > 0:
                    print(f"[INFO] Capacity issues detected: {issues_val}. Running Capacity Adaptation...", file=sys.stderr)
                    run_capacity_adaptation(page)
                    wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                else:
                    print("[INFO] No capacity issues detected. Skipping remediation.", file=sys.stderr)
                    
                kpis_final = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                res_str = f"SUCCESS: Completed capacity remediation for simulation {sim_id}. Initial KPIs: {kpis_start}. Final KPIs: {kpis_final}"
                return res_str
                
        res_str = run_on_worker(run_logic)
        if "SUCCESS" in res_str:
            details += f" | {res_str}"
        return res_str
    except Exception as e:
        status = "ERROR"
        details += f" | {e}"
        return f"ERROR: Capacity remediation failed: {e}"
    finally:
        log_mcp_call("remediate_pmrp_simulation", time.time() - start_time, status, details)


@mcp.tool(timeout=300)
def release_pmrp_simulation(
    simulation_id: Optional[str] = "",
    batch_simulations: Optional[list[str]] = None,
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
    capacity_change_proposals: Optional[bool] = None,
) -> str:
    """
    Release a pMRP simulation to publish proposal results. Supports single/batch runs and configuring release options.
    """
    from sap_automation import run_release_simulation
    import time
    import sys
    
    sap_url = os.getenv("SAP_URL")
    start_time = time.time()
    status = "SUCCESS"
    details = ""
    
    try:
        def run_logic(page):
            if batch_simulations:
                details_local = f"Batch of {len(batch_simulations)} releases"
                results = []
                for s_id in batch_simulations:
                    print(f"[INFO] Releasing simulation {s_id}...", file=sys.stderr)
                    run_release_simulation(
                        page, sap_url, simulation_id=s_id,
                        ddmrp_components=ddmrp_components,
                        ddmrp_req_version=ddmrp_req_version,
                        ddmrp_version_active=ddmrp_version_active,
                        top_level_materials=top_level_materials,
                        top_level_req_version=top_level_req_version,
                        top_level_version_active=top_level_version_active,
                        subassembly_components=subassembly_components,
                        non_mrp_kanban=non_mrp_kanban,
                        selected_non_mrp=selected_non_mrp,
                        selected_non_mrp_type=selected_non_mrp_type,
                        selected_non_mrp_req_version=selected_non_mrp_req_version,
                        selected_non_mrp_version_active=selected_non_mrp_version_active,
                        capacity_change_proposals=capacity_change_proposals
                    )
                    results.append(f"Simulation {s_id}: Released successfully.")
                return "\n".join(results)
            else:
                sim_id = simulation_id if simulation_id else "SIM_PIR"
                details_local = f"Sim ID: {sim_id}"
                print(f"[INFO] Releasing simulation {sim_id}...", file=sys.stderr)
                run_release_simulation(
                    page, sap_url, simulation_id=sim_id,
                    ddmrp_components=ddmrp_components,
                    ddmrp_req_version=ddmrp_req_version,
                    ddmrp_version_active=ddmrp_version_active,
                    top_level_materials=top_level_materials,
                    top_level_req_version=top_level_req_version,
                    top_level_version_active=top_level_version_active,
                    subassembly_components=subassembly_components,
                    non_mrp_kanban=non_mrp_kanban,
                    selected_non_mrp=selected_non_mrp,
                    selected_non_mrp_type=selected_non_mrp_type,
                    selected_non_mrp_req_version=selected_non_mrp_req_version,
                    selected_non_mrp_version_active=selected_non_mrp_version_active,
                    capacity_change_proposals=capacity_change_proposals
                )
                return f"SUCCESS: Released simulation {sim_id} successfully."
                
        return run_on_worker(run_logic)
    except Exception as e:
        status = "ERROR"
        details += f" | {e}"
        return f"ERROR: Release failed: {e}"
    finally:
        log_mcp_call("release_pmrp_simulation", time.time() - start_time, status, details)


@mcp.tool(timeout=600)
def remediate_and_release_simulation(
    simulation_id: Optional[str] = "",
    batch_simulations: Optional[list[str]] = None,
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
    capacity_change_proposals: Optional[bool] = None,
) -> str:
    """
    Run capacity remediation and release for a specific pMRP simulation. Supports configuring release options.
    """
    start_time = time.time()
    status = "SUCCESS"
    details = f"Sim ID: {simulation_id}"
    
    try:
        print("[INFO] Initiating remediate_pmrp_simulation...", file=sys.stderr)
        rem_res = remediate_pmrp_simulation(simulation_id=simulation_id, batch_simulations=batch_simulations)
        if "ERROR" in rem_res:
            raise Exception(f"Remediation step failed: {rem_res}")
            
        print("[INFO] Initiating release_pmrp_simulation...", file=sys.stderr)
        rel_res = release_pmrp_simulation(
            simulation_id=simulation_id, 
            batch_simulations=batch_simulations,
            ddmrp_components=ddmrp_components,
            ddmrp_req_version=ddmrp_req_version,
            ddmrp_version_active=ddmrp_version_active,
            top_level_materials=top_level_materials,
            top_level_req_version=top_level_req_version,
            top_level_version_active=top_level_version_active,
            subassembly_components=subassembly_components,
            non_mrp_kanban=non_mrp_kanban,
            selected_non_mrp=selected_non_mrp,
            selected_non_mrp_type=selected_non_mrp_type,
            selected_non_mrp_req_version=selected_non_mrp_req_version,
            selected_non_mrp_version_active=selected_non_mrp_version_active,
            capacity_change_proposals=capacity_change_proposals
        )
        if "ERROR" in rel_res:
            raise Exception(f"Release step failed: {rel_res}")
            
        return f"{rem_res}\n{rel_res}"
    except Exception as e:
        status = "ERROR"
        details += f" | {e}"
        try:
            page = get_shared_page()
            page.screenshot(path="remediate_error.png")
            print("[INFO] Saved error screenshot to remediate_error.png", file=sys.stderr)
        except Exception:
            pass
        return f"ERROR: Remediate and release failed: {e}"
    finally:
        log_mcp_call("remediate_and_release_simulation", time.time() - start_time, status, details)


def resolve_dates(demands: list[dict[str, Any]], start_date: str, end_date: str) -> tuple[str, str]:
    if start_date and end_date:
        return start_date, end_date
    import datetime
    import re
    parsed_dates = []
    if demands:
        for d in demands:
            date_str = d.get("date") or d.get("period") or d.get("month") or d.get("week") or d.get("day")
            if not date_str:
                continue
            date_str = str(date_str).strip()
            
            m = re.match(r'^(\d{1,2})\.(\d{4})$', date_str)
            if m:
                parsed_dates.append(datetime.date(int(m.group(2)), int(m.group(1)), 1))
                continue
            m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', date_str)
            if m:
                parsed_dates.append(datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
                continue
            m = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', date_str)
            if m:
                parsed_dates.append(datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
                continue
            m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', date_str)
            if m:
                parsed_dates.append(datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
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
            start_date = "01.07.2026"
        if not end_date:
            end_date = "31.12.2026"
    return start_date, end_date


@mcp.tool(timeout=600)
def run_complete_pmrp_pipeline(
    material: Optional[str] = "",
    plant: Optional[str] = "",
    simulation_id: Optional[str] = "",
    reference_id: Optional[str] = "",
    demands: Optional[list[dict[str, Any]]] = None,
    requirements_plan: Optional[str] = "",
    mrp_area: Optional[str] = "",
    version: Optional[str] = "00",
    planning_period: Optional[str] = "M",
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    delete_existing_data: Optional[bool] = True,
    bucket_category: Optional[str] = "Month",
    reference_description: Optional[str] = "Simulation Reference",
    simulation_description: Optional[str] = "Simulation Run",
    bom_usage: Optional[str] = "1",
    task_list_usage: Optional[str] = "Production",
    batch_pipelines: Optional[list[dict[str, Any]]] = None,
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
    capacity_change_proposals: Optional[bool] = None,
) -> str:
    """
    Run the entire end-to-end pMRP pipeline (PIR creation -> Simulation Scheduling -> Remediation -> Release) in a single browser session.
    Supports single and batch runs, and custom release configurations.
    """
    from sap_automation import (
        create_pir_automation,
        run_automation,
        ensure_logged_in,
        get_simulation_kpis,
        run_demand_shifting,
        run_capacity_adaptation,
        run_release_simulation,
        wait_for_simulation_ready
    )
    import time
    import re
    import sys
    
    sap_url = os.getenv("SAP_URL")
    
    start_time = time.time()
    status = "SUCCESS"
    details = ""
    
    try:
        def run_logic(page):
            if batch_pipelines:
                details_local = f"Batch of {len(batch_pipelines)} pipelines"
                results = []
                for idx, pipe in enumerate(batch_pipelines):
                    mat = pipe.get("material")
                    plt = pipe.get("plant")
                    s_id = pipe.get("simulation_id")
                    r_id = pipe.get("reference_id")
                    dems = pipe.get("demands")
                    req_plan = pipe.get("requirements_plan", requirements_plan)
                    area = pipe.get("mrp_area", mrp_area)
                    ver = pipe.get("version", version)
                    period = pipe.get("planning_period", planning_period)
                    start = pipe.get("start_date", start_date)
                    end = pipe.get("end_date", end_date)
                    
                    # Resolve dates from demands if missing
                    start, end = resolve_dates(dems, start, end)
                    
                    del_existing = pipe.get("delete_existing_data", delete_existing_data)
                    bucket = pipe.get("bucket_category", bucket_category)
                    ref_desc = pipe.get("reference_description", reference_description)
                    sim_desc = pipe.get("simulation_description", simulation_description)
                    bom = pipe.get("bom_usage", bom_usage)
                    task = pipe.get("task_list_usage", task_list_usage)
                    
                    print(f"[INFO] Unified E2E: Processing batch pipeline {idx+1}/{len(batch_pipelines)}: simulation {s_id}...", file=sys.stderr)
                    
                    # 1. Create PIR demands
                    create_pir_automation(
                        material=mat,
                        plant=plt,
                        requirements_plan=req_plan,
                        mrp_area=area,
                        version=ver,
                        planning_period=period,
                        start_date=start,
                        end_date=end,
                        demands=dems,
                        page=page
                    )
                    
                    # Navigate to jobs page to bootstrap OData correctly
                    job_init_url = sap_url.rstrip('/') + "/ui#PMRPSimulation-simulate"
                    page.goto(job_init_url, wait_until="domcontentloaded", timeout=0)
                    page.reload(wait_until="domcontentloaded", timeout=0)
                    page.wait_for_timeout(3000)
                    
                    # 2. Schedule simulation
                    fields = {
                        "Simulation ID": s_id,
                        "Plant": plt,
                        "ID for Reference Data": r_id,
                        "Material": mat,
                        "Delete Existing pMRP Data": del_existing,
                        "Bucket Category": bucket,
                        "Start Date of Reference": start,
                        "End Date of Reference": end,
                        "Reference Description": ref_desc,
                        "Simulation Description": sim_desc,
                        "BOM Usage": bom,
                        "Task List Usage": task
                    }
                    res_sched = run_automation(fields=fields, page=page)
                    if "Failed" in res_sched or "ERROR" in res_sched or "failed" in res_sched.lower():
                        results.append(f"Simulation {s_id}: ERROR - Scheduling failed: {res_sched}")
                        continue
                    
                    # 3. Wait for calculation to finish
                    sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{s_id}')"
                    page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
                    wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                    
                    # 4. Extract and remediate KPIs
                    kpis_start = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                    try:
                        issues_val = int(re.sub(r'\D', '', kpis_start.get("Capacity Issues", "0")))
                    except Exception:
                        issues_val = 0
                        
                    if issues_val > 0:
                        run_capacity_adaptation(page)
                        wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                        kpis_after = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                        try:
                            issues_val = int(re.sub(r'\D', '', kpis_after.get("Capacity Issues", "0")))
                        except Exception:
                            issues_val = 0
                    else:
                        kpis_after = kpis_start
                        
                    if issues_val > 0:
                        results.append(f"Simulation {s_id}: ERROR - Unresolved capacity issues (Remaining: {issues_val})")
                        continue
                        
                    # 5. Release
                    run_release_simulation(
                        page, sap_url, simulation_id=s_id,
                        ddmrp_components=pipe.get("ddmrp_components", ddmrp_components),
                        ddmrp_req_version=pipe.get("ddmrp_req_version", ddmrp_req_version),
                        ddmrp_version_active=pipe.get("ddmrp_version_active", ddmrp_version_active),
                        top_level_materials=pipe.get("top_level_materials", top_level_materials),
                        top_level_req_version=pipe.get("top_level_req_version", top_level_req_version),
                        top_level_version_active=pipe.get("top_level_version_active", top_level_version_active),
                        subassembly_components=pipe.get("subassembly_components", subassembly_components),
                        non_mrp_kanban=pipe.get("non_mrp_kanban", non_mrp_kanban),
                        selected_non_mrp=pipe.get("selected_non_mrp", selected_non_mrp),
                        selected_non_mrp_type=pipe.get("selected_non_mrp_type", selected_non_mrp_type),
                        selected_non_mrp_req_version=pipe.get("selected_non_mrp_req_version", selected_non_mrp_req_version),
                        selected_non_mrp_version_active=pipe.get("selected_non_mrp_version_active", selected_non_mrp_version_active),
                        capacity_change_proposals=pipe.get("capacity_change_proposals", capacity_change_proposals)
                    )
                    results.append(f"Simulation {s_id}: E2E completed successfully. KPIs: {kpis_after}")
                return "\n".join(results)
            else:
                if not material or not plant or not simulation_id or not reference_id or not demands:
                    raise Exception("Missing mandatory parameters: material, plant, simulation_id, reference_id, and demands are required for single pipeline.")
                details_local = f"Material: {material}, Plant: {plant}, Sim ID: {simulation_id}"
                
                # Resolve dates from demands if missing
                resolved_start, resolved_end = resolve_dates(demands, start_date, end_date)
                
                # 1. Create PIR demands
                print("[INFO] Unified E2E: Creating PIR demands...", file=sys.stderr)
                create_pir_automation(
                    material=material,
                    plant=plant,
                    requirements_plan=requirements_plan,
                    mrp_area=mrp_area,
                    version=version,
                    planning_period=planning_period,
                    start_date=resolved_start,
                    end_date=resolved_end,
                    demands=demands,
                    page=page
                )
                
                # Navigate to jobs page to bootstrap OData correctly
                print("[INFO] Unified E2E: Navigating to jobs workspace to bootstrap OData...", file=sys.stderr)
                job_init_url = sap_url.rstrip('/') + "/ui#PMRPSimulation-simulate"
                page.goto(job_init_url, wait_until="domcontentloaded", timeout=0)
                page.reload(wait_until="domcontentloaded", timeout=0)
                page.wait_for_timeout(3000)
                
                # 2. Schedule simulation
                print("[INFO] Unified E2E: Scheduling fresh pMRP simulation...", file=sys.stderr)
                fields = {
                    "Simulation ID": simulation_id,
                    "Plant": plant,
                    "ID for Reference Data": reference_id,
                    "Material": material,
                    "Delete Existing pMRP Data": delete_existing_data,
                    "Bucket Category": bucket_category,
                    "Start Date of Reference": resolved_start,
                    "End Date of Reference": resolved_end,
                    "Reference Description": reference_description,
                    "Simulation Description": simulation_description,
                    "BOM Usage": bom_usage,
                    "Task List Usage": task_list_usage
                }
                res_sched = run_automation(fields=fields, page=page)
                if "Failed" in res_sched or "ERROR" in res_sched or "failed" in res_sched.lower():
                    raise Exception(f"Simulation scheduling failed: {res_sched}")
                
                # 3. Wait for calculation to finish
                print("[INFO] Unified E2E: Waiting for simulation ready...", file=sys.stderr)
                sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{simulation_id}')"
                page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
                wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                
                # 4. Extract and remediate KPIs
                print("[INFO] Unified E2E: Checking KPIs...", file=sys.stderr)
                kpis_start = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                print(f"[INFO] Initial KPIs: {kpis_start}", file=sys.stderr)
                
                try:
                    issues_val = int(re.sub(r'\D', '', kpis_start.get("Capacity Issues", "0")))
                except Exception:
                    issues_val = 0
                    
                if issues_val > 0:
                    print("[INFO] Unified E2E: Running Capacity Adaptation...", file=sys.stderr)
                    run_capacity_adaptation(page)
                    wait_for_simulation_ready(page, sim_url, fast_kpi_only=True)
                    
                    kpis_after = get_simulation_kpis(page, wait_for_load=False, extract_details=False)
                    print(f"[INFO] KPIs after adaptation: {kpis_after}", file=sys.stderr)
                    try:
                        issues_val = int(re.sub(r'\D', '', kpis_after.get("Capacity Issues", "0")))
                    except Exception:
                        issues_val = 0
                else:
                    kpis_after = kpis_start
                    print("[INFO] Unified E2E: No capacity issues detected. Skipping adaptation.", file=sys.stderr)
                    
                if issues_val > 0:
                    raise Exception(f"Capacity issues could not be fully resolved (Remaining: {issues_val}). Release aborted.")
                    
                # 5. Release
                print("[INFO] Unified E2E: Releasing simulation...", file=sys.stderr)
                run_release_simulation(
                    page, sap_url, simulation_id=simulation_id,
                    ddmrp_components=ddmrp_components,
                    ddmrp_req_version=ddmrp_req_version,
                    ddmrp_version_active=ddmrp_version_active,
                    top_level_materials=top_level_materials,
                    top_level_req_version=top_level_req_version,
                    top_level_version_active=top_level_version_active,
                    subassembly_components=subassembly_components,
                    non_mrp_kanban=non_mrp_kanban,
                    selected_non_mrp=selected_non_mrp,
                    selected_non_mrp_type=selected_non_mrp_type,
                    selected_non_mrp_req_version=selected_non_mrp_req_version,
                    selected_non_mrp_version_active=selected_non_mrp_version_active,
                    capacity_change_proposals=capacity_change_proposals
                )
                
                res_str = f"SUCCESS: End-to-end simulation {simulation_id} processed and released successfully. Start KPIs: {kpis_start} -> End KPIs: {kpis_after}"
                return res_str
                
        res_str = run_on_worker(run_logic)
        if "SUCCESS" in res_str:
            details += f" | {res_str}"
        return res_str
            
    except Exception as e:
        status = "ERROR"
        details += f" | {e}"
        try:
            if _page:
                _page.screenshot(path="e2e_error.png")
                print("[INFO] Saved error screenshot to e2e_error.png", file=sys.stderr)
        except Exception:
            pass
        return f"ERROR: E2E pipeline failed: {e}"
    finally:
        log_mcp_call("run_complete_pmrp_pipeline", time.time() - start_time, status, details)


if __name__ == "__main__":
    os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
    
    # Wait for the background worker thread to finish launching the browser and logging in
    print("[INFO] Waiting for PlaywrightWorker to initialize browser session...", file=sys.stderr)
    _init_event.wait(timeout=120)
    if _page is not None and not _page.is_closed():
        print("[SUCCESS] Persistent shared browser session is ready!", file=sys.stderr)
    else:
        print("[WARNING] Shared browser session pre-warm did not succeed, will retry on demand.", file=sys.stderr)
    
    # Must bind to 0.0.0.0 so that connection from Docker container succeeds
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8001,
        show_banner=False
    )