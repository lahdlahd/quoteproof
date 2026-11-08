# Final submission checklist — deadline: Sunday 8 November 2026, 17:00

Confirm on lablab.ai whether 17:00 is **WAT (Lagos)** or another time zone. Aim to submit by **14:00 WAT**.

## Required by the challenge
- [ ] **Vultr Serverless Inference** for all agent LLM calls — ✅ implemented (qp-worker → api.vultrinference.com).
- [ ] **Sandbox:** process isolation, no secrets inside, time and memory caps, destroyed after each task — ✅ (M1 28/28, M2 26/26).
- [ ] **At least one containment moment in the video** — scenes 9 and 10 of the storyboard.
- [ ] **Vultr VM backend** — ✅ Cloud Compute, London, Ubuntu 24.04.
- [ ] **Public URL** — ✅ https://95-179-234-10.sslip.io (check it loads in a private window on the day).
- [ ] **Public GitHub repository** with a README that explains the architecture — ⏳ awaiting approval to publish.
- [ ] **Video** — ⏳ record using `VIDEO_SCRIPT.md` + `VIDEO_STORYBOARD.md`.
- [ ] **Slides** — ✅ `QuoteProof_BlastRadiusZero.pptx` (add VM screenshots, then export a PDF copy too).
- [ ] **Cover image** — ⏳ (slide 1 exported as PNG works as a cover).

## Submission form fields (prepare the text in advance)
- [ ] Project title: **QuoteProof — Blast Radius Zero for procurement**
- [ ] Short description (one line): *AI-written extractors run in a gVisor sandbox on Vultr; trusted code verifies every number; a person decides.*
- [ ] Track: **Challenge 01 — Blast Radius Zero (Pattern A: Sandboxed Code Execution)**
- [ ] Technologies: Vultr Cloud Compute, Vultr Serverless Inference (deepseek-v4.1-flash-1017, glm-5.3-flash), Docker + gVisor (runsc), Python, FastAPI, SQLite, Caddy.
- [ ] Links: live app, containment panel, GitHub, video, slides.
- [ ] Team members listed.

## Before the deadline
- [ ] Final acceptance run on the VM: `ALL CHECKS PASSED`.
- [ ] VM left running until judging ends (keep the Vultr balance positive; do not destroy the instance).
- [ ] Rotate the Vultr inference key **after** judging (it was only ever stored on the VM, but rotating is good practice).
- [ ] Delete the unused duplicate inference subscription ("quoteprooff") if not done already.
