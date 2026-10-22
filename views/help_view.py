"""The Help and README page."""
import re
from pathlib import Path

import streamlit as st

from ui_common import github_url


def help_page():
    """The README, shown in the app, with the links to the code and to Dale's site."""
    st.title("Help and README")
    st.markdown(f"This is the project's README, the same one that is on [GitHub]({github_url()}#readme), where the code lives "
                "and where you can report a problem or suggest something. The long-test method (relative gain at each "
                "receiver, linear averaging and long alternating blocks) follows the work of "
                "[Dale, N3HXZ](https://www.n3hxz.net/), published on his site. The quick paired test is this app's own.")
    path = Path(__file__).resolve().parent.parent / "README.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        st.warning(f"The README file isn't here. It is on [GitHub]({github_url()}#readme).")
        return
    chunk = []

    def flush():
        if chunk:
            st.markdown("\n".join(chunk))
            chunk.clear()

    lines, i = text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("# ") or "online, nothing to install" in line:
            continue
        if line.startswith("## What it looks like"):  # the screenshots are for GitHub, not for the app itself
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            continue
        if line.strip() == "<details>":  # a folded section: the first line inside is its <summary>
            end = next((k for k in range(i, len(lines)) if lines[k].strip() == "</details>"), len(lines))
            inside = lines[i:end]
            i = end + 1
            flush()
            title = re.sub(r"<[^>]+>", "", inside[0]).strip() if inside else "More"
            with st.expander(title):
                st.markdown("\n".join(inside[1:]))
            continue
        chunk.append(line)
    flush()
