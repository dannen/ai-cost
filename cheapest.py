#!/usr/bin/env python3
"""
Find the cheapest currently-available GPU options across cloud providers.

Providers: RunPod, Linode, DigitalOcean, Vast.ai

Usage:
    python cheapest.py                          # all providers, top 5
    python cheapest.py --top 10
    python cheapest.py --provider runpod
    python cheapest.py --provider linode
    python cheapest.py --provider digitalocean
    python cheapest.py --provider vast
    python cheapest.py --spot                   # RunPod: rank by spot price
    python cheapest.py --min-vram 48            # only GPUs with >= 48 GB VRAM
    python cheapest.py --min-vram 32 --max-vram 48
    python cheapest.py --cuda 13.0              # Vast.ai: only hosts that run CUDA >= 13.0
    python cheapest.py --na                     # North America only (not Linode)

VRAM ranges available across providers (per GPU):
    16 GB  — RTX 2000 Ada, RTX A4000                        (RunPod, Vast.ai)
    20 GB  — RTX 4000 Ada, RTX A4500                        (RunPod, Linode, DigitalOcean, Vast.ai)
    24 GB  — RTX 3090, RTX 4090, L4, RTX A5000, Quadro RTX 6000  (RunPod, Linode, Vast.ai)
    32 GB  — RTX 5090, RTX PRO 4500, RTX 5000 Ada            (RunPod, Vast.ai)
    40 GB  — A100 40GB                                      (RunPod, Vast.ai)
    48 GB  — A40, L40S, RTX 6000 Ada, RTX A6000, RTX PRO 5000  (RunPod, DigitalOcean, Vast.ai)
    80 GB  — A100 PCIe/SXM, H100 SXM/PCIe                  (RunPod, DigitalOcean, Vast.ai)
    94 GB  — H100 NVL                                       (RunPod, Vast.ai)
    96 GB  — RTX PRO 6000                                   (RunPod, Vast.ai)
   141 GB  — H200 SXM                                       (RunPod, DigitalOcean, Vast.ai)
   180 GB  — B200                                           (RunPod, Vast.ai)
   192 GB  — MI300X                                         (DigitalOcean)
   288 GB  — B300                                           (RunPod, Vast.ai)
"""

import os
import re
import sys
import json
import argparse
import urllib.parse
import urllib.request
import urllib.error
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


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')}")


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
_DO_NA_PREFIXES = ("nyc", "sfo", "tor", "atl", "ric")   # region slugs like nyc2, tor1, atl1


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
    w = 96
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {'Slug':<26} {'vCPUs':>5} {'VRAM':>5} {'RAM':>7} {'Disk':>7}  {'$/hr':>8}  {'$/mo':>10}  Description")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        vram_str = f"{r['vram']}GB" if r["vram"] else "  ?"
        print(
            f"  {i:>2}  {r['slug']:<26} {r['vcpus']:>5} {vram_str:>5} {r['ram_gb']:>6.0f}GB {r['disk_gb']:>6}GB"
            f"  {r['hourly']:>8.4f}  {r['monthly']:>10.2f}  {r['description']}"
        )


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


# ── Main ──────────────────────────────────────────────────────────────────────

PROVIDERS = ["runpod", "linode", "digitalocean", "vast"]


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
                        help="RunPod: rank by spot/interruptable price instead of on-demand")
    parser.add_argument("--min-vram", type=int, default=0, metavar="GB",
                        help="Only show GPUs with at least this much VRAM per GPU in GB "
                             "(e.g. --min-vram 48). Common values: 16, 20, 24, 32, 48, 80, 141, 192")
    parser.add_argument("--max-vram", type=int, default=0, metavar="GB",
                        help="Only show GPUs with at most this much VRAM per GPU in GB "
                             "(e.g. --min-vram 32 --max-vram 48)")
    parser.add_argument("--na", action="store_true",
                        help="North America only: RunPod US/CA datacenters, DigitalOcean "
                             "NYC/SFO/TOR/ATL/RIC regions, Vast.ai hosts in US/CA/MX. "
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

    if not any_results:
        print("\n  No providers returned data. Check your API keys/tokens.")
        sys.exit(1)

    print(f"\n{'═' * W}\n")


if __name__ == "__main__":
    main()
