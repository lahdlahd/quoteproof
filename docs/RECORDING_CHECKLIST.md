# Recording checklist

## Before recording (15 minutes)
- [ ] Server healthy: `systemctl is-active qp-sandboxd qp-worker qp-web caddy` → four × `active`.
- [ ] Quick self-check: `python3 -I /opt/qp/app/tools/m2_acceptance.py | tail -2` → `ALL CHECKS PASSED`.
- [ ] Sample files on the laptop in one folder: `ngn_quote.csv`, `ngn_quote.pdf`, `usd_quote.xlsx`, `poisoned_quote.xlsx`
      (from the repository's `fixtures/` folder).
- [ ] Browser: one window, 1920×1080, zoom 110–125 %, bookmarks bar hidden, no other tabs with personal data.
- [ ] Terminal: SSH session open as root, font 16–18 pt, `clear` before scene 9; hostile command ready to paste.
- [ ] **No secrets on screen:** no Vultr console API key page, no `/etc/qp-secrets`, no other terminals behind the
      browser (an `sk-…` key was visible behind the browser in an earlier screenshot — close that window and rotate
      that key if it was real).
- [ ] Notifications off (Windows Focus Assist / Do Not Disturb), phone silent.
- [ ] Recorder ready: OBS Studio, or Windows **Win + Alt + R** (Xbox Game Bar), or Clipchamp screen recorder.
- [ ] Microphone test: 10-second sample, no echo, consistent distance.
- [ ] Script printed or on a second screen.

## During recording
- [ ] One scene per clip (easier to redo).
- [ ] Move the mouse slowly; pause 1–2 s on each evidence row before moving on.
- [ ] Let real waiting happen, then trim it in editing (don't fake speed).
- [ ] If an AI retry happens on camera, keep it and narrate it.

## After recording
- [ ] Cut to ≤ 5:00 (target 4:30).
- [ ] Add captions from `VIDEO_STORYBOARD.md`; zoom-ins on: runtime=runsc row, ✓ Matches source badge,
      injection findings, hostile JSON `attempts`, containment table.
- [ ] Add the honest caption on scene 9: *"Written by us to act like a compromised model — sent through the AI-code path."*
- [ ] Export 1080p MP4 (H.264), check audio levels, watch it once start to finish.
- [ ] Upload (YouTube unlisted or as lablab requires) and test the link in a private window.
