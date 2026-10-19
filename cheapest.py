#!/usr/bin/env python3
"""
Find the cheapest currently-available GPU options across cloud providers.

Providers: RunPod, Linode, DigitalOcean, Vast.ai, AWS, GCP

Usage:
    python cheapest.py                          # all providers, top 5
    python cheapest.py --top 10
    python cheapest.py --provider runpod
    python cheapest.py --provider linode
    python cheapest.py --provider digitalocean
    python cheapest.py --provider vast
    python cheapest.py --provider aws
    python cheapest.py --provider gcp
    python cheapest.py --spot                   # RunPod, AWS, GCP: rank by spot price
    python cheapest.py --min-vram 48            # only GPUs with >= 48 GB VRAM
    python cheapest.py --min-vram 32 --max-vram 48
    python cheapest.py --cuda 13.0              # Vast.ai: only hosts that run CUDA >= 13.0
    python cheapest.py --na                     # North America only (not Linode)

VRAM ranges available across providers (per GPU):
    16 GB  — RTX 2000 Ada, RTX A4000, T4                    (RunPod, Vast.ai, AWS, GCP)
    20 GB  — RTX 4000 Ada, RTX A4500                        (RunPod, Linode, DigitalOcean, Vast.ai)
    24 GB  — RTX 3090, RTX 4090, L4, RTX A5000, A10G, Quadro RTX 6000  (RunPod, Linode, Vast.ai, AWS, GCP)
    32 GB  — RTX 5090, RTX PRO 4500, RTX 5000 Ada            (RunPod, Vast.ai, AWS)
    40 GB  — A100 40GB                                      (RunPod, Vast.ai, AWS, GCP)
    48 GB  — A40, L40S, RTX 6000 Ada, RTX A6000, RTX PRO 5000  (RunPod, DigitalOcean, Vast.ai, AWS)
    80 GB  — A100 PCIe/SXM, H100 SXM/PCIe                  (RunPod, DigitalOcean, Vast.ai, AWS, GCP)
    94 GB  — H100 NVL                                       (RunPod, Vast.ai)
    96 GB  — RTX PRO 6000                                   (RunPod, Vast.ai, AWS, GCP)
   141 GB  — H200 SXM                                       (RunPod, DigitalOcean, Vast.ai, AWS, GCP)
   180 GB  — B200                                           (RunPod, Vast.ai, AWS, GCP)
   192 GB  — MI300X                                         (DigitalOcean)
   288 GB  — B300                                           (RunPod, Vast.ai, AWS)
"""

import os
import re
import sys
import gzip
import json
import shutil
import argparse
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


# ── Token resolution ──────────────────────────────────────────────────────────

def _read_file(path):
    p = os.path.expanduser(path)
    return open(p).read().strip() if os.path.exists(p) else ""


def _parse_key_file(content):
    """Extract a token from a plain file or a name/key config file."""
    if not content:
        return ""
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("key") and ("=" in stripped):
            return stripped.split("=", 1)[1].strip().strip('"\'')
    if len(lines) == 1:
        return lines[0].strip()
    return ""


def get_runpod_key():
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if key:
        return key
    for path in ("~/.api/runpod/claude.key", "~/.runpod/config.toml"):
        content = _read_file(path)
        if not content:
            continue
        if path.endswith(".toml"):
            for line in content.splitlines():
                if "apiKey" in line and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"\'')
        else:
            return content
    return ""


def get_linode_token():
    tok = os.environ.get("LINODE_TOKEN", "").strip()
    if tok:
        return tok
    tok = _parse_key_file(_read_file("~/.api/linode/token"))
    if tok:
        return tok
    for line in _read_file("~/.config/linode-cli").splitlines():
        if "token" in line and "=" in line:
            return line.split("=", 1)[1].strip()
    return ""


