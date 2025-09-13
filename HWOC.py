#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HWOC v0.2b — Hardware Online Check (Optimized)

"""

import argparse
import concurrent.futures
import contextlib
import datetime as dt
import hashlib
import json
import os
import platform
import random
import re
import sys
import time
import urllib.parse as up
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

# --- Third-party imports with a clear failure message ---
try:
    import psutil  # Used implicitly; keep for import check
except ImportError:
    print("[FATAL] psutil is required. Install with: pip install psutil", file=sys.stderr)
    sys.exit(1)

try:
    import wmi  # Windows Management Instrumentation binding
except ImportError:
    print("[FATAL] wmi is required on Windows. Install with: pip install wmi", file=sys.stderr)
    sys.exit(1)

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("[FATAL] requests and beautifulsoup4 are required. Install with: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.text import Text
    from rich.theme import Theme
except ImportError:
    print("[FATAL] rich is required. Install with: pip install rich", file=sys.stderr)
    sys.exit(1)

# --- Globals & Helper Functions ---

APP_NAME = "HWOC v0.2b"
# Cache directory for online lookups to reduce repeated downloads
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache\\hwoc")
os.makedirs(CACHE_DIR, exist_ok=True)

# A list of user agents to rotate, helping avoid trivial bot detection
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

def now_utc_iso() -> str:
    """Returns a simplified ISO 8601 UTC timestamp."""
    t = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    # Normalize '+00:00' to 'Z' for simplicity
    if t.endswith("+00:00"):
        t = t[:-6] + "Z"
    return t

def normspace(s: Optional[str]) -> str:
    """Normalizes whitespace in a string."""
    return re.sub(r"\s+", " ", s or "").strip()

def safe_int(x: Any) -> Optional[int]:
    """Safely converts a value to an integer, returning None on failure."""
    try:
        return int(x)
    except (ValueError, TypeError):
        return None

def sha1(s: str) -> str:
    """Computes the SHA-1 hash of a string."""
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()

def bytes_to_human(n: Optional[int]) -> str:
    """Converts a byte count to a human-readable format (e.g., GiB)."""
    if not n or n <= 0:
        return ""
    step = 1024.0
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    x = float(n)
    while x >= step and i < len(units) - 1:
        x /= step
        i += 1
    return f"{x:.1f} {units[i]}"

# --- Data Models (kept as-is for consistency) ---
@dataclass
class CPUInfo:
    name: str = ""
    manufacturer: str = ""
    cores: Optional[int] = None
    threads: Optional[int] = None
    max_clock_mhz: Optional[int] = None
    base_clock_mhz: Optional[int] = None
    l3_cache_mb: Optional[int] = None
    processor_id: str = ""

@dataclass
class GPUInfo:
    name: str = ""
    driver_version: str = ""
    vendor_id: Optional[str] = None
    device_id: Optional[str] = None
    adapter_ram: Optional[int] = None
    driver_date: str = ""
    pnp_device_id: str = ""

@dataclass
class MemoryModule:
    capacity_bytes: Optional[int] = None
    speed_mhz: Optional[int] = None
    manufacturer: str = ""
    part_number: str = ""
    serial: str = ""
    form_factor: Optional[int] = None
    memory_type: Optional[int] = None

@dataclass
class DiskInfo:
    model: str = ""
    serial: str = ""
    size_bytes: Optional[int] = None
    interface_type: str = ""
    media_type: str = ""

@dataclass
class MotherboardInfo:
    product: str = ""
    manufacturer: str = ""
    serial: str = ""

@dataclass
class BIOSInfo:
    version: str = ""
    serial: str = ""
    release_date: str = ""

@dataclass
class NICInfo:
    name: str = ""
    mac: str = ""
    manufacturer: str = ""
    status: str = ""
    ip4: List[str] = field(default_factory=list)

@dataclass
class OSInfo:
    caption: str = ""
    version: str = ""
    build: str = ""
    arch: str = ""
    install_date: str = ""

@dataclass
class OnlineSpec:
    source: str = ""
    official_url: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)

@dataclass
class Report:
    """The main data container for all collected information."""
    collected_at: str = field(default_factory=now_utc_iso)
    host: str = field(default_factory=platform.node)
    os: Optional[OSInfo] = None
    cpu: Optional[CPUInfo] = None
    gpus: List[GPUInfo] = field(default_factory=list)
    memory: List[MemoryModule] = field(default_factory=list)
    disks: List[DiskInfo] = field(default_factory=list)
    motherboard: Optional[MotherboardInfo] = None
    bios: Optional[BIOSInfo] = None
    nics: List[NICInfo] = field(default_factory=list)
    online: Dict[str, OnlineSpec] = field(default_factory=dict)

# --- Optimized Core Logic ---

class HardwareReporter:
    """Main class to handle all hardware collection and online enrichment."""
    def __init__(self, timeout: int, max_workers: int):
        self.wmi_c = wmi.WMI()
        # Use a session for persistent connections and header management
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(UA_LIST),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.7",
            "Connection": "keep-alive",
        })
        self.timeout = timeout
        self.max_workers = max_workers

    def _http_get(self, url: str) -> requests.Response:
        """Performs an HTTP GET request with a session and error handling."""
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response

    def _cache_or_fetch_json(self, key: str, fetch_fn) -> Dict[str, Any]:
        """Checks cache, then fetches data if not found, and caches the result."""
        cached = CACHE.get(key)
        if cached:
            return cached
        data = fetch_fn()
        if isinstance(data, dict):
            CACHE.set(key, data)
            return data
        return {}

    def _wikipedia_summary(self, title: str) -> Dict[str, Any]:
        """Fetches a summary from Wikipedia as a robust fallback."""
        def fetch():
            try:
                # Use the MediaWiki API for a structured search and summary
                q = up.quote(title)
                url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&format=json&srlimit=1"
                j = self._http_get(url).json()
                pageid = j.get("query", {}).get("search", [{}])[0].get("pageid")
                if not pageid:
                    return {}

                url2 = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&pageids={pageid}&format=json"
                j2 = self._http_get(url2).json()
                pages = j2.get("query", {}).get("pages", {})
                extract = pages.get(str(pageid), {}).get("extract", "")
                fullurl = f"https://en.wikipedia.org/?curid={pageid}"
                return {"source": "wikipedia", "official_url": fullurl, "fields": {"summary": extract[:1200]}}
            except Exception:
                return {}
        return self._cache_or_fetch_json(f"wikipedia:{title}", fetch)

    def _intel_ark_specs(self, cpu_name: str) -> Dict[str, Any]:
        """Scrapes Intel ARK for detailed CPU specifications."""
        def fetch():
            try:
                q = up.quote(cpu_name)
                search_url = f"https://ark.intel.com/content/www/us/en/ark/search.html?q={q}"
                soup = BeautifulSoup(self._http_get(search_url).text, "html.parser")
                # Find the first product link
                href = next((h for a in soup.select("a") if "/products/" in (h := a.get("href", "")) and h.endswith(".html")), None)
                if not href:
                    return self._wikipedia_summary(cpu_name)

                href = href if href.startswith("http") else f"https://ark.intel.com{href}"
                psoup = BeautifulSoup(self._http_get(href).text, "html.parser")
                specs = {normspace(dt_el.get_text()): normspace(dt_el.find_next_sibling("dd").get_text())
                         for dt_el in psoup.select("dt") if dt_el.find_next_sibling("dd")}
                if not specs:
                    for tr in psoup.select("table tr"):
                        if len(tds := tr.find_all(["td", "th"])) >= 2:
                            specs[normspace(tds[0].get_text())] = normspace(tds[1].get_text())

                # A list of desired fields to extract, to keep the output clean
                wanted = {"Total Cores", "Total Threads", "Max Turbo Frequency", "Processor Base Frequency",
                          "Cache", "Bus Speed", "TDP", "Lithography", "Max Memory Size", "Memory Types",
                          "Max # of PCI Express Lanes", "Processor Graphics"}
                filtered = {k: v for k, v in specs.items() if k in wanted}
                title = normspace(psoup.title.get_text()) if psoup.title else cpu_name
                return {"source": "intel_ark", "official_url": href, "fields": filtered or specs or {"title": title}}
            except Exception:
                return self._wikipedia_summary(cpu_name)
        return self._cache_or_fetch_json(f"intel:{cpu_name}", fetch)

    def _techpowerup_gpu_specs(self, gpu_name: str) -> Dict[str, Any]:
        """Scrapes TechPowerUp for detailed GPU specifications."""
        def fetch():
            try:
                q = up.quote(gpu_name)
                search_url = f"https://www.techpowerup.com/search/?q={q}"
                soup = BeautifulSoup(self._http_get(search_url).text, "html.parser")
                # Find the first GPU spec page link
                href = next((h for a in soup.select("a") if "/gpu-specs/" in (h := a.get("href", "")) and h.endswith(".html")), None)
                if not href:
                    return self._wikipedia_summary(gpu_name)

                href = href if href.startswith("http") else f"https://www.techpowerup.com{href}"
                psoup = BeautifulSoup(self._http_get(href).text, "html.parser")
                specs = {}
                for tr in psoup.select("table tr"):
                    if len(tds := tr.find_all(["td", "th"])) >= 2:
                        specs[normspace(tds[0].get_text())] = normspace(tds[1].get_text())
                
                # A list of desired fields to extract
                wanted = {"GPU Name", "GPU Variant", "Architecture", "Foundry", "Process Size", "TDP", "Transistors",
                          "Die Size", "Base Clock", "Boost Clock", "Memory Size", "Memory Type", "Memory Bus",
                          "Bandwidth", "Release Date"}
                filtered = {k: v for k, v in specs.items() if k in wanted}
                title = normspace(psoup.title.get_text()) if psoup.title else gpu_name
                return {"source": "techpowerup", "official_url": href, "fields": filtered or specs or {"title": title}}
            except Exception:
                return self._wikipedia_summary(gpu_name)
        return self._cache_or_fetch_json(f"tpu:{gpu_name}", fetch)

    def collect_local(self) -> Report:
        """Gathers all local hardware information from WMI. Gracefully handles errors."""
        rep = Report()
        # Using a list of WMI queries to streamline data collection
        wmi_queries = {
            "os": "Win32_OperatingSystem",
            "cpu": "Win32_Processor",
            "gpus": "Win32_VideoController",
            "memory": "Win32_PhysicalMemory",
            "disks": "Win32_DiskDrive",
            "motherboard": "Win32_BaseBoard",
            "bios": "Win32_BIOS",
            "nics": "Win32_NetworkAdapter"
        }

        # OS info
        try:
            os_data = self.wmi_c.Win32_OperatingSystem()[0]
            rep.os = OSInfo(
                caption=normspace(os_data.Caption),
                version=str(os_data.Version or ""),
                build=str(os_data.BuildNumber or ""),
                arch=str(os_data.OSArchitecture or ""),
                install_date=str(os_data.InstallDate or "")
            )
        except Exception:
            pass
        
        # CPU info
        try:
            cpu_data = self.wmi_c.Win32_Processor()[0]
            rep.cpu = CPUInfo(
                name=normspace(cpu_data.Name),
                manufacturer=normspace(cpu_data.Manufacturer),
                cores=safe_int(cpu_data.NumberOfCores),
                threads=safe_int(cpu_data.NumberOfLogicalProcessors),
                max_clock_mhz=safe_int(cpu_data.MaxClockSpeed),
                processor_id=str(cpu_data.ProcessorId or "")
            )
        except Exception:
            pass
        
        # GPUs info
        try:
            rep.gpus = [
                GPUInfo(
                    name=normspace(g.Name),
                    driver_version=str(g.DriverVersion or ""),
                    vendor_id=re.search(r"VEN_([0-9A-F]{4})", g.PNPDeviceID, re.I).group(1).upper() if g.PNPDeviceID else None,
                    device_id=re.search(r"DEV_([0-9A-F]{4})", g.PNPDeviceID, re.I).group(1).upper() if g.PNPDeviceID else None,
                    adapter_ram=int(g.AdapterRAM) if g.AdapterRAM and int(g.AdapterRAM) > 0 else None,
                    driver_date=str(g.DriverDate or ""),
                    pnp_device_id=str(g.PNPDeviceID or "")
                ) for g in self.wmi_c.Win32_VideoController()
            ]
        except Exception:
            pass

        # Memory info
        try:
            rep.memory = [
                MemoryModule(
                    capacity_bytes=int(m.Capacity) if m.Capacity is not None else None,
                    speed_mhz=safe_int(m.Speed),
                    manufacturer=normspace(m.Manufacturer),
                    part_number=normspace(m.PartNumber),
                    serial=normspace(m.SerialNumber),
                    form_factor=safe_int(m.FormFactor),
                    memory_type=safe_int(m.MemoryType)
                ) for m in self.wmi_c.Win32_PhysicalMemory()
            ]
        except Exception:
            pass
        
        # Disks info
        try:
            rep.disks = [
                DiskInfo(
                    model=normspace(d.Model),
                    serial=normspace(getattr(d, "SerialNumber", "")),
                    size_bytes=int(d.Size) if d.Size is not None else None,
                    interface_type=normspace(d.InterfaceType),
                    media_type=normspace(getattr(d, "MediaType", ""))
                ) for d in self.wmi_c.Win32_DiskDrive()
            ]
        except Exception:
            pass
        
        # Motherboard info
        try:
            mb = self.wmi_c.Win32_BaseBoard()[0]
            rep.motherboard = MotherboardInfo(
                product=normspace(mb.Product),
                manufacturer=normspace(mb.Manufacturer),
                serial=normspace(mb.SerialNumber)
            )
        except Exception:
            pass

        # BIOS info
        try:
            bios = self.wmi_c.Win32_BIOS()[0]
            rep.bios = BIOSInfo(
                version=normspace(bios.SMBIOSBIOSVersion),
                serial=normspace(bios.SerialNumber),
                release_date=str(bios.ReleaseDate or "")
            )
        except Exception:
            pass
        
        # NICs info
        try:
            nics_by_index = {
                n.Index: NICInfo(
                    name=normspace(n.Name),
                    mac=str(n.MACAddress or ""),
                    manufacturer=normspace(n.Manufacturer),
                    status=str(n.NetConnectionStatus) if n.NetConnectionStatus is not None else "",
                    ip4=[]
                ) for n in self.wmi_c.Win32_NetworkAdapter(PhysicalAdapter=True)
            }
            # Collect IPv4 addresses from a separate WMI class
            for conf in self.wmi_c.Win32_NetworkAdapterConfiguration(IPEnabled=True):
                if nic := nics_by_index.get(conf.Index):
                    nic.ip4 = [ip for ip in (conf.IPAddress or []) if ":" not in ip]
            rep.nics = list(nics_by_index.values())
        except Exception:
            pass

        return rep

    def enrich_online(self, rep: Report, online: bool = True) -> Report:
        """Runs online enrichment tasks in parallel using a thread pool."""
        if not online:
            return rep

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            futures = []
            # Submit CPU enrichment task
            if rep.cpu and rep.cpu.name:
                # Use a simple check to determine if it's an Intel CPU
                if "intel" in rep.cpu.manufacturer.lower() or rep.cpu.name.lower().startswith("intel "):
                    futures.append(ex.submit(lambda: ("cpu", self._intel_ark_specs(rep.cpu.name))))
                else:
                    futures.append(ex.submit(lambda: ("cpu", self._wikipedia_summary(rep.cpu.name))))
            
            # Submit GPU enrichment tasks for each detected GPU
            for i, gpu in enumerate(rep.gpus):
                if gpu.name:
                    futures.append(ex.submit(lambda gpu_name=gpu.name, i=i: (f"gpu{i}", self._techpowerup_gpu_specs(gpu_name))))

            # Collect results as they complete
            for f in concurrent.futures.as_completed(futures):
                try:
                    k, v = f.result()
                    rep.online[k] = OnlineSpec(**v)
                except Exception:
                    continue

        return rep

# --- Rendering Logic ---

def render_compact(rep: Report):
    """Renders a compact, build-sheet style report with Rich coloring."""
    theme = Theme({"hdr": "bold red", "label": "bold red", "value": "white"})
    console = Console(theme=theme)
    header = Text(f"{APP_NAME} — Hardware Online Check", style="hdr")
    console.print(Panel.fit(header, border_style="white"))

    def print_line(label: str, value: str):
        """Helper to print a single line with colored labels."""
        t = Text()
        t.append(f"{label}: ", style="label")
        t.append(value, style="value")
        console.print(t)

    if rep.cpu:
        cpu_value = rep.cpu.name or "N/A"
        extras = []
        if rep.cpu.cores and rep.cpu.threads:
            extras.append(f"{rep.cpu.cores} cores / {rep.cpu.threads} threads")
        
        # Add online specs to the compact view
        on_cpu = rep.online.get("cpu")
        if on_cpu and on_cpu.fields:
            if base := on_cpu.fields.get("Processor Base Frequency") or on_cpu.fields.get("Base Clock"):
                if m := re.search(r"([\d\.]+)\s*GHz", base, re.I):
                    extras.append(f"{m.group(1)} GHz base")
            if boost := on_cpu.fields.get("Max Turbo Frequency") or on_cpu.fields.get("Boost Clock"):
                if m := re.search(r"([\d\.]+)\s*GHz", boost, re.I):
                    extras.append(f"boost up to {m.group(1)} GHz")
            if cache := on_cpu.fields.get("Cache"):
                extras.append(cache)

        if extras:
            cpu_value += " (" + ", ".join(extras) + ")"
        print_line("CPU", cpu_value)

    if rep.motherboard and rep.motherboard.product:
        mb = " ".join(x for x in [rep.motherboard.manufacturer, rep.motherboard.product] if x)
        print_line("MBR", mb)

    if rep.memory:
        total = sum(m.capacity_bytes or 0 for m in rep.memory)
        speeds = sorted(list(set(m.speed_mhz for m in rep.memory if m.speed_mhz)))
        ram_desc = f"{bytes_to_human(total)}"
        if len(rep.memory) > 0:
            ram_desc += f" ({len(rep.memory)} modules)"
        if speeds:
            ram_desc += f", DDR {'/'.join(str(s) for s in speeds)} MHz"
        print_line("RAM", ram_desc)

    for idx, g in enumerate(rep.gpus):
        gdesc = g.name or f"GPU #{idx+1}"
        if vram := bytes_to_human(g.adapter_ram):
            gdesc += f" ({vram})"
        print_line("GPU", gdesc)

    if rep.disks:
        console.print(Text("STORAGE:", style="label"))
        for d in rep.disks:
            size = bytes_to_human(d.size_bytes)
            parts = [p for p in [d.model, f"({size})" if size else "", d.interface_type or "", d.media_type or ""] if p]
            console.print(Text("- ", style="value").append(" ".join(parts), style="value"))

    if rep.nics:
        nics_names = [n.name for n in rep.nics if n.name]
        if nics_names:
            print_line("NIC", " + ".join(nics_names))

    if rep.os:
        os_line = rep.os.caption or "Windows"
        if rep.os.build:
            os_line += f" (Build {rep.os.build})"
        print_line("OS", os_line)


def render_tables(rep: Report):
    """Renders a detailed, tabular report with Rich formatting."""
    theme = Theme({"info": "bold cyan", "hdr": "bold red", "dim": "dim"})
    console = Console(theme=theme)
    console.print(Panel.fit(Text(f"{APP_NAME} — Detailed Report", style="hdr"), border_style="white"))

    if rep.os:
        t = Table(box=box.SIMPLE_HEAVY, title="Operating System")
        t.add_column("Field", style="bold")
        t.add_column("Value")
        t.add_row("Host", rep.host)
        t.add_row("Collected", rep.collected_at)
        t.add_row("OS", f"{rep.os.caption} ({rep.os.arch})")
        t.add_row("Version", f"{rep.os.version} (Build {rep.os.build})")
        if rep.os.install_date:
            t.add_row("Installed", rep.os.install_date)
        console.print(Panel(t, border_style="green"))

    if rep.cpu:
        t = Table(box=box.SIMPLE_HEAVY, title="CPU")
        t.add_column("Field", style="bold")
        t.add_column("Value")
        t.add_row("Name", rep.cpu.name)
        t.add_row("Manufacturer", rep.cpu.manufacturer)
        t.add_row("Cores/Threads", f"{rep.cpu.cores or ''}/{rep.cpu.threads or ''}")
        if rep.cpu.max_clock_mhz:
            t.add_row("Max Clock", f"{rep.cpu.max_clock_mhz} MHz")
        if rep.cpu.processor_id:
            t.add_row("ProcessorId", rep.cpu.processor_id)
        on = rep.online.get("cpu")
        if on and on.official_url:
            t.add_row("Online Spec", on.official_url)
            for k, v in on.fields.items():
                t.add_row(k, str(v))
        console.print(Panel(t, border_style="yellow"))

    for idx, g in enumerate(rep.gpus):
        t = Table(box=box.SIMPLE_HEAVY, title=f"GPU #{idx+1}")
        t.add_column("Field", style="bold")
        t.add_column("Value")
        t.add_row("Name", g.name)
        if g.vendor_id or g.device_id:
            t.add_row("PCI IDs", f"VEN_{g.vendor_id or ''} DEV_{g.device_id or ''}")
        t.add_row("Driver", g.driver_version)
        if vram := bytes_to_human(g.adapter_ram):
            t.add_row("Adapter RAM", vram)
        if g.driver_date:
            t.add_row("Driver Date", g.driver_date)
        if g.pnp_device_id:
            t.add_row("PNPDeviceID", g.pnp_device_id[:100] + ("..." if len(g.pnp_device_id) > 100 else ""))
        on = rep.online.get(f"gpu{idx}")
        if on and on.official_url:
            t.add_row("Online Spec", on.official_url)
            for k, v in on.fields.items():
                t.add_row(k, str(v))
        console.print(Panel(t, border_style="blue"))

    if rep.memory:
        t = Table(box=box.SIMPLE_HEAVY, title="Memory Modules")
        t.add_column("#", style="bold", justify="right")
        t.add_column("Capacity")
        t.add_column("Speed (MHz)")
        t.add_column("Manufacturer")
        t.add_column("Part Number")
        t.add_column("Serial")
        for i, m in enumerate(rep.memory, 1):
            t.add_row(str(i), bytes_to_human(m.capacity_bytes), str(m.speed_mhz or ""), m.manufacturer, m.part_number, m.serial)
        console.print(Panel(t, border_style="magenta"))

    if rep.disks:
        t = Table(box=box.SIMPLE_HEAVY, title="Disk Drives")
        t.add_column("#", style="bold", justify="right")
        t.add_column("Model")
        t.add_column("Serial")
        t.add_column("Size")
        t.add_column("Interface")
        t.add_column("Media Type")
        for i, d in enumerate(rep.disks, 1):
            t.add_row(str(i), d.model, d.serial, bytes_to_human(d.size_bytes), d.interface_type, d.media_type)
        console.print(Panel(t, border_style="cyan"))

    if rep.motherboard or rep.bios:
        t = Table(box=box.SIMPLE_HEAVY, title="Motherboard / BIOS")
        t.add_column("Field", style="bold")
        t.add_column("Value")
        if rep.motherboard:
            t.add_row("Manufacturer", rep.motherboard.manufacturer)
            t.add_row("Product", rep.motherboard.product)
            if rep.motherboard.serial:
                t.add_row("Serial", rep.motherboard.serial)
        if rep.bios:
            t.add_row("BIOS Version", rep.bios.version)
            if rep.bios.release_date:
                t.add_row("BIOS Release", rep.bios.release_date)
            if rep.bios.serial:
                t.add_row("BIOS Serial", rep.bios.serial)
        console.print(Panel(t, border_style="white"))

    if rep.nics:
        t = Table(box=box.SIMPLE_HEAVY, title="Network Adapters")
        t.add_column("#", style="bold", justify="right")
        t.add_column("Name")
        t.add_column("MAC")
        t.add_column("Manufacturer")
        t.add_column("Status")
        t.add_column("IPv4")
        for i, n in enumerate(rep.nics, 1):
            t.add_row(str(i), n.name, n.mac, n.manufacturer, n.status, ", ".join(n.ip4))
        console.print(Panel(t, border_style="bright_black"))

# --- Main Logic ---

def main():
    """Parses arguments, runs the main logic, and renders the output."""
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME}: A Speccy-like CLI with compact colored output and optional online enrichment."
    )
    parser.add_argument("--json", dest="json_out", help="Write a full JSON report to a file.")
    parser.add_argument("--no-online", action="store_true", help="Skip online lookups (offline mode).")
    parser.add_argument("--timeout", type=int, default=12, help="HTTP timeout per request (in seconds).")
    parser.add_argument("--max-workers", type=int, default=min(4, (os.cpu_count() or 4)),
                        help="Max concurrent HTTP workers.")
    parser.add_argument("--refresh-cache", action="store_true", help="Invalidate the HTTP cache before running.")
    parser.add_argument("--table", action="store_true", help="Show a detailed tabular view instead of the compact one.")
    parser.add_argument("--compact", action="store_true", help="Force the compact view (default).")
    args = parser.parse_args()

    # Determine which view to display
    show_compact = True if args.compact or not args.table else False

    if os.name != "nt":
        print("[WARN] This tool targets Windows (WMI). Some information may be unavailable.", file=sys.stderr)

    if args.refresh_cache:
        CACHE.invalidate()

    reporter = HardwareReporter(timeout=args.timeout, max_workers=args.max_workers)

    try:
        # Collect local data first
        rep = reporter.collect_local()
        # Then enrich it with online data
        rep = reporter.enrich_online(rep, online=not args.no_online)
    except wmi.x_wmi:
        print("[FATAL] WMI error. Please run the script as an Administrator.", file=sys.stderr)
        sys.exit(1)

    # Optional JSON dump
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(asdict(rep), f, ensure_ascii=False, indent=2)

    # Render the report
    if show_compact:
        render_compact(rep)
    else:
        render_tables(rep)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INFO] Aborted by user.")
