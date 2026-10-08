"""Text from other people and agents, made safe to put on a terminal or in a file someone will `cat`.

A terminal obeys control characters: an escape sequence in a chat message can retitle the window, move the
cursor over earlier output, or ask the terminal to write to the clipboard or answer back. Direction
overrides can make text read differently from how it is stored. None of that has a place in a chat message.
"""

import unicodedata

REPLACEMENT = "�"
# Right-to-left and other direction controls: they reorder how text is displayed, which is only ever used to mislead.
DIRECTION_CONTROLS = set("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")


def clean(text: str, keep_newlines: bool = True) -> str:
    """`text` with control characters replaced by U+FFFD. Newlines are kept (tabs become a space) unless told otherwise."""
    out = []
    for ch in text:
        if ch == "\n":
            out.append("\n" if keep_newlines else " ")
        elif ch == "\t":
            out.append(" ")
        elif ch in DIRECTION_CONTROLS or unicodedata.category(ch) in ("Cc", "Cs"):
            out.append(REPLACEMENT)
        else:
            out.append(ch)
    return "".join(out)


def one_line(text: str) -> str:
    return clean(text, keep_newlines=False)