def get_do_token():
    for var in ("DIGITALOCEAN_TOKEN", "DO_TOKEN"):
        tok = os.environ.get(var, "").strip()
        if tok:
            return tok
    tok = _parse_key_file(_read_file("~/.api/digitalocean/token"))
    if tok:
        return tok
    for line in _read_file("~/.config/doctl/config.yaml").splitlines():
        if "access-token" in line and ":" in line:
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def get_gcp_auth():
    """(query param, headers) for the Cloud Billing Catalog API: an API key, else gcloud's login."""
    key = os.environ.get("GCP_API_KEY", "").strip() or _parse_key_file(_read_file("~/.api/gcp/key"))
    if key:
        return f"&key={urllib.parse.quote(key)}", {}
    if shutil.which("gcloud"):
        try:
            tok = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True,
                                 text=True, timeout=30, stdin=subprocess.DEVNULL).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            tok = ""
        if tok:
            return "", {"Authorization": f"Bearer {tok}"}
    return None, None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_read(url, headers, timeout=20):
    """Response body as bytes, un-gzipped if the server sent it compressed."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def http_get(url, headers, timeout=20):
    return json.loads(http_read(url, headers, timeout))


def graphql_post(url, headers, query):
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')}")
    if "errors" in result:
        raise RuntimeError(result["errors"][0]["message"])
    return result["data"]


def _vram_ok(vram, min_vram, max_vram):
    """Per-GPU VRAM in GB within [min_vram, max_vram]; 0 means no bound."""
    return (not min_vram or vram >= min_vram) and (not max_vram or vram <= max_vram)


# ── RunPod ────────────────────────────────────────────────────────────────────

_RUNPOD_GQL = "https://api.runpod.io/graphql"
_RUNPOD_IN_STOCK = {"High", "Medium", "Low"}
_RUNPOD_STOCK_RANK = {"High": 0, "Medium": 1, "Low": 2}
_RUNPOD_NA_PREFIXES = ("US-", "CA-")   # datacenter IDs like US-MO-1, CA-MTL-1


def _runpod_fetch(api_key):
    return graphql_post(_RUNPOD_GQL, {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "ai-cost/1.0",
    }, """
    {
      gpuTypes {
        id displayName memoryInGb secureCloud communityCloud
        securePrice communityPrice
        lowestPrice(input: { gpuCount: 1 }) {
          minimumBidPrice uninterruptablePrice
        }
      }
      dataCenters {
        id name location
        gpuAvailability { gpuTypeId stockStatus }
      }
    }
    """)


def _runpod_stock_map(datacenters, na_only):
    stock = {}
    for dc in datacenters:
        if na_only and not dc["id"].startswith(_RUNPOD_NA_PREFIXES):
            continue
        for ga in dc.get("gpuAvailability") or []:
            gid = ga["gpuTypeId"]
            status = ga.get("stockStatus")
            if status in _RUNPOD_IN_STOCK:
                stock.setdefault(gid, []).append((dc["id"], dc["location"], status))
    return stock


def _runpod_rank(gpu_types, stock_map, top_n, use_spot, min_vram, max_vram):
    candidates = []
    for gid, gpu in gpu_types.items():
        locs = stock_map.get(gid)
        if not locs:
            continue
        vram = gpu.get("memoryInGb", 0)
        if not _vram_ok(vram, min_vram, max_vram):
            continue
        lp = gpu.get("lowestPrice") or {}
        secure = gpu.get("securePrice")
        community = gpu.get("communityPrice")
        spot = lp.get("minimumBidPrice")
        ondemand = lp.get("uninterruptablePrice") or secure or community
        prices = [p for p in (secure, community, ondemand) if p]
        if not prices and not spot:
            continue
        cheapest = min(prices) if prices else None
        rank_by = (spot or cheapest) if use_spot else (cheapest or spot)
        if rank_by is None:
            continue
        best_stock = min(locs, key=lambda x: _RUNPOD_STOCK_RANK.get(x[2], 99))[2]
        candidates.append({
            "name": gpu["displayName"],
            "vram": vram,
            "secure": secure,
            "community": community,
            "ondemand": cheapest,
            "spot": spot,
            "rank": rank_by,
            "stock": best_stock,
            "locations": sorted({dc_id for dc_id, _, _ in locs}),
        })
    candidates.sort(key=lambda x: (x["rank"], x["name"]))
    return candidates[:top_n]


def fetch_runpod(top_n, use_spot, min_vram, max_vram, na_only):
    api_key = get_runpod_key()
    if not api_key:
        return None, "no API key — set RUNPOD_API_KEY or write to ~/.api/runpod/claude.key"
    try:
        data = _runpod_fetch(api_key)
    except Exception as e:
        return None, str(e)
    gpu_types = {g["id"]: g for g in data["gpuTypes"]}
    stock_map = _runpod_stock_map(data["dataCenters"], na_only)
    n_available = sum(1 for g in gpu_types if g in stock_map)
    ranked = _runpod_rank(gpu_types, stock_map, top_n, use_spot, min_vram, max_vram)
    return {"ranked": ranked, "n_available": n_available}, None


# ── Linode ────────────────────────────────────────────────────────────────────

_LINODE_API = "https://api.linode.com/v4"

# Linode doesn't expose per-GPU VRAM in the API; map by instance type prefix.
_LINODE_VRAM = {
    "g2-gpu-rtx4000a": 20,   # RTX 4000 Ada — 20 GB per GPU
    "g1-gpu-rtx6000":  24,   # Quadro RTX 6000 (Turing) — 24 GB per GPU
}


def _linode_vram(type_id):
    for prefix, vram in _LINODE_VRAM.items():
        if type_id.startswith(prefix):
            return vram
    return 0


def _linode_fetch(token):
    data = http_get(f"{_LINODE_API}/linode/types", {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    return data.get("data", [])


def fetch_linode(top_n, min_vram, max_vram):
    token = get_linode_token()
    if not token:
        return None, "no token — set LINODE_TOKEN or write to ~/.api/linode/token"
    try:
        all_types = _linode_fetch(token)
    except Exception as e:
        return None, str(e)
    gpu_types = [t for t in all_types if t.get("class") == "gpu"]
    if min_vram or max_vram:
        gpu_types = [t for t in gpu_types
                     if _vram_ok(_linode_vram(t.get("id", "")), min_vram, max_vram)]
    gpu_types.sort(key=lambda t: t.get("price", {}).get("hourly") or 0)
    ranked = []
    for t in gpu_types[:top_n]:
        price = t.get("price", {})
        ranked.append({
            "id": t.get("id", ""),
            "label": t.get("label", ""),
            "vcpus": t.get("vcpus", 0),
            "vram": _linode_vram(t.get("id", "")),
            "ram_gb": t.get("memory", 0) / 1024,
            "disk_gb": t.get("disk", 0) / 1024,
            "hourly": price.get("hourly") or 0,
            "monthly": price.get("monthly") or 0,
        })
    return {"ranked": ranked, "n_available": len(gpu_types)}, None


# ── DigitalOcean ──────────────────────────────────────────────────────────────

_DO_API = "https://api.digitalocean.com/v2"
_DO_NA_PREFIXES = ("nyc", "sfo", "tor", "atl", "ric", "mem", "mkc")   # region slugs like nyc2, tor1, mkc1


def _do_vram_per_gpu(slug):
    """Parse per-GPU VRAM from slug like 'gpu-h100x8-640gb' → 80."""
    m = re.search(r'x(\d+)-(\d+)gb', slug)
    if m:
        count, total = int(m.group(1)), int(m.group(2))
        return total // count if count else 0
    return 0


def _do_paginate(token, path, key):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    results = []
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        data = http_get(f"{_DO_API}{path}{sep}page={page}&per_page=200", headers)
        results.extend(data.get(key, []))
        if "next" not in data.get("links", {}).get("pages", {}):
            break
        page += 1
    return results


def fetch_digitalocean(top_n, min_vram, max_vram, na_only):
    token = get_do_token()
    if not token:
        return None, "no token — set DIGITALOCEAN_TOKEN or write to ~/.api/digitalocean/token"
    try:
        all_sizes = _do_paginate(token, "/sizes", "sizes")
    except Exception as e:
        return None, str(e)
    gpu_sizes = [s for s in all_sizes if s.get("slug", "").startswith("gpu-")]
    if min_vram or max_vram:
        gpu_sizes = [s for s in gpu_sizes
                     if _vram_ok(_do_vram_per_gpu(s.get("slug", "")), min_vram, max_vram)]
    if na_only:
        for s in gpu_sizes:
            s["regions"] = [r for r in s.get("regions", []) if r.startswith(_DO_NA_PREFIXES)]
        gpu_sizes = [s for s in gpu_sizes if s["regions"]]
    gpu_sizes.sort(key=lambda s: s.get("price_hourly") or 0)
    ranked = []
    for s in gpu_sizes[:top_n]:
        ranked.append({
            "slug": s.get("slug", ""),
            "description": s.get("description", ""),
            "vcpus": s.get("vcpus", 0),
            "vram": _do_vram_per_gpu(s.get("slug", "")),
            "ram_gb": s.get("memory", 0) / 1024,
            "disk_gb": s.get("disk", 0),
            "hourly": s.get("price_hourly") or 0,
            "monthly": s.get("price_monthly") or 0,
            "regions": s.get("regions", []),
        })
    return {"ranked": ranked, "n_available": len(gpu_sizes)}, None


# ── Vast.ai ───────────────────────────────────────────────────────────────────

_VAST_API = "https://console.vast.ai/api/v0/bundles/"
_VAST_MAX_PAGES = 30   # the search returns at most 64 offers per request
_VAST_NA_COUNTRIES = ["US", "CA", "MX"]
# Hosts report usable VRAM (an L40S shows 45 GiB); round up to the card's nominal size.
_VAST_NOMINAL_GB = (4, 6, 8, 10, 11, 12, 16, 20, 24, 32, 40, 48, 64, 80, 94, 96, 141, 180, 192, 288)


def _vast_vram(mib):
    gib = mib / 1024
    return next((n for n in _VAST_NOMINAL_GB if n >= gib - 0.5), round(gib))


def _vast_search(query):
    url = f"{_VAST_API}?q={urllib.parse.quote(json.dumps(query))}"
    return http_get(url, {"Accept": "application/json", "User-Agent": "ai-cost/1.0"}).get("offers", [])


def fetch_vast(top_n, min_vram, max_vram, cuda, na_only):
    """Cheapest rentable single-GPU offer per GPU model. The search needs no API key."""
    query = {
        "rentable": {"eq": True}, "rented": {"eq": False}, "verified": {"eq": True},
        "num_gpus": {"eq": 1}, "type": "on-demand",
        "order": [["dph_total", "asc"]], "limit": 64,
    }
    # gpu_ram is MiB as the host reports it: a 48 GB L40S shows 46068, an A6000 49140
    gpu_ram = {}
    if min_vram:
        gpu_ram["gte"] = min_vram * 1000 * 0.95
    if max_vram:
        gpu_ram["lte"] = max_vram * 1024 * 1.02
    if gpu_ram:
        query["gpu_ram"] = gpu_ram
    if na_only:
        query["geolocation"] = {"in": _VAST_NA_COUNTRIES}
    if cuda:
        query["cuda_max_good"] = {"gte": cuda}
        if cuda >= 13:
            query["compute_cap"] = {"gte": 750}       # CUDA 13 dropped Volta and older
    best = {}
    try:
        for _ in range(_VAST_MAX_PAGES):
            if best:
                query["gpu_name"] = {"notin": list(best)}
            offers = _vast_search(query)
            if not offers:
                break
            for o in offers:   # sorted by price, so the first offer per model is its cheapest
                best.setdefault(o["gpu_name"], o)
            if len(best) >= top_n:
                break
    except Exception as e:
        return None, str(e)
    ranked = sorted(best.values(), key=lambda o: o["dph_total"])[:top_n]
    ranked = [{
        "name": o["gpu_name"],
        "vram": _vast_vram(o.get("gpu_ram", 0)),
        "hourly": o["dph_total"],
        "cuda": o.get("cuda_max_good"),
        "reliability": o.get("reliability2") or 0,
        "location": o.get("geolocation") or "?",
    } for o in ranked]
    return {"ranked": ranked, "n_available": len(best)}, None


# ── AWS ───────────────────────────────────────────────────────────────────────
# Public price feeds behind aws.amazon.com/ec2/pricing; no credentials needed.
# "Available" means AWS lists the type in that region, not that capacity is free.

_AWS_LOCATIONS = "https://b0.p.awsstatic.com/locations/1.0/aws/current/locations.json"
_AWS_ONDEMAND = ("https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/"
                 "ec2-ondemand-without-sec-sel/{}/Linux/index.json")
_AWS_SPOT = "https://website.spot.ec2.aws.a2z.com/spot.js"   # lacks some newer families
_AWS_NA_PREFIXES = ("us-", "ca-", "mx-")
_AWS_UA = {"User-Agent": "ai-cost/1.0"}

# The feeds don't name the GPU: family -> (GPU, GB per GPU, {size: GPUs} for multi-GPU sizes).
_AWS_GPU = {
    "g4dn":    ("T4", 16, {"12xlarge": 4, "metal": 8}),
    "g5g":     ("T4G", 16, {"16xlarge": 2, "metal": 2}),
    "g5":      ("A10G", 24, {"12xlarge": 4, "24xlarge": 4, "48xlarge": 8}),
    "g6":      ("L4", 24, {"12xlarge": 4, "24xlarge": 4, "48xlarge": 8}),
    "gr6":     ("L4", 24, {}),
    "g7":      ("RTX PRO 4500 Blackwell", 32, {"12xlarge": 2, "24xlarge": 4, "48xlarge": 8}),
    "g6e":     ("L40S", 48, {"12xlarge": 4, "24xlarge": 4, "48xlarge": 8}),
    "g7e":     ("RTX PRO 6000 Blackwell", 96, {"12xlarge": 2, "24xlarge": 4, "48xlarge": 8}),
    "p4d":     ("A100", 40, {"24xlarge": 8}),
    "p4de":    ("A100", 80, {"24xlarge": 8}),
    "p5":      ("H100", 80, {"48xlarge": 8}),
    "p5en":    ("H200", 141, {"48xlarge": 8}),
    "p6-b200": ("B200", 180, {"48xlarge": 8}),
    "p6-b300": ("B300", 288, {"48xlarge": 8}),
}


def _aws_region_prices(region):
    code, name = region
    d = http_get(_AWS_ONDEMAND.format(urllib.parse.quote(name)), _AWS_UA, timeout=60)
    prices = {}
    for item in next(iter(d["regions"].values())).values():
        itype = item["Instance Type"]
        if itype.split(".")[0] in _AWS_GPU and float(item["price"]) > 0:
            prices[itype] = float(item["price"])
    return code, prices


def _aws_spot_prices():
    """{instance type: {region: $/hr}}; empty if the feed can't be read."""
    try:
        text = http_read(_AWS_SPOT, _AWS_UA, timeout=60).decode()
        data = json.loads(text[text.index("(") + 1:text.rindex(")")])
    except Exception:
        return {}
    spot = {}
    for r in data["config"]["regions"]:
        for it in r["instanceTypes"]:
            for s in it["sizes"]:
                for col in s["valueColumns"]:
                    try:
                        if col["name"] == "linux":
                            spot.setdefault(s["size"], {})[r["region"]] = float(col["prices"]["USD"])
                    except (KeyError, ValueError):
                        pass
    return spot


