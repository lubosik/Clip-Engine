# assets/fitness/

This directory holds the visual and audio assets for the **fitness** campaign.
These files are referenced by `campaigns/fitness.yaml` and are NOT committed to git
(they contain brand/media files).

Place the following files here before running the producer:

| File | Required | Description |
|------|----------|-------------|
| `logo.png` | Yes | Centered semi-transparent watermark. Recommended: white on transparent PNG, ~500px wide. |
| `logo_circle.png` | Yes | Corner badge (top-right). Recommended: circular PNG, ~200×200px. |
| `outro.mov` | Yes (if outro.enabled = true) | Outro clip. Must be 1080×1920 (9:16) at the target fps. |
| `Montserrat-ExtraBold.ttf` | Yes | Font for captions and hook overlay. Download from Google Fonts. |

## Getting the assets

1. **Logo files**: export from Figma/Canva as transparent PNG.
2. **Outro**: render a short branded 9:16 clip (e.g. 2–3s) in any video editor.
3. **Font**: download Montserrat ExtraBold from https://fonts.google.com/specimen/Montserrat

## Adding a new campaign

1. Duplicate `campaigns/fitness.yaml` → `campaigns/<your_niche>.yaml`
2. Create `assets/<your_niche>/` with the same file structure
3. Update all paths in the YAML to point to `assets/<your_niche>/...`
4. Run: `python -m producer.run <your_niche>`

Or use the Campaign Wizard in the web UI (uploads assets automatically).
