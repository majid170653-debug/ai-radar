"""رادار هوش مصنوعی: خبرهای ۴۸ ساعت اخیر را جمع و دسته‌بندی می‌کند.

فقط از کتابخانه‌های استاندارد پایتون استفاده می‌کند.
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
PROMPT_FILE = ROOT / "prompts" / "sort.md"
OUTPUT_FILE = ROOT / "data" / "latest.json"
ENV_FILE = ROOT / ".env"

CATEGORIES = ("important", "practical", "must_read")
MAX_PER_CATEGORY = 8
WINDOW_HOURS = 48

MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"

USER_AGENT = "ai-radar/0.1 (+rss reader)"

KEYWORDS = {
    "practical": [
        "how to", "tutorial", "guide", "open source", "open-source", "library",
        "sdk", "api", "tool", "cli", "plugin", "release", "fine-tun", "prompt",
        "workflow", "github", "python", "notebook", "deploy", "inference",
        "quantiz", "benchmark", "dataset",
    ],
    "must_read": [
        "analysis", "deep dive", "essay", "opinion", "interview", "explainer",
        "explained", "why ", "what it means", "research", "paper", "study",
        "lessons", "the case for", "inside ",
    ],
    "important": [
        "launch", "announce", "unveil", "introduc", "acquire", "acquisition",
        "funding", "raises", "billion", "valuation", "lawsuit", "sues", "court",
        "regulat", "law", "ban", "ceo", "partnership", "gpt", "gemini", "claude",
        "llama", "model",
    ],
}


# ---------- خواندن خوراک‌ها ----------

def _local(tag):
    """اسم تگ بدون namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(elem, *names):
    for child in elem:
        if _local(child.tag) in names:
            return child
    return None


def _text(elem, *names):
    child = _child(elem, *names)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def clean_text(raw, limit=300):
    """حذف HTML و فاصله‌های اضافه، و کوتاه کردن متن."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r" ([.,;:!?])", r"\1", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def parse_date(value):
    """تاریخ RFC 822 (RSS) یا ISO 8601 (Atom) را به datetime با منطقه‌ی زمانی تبدیل می‌کند."""
    value = (value or "").strip()
    if not value:
        return None
    dt = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        iso = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _atom_link(entry):
    fallback = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.get("href", "").strip()
        rel = child.get("rel", "alternate")
        if rel == "alternate" and href:
            return href
        if href and not fallback:
            fallback = href
    return fallback


def parse_feed(data, source):
    """یک خوراک RSS یا Atom را می‌خواند و فهرست خبرها را برمی‌گرداند."""
    root = ET.fromstring(data)
    items = []
    for elem in root.iter():
        kind = _local(elem.tag)
        if kind == "item":  # RSS 2.0 / RSS 1.0
            title = _text(elem, "title")
            link = _text(elem, "link") or _text(elem, "guid")
            date = parse_date(_text(elem, "pubDate") or _text(elem, "date"))
            desc = _text(elem, "description") or _text(elem, "encoded")
        elif kind == "entry":  # Atom
            title = _text(elem, "title")
            link = _atom_link(elem)
            date = parse_date(_text(elem, "published") or _text(elem, "updated"))
            desc = _text(elem, "summary") or _text(elem, "content")
        else:
            continue
        if not title or not link:
            continue
        items.append({
            "title": clean_text(title, limit=500),
            "link": link,
            "source": source,
            "published": date,
            "description": clean_text(desc),
        })
    return items


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def collect(sources):
    """همه‌ی خوراک‌ها را می‌گیرد؛ خطای هر خوراک گزارش می‌شود و کار ادامه پیدا می‌کند."""
    items, errors = [], []
    for src in sources:
        name, url = src["name"], src["url"]
        try:
            feed_items = parse_feed(fetch(url), name)
            print(f"  {name}: {len(feed_items)} خبر", file=sys.stderr)
            items.extend(feed_items)
        except (urllib.error.URLError, OSError, ET.ParseError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            print(f"  خطا در {name}: {exc}", file=sys.stderr)
    return items, errors


def filter_recent(items, now=None, hours=WINDOW_HOURS):
    """فقط خبرهای چند ساعت اخیر را نگه می‌دارد، بدون تکرار لینک، جدیدترین اول."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    seen, recent = set(), []
    for item in items:
        date = item.get("published")
        if date is None or date < cutoff or date > now + timedelta(hours=1):
            continue
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        recent.append(item)
    recent.sort(key=lambda i: i["published"], reverse=True)
    return recent


# ---------- دسته‌بندی کلیدواژه‌ای ----------

