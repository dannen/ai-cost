# ai-cost

CLI tool that queries multiple cloud GPU providers and shows the cheapest currently-available options.

**Providers:** RunPod · Linode · DigitalOcean · Vast.ai · AWS · GCP

## Requirements

- Python 3.8+ (stdlib only, no dependencies)
- API key/token for each provider you want to query (Vast.ai's search needs none)

## API key setup

### RunPod

Get your key at [RunPod console → Settings → API Keys](https://www.runpod.io/console/user/settings).

```bash
# Option A — env var
export RUNPOD_API_KEY="rpa_xxxxxxxxxxxxxxxxxxxx"

# Option B — key file (name/key format or plain token)
mkdir -p ~/.api/runpod
echo 'rpa_xxxxxxxxxxxxxxxxxxxx' > ~/.api/runpod/claude.key
chmod 600 ~/.api/runpod/claude.key
```

Also supports `~/.runpod/config.toml` (official CLI config).

### Linode

Get your token at [Linode console → Profile → API Tokens](https://cloud.linode.com/profile/tokens).

```bash
# Option A — env var
export LINODE_TOKEN="your_token_here"

# Option B — key file
mkdir -p ~/.api/linode
printf 'name = mytoken\nkey = your_token_here\n' > ~/.api/linode/token
chmod 600 ~/.api/linode/token
```

Also supports a plain-token file or `~/.config/linode-cli` (linode-cli config).

### DigitalOcean

Get your token at [DigitalOcean console → API → Personal Access Tokens](https://cloud.digitalocean.com/account/api/tokens).

```bash
# Option A — env var
export DIGITALOCEAN_TOKEN="dop_v1_xxxxxxxxxxxxxxxxxxxx"

# Option B — key file
mkdir -p ~/.api/digitalocean
printf 'key=dop_v1_xxxxxxxxxxxxxxxxxxxx\nname=digitalocean\n' > ~/.api/digitalocean/token
chmod 600 ~/.api/digitalocean/token
```

Also supports `DO_TOKEN` env var or `~/.config/doctl/config.yaml` (doctl config).

### Vast.ai

No key needed: the offer search is public.

### AWS

No credentials needed: prices come from the public feeds behind the EC2 pricing pages.

### GCP

Prices come from the Cloud Billing Catalog API. Either use an API key (with the Cloud Billing API enabled on its project):

```bash
export GCP_API_KEY="AIza..."            # or write it to ~/.api/gcp/key
```

or, with no key, the script uses your gcloud login (`gcloud auth print-access-token`). If that has expired, run `gcloud auth login`.

## Usage

```bash
python cheapest.py                          # all providers, top 5 each
python cheapest.py --top 10                 # show more results
python cheapest.py --provider runpod        # single provider
python cheapest.py --provider linode
python cheapest.py --provider digitalocean
python cheapest.py --provider vast
python cheapest.py --provider aws
python cheapest.py --provider gcp
python cheapest.py --spot                   # RunPod, AWS, GCP: rank by spot price
python cheapest.py --min-vram 48            # only GPUs with >= 48 GB VRAM per GPU
python cheapest.py --min-vram 80 --top 10
python cheapest.py --provider vast --min-vram 32 --cuda 13.0   # Vast.ai hosts that run CUDA 13
python cheapest.py --min-vram 32 --max-vram 48                # VRAM range
python cheapest.py --na                                      # North America only
```

Providers with no configured token are skipped with a note rather than erroring.

`--cuda VER` applies to Vast.ai only: it keeps hosts whose driver supports at least that CUDA version (and, for 13.0+, Turing or newer GPUs, since CUDA 13 dropped Volta and older). Linode and DigitalOcean are VMs where you install the driver yourself.

`--na` keeps North America only: RunPod `US-*`/`CA-*` datacenters, DigitalOcean NYC/SFO/TOR/ATL/RIC/MEM/MKC regions, Vast.ai hosts in the US, Canada or Mexico, AWS `us-`/`ca-`/`mx-` regions and GCP `us-`/`northamerica-` regions. Linode isn't filtered, because its API doesn't say which regions have each GPU type.

## VRAM ranges

`--min-vram` / `--max-vram` filter by per-GPU VRAM. Values available across providers:

| VRAM | GPUs | Providers |
|-----:|------|-----------|
| 16 GB | RTX 2000 Ada, RTX A4000, T4 | RunPod, Vast.ai, AWS, GCP |
| 20 GB | RTX 4000 Ada, RTX A4500 | RunPod, Linode, DigitalOcean, Vast.ai |
| 24 GB | RTX 3090, RTX 4090, L4, RTX A5000, A10G, Quadro RTX 6000 | RunPod, Linode, Vast.ai, AWS, GCP |
| 32 GB | RTX 5090, RTX PRO 4500, RTX 5000 Ada | RunPod, Vast.ai, AWS |
| 40 GB | A100 40GB | RunPod, Vast.ai, AWS, GCP |
| 48 GB | A40, L40S, RTX 6000 Ada, RTX A6000, RTX PRO 5000 | RunPod, DigitalOcean, Vast.ai, AWS |
| 80 GB | A100 PCIe/SXM, H100 SXM/PCIe | RunPod, DigitalOcean, Vast.ai, AWS, GCP |
| 94 GB | H100 NVL | RunPod, Vast.ai |
| 96 GB | RTX PRO 6000 | RunPod, Vast.ai, AWS, GCP |
| 141 GB | H200 SXM | RunPod, DigitalOcean, Vast.ai, AWS, GCP |
| 180 GB | B200 | RunPod, Vast.ai, AWS, GCP |
| 192 GB | MI300X | DigitalOcean |
| 288 GB | B300 | RunPod, Vast.ai, AWS |

## Output

```
══════════════════════════════════════════════════════════════════════════════════════════
  AI GPU Cost — 5 Cheapest per Provider
  2026-06-20 18:34
══════════════════════════════════════════════════════════════════════════════════════════
Querying RunPod ... done.

  RunPod  (27 GPU types confirmed in-stock, ranked by cheapest on-demand)
  ──────────────────────────────────────────────────────────────────────────────────────
   #  GPU                                VRAM   Secure   Commty     Spot  Stock  Regions
  ──────────────────────────────────────────────────────────────────────────────────────
   1  RTX A5000                           24GB   $0.270   $0.160   $0.160  Low    CA-MTL-1, EU-SE-1, US-IL-1
   2  RTX A4000                           16GB   $0.250   $0.170   $0.170  Low    EUR-IS-1
   ...

  Linode  (13 GPU instance types, ranked by $/hr)
  ──────────────────────────────────────────────────────────────────────────────────────
   #  Instance Type                vCPUs     RAM    Disk      $/hr        $/mo  Label
  ──────────────────────────────────────────────────────────────────────────────────────
   1  g2-gpu-rtx4000a1-s               4     16GB    512GB    0.5200      350.00  RTX4000 Ada x1 Small
   ...

  DigitalOcean  (9 GPU Droplet sizes, ranked by $/hr)
  ──────────────────────────────────────────────────────────────────────────────────────────
   #  Slug                       vCPUs     RAM    Disk      $/hr        $/mo  Description
  ──────────────────────────────────────────────────────────────────────────────────────────
   1  gpu-4000adax1-20gb             8     32GB    500GB    0.7600      565.44  RTX 4000 Ada GPU Droplet - 1X
   2  gpu-l40sx1-48gb                8     64GB    500GB    1.5700     1168.08  L40S GPU Droplet - 1X
   ...
══════════════════════════════════════════════════════════════════════════════════════════
```

**RunPod columns:**
- `Secure` — secure cloud on-demand $/hr (supports network volumes)
- `Commty` — community cloud on-demand $/hr (cheaper, no network volumes)
- `Spot` — interruptible/spot price $/hr (cheapest, worker can be preempted)
- `Stock` — HIGH = plentiful, MED = some available, LOW = limited
- `Regions` — datacenter IDs where this GPU is confirmed in-stock

Only RunPod GPUs with at least one datacenter reporting `stockStatus: High/Medium/Low` are shown.

**Linode / DigitalOcean columns:**
- `$/hr` — on-demand hourly rate
- `$/mo` — monthly equivalent (as listed by provider)
- `Regions` (DigitalOcean) — regions where the size can be created now; `none` = sold out everywhere

**Vast.ai columns** (cheapest verified, rentable single-GPU offer per GPU model):
- `$/hr` — on-demand price including the host's default disk
- `VRAM` — nominal size (hosts report usable memory, e.g. an L40S shows ~45 GiB)
- `CUDA` — highest CUDA version the host's driver supports
- `Reliab` — Vast.ai's host reliability score

Vast.ai machines are peer-hosted: an offer can be rented by someone else at any time.

**AWS / GCP columns** (cheapest instance or machine type per GPU):
- `On-dem` / `Spot` — $/hr for the whole VM (GCP adds the machine's vCPU and RAM to the GPU price)
- `Cheapest region` — where the on-demand price is lowest
- `Regions` — how many regions sell it (a listing, not live capacity)

AWS's public spot feed leaves out some newer families (g6e, g7, g7e and others), shown as `N/A`. Neither cloud's GPU quota is checked: new accounts and projects often start at 0. GCP machine shapes (vCPU/RAM per GPU) and the AWS GPU-per-instance table are hard-coded, so new families need adding.
