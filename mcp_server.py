import os
import sys
from typing import Optional
from fastmcp import FastMCP
from sap_automation import run_automation

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
    Provides SAP browser automation capabilities.

    Use this tool when users want to create, configure,
    validate, schedule, or submit SAP planning and simulation jobs.

    The tool accepts user-provided SAP fields and performs
    the required automation workflow in SAP.
    """,
    version="1.0.0",
)


@mcp.tool(timeout=300)
def create_pmrp_simulation(
    fields: dict[str, str],
    filter_labels: Optional[dict[str, str]] = None,
) -> str:
    """
    Execute an SAP automation workflow.

    Pass SAP field names and values in the fields dictionary.
    The automation will open SAP, enter the provided data,
    perform validation steps, and submit the process.

    Returns a success or error message.
    """
    try:
        return run_automation(
            fields=fields,
            filter_labels=filter_labels,
        )
    except Exception as e:
        return f"ERROR: {e}"


if __name__ == "__main__":
    os.environ["FASTMCP_SHOW_SERVER_BANNER"] = "false"
    
    # Must bind to 0.0.0.0 so that connection from Docker container succeeds
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8001,
        show_banner=False
    )