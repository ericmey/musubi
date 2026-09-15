"""A small stateful stand-in for `iptables`, for the tei-dense firewall tests.

Chains live in a JSON file ($IPT_STUB_STATE). Every successful mutating command
appends a snapshot of the whole state to $IPT_STUB_LOG, so a test can replay
the script command by command and check the endpoint at every intermediate
state, not only at the end. Only the flags the firewall script uses exist.
"""

from __future__ import annotations

import json
import os
import sys


def _load() -> dict[str, list[list[str]]]:
    with open(os.environ["IPT_STUB_STATE"]) as f:
        return json.load(f)  # type: ignore[no-any-return]


def _save(state: dict[str, list[list[str]]]) -> None:
    with open(os.environ["IPT_STUB_STATE"], "w") as f:
        json.dump(state, f)
    with open(os.environ["IPT_STUB_LOG"], "a") as f:
        f.write(json.dumps(state) + "\n")


def main(argv: list[str]) -> int:
    args = [a for a in argv if a not in ("-w", "-n")]
    state = _load()
    op, rest = args[0], args[1:]
    if op == "-L":
        chain = rest[0]
        if chain not in state:
            return 1
        if "--line-numbers" in rest:
            for i, rule in enumerate(state[chain], 1):
                target = rule[rule.index("-j") + 1] if "-j" in rule else ""
                print(f"{i} {target}")
        return 0
    if op == "-N":
        if rest[0] in state:
            return 1
        state[rest[0]] = []
    elif op == "-F":
        if rest[0] not in state:
            return 1
        state[rest[0]] = []
    elif op == "-X":
        name = rest[0]
        referenced = any(
            r[r.index("-j") + 1] == name for rules in state.values() for r in rules if "-j" in r
        )
        if name not in state or state[name] or referenced:
            return 1
        del state[name]
    elif op == "-E":
        old, new = rest
        if old not in state or new in state:
            return 1
        state[new] = state.pop(old)
        for rules in state.values():
            for r in rules:
                if "-j" in r and r[r.index("-j") + 1] == old:
                    r[r.index("-j") + 1] = new
    elif op == "-A":
        state[rest[0]].append(rest[1:])
    elif op == "-I":
        state[rest[0]].insert(int(rest[1]) - 1, rest[2:])
    elif op == "-D":
        chain, spec = rest[0], rest[1:]
        if len(spec) == 1 and spec[0].isdigit():
            idx = int(spec[0]) - 1
            if not 0 <= idx < len(state[chain]):
                return 1
            del state[chain][idx]
        elif spec in state.get(chain, []):
            state[chain].remove(spec)
        else:
            return 1
    elif op == "-C":
        return 0 if rest[1:] in state.get(rest[0], []) else 1
    else:
        raise SystemExit(f"stub: unsupported {op}")
    _save(state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
