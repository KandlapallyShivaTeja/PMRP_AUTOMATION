import os
import sys
from typing import Optional, Any
from fastmcp import FastMCP
from sap_automation import run_automation, ensure_session, create_pir_automation

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


@mcp.tool(timeout=300)
def create_pmrp_simulation(
    simulation_id: str,
    plant: str,
    reference_id: str,
    material: Optional[str] = "",
    delete_existing_data: Optional[bool] = True,
    bucket_category: Optional[str] = "Month",
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    reference_description: Optional[str] = "Simulation Reference",
    simulation_description: Optional[str] = "Simulation Run",
    bom_usage: Optional[str] = "1",
    task_list_usage: Optional[str] = "Production",
) -> str:
    """
    Execute an SAP OData-first API automation to schedule a pMRP job.

    Parameters:
    - simulation_id: The Simulation ID (e.g. "SIM_PIR"). Mandatory.
    - plant: The plant code or comma-separated list of plants (e.g. "1001"). Mandatory.
    - reference_id: The ID for Reference Data (e.g. "REF_PIR"). Mandatory.
    - material: Optional material number or comma-separated list of materials (e.g. "3381").
    - delete_existing_data: Whether to delete existing pMRP data for this simulation ID, defaults to True.
    - bucket_category: The bucket category ("Month" or "Week"), defaults to "Month".
    - start_date: Start date of reference in YYYY-MM-DD or DD.MM.YYYY format.
    - end_date: End date of reference in YYYY-MM-DD or DD.MM.YYYY format.
    - reference_description: Optional description for reference data.
    - simulation_description: Optional description for simulation.
    - bom_usage: BOM Usage code, defaults to "1".
    - task_list_usage: Task List Usage, defaults to "Production".
    """
    fields = {
        "Simulation ID": simulation_id,
        "Plant": plant,
        "ID for Reference Data": reference_id,
        "Material": material,
        "Delete Existing pMRP Data": delete_existing_data,
        "Bucket Category": bucket_category,
        "Start Date of Reference": start_date,
        "End Date of Reference": end_date,
        "Reference Description": reference_description,
        "Simulation Description": simulation_description,
        "BOM Usage": bom_usage,
        "Task List Usage": task_list_usage
    }
    try:
        return run_automation(fields=fields)
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool(timeout=300)
def create_sap_pir(
    material: str,
    plant: str,
    requirements_plan: Optional[str] = "",
    mrp_area: Optional[str] = "",
    version: Optional[str] = "00",
    planning_period: Optional[str] = "M",
    start_date: Optional[str] = "",
    end_date: Optional[str] = "",
    demands: list[dict[str, Any]] = None
) -> str:
    """
    Automate Planned Independent Requirements (PIR) creation in SAP S/4HANA via MD61 transaction.

    This tool navigates directly to the MD61 transaction and injects monthly or weekly
    demand quantity values for the specified material and plant, then saves the requirements.

    Parameters:
    - material: The material number/code (e.g. "3381"). Mandatory.
    - plant: The plant code (e.g. "1001"). Mandatory.
    - requirements_plan: Optional requirement plan filter name.
    - mrp_area: Optional MRP area code.
    - version: Requirement version, defaults to "00".
    - planning_period: The planning period type ("M" for Month, "W" for Week), defaults to "M".
    - start_date: Evaluation start date in DD.MM.YYYY format (computed from demands if empty).
    - end_date: Evaluation end date in DD.MM.YYYY format (computed from demands if empty).
    - demands: A list of dicts specifying target planning periods and quantity. E.g.:
               [{"date": "07.2026", "quantity": 250}, {"date": "08.2026", "quantity": 600}]
    """
    try:
        return create_pir_automation(
            material=material,
            plant=plant,
            requirements_plan=requirements_plan,
            mrp_area=mrp_area,
            version=version,
            planning_period=planning_period,
            start_date=start_date,
            end_date=end_date,
            demands=demands
        )
    except Exception as e:
        return f"ERROR: {e}"


@mcp.tool(timeout=300)
def check_pmrp_simulation_kpis(
    simulation_id: str = "SIM_PIR"
) -> str:
    """
    Check the status and details of pMRP Simulation KPIs.

    This tool navigates to the Fiori Simulation dashboard for the given simulation,
    extracts the current KPIs (Capacity Issues, Delivery Performance, Invalid Sources, 
    Violated Constraints, Red Input Cells), and returns a detailed report.

    Parameters:
    - simulation_id: The Simulation ID to check (defaults to "SIM_PIR").
    """
    from playwright.sync_api import sync_playwright
    from sap_automation import ensure_logged_in, get_simulation_kpis
    import sys
    
    sap_url = os.getenv("SAP_URL")
    state_path = "sap_session_state.json"
    sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{simulation_id}')"
    headless = os.getenv("SAP_HEADLESS", "true").lower() == "true"
    
    print(f"[INFO] Checking KPIs for simulation {simulation_id}...", file=sys.stderr)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context_args = {}
        if os.path.exists(state_path):
            context_args["storage_state"] = state_path
            
        context = browser.new_context(**context_args)
        page = context.new_page()
        try:
            ensure_logged_in(page, context, sim_url)
            kpis = get_simulation_kpis(page)
            
            report = f"""# SAP pMRP Simulation KPI Status: {simulation_id}
            
| KPI Metric | Value |
|------------|-------|
| **Capacity Issues** | {kpis.get('Capacity Issues', 'unknown')} |
| **Delivery Performance** | {kpis.get('Delivery Performance', 'unknown')} |
| **Materials with Invalid Source** | {kpis.get('Invalid Source', 'unknown')} |
| **Violated Constraints** | {kpis.get('Violated Constraints', 'unknown')} |
| **Red (Overloaded) Input Cells** | {kpis.get('Red Input Cells', '0')} |
"""
            return report
        except Exception as e:
            return f"ERROR: Failed to extract KPIs: {e}"
        finally:
            browser.close()