def fetch_aws(top_n, min_vram, max_vram, na_only, use_spot):
    """Cheapest instance per GPU family, on-demand Linux, plus spot where the feed has it."""
    try:
        locations = http_get(_AWS_LOCATIONS, _AWS_UA)
    except Exception as e:
        return None, str(e)
    regions = [(v["code"], v["name"]) for v in locations.values()
               if v.get("type") == "AWS Region"
               and (not na_only or v["code"].startswith(_AWS_NA_PREFIXES))]
    by_type = {}   # instance type -> {region: $/hr}

    def one(region):
        try:
            return _aws_region_prices(region)
        except Exception:
            return region[0], {}   # some listed regions have no EC2 price page

    with ThreadPoolExecutor(max_workers=8) as pool:
        spot_future = pool.submit(_aws_spot_prices)
        for code, prices in pool.map(one, regions):
            for itype, price in prices.items():
                by_type.setdefault(itype, {})[code] = price
        spot = spot_future.result()
    if not by_type:
        return None, "no prices returned"
    best = {}   # family -> cheapest instance row
    for itype, regs in by_type.items():
        family, size = itype.split(".", 1)
        gpu, vram, multi = _AWS_GPU[family]
        if not _vram_ok(vram, min_vram, max_vram):
            continue
        region = min(regs, key=regs.get)
        sp = {r: p for r, p in spot.get(itype, {}).items() if r in regs}
        row = {
            "name": itype, "gpu": gpu, "gpus": multi.get(size, 1), "vram": vram,
            "ondemand": regs[region], "region": region, "n_regions": len(regs),
            "spot": min(sp.values()) if sp else None,
        }
        row["rank"] = (row["spot"] or row["ondemand"]) if use_spot else row["ondemand"]
        if family not in best or row["rank"] < best[family]["rank"]:
            best[family] = row
    ranked = sorted(best.values(), key=lambda r: r["rank"])[:top_n]
    return {"ranked": ranked, "n_available": len(best)}, None


