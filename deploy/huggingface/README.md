---
title: ChainTrace AI
emoji: 🔎
colorFrom: indigo
colorTo: red
sdk: docker
app_port: 7860
pinned: false
short_description: Offline ML triage for Bitcoin + network metadata (SIH26146)
---

# ChainTrace AI — live prototype

Offline investigative console for **SIH 2026 · problem statement SIH26146 · NTRO · Cryptocurrency**.

It takes two piles of evidence nobody has time to cross-reference by hand — raw Bitcoin
**transaction** records and raw **computer-network** metadata — lines them up in time, builds one
graph out of both, runs **four machine-learning analyses** over it, and turns every finding into a
ranked, plain-English, SHA-256-stamped alert an investigator can act on.

**No cloud model, no external API, not even at runtime:** synthetic data with planted patterns,
locally trained models, an offline GeoIP table. After the image build the container never touches
the network again.

> This Space runs the **same Docker image** as the repository, so the build you are looking at
> generated the dataset, trained the models and ran a full pipeline pass. Both the Streamlit
> console (proxied here on 7860) and the FastAPI backend (internal on 8000) are live.

## Read this first

Three tabs, in the order an investigation actually moves:

1. **Overview & Ranked Alerts** — the counters, the ranked alert table, and for whichever alert you
   select: the plain-English reasons, the per-detector contribution bars, and the counterfactual —
   *what evidence would have to fall away to clear this entity*.
2. **Interactive Graph** — click any node to put it in focus. That node and its direct connections
   stay lit while the rest fades; click a lit neighbour to walk outward one hop. Once a wallet is in
   focus you get its own **timeline**, where a seek bar you drag by hand unrolls the collection
   window hop by hop (there is no auto-play on purpose).
3. **Evidence & Integrity** — the full evidence package for an alert with its SHA-256 hash, live
   re-verification, the hash-chained ledger including a tamper demo, and the one-click case-file
   export.

## What to look at if you have thirty seconds

- Screen 1 → the **highest-risk alert** → read the reasons and the recommended action.
- Screen 2 → click the largest node → **💸 Who sent what to whom**.
- Screen 4 → **re-verify an alert**, then run the **tamper demo** and watch the ledger reject it.

## Source, docs and the honest limitations

Full source, the twelve-part written documentation, the dataset audit and the measured results:
**__GITHUB_REPO_URL__**

Two things worth knowing about this hosted copy:

- Only the dashboard port is proxied, so the API is live *inside* the container and the console
  reads it there. The interactive API docs (`/docs`) are only reachable in a local run
  (`docker compose up --build` → http://localhost:8000/docs).
- The Space's disk is ephemeral: the artefacts and models are baked into the image, so the console
  is never empty, but anything written at runtime (an appended ledger line from the tamper demo, an
  uploaded file) resets when the Space rebuilds.
