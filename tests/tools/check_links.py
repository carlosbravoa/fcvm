#!/usr/bin/env python3
"""Check the Markdown docs: every relative link points at an existing file,
and every #anchor at an existing heading (GitHub's slug rules)."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CODE = re.compile(r"```.*?```", re.S)


def slug(heading):
    h = re.sub(r"`", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", h).replace(" ", "-")


def main():
    os.chdir(ROOT)
    files = [f for f in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md") if os.path.exists(f)]
    files += sorted(os.path.join("docs", f) for f in os.listdir("docs") if f.endswith(".md"))
    text = {os.path.normpath(f): CODE.sub("", open(f).read()) for f in files}
    anchors = {f: {slug(h) for h in re.findall(r"^#+ (.+)$", t, re.M)} for f, t in text.items()}
    bad = 0
    for f, t in text.items():
        for link in re.findall(r"\]\(([^)\s]+)\)", t):
            if re.match(r"[a-z]+:", link):
                continue
            path, _, frag = link.partition("#")
            target = os.path.normpath(os.path.join(os.path.dirname(f), path)) if path else f
            if not os.path.exists(target):
                print(f"{f}: broken link {link}")
                bad += 1
            elif frag and target.endswith(".md") and frag not in anchors.get(target, set()):
                print(f"{f}: no heading for {link}")
                bad += 1
    print(f"docs: {len(files)} files, {bad} broken link(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
