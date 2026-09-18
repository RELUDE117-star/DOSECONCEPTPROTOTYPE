#!/usr/bin/env python3
"""Find job scripts that EXECUTE data coming from the Raspberry Pi.

This is the single most important check in the security audit. The trust
direction is one-way by design — the Mac drives the Pi over SSH, the Pi
holds no credential for the Mac, and anything the Pi says is DATA. The
one way that could break is a job script that takes the Pi's output and
runs it, so this looks for exactly that and nothing else.

WHY THIS IS A PYTHON FILE AND NOT A `grep -rlE`
-----------------------------------------------
Because a plain grep over these files is wrong three different ways, and
each wrong answer cost a real investigation:

  1. `OUT=$(ssh host hostname)` is capture-and-compare, not execution.
     It is what every job in this project does. Flagged on the first
     run: four false positives.

  2. A COMMENT explaining the check matches the check. The block of
     prose above this one names `eval`, `bash <(…)` and `$(ssh …)`
     precisely so a reader understands the rule — and grep cannot tell
     an explanation from an instruction.

  3. A COMMIT MESSAGE in a quoted heredoc matches too. The job that
     shipped the fix for (1) and (2) was itself flagged, because its
     `git commit -F - <<'MSG'` body described the patterns. A quoted
     heredoc is literal data handed to another program; it is not shell
     code and cannot execute anything.

  4. A SINGLE-QUOTED ARGUMENT matches. An older job passed the previous
     detector's own regex to grep:

         grep -lE 'eval|$(ssh|`ssh|bash <(|sh <(|source .*out/' …

     The text `|sh <(` inside those quotes read as "piped into a shell".
     Single quotes in shell are absolutely literal — no expansion, no
     substitution, no execution — so their contents are an argument to
     some other program and never code this shell runs.

     DOUBLE quotes are different and are deliberately NOT stripped:
     `$( )` and backticks still execute inside them.

Four rounds of false positives from one tool. That matters more than it
sounds: an audit that fails on a clean tree teaches its owner to skip
the summary line, and the day it finds something real he skips that too.
So this strips comments, quoted-heredoc bodies and single-quoted spans
first, and tests only what the shell would actually run.

Usage:  scan_job_injection.py DIR [DIR ...]
Prints one `path:line: text` per finding. Exit 1 if any, 0 if none.
"""
import os
import re
import sys

# Constructs that RUN their input. Deliberately narrow.
PATTERNS = [
    (re.compile(r'(^|[;&|(]|\s)eval\s'),            "eval"),
    (re.compile(r'(^|[;&|(]|\s)(bash|sh|source|\.)\s+<\('), "process substitution into a shell"),
    (re.compile(r'\|\s*(bash|sh)(\s|$|;)'),          "piped into a shell"),
    (re.compile(r'(^|;|&&|\|\||\(|\bthen\b|\bdo\b)\s*[`$]\(?\s*ssh\b'),
     "ssh output in command position"),
    # `sh -c "$(…)"` / `bash -c `…`` runs a substitution directly. The
    # single-quote stripping below cannot hide this one, because a
    # substitution inside single quotes would not expand either.
    (re.compile(r'\b(bash|sh)\s+-c\s*["]?\s*(\$\(|`)'),
     "shell -c on a command substitution"),
]

# A single-quoted span is literal: no expansion, no substitution, no
# execution. Its contents are an argument to some other program. Double
# quotes are NOT included here on purpose — $( ) and backticks still run
# inside them.
_SQ = re.compile(r"'[^']*'")


def _blank_single_quoted(line):
    """Replace single-quoted spans with same-length blanks.

    Same length so reported columns and the quoted text stay sane, and
    so a quoted span can never join two halves of a pattern together.
    """
    return _SQ.sub(lambda m: " " * (m.end() - m.start()), line)


def strip_shell_noise(text):
    """Blank out comments and quoted-heredoc bodies.

    Returns a list of (lineno, code) for lines that the shell would
    actually execute. Line numbers are preserved so findings point at
    the real place in the file.
    """
    out = []
    heredoc_end = None
    for n, raw in enumerate(text.splitlines(), 1):
        if heredoc_end is not None:
            # Inside a heredoc body: data, not code. The terminator may
            # be indented when <<- was used.
            if raw.strip() == heredoc_end:
                heredoc_end = None
            continue

        # A quoted delimiter (<<'EOF' or <<"EOF") means the body is
        # passed through literally with no expansion — it is a payload
        # for another program (git commit -F -, ssh 'bash -s'), never
        # something this shell runs.
        m = re.search(r'<<-?\s*([\'"])([A-Za-z_][A-Za-z0-9_]*)\1', raw)
        if m:
            heredoc_end = m.group(2)
            # the line itself still counts, minus the heredoc marker
            raw = raw[:m.start()]
        else:
            # An UNQUOTED heredoc does expand, so its body can execute
            # things. Those lines are kept and scanned.
            m2 = re.search(r'<<-?\s*([A-Za-z_][A-Za-z0-9_]*)\s*$', raw)
            if m2:
                # scan the body too — just note where it ends
                pass

        code = raw
        # Full-line comments carry no instructions.
        if re.match(r'^\s*#', code):
            continue
        # A trailing comment cannot execute anything either, but a '#'
        # inside quotes is not a comment. Only strip when it is clearly
        # outside quotes: an even number of quote characters precedes it.
        for i, ch in enumerate(code):
            if ch != '#':
                continue
            before = code[:i]
            if before.count("'") % 2 == 0 and before.count('"') % 2 == 0 \
                    and (i == 0 or code[i - 1] in ' \t'):
                code = before
                break
        if code.strip():
            out.append((n, code))
    return out


def scan_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    hits = []
    for lineno, code in strip_shell_noise(text):
        testable = _blank_single_quoted(code)
        for pat, why in PATTERNS:
            if pat.search(testable):
                # Report the ORIGINAL line, so a finding is readable;
                # match against the blanked one, so quoted data cannot
                # masquerade as code.
                hits.append((lineno, why, code.strip()[:120]))
                break
    return hits


def main(argv):
    roots = argv[1:]
    if not roots:
        print("usage: scan_job_injection.py DIR [DIR ...]", file=sys.stderr)
        return 2
    findings = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith(".sh"):
                    continue
                p = os.path.join(dirpath, name)
                for lineno, why, code in scan_file(p):
                    findings += 1
                    print("%s:%d: %s: %s" % (p, lineno, why, code))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