# ── GCP ───────────────────────────────────────────────────────────────────────
# Prices from the Cloud Billing Catalog API (Compute Engine service). Regions are
# where Google sells the SKU; zones, quota and free capacity aren't checked.

_GCP_SKUS = ("https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"
             "?pageSize=5000&currencyCode=USD")
_GCP_NA_PREFIXES = ("us-", "northamerica-")

# Smallest machine type per GPU. G2/G4/A2/A3/N1 bill vCPU and RAM on top of the GPU;
# A4 (B200) SKUs are per GPU slice with the machine included.
# (label, GPU SKU name, GPUs, GB per GPU, machine type, vCPU, RAM GB, core/RAM SKU family)
_GCP_OFFERS = [
    ("T4",           "Nvidia Tesla T4 GPU",          1, 16,  "n1-standard-4 + T4",   4,   15,   "N1 Predefined"),
    ("L4",           "Nvidia L4 GPU",                1, 24,  "g2-standard-4",        4,   16,   "G2"),
    ("A100 40GB",    "Nvidia Tesla A100 GPU",        1, 40,  "a2-highgpu-1g",        12,  85,   "A2"),
    ("A100 80GB",    "Nvidia Tesla A100 80GB GPU",   1, 80,  "a2-ultragpu-1g",       12,  170,  "A2"),
    ("RTX PRO 6000", "RTX 6000 96GB",                1, 96,  "g4-standard-48",       48,  180,  "G4"),
    ("H100",         "Nvidia H100 80GB GPU",         8, 80,  "a3-highgpu-8g",        208, 1872, "A3"),
    ("H100 Mega",    "Nvidia H100 80GB Mega GPU",    8, 80,  "a3-megagpu-8g",        208, 1872, "A3"),
    ("H200",         "H200 141GB GPU",               8, 141, "a3-ultragpu-8g",       224, 2952, "A3Ultra"),
    ("B200",         "A4 Nvidia B200 (1 gpu slice)", 8, 180, "a4-highgpu-8g",        0,   0,    None),
]


