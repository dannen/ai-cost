#!/usr/bin/env python3
"""
Find the cheapest currently-available GPU options across cloud providers.

Providers: RunPod, Linode, DigitalOcean

Usage:
    python cheapest.py                          # all providers, top 5
    python cheapest.py --top 10
    python cheapest.py --provider runpod
    python cheapest.py --provider linode
    python cheapest.py --provider digitalocean
    python cheapest.py --spot                   # RunPod: rank by spot price
"""

import os
import sys
import json
import argparse
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


# ── RunPod ────────────────────────────────────────────────────────────────────

_RUNPOD_GQL = "https://api.runpod.io/graphql"
_RUNPOD_IN_STOCK = {"High", "Medium", "Low"}
_RUNPOD_STOCK_RANK = {"High": 0, "Medium": 1, "Low": 2}


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


def _runpod_stock_map(datacenters):
    stock = {}
    for dc in datacenters:
        for ga in dc.get("gpuAvailability") or []:
            gid = ga["gpuTypeId"]
            status = ga.get("stockStatus")
            if status in _RUNPOD_IN_STOCK:
                stock.setdefault(gid, []).append((dc["id"], dc["location"], status))
    return stock


def _runpod_rank(gpu_types, stock_map, top_n, use_spot):
    candidates = []
    for gid, gpu in gpu_types.items():
        locs = stock_map.get(gid)
        if not locs:
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
            "vram": gpu.get("memoryInGb", 0),
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


def fetch_runpod(top_n, use_spot):
    api_key = get_runpod_key()
    if not api_key:
        return None, "no API key — set RUNPOD_API_KEY or write to ~/.api/runpod/claude.key"
    try:
        data = _runpod_fetch(api_key)
    except Exception as e:
        return None, str(e)
    gpu_types = {g["id"]: g for g in data["gpuTypes"]}
    stock_map = _runpod_stock_map(data["dataCenters"])
    n_available = sum(1 for g in gpu_types if g in stock_map)
    ranked = _runpod_rank(gpu_types, stock_map, top_n, use_spot)
    return {"ranked": ranked, "n_available": n_available}, None


# ── Linode ────────────────────────────────────────────────────────────────────

_LINODE_API = "https://api.linode.com/v4"


def _linode_fetch(token):
    data = http_get(f"{_LINODE_API}/linode/types", {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    return data.get("data", [])


def fetch_linode(top_n):
    token = get_linode_token()
    if not token:
        return None, "no token — set LINODE_TOKEN or write to ~/.api/linode/token"
    try:
        all_types = _linode_fetch(token)
    except Exception as e:
        return None, str(e)
    gpu_types = [t for t in all_types if t.get("class") == "gpu"]
    gpu_types.sort(key=lambda t: t.get("price", {}).get("hourly") or 0)
    ranked = []
    for t in gpu_types[:top_n]:
        price = t.get("price", {})
        ranked.append({
            "id": t.get("id", ""),
            "label": t.get("label", ""),
            "vcpus": t.get("vcpus", 0),
            "ram_gb": t.get("memory", 0) / 1024,
            "disk_gb": t.get("disk", 0) / 1024,
            "hourly": price.get("hourly") or 0,
            "monthly": price.get("monthly") or 0,
        })
    return {"ranked": ranked, "n_available": len(gpu_types)}, None


# ── DigitalOcean ──────────────────────────────────────────────────────────────

_DO_API = "https://api.digitalocean.com/v2"


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


def fetch_digitalocean(top_n):
    token = get_do_token()
    if not token:
        return None, "no token — set DIGITALOCEAN_TOKEN or write to ~/.api/digitalocean/token"
    try:
        all_sizes = _do_paginate(token, "/sizes", "sizes")
    except Exception as e:
        return None, str(e)
    gpu_sizes = [s for s in all_sizes if s.get("slug", "").startswith("gpu-")]
    gpu_sizes.sort(key=lambda s: s.get("price_hourly") or 0)
    ranked = []
    for s in gpu_sizes[:top_n]:
        ranked.append({
            "slug": s.get("slug", ""),
            "description": s.get("description", ""),
            "vcpus": s.get("vcpus", 0),
            "ram_gb": s.get("memory", 0) / 1024,
            "disk_gb": s.get("disk", 0),
            "hourly": s.get("price_hourly") or 0,
            "monthly": s.get("price_monthly") or 0,
            "regions": s.get("regions", []),
        })
    return {"ranked": ranked, "n_available": len(gpu_sizes)}, None


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
    w = 86
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {'Instance Type':<28} {'vCPUs':>5} {'RAM':>7} {'Disk':>7}  {'$/hr':>8}  {'$/mo':>10}  Label")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        print(
            f"  {i:>2}  {r['id']:<28} {r['vcpus']:>5} {r['ram_gb']:>6.0f}GB {r['disk_gb']:>6.0f}GB"
            f"  {r['hourly']:>8.4f}  {r['monthly']:>10.2f}  {r['label']}"
        )


def print_digitalocean(data):
    ranked = data["ranked"]
    n = data["n_available"]
    print(f"\n  DigitalOcean  ({n} GPU Droplet sizes, ranked by $/hr)")
    w = 90
    print(f"  {'─' * w}")
    print(f"  {'#':>2}  {'Slug':<26} {'vCPUs':>5} {'RAM':>7} {'Disk':>7}  {'$/hr':>8}  {'$/mo':>10}  Description")
    print(f"  {'─' * w}")
    for i, r in enumerate(ranked, 1):
        print(
            f"  {i:>2}  {r['slug']:<26} {r['vcpus']:>5} {r['ram_gb']:>6.0f}GB {r['disk_gb']:>6}GB"
            f"  {r['hourly']:>8.4f}  {r['monthly']:>10.2f}  {r['description']}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

PROVIDERS = ["runpod", "linode", "digitalocean"]


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
    args = parser.parse_args()

    want = set(PROVIDERS) if args.provider == "all" else {args.provider}

    print(f"\n{'═' * W}")
    print(f"  AI GPU Cost — {args.top} Cheapest per Provider")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * W}")

    any_results = False

    if "runpod" in want:
        print("Querying RunPod ...", end=" ", flush=True)
        data, err = fetch_runpod(args.top, args.spot)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_runpod(data, args.spot)
            any_results = True

    if "linode" in want:
        print("Querying Linode ...", end=" ", flush=True)
        data, err = fetch_linode(args.top)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_linode(data)
            any_results = True

    if "digitalocean" in want:
        print("Querying DigitalOcean ...", end=" ", flush=True)
        data, err = fetch_digitalocean(args.top)
        if err:
            print(f"skipped ({err})")
        else:
            print("done.")
            print_digitalocean(data)
            any_results = True

    if not any_results:
        print("\n  No providers returned data. Check your API keys/tokens.")
        sys.exit(1)

    print(f"\n{'═' * W}\n")


if __name__ == "__main__":
    main()
