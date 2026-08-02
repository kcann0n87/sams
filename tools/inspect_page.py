"""List the interactive elements in a saved page, as selectors you can paste.

    .venv/bin/python tools/inspect_page.py screenshots/walmart-error.html
    .venv/bin/python tools/inspect_page.py                # newest walmart-*.html

The browser flow saves the page's HTML next to each screenshot. A screenshot
shows you which screen you're on; this shows you what to put in config.yaml for
it. Output is short enough to read or paste into a conversation, which the raw
HTML is not.

Stdlib only — no bs4, nothing to install.
"""
import pathlib
import sys
from html.parser import HTMLParser

INTERESTING = {"input", "button", "a", "select", "textarea", "form", "iframe"}
# Attributes worth turning into a selector, best first.
IDENT = ("id", "name", "data-automation-id", "data-testid", "aria-label", "type")


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, dict[str, str], str]] = []
        self._open: list[int] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in INTERESTING:
            self.found.append((tag, {k: (v or "") for k, v in attrs}, ""))
            self._open.append(len(self.found) - 1)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in INTERESTING and self._open:
            self._open.pop()

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        text = " ".join(data.split())
        if text and self._open:
            index = self._open[-1]
            tag, attrs, existing = self.found[index]
            # Cap it: some buttons wrap a whole paragraph of legal text.
            self.found[index] = (tag, attrs, (existing + " " + text).strip()[:60])


def selector_for(tag: str, attrs: dict[str, str]) -> str:
    """The most specific stable selector for this element."""
    if attrs.get("id"):
        return f"#{attrs['id']}"
    for key in IDENT[1:]:
        value = attrs.get(key)
        if value:
            return f"{tag}[{key}='{value}']"
    return tag


def describe(tag: str, attrs: dict[str, str], text: str) -> str:
    bits = []
    for key in ("type", "placeholder", "aria-label", "value", "href"):
        value = attrs.get(key)
        if value:
            bits.append(f"{key}={value[:40]!r}")
    if text:
        bits.append(f"text={text!r}")
    if attrs.get("hidden") is not None or attrs.get("type") == "hidden":
        bits.append("HIDDEN")
    return "  ".join(bits)


def main() -> int:
    if len(sys.argv) > 1:
        path = pathlib.Path(sys.argv[1])
    else:
        pages = sorted(
            pathlib.Path("screenshots").glob("walmart-*.html"),
            key=lambda p: p.stat().st_mtime,
        )
        if not pages:
            print("No saved pages in screenshots/. Run the flow once first.")
            return 1
        path = pages[-1]
        print(f"(newest: {path})\n")
    if not path.exists():
        print(f"No such file: {path}")
        return 1

    collector = Collector()
    collector.feed(path.read_text(encoding="utf-8", errors="replace"))
    print(f"title: {collector.title!r}")
    print(f"bytes: {path.stat().st_size:,}\n")

    groups: dict[str, list[str]] = {}
    for tag, attrs, text in collector.found:
        if tag == "input" and attrs.get("type") == "hidden":
            continue  # never fillable, and there are dozens
        if tag == "a" and not text:
            continue  # icon links, no way to identify them by eye
        line = f"  {selector_for(tag, attrs):<46} {describe(tag, attrs, text)}"
        groups.setdefault(tag, []).append(line.rstrip())

    for tag in ("form", "input", "select", "textarea", "button", "a", "iframe"):
        lines = groups.get(tag)
        if not lines:
            continue
        print(f"{tag} ({len(lines)}):")
        # Long pages have hundreds of links; the head of the list is the useful
        # part and the rest is footer navigation.
        for line in lines[:40]:
            print(line)
        if len(lines) > 40:
            print(f"  ... and {len(lines) - 40} more")
        print()

    if not collector.found:
        print("No interactive elements at all — this is a block page or an\n"
              "empty shell, not the sign-in form.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