def _gcp_unit_price(sku):
    rate = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]
    return int(rate.get("units") or 0) + rate.get("nanos", 0) / 1e9


def _gcp_prices(key_param, headers):
    """{(SKU name, is_spot): {region: $/unit/hr}} for on-demand and spot Compute SKUs."""
    prices, page = {}, ""
    while True:
        url = _GCP_SKUS + key_param + (f"&pageToken={page}" if page else "")
        data = http_get(url, headers, timeout=60)
        for sku in data.get("skus", []):
            cat = sku["category"]
            if cat["resourceFamily"] != "Compute" or cat["usageType"] not in ("OnDemand", "Preemptible"):
                continue
            name = re.sub(r" (running )?in .*", "", sku["description"])
            if any(w in name for w in ("DWS", "Reserved", "Custom", "Sole Tenancy", "Commitment")):
                continue
            spot = cat["usageType"] == "Preemptible" or name.startswith("Spot")
            name = name.replace(" attached to Spot Preemptible VMs", "").replace("Spot Preemptible ", "")
            price = _gcp_unit_price(sku)
            if price <= 0:
                continue
            for region in sku["serviceRegions"]:
                prices.setdefault((name, spot), {})[region] = price
        page = data.get("nextPageToken")
        if not page:
            return prices


def _gcp_vm_prices(prices, offer, spot, na_only):
    """{region: $/hr} for the whole VM: GPUs plus the machine's vCPU and RAM."""
    _, sku, gpus, _, _, vcpu, ram, family = offer
    gpu = prices.get((sku, spot), {})
    core = prices.get((f"{family} Instance Core", spot), {}) if family else {}
    mem = prices.get((f"{family} Instance Ram", spot), {}) if family else {}
    out = {}
    for region, p in gpu.items():
        if na_only and not region.startswith(_GCP_NA_PREFIXES):
            continue
        if family and (region not in core or region not in mem):
            continue
        out[region] = gpus * p + vcpu * core.get(region, 0) + ram * mem.get(region, 0)
    return out


