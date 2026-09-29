# Part 14 — Deployment: put it on GitHub, then put it on the internet

This part is about shipping the prototype: publishing the repository, hosting a live copy a judge
can open on a phone, and — just as importantly — being precise about what a hosted copy does and
does not prove.

Read Part 8 first if you only want to run it on your own machine. This part assumes that works.

**The recommended free route is Streamlit Community Cloud (§4).** It is checked against the prices
and limits of September 2026; free tiers move, so re-read the table in §1 before committing.

---

## 0. Two shapes of the same system

Everything here is designed around **one container with two processes**, which is what makes the
self-hosted and Docker routes workable:

| Process | Port | What it is | Reachable from the internet? |
|---|---|---|---|
| Streamlit console | **8501** (local Docker) · **7860** (Hugging Face) · whatever the host injects (Render `$PORT`) | the investigator dashboard | yes — this is the port that gets proxied |
| FastAPI backend | **8000** | `/alerts`, `/graph`, `/run`, `/upload`, `/ledger/verify` | only on a full-container host (Docker, Render, Hugging Face internally) |
| uvicorn's Swagger UI | `/docs` on 8000 | interactive API documentation | same as the API |

The dashboard calls the API first (`CHAINTRACE_API_URL`, default `http://127.0.0.1:8000`) and
**silently falls back to reading `data/artifacts/` from disk**. That fallback is the reason the
console works unchanged on a host that runs a single Python process: the sidebar badge switches from
`API · live` to `artefacts · filesystem`, which is a cosmetic difference, not a functional one.

Nothing in the running system needs the internet, ever. The build is the only step that touches the
network, and it uses it for exactly three things: installing the pinned dependencies, generating the
synthetic dataset, and training the models.

**Where the artefacts come from depends on the host**, and it is the one thing that differs between
them:

| Host | Who warms the copy |
|---|---|
| Docker | the image build, then `entrypoint.sh` |
| local checkout | you: `python scripts/run_full_pipeline.py` |
| Streamlit Community Cloud | the app itself: `app/cloud_app.py` → `src/bootstrap.py`, about 3 s on first paint |

The artefacts are deliberately **not** committed. They cost seconds to rebuild and they would drift
from the code the moment anything in `src/` changed; the dataset they are derived from *is*
committed, because that is the evidence a reviewer wants to read.

---

## 1. Choosing where to host it

Checked September 2026 — verify before you commit, because these tiers moved a lot in 2026:

| Route | Cost | Runs | Sign-in | Catch |
|---|---|---|---|---|
| **Streamlit Community Cloud** | free | the dashboard (one Python process) | GitHub | no FastAPI, no `/docs`; sleeps when idle |
| **Render free web service** | free | the real Docker image — both processes | GitHub | 512 MB / 0.1 CPU; ~1 min cold start after 15 min idle |
| **Hugging Face Space (Docker)** | **PRO, about $9/month** | the real Docker image | HF account | Docker and Gradio Spaces now need a paid plan; only *Static* Spaces are free |
| **Local `docker compose`** | free | both processes | none | reachable only on the machine running it |

Hugging Face is worth calling out because it used to be the obvious free choice and is not any more:
their current documentation states that *"Static Spaces are free for everyone. Gradio and Docker
Spaces run on compute and require a paid plan to create: PRO for personal accounts, Team or
Enterprise for organizations."* The Space path is fully prepared anyway (§6) if you ever want it.

---

## 2. GitHub

The repository root is the `chaintrace-ai/` directory itself, so the repository root *is* the build
context — no subdirectory trickery on any host.

```bash
cd chaintrace-ai
git init -b main
git add -A
git commit -m "ChainTrace AI - SIH26146 offline ML triage prototype"

# with the GitHub CLI (creates the remote and pushes in one step)
gh repo create chaintrace-ai --public --source=. --remote=origin --push
```

Without `gh`: create an empty repository in the browser, then

```bash
git remote add origin https://github.com/<you>/chaintrace-ai.git
git push -u origin main
```