def keyword_category(item):
    text = f" {item['title']} {item.get('description', '')} ".lower()
    scores = {cat: sum(1 for kw in KEYWORDS[cat] if kw in text) for cat in CATEGORIES}
    # عنوان وزن بیشتری دارد
    title = f" {item['title']} ".lower()
    for cat in CATEGORIES:
        scores[cat] += sum(1 for kw in KEYWORDS[cat] if kw in title)
    best = max(CATEGORIES, key=lambda c: (scores[c], c == "important"))
    return best if scores[best] > 0 else "important"


def categorize_keywords(items):
    result = {cat: [] for cat in CATEGORIES}
    for item in items:
        entry = dict(item, summary=item.get("description") or item["title"])
        result[keyword_category(item)].append(entry)
    return result


# ---------- دسته‌بندی با Claude ----------

def load_api_key(env_file=ENV_FILE):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    try:
        lines = Path(env_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        name, sep, value = line.partition("=")
        if sep and name.strip() == "ANTHROPIC_API_KEY":
            return value.strip().strip('"').strip("'")
    return ""


def extract_json(text):
    """اولین شیء JSON داخل متن پاسخ را بیرون می‌کشد."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("پاسخ مدل JSON نداشت")
    return json.loads(text[start:end + 1])


def apply_model_result(items, parsed):
    result = {cat: [] for cat in CATEGORIES}
    by_id = {}
    for row in parsed.get("items", []):
        if isinstance(row, dict) and isinstance(row.get("id"), int):
            by_id[row["id"]] = row
    for idx, item in enumerate(items):
        row = by_id.get(idx, {})
        cat = row.get("category")
        if cat not in CATEGORIES:
            cat = keyword_category(item)
        summary = str(row.get("summary") or "").strip() or item.get("description") or item["title"]
        result[cat].append(dict(item, summary=summary))
    return result


def categorize_with_claude(items, api_key):
    system = PROMPT_FILE.read_text(encoding="utf-8")
    payload_items = [
        {"id": i, "title": it["title"], "source": it["source"], "description": it["description"]}
        for i, it in enumerate(items)
    ]
    body = {
        "model": MODEL,
        "max_tokens": 16000,
        "system": system,
        "output_config": {"effort": "low"},
        "messages": [{
            "role": "user",
            "content": json.dumps(payload_items, ensure_ascii=False),
        }],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "user-agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        response = json.loads(resp.read().decode("utf-8"))
    stop = response.get("stop_reason")
    if stop == "refusal":
        raise ValueError("مدل درخواست را رد کرد")
    if stop == "max_tokens":
        raise ValueError("پاسخ مدل نیمه‌تمام ماند (max_tokens)")
    text = "".join(b.get("text", "") for b in response.get("content", []) if b.get("type") == "text")
    return apply_model_result(items, extract_json(text))


# ---------- خروجی ----------

def build_output(categorized, now=None):
    now = now or datetime.now(timezone.utc)
    out = {"generated_at": now.isoformat(timespec="seconds")}
    for cat in CATEGORIES:
        rows = sorted(categorized.get(cat, []), key=lambda i: i["published"], reverse=True)
        out[cat] = [
            {
                "title": it["title"],
                "link": it["link"],
                "source": it["source"],
                "date": it["published"].isoformat(timespec="seconds"),
                "summary": it["summary"],
            }
            for it in rows[:MAX_PER_CATEGORY]
        ]
    return out


def write_output(data, path=OUTPUT_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    print("گرفتن خوراک‌ها...", file=sys.stderr)
    items, errors = collect(sources)
    recent = filter_recent(items)
    print(f"{len(recent)} خبر از {WINDOW_HOURS} ساعت اخیر پیدا شد.", file=sys.stderr)

    if not recent:
        print("خطا: هیچ خبری از ۴۸ ساعت اخیر پیدا نشد؛ فایل قبلی دست نخورد.", file=sys.stderr)
        return 1

    api_key = load_api_key()
    categorized = None
    if api_key:
        print(f"دسته‌بندی با {MODEL}...", file=sys.stderr)
        try:
            categorized = categorize_with_claude(recent, api_key)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"  خطا در تماس با مدل ({exc})؛ از کلیدواژه‌ها استفاده می‌شود.", file=sys.stderr)
    else:
        print("کلید API پیدا نشد؛ دسته‌بندی با کلیدواژه‌ها.", file=sys.stderr)
    if categorized is None:
        categorized = categorize_keywords(recent)

    output = build_output(categorized)
    write_output(output)
    counts = ", ".join(f"{cat}: {len(output[cat])}" for cat in CATEGORIES)
    print(f"نوشته شد در {OUTPUT_FILE.relative_to(ROOT)} — {counts}", file=sys.stderr)
    if errors:
        print(f"{len(errors)} خوراک خطا داد.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
