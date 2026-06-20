# ai-cost

CLI tool that queries multiple cloud GPU providers and shows the cheapest currently-available options.

**Providers:** RunPod · Linode · DigitalOcean

## Requirements

- Python 3.8+ (stdlib only, no dependencies)
- API key/token for each provider you want to query

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

## Usage

```bash
python cheapest.py                          # all providers, top 5 each
python cheapest.py --top 10                 # show more results
python cheapest.py --provider runpod        # single provider
python cheapest.py --provider linode
python cheapest.py --provider digitalocean
python cheapest.py --spot                   # RunPod: rank by spot price
```

Providers with no configured token are skipped with a note rather than erroring.

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
