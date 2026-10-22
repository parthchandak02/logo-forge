# logo-forge

Turn one logo into dozens of styled variants. Give it a base image, optionally a line about what the design is for, and a list of styles; it locks the parts that must not change (text, letterforms, outline, layout), restyles everything else with Gemini image editing, and lays the results out in a gallery.

Single Python file, standard library only.

```bash
export GEMINI_API_KEY=...                     # or put GEMINI_API_KEY=... in ./.env
python3 forge.py --image logo.png             # 8 starter styles
python3 forge.py --image logo.png --brief "Family bakery in Lisbon since 1962" --auto 8 --review
open out/logo/index.html
```

## How it works

1. **Lock.** A vision model reads the base image and writes one sentence pinning what must survive, e.g. *"keep the interlocking 'A & B' monogram, the shield outline and the centred layout unchanged."* It also records the exact text in the design. Override with `--lock`.
2. **Styles.** From the built-in library, your own JSON, free text, or `--auto N`, which asks the model for N directions tailored to the image and your `--brief`. Each style varies one axis only (palette, material, finish, theme or format); mixing axes in one prompt tends to produce mush.
3. **Generate.** Every prompt is `lock + style + guard rails`, sent with the base image (plus any `--ref` images). Runs in parallel, retries, and falls back to cheaper models if one is unavailable. The aspect ratio matches the base image unless a style needs its own (patterns are 1:1, product shots 3:2).
4. **Review** (`--review`). A vision model compares each result with the original and scores it 1-5 for fidelity, flagging misspelled text or a changed outline, so you know which ones to regenerate.
5. **Gallery.** `out/<name>/index.html` shows every variant with its score and prompt. `manifest.json` records model, latency, cost and review per image. Re-running skips finished styles, so you can add styles and run again.

## Styles

```bash
python3 forge.py --list-styles          # 30 built-ins grouped by axis; * marks the starter set
python3 forge.py --list-styles format
```

`--styles` takes a comma list mixing any of:

| Form | Example |
|---|---|
| Library slug | `letterpress,neon-sign` |
| Whole axis | `axis:palette` |
| Your style file | `my-styles.json` (array of `{slug, title, axis, prompt, aspect?}`) |
| Free text | `"pixel art with an 8-bit palette"` |

`--auto N` saves its ideas to `out/<name>/styles.suggested.json`. Edit that file and pass it back with `--styles` to iterate. Themes come out far more personal when `--brief` contains concrete nouns (places, hobbies, dates, objects) than when it is generic.

## Job files

For anything you will rerun, put the settings in a JSON file. Paths are relative to the file, and CLI flags override it:

```json
{
  "image": "logo.png",
  "refs": ["palette-swatch.png"],
  "brief": "Neighbourhood coffee roaster; bike delivery, rainy mornings",
  "styles": ["mono-minimal", "letterpress", "axis:format", "hand-painted on a ceramic mug"],
  "auto": 6,
  "review": true,
  "out": "out"
}
```

```bash
python3 forge.py jobs/coffee/job.json
python3 forge.py jobs/coffee/job.json --only mug neon --force   # redo two
```

The `jobs/` folder is git-ignored; keep your own logos and outputs there. See [`examples/job.json`](examples/job.json).

## Options

| Flag | Default | |
|---|---|---|
| `--image` | | Base image (png, jpg, webp) |
| `--ref` | | Extra reference image, repeatable |
| `--brief` | | Context for the lock and `--auto` themes |
| `--lock` | derived | Sentence describing what must not change |
| `--styles` | starter set | See above |
| `--auto N` | 0 | Model-suggested styles |
| `--only` | | Run only slugs containing any of these words |
| `--variants N` | 1 | Images per style |
| `--model` | `gemini-3-pro-image` | Image model; `gemini-nano-banana-2.1` is about half the price |
| `--text-model` | `gemini-flash-latest` | For lock, suggestions and review |
| `--aspect` | `auto` | Or `1:1`, `3:4`, `16:9`, ... |
| `--size` | `2K` | `1K`, `2K`, `4K` |
| `--review` | off | Fidelity score per image |
| `--raw` | off | Send style prompts verbatim, no lock |
| `--force` | off | Regenerate finished styles |
| `--dry-run` | | Print prompts and estimated cost, generate nothing |
| `--gallery` | | Rebuild the gallery from the manifest |
| `--env-file` | `./.env` | File containing `GEMINI_API_KEY=` |

## Cost

Roughly $0.13 per image on `gemini-3-pro-image` (best at keeping lettering intact), about $0.07 on `gemini-nano-banana-2.1`. Analysis, suggestions and reviews use a flash text model and cost fractions of a cent. `--dry-run` prints the estimate first. Generated images carry Google's SynthID watermark.

## Tips

- Short in-image text (dates, 2-4 word captions) renders cleanly; longer strings drift.
- If a style keeps redrawing the letters, use `--review` to find it, then rerun just that one with `--only <slug> --force`, or tighten the wording with `--lock`.
- A clean, high-contrast base image (flat logo on white) gives the most faithful results.

## License

MIT
