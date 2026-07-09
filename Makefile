# Clip Engine — Makefile
# All targets use the project's .venv Python and tools.

PYTHON  := .venv/bin/python
PYTEST  := .venv/bin/pytest
MODAL   := .venv/bin/modal

.PHONY: healthcheck smoke demo test deploy-modal upload-hero

# ── System readiness check ──────────────────────────────────────────────────
# Verifies Postgres, R2 (put/get/delete), Apify, Postiz, and Modal (token +
# deployed function).  Prints aligned PASS/FAIL table; exits 1 on any FAIL.
healthcheck:
	$(PYTHON) scripts/healthcheck.py

# ── End-to-end smoke test ───────────────────────────────────────────────────
# Downloads one known-good YouTube URL (override with YOUTUBE_URL=...), renders
# one ~19-second clip via the configured backend, inserts a Clip row, and
# prints where to view it.  Completes in under 2 minutes.
smoke:
	$(PYTHON) scripts/smoke.py

# ── Full demo-mode pipeline ─────────────────────────────────────────────────
# Runs the fitness campaign end-to-end in demo mode, capped at $2 Apify and
# $2 Modal spend.  Real clips land in R2; queue shows them tagged 'demo'.
demo:
	$(PYTHON) -m producer.run fitness --mode demo --max-apify-spend 2 --max-modal-spend 2

# ── Test suite ───────────────────────────────────────────────────────────────
test:
	$(PYTEST) -q

# ── Deploy Modal GPU worker ─────────────────────────────────────────────────
# Deploys render/modal_app.py to the 'lubosi' workspace.
# Requires Modal credentials in env or ~/.modal.toml.
deploy-modal:
	$(MODAL) deploy render/modal_app.py

# ── Upload hero assets to R2 ────────────────────────────────────────────────
# Uploads assets/hero/{hero_loop.mp4, hero_loop_vertical.mp4,
# hero_poster_web.jpg, hero_poster_mobile.jpg} to R2 under hero/...
# Required for the login page hero video to load from R2.
upload-hero:
	$(PYTHON) scripts/upload_hero.py