def fetch_gcp(top_n, min_vram, max_vram, na_only, use_spot):
    key_param, headers = get_gcp_auth()
    if key_param is None:
        return None, "no credentials — set GCP_API_KEY, write ~/.api/gcp/key, or run 'gcloud auth login'"
    try:
        prices = _gcp_prices(key_param, headers)
    except Exception as e:
        return None, str(e)
    ranked = []
    for offer in _GCP_OFFERS:
        label, _, gpus, vram, machine = offer[:5]
        if not _vram_ok(vram, min_vram, max_vram):
            continue
        od = _gcp_vm_prices(prices, offer, False, na_only)
        sp = _gcp_vm_prices(prices, offer, True, na_only)
        if not od and not sp:
            continue
        region = min(od, key=od.get) if od else min(sp, key=sp.get)
        row = {
            "name": machine, "gpu": label, "gpus": gpus, "vram": vram,
            "ondemand": od.get(region), "region": region, "n_regions": len(od or sp),
            "spot": min(sp.values()) if sp else None,
        }
        row["rank"] = (row["spot"] or row["ondemand"]) if use_spot else (row["ondemand"] or row["spot"])
        ranked.append(row)
    ranked.sort(key=lambda r: r["rank"])
    return {"ranked": ranked[:top_n], "n_available": len(ranked)}, None


# ── Output ────────────────────────────────────────────────────────────────────

W = 90


def _fp(price):
    return f"${price:.3f}" if price is not None else "  N/A "