@mcp.tool(timeout=300)
def remediate_and_release_simulation(
    simulation_id: str = "SIM_PIR"
) -> str:
    """
    Run the capacity adaptation and release flow for a specific pMRP simulation.

    This tool navigates to the Fiori Simulation dashboard for the given simulation,
    performs prioritized demand shifting, falls back to capacity adaptation if issues remain,
    and triggers the final simulation release.

    Parameters:
    - simulation_id: The Simulation ID to remediate and release (defaults to "SIM_PIR").
    """
    from playwright.sync_api import sync_playwright
    from sap_automation import ensure_logged_in, get_simulation_kpis, wait_for_simulation_ready, run_demand_shifting, run_capacity_adaptation, run_release_simulation
    import sys
    import re
    
    sap_url = os.getenv("SAP_URL")
    state_path = "sap_session_state.json"
    sim_url = sap_url.rstrip('/') + f"/ui#PMRPSimulation-simulate&/PmrpSimulation('{simulation_id}')"
    headless = os.getenv("SAP_HEADLESS", "true").lower() == "true"
    
    print(f"[INFO] Running remediate_and_release_simulation for simulation {simulation_id}...", file=sys.stderr)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context_args = {}
        if os.path.exists(state_path):
            context_args["storage_state"] = state_path
            
        context = browser.new_context(**context_args)
        page = context.new_page()
        try:
            ensure_logged_in(page, context, sim_url)
            
            kpis_start = get_simulation_kpis(page)
            print(f"[INFO] Initial KPIs: {kpis_start}", file=sys.stderr)
            
            try:
                issues_val = int(re.sub(r'\D', '', kpis_start.get("Capacity Issues", "0")))
            except Exception:
                issues_val = 0
                
            if issues_val > 0:
                # 1. Prioritize Shift Demand (Demand Shifting)
                run_demand_shifting(page)
                
                # Check issues count after shifting
                kpis_mid = get_simulation_kpis(page)
                print(f"[INFO] KPIs after demand shifting: {kpis_mid}", file=sys.stderr)
                try:
                    issues_val = int(re.sub(r'\D', '', kpis_mid.get("Capacity Issues", "0")))
                except Exception:
                    issues_val = 0
                    
                # 2. Fallback to Capacity Adaptation if issues remain
                if issues_val > 0:
                    print("[INFO] Capacity issues still remain. Falling back to Capacity Adaptation...", file=sys.stderr)
                    run_capacity_adaptation(page)
                    wait_for_simulation_ready(page, sim_url)
                    
                    # 3. Second-stage Demand Shifting for any newly exposed overloads
                    kpis_after_adapt = get_simulation_kpis(page)
                    try:
                        issues_val = int(re.sub(r'\D', '', kpis_after_adapt.get("Capacity Issues", "0")))
                    except Exception:
                        issues_val = 0
                    if issues_val > 0:
                        print("[INFO] Running second-stage demand shifting on newly exposed overloads...", file=sys.stderr)
                        run_demand_shifting(page)
                else:
                    print("[INFO] All capacity issues resolved via demand shifting. Skipping Capacity Adaptation.", file=sys.stderr)
            else:
                print("[INFO] No capacity issues detected. Skipping remediation.", file=sys.stderr)
                
            # Check final issues count before release
            kpis_post_remediate = get_simulation_kpis(page)
            try:
                final_issues = int(re.sub(r'\D', '', kpis_post_remediate.get("Capacity Issues", "0")))
            except Exception:
                final_issues = 0
                
            if final_issues > 0:
                raise Exception(f"Capacity issues could not be fully resolved (Remaining: {final_issues}). Release aborted.")
                
            # 3. Finalize and Release
            run_release_simulation(page, sap_url)
            
            # Fetch final KPIs after release
            page.goto(sim_url, wait_until="domcontentloaded", timeout=0)
            kpis_final = get_simulation_kpis(page)
            
            return f"SUCCESS: Successfully completed capacity remediation and release for simulation {simulation_id}. Final KPIs: {kpis_final}"
        except Exception as e:
            try:
                page.screenshot(path="remediate_error.png")
                print("[INFO] Saved error screenshot to remediate_error.png", file=sys.stderr)
            except Exception:
                pass
            return f"ERROR: {e}"
        finally:
            browser.close()


if __name__ == "__main__":
    os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
    
    # Pre-warm SAP session at startup to enable instantaneous tool responses
    try:
        ensure_session(force=False)
    except Exception as startup_err:
        print(f"[WARNING] Startup pre-warm failed: {startup_err}", file=sys.stderr)
    
    # Must bind to 0.0.0.0 so that connection from Docker container succeeds
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8001,
        show_banner=False
    )