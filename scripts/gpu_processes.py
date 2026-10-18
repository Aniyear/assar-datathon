"""Conservative WDDM GPU inventory; never terminates processes.

This driver's compute-apps list includes desktop C+G applications. Exempt only
known GUI executable paths in their usual installation roots. Unknown C+G and
all pure-compute processes still block startup, including Python and llama-server.
The inventory is a point-in-time check, not a GPU lock or a VRAM guarantee.
"""
from fnmatch import fnmatchcase
import os
import xml.etree.ElementTree as ET


def process_inventory(xml_text: str) -> dict[str, list[int]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid GPU process inventory") from exc
    sections = root.findall("./gpu/processes")
    if not sections:
        raise ValueError("GPU process inventory absent")
    windows = os.environ.get("SystemRoot", r"C:\Windows").replace("/", "\\").lower().rstrip("\\")
    programs = os.environ.get("ProgramFiles", r"C:\Program Files").replace("/", "\\").lower().rstrip("\\")
    programs86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)").replace("/", "\\").lower().rstrip("\\")
    local = os.environ.get("LOCALAPPDATA", "").replace("/", "\\").lower().rstrip("\\")
    patterns = [windows + "\\" + p for p in (
        "explorer.exe", "system32\\shellhost.exe", "system32\\nahimicsvc64.exe",
        "system32\\applicationframehost.exe", "immersivecontrolpanel\\systemsettings.exe",
        "systemapps\\microsoftwindows.client.cbs_*\\crossdeviceresume.exe",
        "systemapps\\microsoftwindows.client.cbs_*\\searchhost.exe",
        "systemapps\\microsoftwindows.client.cbs_*\\textinputhost.exe",
        "systemapps\\microsoft.windows.startmenuexperiencehost_*\\startmenuexperiencehost.exe",
        "systemapps\\shellexperiencehost_*\\shellexperiencehost.exe")]
    patterns += [programs + "\\windowsapps\\" + p for p in (
        "microsoft.yourphone_*\\phoneexperiencehost.exe", "a-volute.nahimic_*\\nahimic3.exe",
        "openai.codex_*\\app\\chatgpt.exe", "telegrammessengerllp.telegramdesktop_*\\telegram.exe",
        "microsoft.screensketch_*\\snippingtool\\snippingtool.exe")]
    patterns += [programs86 + "\\" + p for p in (
        "microsoft\\edge\\application\\msedge.exe",
        "microsoft\\edgewebview\\application\\*\\msedgewebview2.exe",
        "kaspersky lab\\kaspersky *\\avpui.exe")]
    if local:
        patterns.append(local + "\\openai\\codex\\runtimes\\cua_node\\*\\bin\\node_modules\\@oai\\sky\\bin\\windows\\swift\\x64\\codex-computer-use-swift.exe")
    busy, background = set(), set()
    for section in sections:
        for row in section.findall("process_info"):
            pid = int(row.findtext("pid", ""))
            kind = row.findtext("type", "").strip()
            path = row.findtext("process_name", "").replace("/", "\\").lower()
            gui = kind == "G" or (kind == "C+G" and any(fnmatchcase(path, p) for p in patterns))
            (background if gui else busy).add(pid)
    return {"compute_pids": sorted(busy), "background_graphics_pids": sorted(background)}