A private repository works everywhere except the hosted step below, which needs the repository
public (or the app deployed through Streamlit's GitHub authorisation). Flipping it later is one
command: `gh repo edit --visibility public`.

### What is in the repository, and what is deliberately not

| Tracked | Ignored | Why |
|---|---|---|
| the whole `src/`, `app/`, `scripts/`, `tests/` tree | `.venv/`, `.venv311/` (~1 GB each) | a virtualenv is not source |
| `data/synthetic/` — the 1,200-transaction dataset and the ground-truth manifest | `data/artifacts/`, `data/models/*.joblib` | the dataset is the evidence and is byte-stable from `RANDOM_SEED = 42`; the artefacts are rebuilt in seconds and would drift |
| `data/geo/mock_geoip.json` (offline GeoIP table) | `data/evidence_ledger.jsonl` | rebuilt on every run, and one ledger line is one hash |
| the sample upload under `data/raw/upload_*/` | `.env`, `.streamlit/secrets.toml` | never commit secrets |
| `requirements.txt` (runtime) and `requirements-dev.txt` (adds pytest) | — | the shipped image must not carry the test runner |

### CI

`.github/workflows/ci.yml` runs on every push and proves the reproducibility claim on a clean
checkout — no data, no models, no artefacts:

1. `generate_sample_data.py` + `audit_dataset.py` → the dataset still satisfies the SIH26146 contract.
2. `train_models.py` + `run_full_pipeline.py` → all eight stages still run from scratch.
3. `validate_detections.py` → precision/recall against the planted ground truth.
4. `pytest -q` → 64 tests.
5. a second job that builds the real Docker image (nothing pushed), so "one command" stays true.

> Two bugs were only ever visible in CI, which is the argument for having it: `pytest` was missing
> from `requirements.txt` (it had only ever existed in the local virtualenv), and the bare `pytest`
> console script does not put the repository root on `sys.path` the way `python -m pytest` does.
> Both are fixed in the repository — `requirements-dev.txt` and `pytest.ini`.

---

## 3. One more thing before you deploy: the warm-up

A hosted copy starts from a clean checkout, which means **the dataset is there but the artefacts are
not**. On Docker the image build handles that. On Streamlit Community Cloud there is no build step,
so `app/cloud_app.py` does it in-process:

```python
ensure_artefacts()                       # src/bootstrap.py - ~3 s, no-op when already warm
runpy.run_path("app/dashboard.py")       # then the console, unchanged
```

`src/bootstrap.py` generates the dataset if it is missing, runs the eight-stage pipeline once and
stops. The pipeline fits its own models as it runs (`detect_anomalies` fits and scores in one call),
so the `.joblib` files are a by-product of a run rather than a prerequisite for one. The three files
it insists on are `alerts.json`, `graph.json` and `pipeline_summary.json`; the console reads nothing
else it cannot render without.

This is also why a bare clone now works with no setup at all:

```bash
streamlit run app/cloud_app.py          # warms itself, then serves the console
```

`tests/test_bootstrap.py` covers it: a bare copy becomes a populated console, a warm copy costs
nothing, a deleted dataset is regenerated, and the hosted entrypoint renders the whole console.

---

## 4. Streamlit Community Cloud (free, recommended)

### 4.1 Deploy

1. Sign in at **https://share.streamlit.io** with the GitHub account that owns the repository
   (authorise Streamlit to read it — this is the whole signup, there is no separate account).
2. **Create app** → **Deploy a public app from GitHub**.
3. Fill in:

   | Field | Value |
   |---|---|
   | Repository | `<you>/chaintrace-ai` |
   | Branch | `main` |
   | **Main file path** | **`app/cloud_app.py`** (not `dashboard.py` — the wrapper is what warms the copy) |
   | App URL | pick the subdomain, e.g. `chaintrace-ai` |
   | Python version (Advanced settings) | **3.11** |

4. **Deploy.** The first build installs `requirements.txt` and opens the console; the first page load
   spends about three seconds warming up, and every load after that is instant.

The app is then live at `https://<subdomain>.streamlit.app`.

### 4.2 What runs there

| | |
|---|---|
| the four console screens | yes — alerts, graph, infrastructure, evidence |
| Run Analysis (any of the three input sources) | yes — it runs in-process, no API needed |
| the SHA-256 verification and the tamper demo | yes |
| the FastAPI service and its `/docs` | **no** — one process only. The sidebar reads `artefacts · filesystem` |
| the standalone pyvis HTML export | yes (the download button builds it in-process) |

---

## 5. What is different in the hosted copy

| Difference | Handling |
|---|---|
| no second process, so no API | the console's documented fallback reads `data/artifacts/` instead. The badge changes, nothing else does |
| no build step, so no artefacts | `app/cloud_app.py` → `src/bootstrap.py` warms the copy in about three seconds on first paint (§3) |
| the disk is **ephemeral** | a sleep/wake or a redeploy resets the container: an appended ledger line from the tamper demo and anything you uploaded vanish, and a fresh 3-second warm-up runs. The committed dataset is what makes that cheap |
| **1 GB RAM**, shared CPU | comfortable: the whole pipeline peaks well under 500 MB on the 1,200-transaction dataset. If a host is tighter than expected, lower `DEFAULT_TX_COUNT` and `MAX_GRAPH_NODES_FOR_EXPORT` in `src/config.py` |
| the app **sleeps when idle** (about 12 hours with no traffic on the free tier) | the next visit wakes it; expect a slow first paint, not an error |
| the URL is **public** and there is no authentication | anyone with the link sees the whole console, and the upload control accepts files from anyone. Take the app down, or run it locally, for a dry run you do not want mirrored |
| `.streamlit/config.toml` sets `enableXsrfProtection = false` and `enableCORS = false` | required for the reverse-proxy hosts (Docker behind a proxy, Hugging Face, Cloud Run). Turn XSRF protection back on if you only ever deploy here |

---

## 6. Verifying a live deployment

Thirty seconds, and it catches the failure that matters: an app that *looks* healthy while the
console is quietly empty.

```bash
# 1. Streamlit is serving
curl -s -o /dev/null -w '%{http_code}\n' https://<subdomain>.streamlit.app/          # -> 200
curl -s -o /dev/null -w '%{http_code}\n' https://<subdomain>.streamlit.app/healthz   # -> ok

# 2. the warm-up actually produced an analysis (from the app log, or on screen)
#    counters should read 1,200 transactions / 60 alerts - not 0
```

In the browser, in order:

1. **Screen 1** — counters non-zero, the ranked alert table populated; select the top alert and read
   its reasons and recommended action.
2. **Screen 2** — click a node, confirm the halo/focus behaviour, drag the timeline seek bar.
3. **Screen 3** — the infrastructure roll-up has ASNs and countries, not `unknown`.
4. **Screen 4** — re-verify an alert (hash matches), then run the tamper demo and watch the ledger
   reject it.

If the counters read zero, the warm-up did not finish — read the app log (Manage app → Logs), not
the screen.

---

## 7. Hugging Face Space (the paid path, already built)

Everything for this is in the repository and tested: `deploy/huggingface/README.md` is the Space
card (`sdk: docker`, `app_port: 7860`), `scripts/deploy_hf_space.sh` publishes a commit to a Space,
and `entrypoint.sh` / the `Dockerfile` already handle the things a Space does differently — it
proxies a single port (7860, chosen when `SPACE_ID` is present) and runs the container as **UID
1000** (the image chmods its data tree, and the entrypoint falls back to a writable copy in `/tmp`
if a host hands it a read-only tree).

```bash
# create the Space first: huggingface.co/new-space -> SDK Docker -> Blank
DRY_RUN=1 scripts/deploy_hf_space.sh <you>/chaintrace-ai          # inspect what would ship
scripts/deploy_hf_space.sh <you>/chaintrace-ai <hf-write-token>   # then push
```

The script exports the commit you have checked out with `git archive` (so uncommitted edits and
ignored files cannot leak), substitutes the Space card for the repository README, and refuses to
push if the `sdk:` or `app_port:` lines are missing. Revoke the write token once the build is green.

Live at `huggingface.co/spaces/<you>/chaintrace-ai` and `<you>-chaintrace-ai.hf.space`.

---

## 8. Render free web service (the full two-process shape, free)

1. **https://render.com** → sign in with GitHub → **New** → **Web Service** → pick the repository.
2. Language: **Docker**. Leave the Dockerfile path as `./Dockerfile`.
3. Environment variables: `CHAINTRACE_DASHBOARD_PORT` = the port Render injects (Render exposes
   `$PORT`; if you cannot reference it, set `CHAINTRACE_DASHBOARD_PORT` to `10000` and change the
   port Render listens on to match — this is the one setting that is fiddly on Render).
4. Instance type: **Free**.

Expect the image build to take a few minutes and the first request after an idle period to take
about a minute. Both processes run, so the sidebar reads `API · live`.

---

## 9. Updating a deployment

| Host | How to update |
|---|---|
| Streamlit Community Cloud | `git push` — it redeploys automatically |
| Render | `git push` — it redeploys automatically |
| Hugging Face Space | `scripts/deploy_hf_space.sh <you>/chaintrace-ai <new-token>` |
| Local Docker | `docker compose up --build` |

**Rebuild, do not restart.** The code lives *inside* the Docker image; only `./data` is bind-mounted
(`docker-compose.yml`). Restarting an old container after editing `app/dashboard.py` serves the old
dashboard, which has cost this project an afternoon of debugging a "missing" feature that was right
there in the source. If a change does not appear, the image is stale.

---

## 10. Cost and limits

| | Streamlit Community Cloud | Render free | Hugging Face (CPU basic) |
|---|---|---|---|
| price | free | free | **PRO, about $9/month** |
| RAM / CPU | 1 GB | 512 MB / 0.1 CPU | 16 GB / 2 vCPU |
| disk | ephemeral | ephemeral | 50 GB, not persistent |
| sleeps? | after ~12 h idle | after 15 min idle (~1 min wake) | after long inactivity |
| custom Docker | no | yes | yes |
| both processes live | no | yes | yes |

The image needs roughly 600 MB to run (pandas + scikit-learn + Streamlit + an Isolation Forest over
~1,200 transactions), and the warm-up peak stays under about 500 MB, so the 1 GB free tier has room.

---

## 11. Troubleshooting a deployment

| Symptom | Cause | Fix |
|---|---|---|
| the console opens but every counter is 0 | the warm-up did not run or did not finish | check the app log for the `[chaintrace] warm-up complete: ...` line; the wrapper must be the main file |
| `ModuleNotFoundError: No module named 'src'` on a host | the file being run is outside the project tree | every entrypoint puts the project root on `sys.path` from its own `__file__`, so run one of the repository's own entrypoints (`app/cloud_app.py`, `app/dashboard.py`) rather than a copy of them |
| the app builds, then reports *no application is running on port 7860* (Hugging Face) | the dashboard bound the wrong port | the Space card's `app_port: 7860` must be present; `entrypoint.sh` picks 7860 when `SPACE_ID` is set |
| app over its resource limits (Streamlit Cloud) | 1 GB exceeded, usually a much larger dataset | lower `DEFAULT_TX_COUNT` / `MAX_GRAPH_NODES_FOR_EXPORT` in `src/config.py` |
| `Permission denied` writing the ledger | a host running the container as UID 1000 over a root-owned tree | the `Dockerfile` chmods the tree, and `entrypoint.sh` falls back to `/tmp/chaintrace-data` |
| the build fails at `pip install` | a transient index problem, or a builder without BuildKit | retry; if BuildKit is unavailable, drop the `--mount=type=cache` line from the `Dockerfile` |
| uploaded files vanish after a while | ephemeral disk by design | expected — see §5 |
| the sidebar reads `artefacts · filesystem` instead of `API · live` | there is no API process on this host (or it is not answering) | correct and cosmetic; the console reads the artefacts instead |

---

## 12. What a deployed copy does *not* prove

Being explicit about this is part of the deliverable:

- **The data is synthetic, and always will be.** A hosted copy proves the pipeline, the models, the
  explanations and the evidence chain work end to end — it proves nothing about real-world detection
  rates, which no synthetic dataset can.
- **The detectors are unsupervised and unlabelled by design.** The precision/recall numbers come from
  the *planted* ground truth in `data/synthetic/planted_patterns.json`, which is a measurement
  harness, not training data. Part 12 is the honest audit, including the four things it cannot show.
- **A hosted console is not an operational system.** No authentication, no multi-analyst case
  management, no chain access, no retention policy. README sections 5 and 17 say the same in more
  detail.
- **The public URL is not a security boundary.** Anyone who finds it can use the upload control and
  press Run Analysis (which rewrites the artefacts for everyone until the container resets). Keep the
  link inside the team until you are ready to show it, and take the app down afterwards.

---

Next: README section 20 is the live demo script — the order to click things in front of a judge.
Rehearsing that against the deployed URL, not a local one, is the difference between a demo and a
fumble.