def _loc_str(locations, max_show=4):
    s = ", ".join(locations[:max_show])
    if len(locations) > max_show:
        s += f" +{len(locations) - max_show}"
    return s


def print_runpod(data, use_spot):
    ranked = data["ranked"]
    n = data["n_available"]
    rank_label = "spot price" if use_spot else "cheapest on-demand"
    print(f"\n  RunPod  ({n} GPU types confirmed in-stock, ranked by {rank_label})")
    w = 86
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {'GPU':<33} {'VRAM':>5}  {'Secure':>7}  {'Commty':>7}  {'Spot':>7}  {'Stock':5}  Regions")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        print(
            f"  {i:>2}  {r['name']:<33} {r['vram']:>4}GB"
            f"  {_fp(r['secure']):>7}"
            f"  {_fp(r['community']):>7}"
            f"  {_fp(r['spot']):>7}"
            f"  {r['stock']:<5}  {_loc_str(r['locations'])}"
        )
    print(f"\n  Prices $/hr  |  Stock: HIGH plentiful  MED some  LOW limited")
    print(f"  Secure = secure cloud  Commty = community cloud  Spot = interruptible")


def print_linode(data):
    ranked = data["ranked"]
    n = data["n_available"]
    print(f"\n  Linode  ({n} GPU instance types, ranked by $/hr)")
    w = 92
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {'Instance Type':<28} {'vCPUs':>5} {'VRAM':>5} {'RAM':>7} {'Disk':>7}  {'$/hr':>8}  {'$/mo':>10}  Label")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        vram_str = f"{r['vram']}GB" if r["vram"] else "  ?"
        print(
            f"  {i:>2}  {r['id']:<28} {r['vcpus']:>5} {vram_str:>5} {r['ram_gb']:>6.0f}GB {r['disk_gb']:>6.0f}GB"
            f"  {r['hourly']:>8.4f}  {r['monthly']:>10.2f}  {r['label']}"
        )


def print_digitalocean(data):
    ranked = data["ranked"]
    n = data["n_available"]
    print(f"\n  DigitalOcean  ({n} GPU Droplet sizes, ranked by $/hr)")
    w = 112
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {'Slug':<26} {'vCPUs':>5} {'VRAM':>5} {'RAM':>7} {'Disk':>7}  {'$/hr':>8}  {'$/mo':>10}  {'Regions':<16}  Description")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        vram_str = f"{r['vram']}GB" if r["vram"] else "  ?"
        regions = _loc_str(r["regions"], max_show=3) or "none"
        print(
            f"  {i:>2}  {r['slug']:<26} {r['vcpus']:>5} {vram_str:>5} {r['ram_gb']:>6.0f}GB {r['disk_gb']:>6}GB"
            f"  {r['hourly']:>8.4f}  {r['monthly']:>10.2f}  {regions:<16}  {r['description']}"
        )
    print(f"\n  Regions = where the size can be created now; none = sold out everywhere")


def print_vast(data, cuda):
    ranked = data["ranked"]
    cuda_note = f", CUDA >= {cuda}" if cuda else ""
    print(f"\n  Vast.ai  (cheapest verified single-GPU offer per model{cuda_note})")
    w = 86
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {'GPU':<24} {'VRAM':>5}  {'$/hr':>7}  {'CUDA':>5}  {'Reliab':>6}  Location")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        print(
            f"  {i:>2}  {r['name']:<24} {r['vram']:>3}GB  {_fp(r['hourly']):>7}"
            f"  {r['cuda'] or '?':>5}  {r['reliability']:>6.1%}  {r['location']}"
        )
    print(f"\n  $/hr includes the host's default disk  |  Reliab = host uptime score")
    print(f"  Peer-hosted machines: an offer can be rented by someone else at any time")


def print_cloud(title, unit, data, use_spot):
    """AWS / GCP: cheapest instance or machine type per GPU."""
    ranked = data["ranked"]
    rank_label = "spot price" if use_spot else "on-demand"
    print(f"\n  {title}  (cheapest {unit} per GPU, {data['n_available']} matching, ranked by {rank_label})")
    w = 98
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {unit.capitalize():<22} {'GPUs':<27} {'VRAM':>5}  {'On-dem':>8}  {'Spot':>8}  {'Cheapest region':<24} Regions")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        gpus = f"{r['gpus']}x {r['gpu']}"
        print(
            f"  {i:>2}  {r['name']:<22} {gpus:<27} {r['vram']:>3}GB"
            f"  {_fp(r['ondemand']):>8}  {_fp(r['spot']):>8}  {r['region']:<24} {r['n_regions']:>3}"
        )
    print(f"\n  Prices $/hr for the whole VM  |  Regions = where it's sold (not live capacity)")
    print(f"  Spot N/A = not in the public spot feed (may still be offered)  |  GPU quota not checked")


