#!/usr/bin/env python3
"""logo-forge: one base image + a list of styles -> a batch of restyled variants (Gemini image editing).

  python3 forge.py --image logo.png                       # starter styles from styles.json
  python3 forge.py --image logo.png --styles neon-sign,wax-seal,my-styles.json
  python3 forge.py --image logo.png --brief "Bakery in Lisbon, family-run since 1962" --auto 8
  python3 forge.py job.json --only neon wax --review
  python3 forge.py --list-styles

Standard library only. Needs GEMINI_API_KEY (environment or a .env file). See README.md.
"""
import argparse
import base64
import html
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIBRARY = HERE / "styles.json"
API = "https://generativelanguage.googleapis.com/v1beta/models"

IMAGE_MODEL = "gemini-3-pro-image"
FALLBACK_MODELS = ["gemini-nano-banana-2.1", "gemini-3.1-flash-image", "gemini-2.5-flash-image"]
TEXT_MODEL = "gemini-flash-latest"
# Approximate USD per generated image; unknown models fall back to the cheapest tier.
PRICE = {"pro-image": 0.134, "nano-banana": 0.067, "flash-image": 0.039}
ASPECTS = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]
AXES = ["palette", "material", "finish", "theme", "format"]
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

DEFAULT_LOCK = ("Using the attached image as the exact base, keep every letter, word, shape and the overall "
                "layout and outline unchanged; change only the styling described below.")
TAIL = ("Return only the finished design. Do not add text, logos or watermarks that are not in the base image "
        "unless the style explicitly asks for them, and spell all existing text exactly as in the base image.")

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------- config

