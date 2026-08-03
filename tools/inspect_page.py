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

INTERESTING = {"input", "button", "a", "select", "textarea", "form", "iframe", "label"}
# Anything clickable that isn't a <button>. Walmart's code-choice screen has
# input=0 and only two real buttons, so its options are built from divs and
# list items carrying a role or a test id — invisible to a tag-name scan.
ROLES = {"button", "radio", "checkbox", "link", "menuitem", "option", "tab"}
CLICKABLE_ATTRS = ("data-automation-id", "data-testid", "data-testid-id")
# Void elements have no end tag, so they must never go on the open stack —
# otherwise every following run of text gets attributed to the last <input>.
VOID = {"input", "iframe", "br", "img", "hr", "source", "area", "embed"}
# Attributes worth turning into a selector, best first.
IDENT = ("id", "name", "data-automation-id", "data-testid", "aria-label", "type")


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, dict[str, str], str]] = []
        # Every open non-void element, as (tag, index into found or None).
        # Tracking the uninteresting ones too is what keeps the nesting right:
        # a </div> closing a plain wrapper must not close the <div role=button>
        # around it, or all the following text lands on the wrong element.
        self._stack: list[tuple[str, int | None]] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        mapping = {k: (v or "") for k, v in attrs}
        interesting = tag in INTERESTING or mapping.get("role", "").lower() in ROLES
        if not interesting and tag in ("div", "span", "li"):
            # A test id on a container is how a component library labels the
            # thing you're meant to click.
            interesting = any(mapping.get(a) for a in CLICKABLE_ATTRS)

        index = None
        if interesting:
            self.found.append((tag, mapping, ""))
            index = len(self.found) - 1
        if tag not in VOID:
            self._stack.append((tag, index))

    def handle_startendtag(self, tag, attrs):
        # Self-closing: record it, but it opens nothing.
        depth = len(self._stack)
        self.handle_starttag(tag, attrs)
        del self._stack[depth:]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        for position in range(len(self._stack) - 1, -1, -1):
            if self._stack[position][0] == tag:
                del self._stack[position:]
                return
        # No matching open tag — a stray close. Leave the stack alone.

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        text = " ".join(data.split())
        if not text:
            return
        for _, index in reversed(self._stack):
            if index is not None:
                tag, attrs, existing = self.found[index]
                # Cap it: some buttons wrap a whole paragraph of legal text.
                self.found[index] = (tag, attrs, (existing + " " + text).strip()[:60])
                return


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
    # Whether an element is fillable is the difference between the right
    # selector and a 30-second timeout, so flag anything that won't be.
    style = attrs.get("style", "").replace(" ", "").lower()
    if (
        attrs.get("hidden") is not None
        or attrs.get("type") == "hidden"
        or attrs.get("aria-hidden") == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    ):
        bits.append("NOT VISIBLE")
    if attrs.get("disabled") is not None:
        bits.append("DISABLED")
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
        # A div carrying role="button" is a button as far as clicking goes, so
        # group it with the buttons rather than burying it under "div".
        role = attrs.get("role", "").lower()
        bucket = tag if tag in INTERESTING else ("clickable" if role in ROLES else "labelled")
        groups.setdefault(bucket, []).append(line.rstrip())

    for tag in ("form", "input", "select", "textarea", "label", "button",
                "clickable", "labelled", "a", "iframe"):
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