# ── Main ──────────────────────────────────────────────────────────────────────

PROVIDERS = ["runpod", "linode", "digitalocean", "vast", "aws", "gcp"]


def main():
    parser = argparse.ArgumentParser(
        description="Show cheapest GPU options across cloud providers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--top", type=int, default=5, metavar="N",
                        help="Results to show per provider (default: 5)")
    parser.add_argument("--provider", choices=PROVIDERS + ["all"], default="all",
                        help="Which provider(s) to query (default: all)")
    parser.add_argument("--spot", action="store_true",
                        help="RunPod, AWS, GCP: rank by spot/interruptible price instead of on-demand")
    parser.add_argument("--min-vram", type=int, default=0, metavar="GB",
                        help="Only show GPUs with at least this much VRAM per GPU in GB "
                             "(e.g. --min-vram 48). Common values: 16, 20, 24, 32, 48, 80, 141, 192")
    parser.add_argument("--max-vram", type=int, default=0, metavar="GB",
                        help="Only show GPUs with at most this much VRAM per GPU in GB "
                             "(e.g. --min-vram 32 --max-vram 48)")
    parser.add_argument("--na", action="store_true",
                        help="North America only: RunPod US/CA datacenters, DigitalOcean "
                             "NYC/SFO/TOR/ATL/RIC/MEM/MKC regions, Vast.ai hosts in US/CA/MX, "
                             "AWS us-/ca-/mx- and GCP us-/northamerica- regions. "
                             "Linode is not filtered (its API has no per-region GPU data)")
    parser.add_argument("--cuda", type=float, default=0, metavar="VER",
                        help="Vast.ai: only hosts whose driver supports at least this CUDA "
                             "version (e.g. --cuda 13.0). Linode/DigitalOcean are VMs where "
                             "you install the driver yourself")
    args = parser.parse_args()

    want = set(PROVIDERS) if args.provider == "all" else {args.provider}

    notes = []
    if args.min_vram and args.max_vram:
        notes.append(f"VRAM {args.min_vram}-{args.max_vram} GB")
    elif args.min_vram:
        notes.append(f"VRAM >= {args.min_vram} GB")
    elif args.max_vram:
        notes.append(f"VRAM <= {args.max_vram} GB")
    if args.na:
        notes.append("North America")
    note = "".join(f", {n}" for n in notes)
    print(f"\n{'═' * W}")
    print(f"  AI GPU Cost — {args.top} Cheapest per Provider{note}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * W}")

    any_results = False

    if "runpod" in want:
        print("Querying RunPod ...", end=" ", flush=True)
        data, err = fetch_runpod(args.top, args.spot, args.min_vram, args.max_vram, args.na)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_runpod(data, args.spot)
            any_results = True

    if "linode" in want:
        print("Querying Linode ...", end=" ", flush=True)
        data, err = fetch_linode(args.top, args.min_vram, args.max_vram)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_linode(data)
            if args.na:
                print("  (--na not applied: Linode's API doesn't say which regions have each GPU type)")
            any_results = True

    if "digitalocean" in want:
        print("Querying DigitalOcean ...", end=" ", flush=True)
        data, err = fetch_digitalocean(args.top, args.min_vram, args.max_vram, args.na)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_digitalocean(data)
            any_results = True

    if "vast" in want:
        print("Querying Vast.ai ...", end=" ", flush=True)
        data, err = fetch_vast(args.top, args.min_vram, args.max_vram, args.cuda, args.na)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_vast(data, args.cuda)
            any_results = True

    if "aws" in want:
        print("Querying AWS ...", end=" ", flush=True)
        data, err = fetch_aws(args.top, args.min_vram, args.max_vram, args.na, args.spot)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_cloud("AWS", "instance", data, args.spot)
            any_results = True

    if "gcp" in want:
        print("Querying GCP ...", end=" ", flush=True)
        data, err = fetch_gcp(args.top, args.min_vram, args.max_vram, args.na, args.spot)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_cloud("GCP", "machine", data, args.spot)
            any_results = True

    if not any_results:
        print("\n  No providers returned data. Check your API keys/tokens.")
        sys.exit(1)

    print(f"\n{'═' * W}\n")


if __name__ == "__main__":
    main()