def load_key(env_file: str | None) -> str | None:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    candidates = [Path(env_file).expanduser()] if env_file else [Path.cwd() / ".env", HERE / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY=") or line.startswith("export GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def load_library() -> dict:
    return {s["slug"]: s for s in json.loads(LIBRARY.read_text())}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "style"


def resolve_styles(specs: list, base_dir: Path, library: dict) -> list:
    """Turn library slugs, style-file paths, inline dicts and free text into style dicts."""
    styles = []
    for spec in specs:
        if isinstance(spec, dict):
            styles.append(spec)
            continue
        spec = str(spec).strip()
        if not spec:
            continue
        if spec in library:
            styles.append(library[spec])
        elif spec.endswith(".json"):
            path = Path(spec) if Path(spec).is_absolute() else base_dir / spec
            if not path.is_file():
                sys.exit(f"style file not found: {path}")
            styles.extend(json.loads(path.read_text()))
        elif spec.startswith("axis:"):
            styles.extend(s for s in library.values() if s.get("axis") == spec[5:])
        elif " " in spec:  # free-text style, e.g. --styles "pixel art, 8-bit palette"
            styles.append({"slug": slugify(spec), "title": spec[:60], "prompt": spec})
        else:
            close = [s for s in library if spec in s]
            sys.exit(f"unknown style '{spec}'" + (f" (did you mean {', '.join(close)}?)" if close else
                                                   " - see --list-styles"))
    seen, out = set(), []
    for s in styles:
        s = dict(s)
        s.setdefault("slug", slugify(s.get("title") or s["prompt"]))
        if s["slug"] in seen:
            continue
        seen.add(s["slug"])
        out.append(s)
    return out


# ---------------------------------------------------------------- images

def image_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    return None


def nearest_aspect(size: tuple[int, int] | None) -> str:
    if not size:
        return "1:1"
    ratio = size[0] / size[1]

    def dist(a: str) -> float:
        w, h = map(int, a.split(":"))
        return abs(math.log(ratio) - math.log(w / h))
    return min(ASPECTS, key=dist)


class Img:
    def __init__(self, path: Path):
        if not path.is_file():
            sys.exit(f"image not found: {path}")
        self.path = path
        self.data = path.read_bytes()
        self.mime = MIME.get(path.suffix.lower(), "image/png")
        self.b64 = base64.b64encode(self.data).decode()
        self.size = image_size(self.data)

    def part(self) -> dict:
        return {"inlineData": {"mimeType": self.mime, "data": self.b64}}


# ---------------------------------------------------------------- API

class Gemini:
    def __init__(self, key: str):
        self.key = key

    def post(self, model: str, body: dict, timeout: int = 300) -> dict:
        req = urllib.request.Request(
            f"{API}/{model}:generateContent",
            data=json.dumps(body).encode(),
            headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def json_call(self, model: str, prompt: str, images: list) -> object:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}] + [i.part() for i in images]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
        }
        for attempt in range(3):
            try:
                resp = self.post(model, body, timeout=120)
                parts = resp["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
                return json.loads(text)
            except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as e:
                err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{model} JSON call failed: {err}")


def analyze(gem: Gemini, model: str, base: Img, brief: str) -> dict:
    prompt = (
        "You are preparing an image-to-image restyling job for the attached design (a logo, mark or illustration). "
        "Return JSON with keys:\n"
        '  "subject": one sentence describing what the design is,\n'
        '  "text": array of every exact text string visible in the design (empty if none),\n'
        '  "lock": ONE sentence beginning "Using the attached image as the exact base," that names the specific '
        "elements that must not change: the exact text and letterforms, the outer shape or outline, and the layout. "
        "Be concrete (e.g. 'keep the \"A & B\" monogram and the shield outline unchanged').\n"
        + (f"Context from the owner: {brief}\n" if brief else "")
    )
    return gem.json_call(model, prompt, [base])


def suggest(gem: Gemini, model: str, base: Img, brief: str, n: int, avoid: list) -> list:
    prompt = (
        f"Propose {n} distinct restyling directions for the attached design. Each direction must vary exactly ONE axis:\n"
        "  palette (colours only), material (the surface it is made from or printed on), finish (illustration or "
        "print technique), theme (a story element woven into the art, drawn from the owner's context), or format "
        "(the real object it appears on, as a product shot).\n"
        "Mix the axes. If the owner gives specific context, at least a third of the directions should be 'theme' "
        "items built from concrete nouns in that context; make them feel personal, not generic.\n"
        "Never describe changing the design's text, letterforms, outline or layout; that part is locked separately.\n"
        + (f"Owner's context: {brief}\n" if brief else "")
        + (f"Avoid these existing directions: {', '.join(avoid)}\n" if avoid else "")
        + 'Return a JSON array of objects: {"slug": short-kebab-case, "title": 2-5 words, "axis": one of '
        + f"{AXES}, \"prompt\": 1-3 vivid sentences describing only the new styling, "
        + '"aspect": optional, only for format items that need a different shape (e.g. "1:1" for patterns, '
        + '"3:2" for product shots)}.'
    )
    raw = gem.json_call(model, prompt, [base])
    items = raw if isinstance(raw, list) else raw.get("styles", [])
    out = []
    for s in items[:n]:
        if not isinstance(s, dict) or not s.get("prompt"):
            continue
        s["slug"] = "auto-" + slugify(s.get("slug") or s.get("title") or s["prompt"])
        s["auto"] = True
        if s.get("aspect") not in ASPECTS:
            s.pop("aspect", None)
        out.append(s)
    return out


def review(gem: Gemini, model: str, base: Img, out_path: Path, expected_text: list) -> dict:
    prompt = (
        "Image 1 is an original design; image 2 is a restyled version that was supposed to keep the text, "
        "letterforms, outline and layout of image 1 while changing only the styling. "
        + (f"The exact text that must appear is: {json.dumps(expected_text)}. " if expected_text else "")
        + 'Return JSON {"score": 1-5 (5 = structure perfectly preserved, 1 = unrecognisable or text wrong), '
        '"issues": short string listing misspellings, missing or extra elements, or "" if none}.'
    )
    return gem.json_call(model, prompt, [base, Img(out_path)])


def price(model: str) -> float:
    return next((v for k, v in PRICE.items() if k in model), min(PRICE.values()))


# ---------------------------------------------------------------- generation

def compose(style: dict, lock: str, raw: bool) -> str:
    if raw or style.get("raw"):
        return style["prompt"]
    return f"{lock}\n\nNew style: {style['prompt']}\n\n{TAIL}"


def generate(gem: Gemini, job: dict, task: dict, images: list, out: Path) -> dict:
    rec = {"id": task["id"], "slug": task["slug"], "title": task.get("title", ""), "axis": task.get("axis", ""),
           "prompt": task["full_prompt"], "aspect": task["aspect"], "status": "error", "files": []}
    models = [job["model"]] + [m for m in FALLBACK_MODELS if m != job["model"]]
    for attempt in range(1, job["retries"] + 1):
        model = models[min(attempt - 1, len(models) - 1)]
        body = {
            "contents": [{"role": "user", "parts": [{"text": task["full_prompt"]}] + [i.part() for i in images]}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": {"aspectRatio": task["aspect"], "imageSize": job["size"]}},
        }
        t0 = time.time()
        try:
            resp = gem.post(model, body)
        except urllib.error.HTTPError as e:
            rec["error"] = f"{e.code} {e.read().decode()[:300]}"
            if e.code not in (400, 404):  # 400/404 = model unavailable or bad config -> next model now
                time.sleep(3 * attempt)
            continue
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
            time.sleep(3 * attempt)
            continue
        parts = (resp.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        imgs = [p["inlineData"] for p in parts if p.get("inlineData")]
        if not imgs:
            reason = (resp.get("candidates") or [{}])[0].get("finishReason", "")
            rec["error"] = f"no image in response {reason} " + json.dumps(resp)[:200]
            time.sleep(3 * attempt)
            continue
        for i, img in enumerate(imgs, 1):
            ext = "png" if "png" in img.get("mimeType", "") else "jpg"
            name = f"{task['id']}.{ext}" if len(imgs) == 1 else f"{task['id']}-{i}.{ext}"
            (out / name).write_bytes(base64.b64decode(img["data"]))
            rec["files"].append(name)
        rec.update(status="ok", model=resp.get("modelVersion", model), latency_s=round(time.time() - t0, 1),
                   cost_usd=price(model))
        rec.pop("error", None)
        if job["review"]:
            try:
                rec["review"] = review(gem, job["text_model"], images[0], out / rec["files"][0], job.get("text", []))
            except Exception as e:  # noqa: BLE001
                rec["review"] = {"score": None, "issues": f"review failed: {e}"}
        score = rec.get("review", {}).get("score")
        log(f"[ok]   {task['id']:<32} {rec['model']:<24} {rec['latency_s']:>5}s"
            + (f"  fidelity {score}/5" if score else ""))
        return rec
    log(f"[FAIL] {task['id']:<32} {rec.get('error', '')[:160]}")
    return rec


# ---------------------------------------------------------------- gallery

def write_gallery(out: Path, manifest: dict, job: dict) -> Path:
    cards = []
    for rec in manifest["results"]:
        if rec["status"] != "ok":
            continue
        rv = rec.get("review") or {}
        badge = f'<span class="score s{rv["score"]}">{rv["score"]}/5</span>' if rv.get("score") else ""
        for f in rec["files"]:
            cards.append(
                f'<figure><a href="{html.escape(f)}"><img loading="lazy" src="{html.escape(f)}" alt=""></a>'
                f'<figcaption><b>{html.escape(rec.get("title") or rec["slug"])}</b>{badge}'
                f'<small>{html.escape(rec.get("axis", ""))} · {html.escape(rec["id"])}</small>'
                + (f'<em>{html.escape(rv["issues"])}</em>' if rv.get("issues") else "")
                + f'<details><summary>prompt</summary><p>{html.escape(rec.get("prompt", ""))}</p></details>'
                f'</figcaption></figure>')
    base = manifest.get("base_copy", "")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>logo-forge gallery</title>
<style>
:root{{--bg:#f6f5f2;--fg:#1d1d1b;--mut:#6b6a65;--card:#fff;--line:#e3e1db}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--mut:#9a9890;--card:#1f1f1d;--line:#2e2e2b}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;padding:24px 16px}}
header{{display:flex;gap:16px;align-items:center;max-width:1400px;margin:0 auto 20px}}
header img{{width:96px;height:96px;object-fit:contain;background:var(--card);border:1px solid var(--line);border-radius:8px}}
header p{{margin:4px 0;color:var(--mut)}}
main{{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));max-width:1400px;margin:0 auto}}
figure{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
figure img{{width:100%;aspect-ratio:1;object-fit:contain;display:block;background:#0001}}
figcaption{{padding:10px 12px;display:grid;gap:4px}}
small{{color:var(--mut)}} em{{color:#c2410c;font-size:13px}}
.score{{float:right;font-size:12px;padding:1px 6px;border-radius:9px;background:#16a34a22;color:#15803d}}
.s1,.s2{{background:#dc262622;color:#b91c1c}} .s3{{background:#ca8a0422;color:#a16207}}
details p{{font-size:13px;color:var(--mut);margin:6px 0 0}}
</style></head><body><header>{f'<img src="{html.escape(base)}" alt="base">' if base else ""}
<div><b>{len(cards)} variants</b><p>{html.escape(manifest.get("subject", ""))}</p>
<p><small>{html.escape(job["lock"])}</small></p></div></header><main>{"".join(cards)}</main></body></html>"""
    path = out / "index.html"
    path.write_text(page)
    return path


# ---------------------------------------------------------------- main

def build_job(args) -> tuple[dict, Path]:
    job, base_dir = {}, Path.cwd()
    if args.job:
        job_path = Path(args.job)
        job = json.loads(job_path.read_text())
        base_dir = job_path.resolve().parent
    absolute = lambda p: str(Path(p).resolve()) if p else None  # noqa: E731  CLI paths are relative to cwd
    cli = {
        "image": absolute(args.image), "refs": [absolute(r) for r in args.ref] if args.ref else None, "brief": args.brief, "lock": args.lock,
        "styles": args.styles.split(",") if args.styles else None, "auto": args.auto, "model": args.model,
        "text_model": args.text_model, "aspect": args.aspect, "size": args.size, "variants": args.variants,
        "out": absolute(args.out), "workers": args.workers, "retries": args.retries,
    }
    job.update({k: v for k, v in cli.items() if v is not None})
    job["review"] = args.review or job.get("review", False)
    job["raw"] = args.raw or job.get("compose") is False or job.get("raw", False)
    defaults = {"refs": [], "brief": "", "lock": "", "auto": 0, "model": IMAGE_MODEL, "text_model": TEXT_MODEL,
                "aspect": "auto", "size": "2K", "variants": 1, "workers": 3, "retries": 3}
    for k, v in defaults.items():
        job.setdefault(k, v)
    if not job.get("image"):
        sys.exit("no base image: pass --image or set \"image\" in the job file")
    if isinstance(job.get("styles"), str):
        job["styles"] = [job["styles"]]
    if "out" not in job:
        job["out"] = "out" if args.job else f"out/{Path(job['image']).stem}"
    return job, base_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate styled variants of a logo or design with Gemini.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("job", nargs="?", help="job JSON file (CLI flags override its values)")
    ap.add_argument("--image", help="base image (png/jpg/webp)")
    ap.add_argument("--ref", action="append", help="extra reference image, repeatable (e.g. a texture or palette)")
    ap.add_argument("--brief", help="what the design is / who it is for; feeds the lock and --auto themes")
    ap.add_argument("--lock", help="sentence that pins what must not change (default: derived from the image)")
    ap.add_argument("--styles", help="comma list: library slugs, axis:<name>, style .json files, or free text")
    ap.add_argument("--auto", type=int, help="ask the model to propose N styles tailored to the image + brief")
    ap.add_argument("--only", nargs="+", help="run only styles whose slug contains any of these")
    ap.add_argument("--variants", type=int, help="images per style (default 1)")
    ap.add_argument("--model", help=f"image model (default {IMAGE_MODEL})")
    ap.add_argument("--text-model", help=f"model for analysis/suggestions/review (default {TEXT_MODEL})")
    ap.add_argument("--aspect", help="auto (match base image) or one of " + ", ".join(ASPECTS))
    ap.add_argument("--size", help="1K, 2K or 4K (default 2K)")
    ap.add_argument("--out", help="output directory")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--retries", type=int)
    ap.add_argument("--review", action="store_true", help="score each result for fidelity to the base image")
    ap.add_argument("--raw", action="store_true", help="send style prompts verbatim (no lock / tail)")
    ap.add_argument("--force", action="store_true", help="regenerate styles that already succeeded")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and prompts; generate nothing")
    ap.add_argument("--no-analyze", action="store_true", help="skip image analysis; use the generic lock")
    ap.add_argument("--gallery", action="store_true", help="only rebuild out/index.html from the manifest")
    ap.add_argument("--list-styles", nargs="?", const="", metavar="AXIS", help="show the style library")
    ap.add_argument("--env-file", help="file containing GEMINI_API_KEY=... (default ./.env)")
    args = ap.parse_args()

    library = load_library()
    if args.list_styles is not None:
        for axis in AXES:
            rows = [s for s in library.values() if s.get("axis") == axis and args.list_styles in ("", axis)]
            if rows:
                print(f"\n{axis.upper()}")
            for s in rows:
                print(f"  {s['slug']:<20} {'*' if s.get('starter') else ' '} {s['title']}")
        print("\n* = starter set (used when no --styles/--auto given)")
        return 0

    job, base_dir = build_job(args)
    resolve = lambda p: Path(p) if Path(p).is_absolute() else base_dir / p  # noqa: E731
    out = resolve(job["out"])
    out.mkdir(parents=True, exist_ok=True)
    man_path = out / "manifest.json"
    manifest = json.loads(man_path.read_text()) if man_path.exists() else {"results": []}
    if isinstance(manifest, list):  # older flat-list manifests
        manifest = {"results": [dict(r, id=r.get("id", r["slug"])) for r in manifest]}

    if args.gallery:
        job.setdefault("lock", manifest.get("lock", ""))
        print(write_gallery(out, manifest, job))
        return 0

    base = Img(resolve(job["image"]))
    images = [base] + [Img(resolve(r)) for r in job["refs"]]
    key = load_key(args.env_file)
    if not key and not args.dry_run:
        sys.exit("GEMINI_API_KEY not set (export it, or put GEMINI_API_KEY=... in ./.env or --env-file)")
    gem = Gemini(key) if key else None

    # 1. Lock sentence: explicit > cached analysis > fresh analysis > generic.
    if not job["raw"] and not job["lock"]:
        cached = manifest.get("analysis") if manifest.get("image") == base.path.name else None
        if cached:
            analysis = cached
        elif gem and not args.no_analyze:
            log(f"analysing {base.path.name} with {job['text_model']} ...")
            try:
                analysis = analyze(gem, job["text_model"], base, job["brief"])
            except Exception as e:  # noqa: BLE001
                log(f"  analysis failed ({e}); using generic lock")
                analysis = {}
        else:
            analysis = {}
        job["lock"] = analysis.get("lock") or DEFAULT_LOCK
        job["text"] = analysis.get("text", [])
        manifest["analysis"] = analysis
        manifest["subject"] = analysis.get("subject", "")
    job.setdefault("text", (manifest.get("analysis") or {}).get("text", []))

    # 2. Styles: explicit list, plus auto-suggested; starter set if neither.
    styles = resolve_styles(job.get("styles") or [], base_dir, library)
    if job["auto"]:
        if not gem:
            sys.exit("--auto needs GEMINI_API_KEY")
        log(f"asking {job['text_model']} for {job['auto']} style ideas ...")
        ideas = suggest(gem, job["text_model"], base, job["brief"], job["auto"], [s["slug"] for s in styles])
        (out / "styles.suggested.json").write_text(json.dumps(ideas, indent=2))
        log(f"  {len(ideas)} ideas saved to {out / 'styles.suggested.json'} (edit and pass back with --styles)")
        styles += ideas
    if not styles:
        styles = [s for s in library.values() if s.get("starter")]
    if args.only:
        styles = [s for s in styles if any(f in s["slug"] for f in args.only)]
    if not styles:
        sys.exit("no styles matched")

    # 3. Expand into tasks and skip what already exists.
    default_aspect = nearest_aspect(base.size) if job["aspect"] == "auto" else job["aspect"]
    done = {r["id"]: r for r in manifest["results"]}
    tasks, skipped = [], 0
    for s in styles:
        for v in range(1, job["variants"] + 1):
            tid = s["slug"] if job["variants"] == 1 else f"{s['slug']}-v{v}"
            prev = done.get(tid)
            if (not args.force and prev and prev["status"] == "ok"
                    and all((out / f).exists() for f in prev["files"])):
                skipped += 1
                continue
            tasks.append(dict(s, id=tid, aspect=s.get("aspect") or default_aspect,
                              full_prompt=compose(s, job["lock"], job["raw"])))

    est = len(tasks) * price(job["model"])
    log(f"base={base.path.name} {base.size or ''} aspect={default_aspect} size={job['size']} model={job['model']}")
    log(f"lock: {job['lock'] if not job['raw'] else '(raw prompts)'}")
    log(f"{len(tasks)} to generate, {skipped} already done (--force to redo)  ~${est:.2f}  -> {out}")
    if args.dry_run:
        for t in tasks:
            log(f"\n--- {t['id']} [{t.get('axis', '')}, {t['aspect']}]\n{t['full_prompt']}")
        return 0
    if not tasks:
        print(write_gallery(out, manifest, job))
        return 0

    with ThreadPoolExecutor(max_workers=job["workers"]) as ex:
        results = list(ex.map(lambda t: generate(gem, job, t, images, out), tasks))

    # 4. Merge into the manifest (keeping earlier runs) and rebuild the gallery.
    by_id = {r["id"]: r for r in manifest["results"]}
    by_id.update({r["id"]: r for r in results})
    base_copy = "_base" + base.path.suffix.lower()
    shutil.copyfile(base.path, out / base_copy)
    manifest.update(image=base.path.name, base_copy=base_copy, lock=job["lock"], results=list(by_id.values()))
    man_path.write_text(json.dumps(manifest, indent=2))
    gallery = write_gallery(out, manifest, job)

    ok = [r for r in results if r["status"] == "ok"]
    low = [r["id"] for r in ok if (r.get("review") or {}).get("score") and r["review"]["score"] <= 2]
    log(f"\n{len(ok)}/{len(results)} ok  ~${sum(r['cost_usd'] for r in ok):.2f}  gallery: {gallery}")
    if low:
        log(f"low fidelity, consider --only {' '.join(low)} --force: {', '.join(low)}")
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
