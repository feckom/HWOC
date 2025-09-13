# HWOC — Hardware Online Check (Optimized)

**A powerful, offline-capable CLI system information tool with intelligent online enrichment for Windows.**

---

## 🚀 Overview

`HWOC` (Hardware Online Check) is a **Windows-focused**, **Rich-powered**, **offline-first** system information collector that gathers detailed hardware specs from WMI and optionally enriches them using real-time web scraping from trusted sources like **Intel ARK** and **TechPowerUp**.

It's designed as a modern replacement for tools like Speccy or CPU-Z, with support for:
- Compact one-line summaries for quick overviews
- Detailed tabular reports for in-depth analysis
- Automatic HTTP caching to reduce redundant requests
- Parallelized online lookups for speed
- JSON export for automation and scripting

Perfect for IT professionals, overclockers, hardware reviewers, and system administrators.

> ✅ **Built for Windows only** — uses WMI extensively.  
> ⚡ **Fast & efficient** — caches results and uses concurrent requests.  
> 🔍 **Smart enrichment** — auto-detects Intel CPUs and NVIDIA/AMD GPUs for precise spec lookup.

---

## 📦 Features

| Feature | Description |
|--------|-------------|
| **Local Hardware Detection** | Collects full system info via WMI: CPU, GPU, RAM, Disk, MB, BIOS, NICs, OS |
| **Online Spec Enrichment** | Auto-fetches detailed specs from [Intel ARK](https://ark.intel.com) and [TechPowerUp GPU Specs](https://www.techpowerup.com/gpu-specs/) |
| **Wikipedia Fallback** | If vendor sites fail, falls back to Wikipedia summaries for context |
| **HTTP Caching** | All online responses are cached locally (`~/.cache/hwoc`) to avoid repeated downloads |
| **Dual Output Modes** | Compact summary view (default) or rich tabular report |
| **JSON Export** | Generate machine-readable reports for automation |
| **Offline Mode** | Disable all network calls with `--no-online` |
| **Threaded Enrichment** | Concurrent HTTP requests using up to 4 workers by default |
| **User-Agent Rotation** | Avoids bot detection with rotating browser-like headers |
| **Rich Terminal UI** | Color-coded, beautifully formatted output with panels and tables |

---

## 🛠️ Installation

### Prerequisites
- **Windows 10/11** (WMI required)
- Python 3.8+

### Install Dependencies
```bash
pip install psutil wmi requests beautifulsoup4 rich
