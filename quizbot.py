"""DSO 429 HW3 quiz bot.

Reads questions.csv, answers every round that still needs answering, and writes
answers/round-NNNN.json. Each question is solved by an exact formula when it matches a
known question type. Anything unrecognised gets a placeholder letter so the round is never
missing, and is listed by --todo so the Claude routine can solve it and record the answer
with --set.

Run:  python3 quizbot.py --questions path/to/questions.csv
      python3 quizbot.py                # downloads questions.csv from GitHub
      python3 quizbot.py --todo         # questions still holding a placeholder
      python3 quizbot.py --set 49 q7 C  # record an answer worked out by hand
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import sys
import urllib.request
from collections import Counter, defaultdict

HERE = pathlib.Path(__file__).resolve().parent
ANSWERS = HERE / "answers"
STATE = HERE / "state"
# raw.githubusercontent.com can lag a few minutes behind; the API is current but is blocked
# in cloud sandboxes for repos not attached to the session. Try both, keep the newer copy.
QUESTION_SOURCES = [
    ("https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/questions.csv", {}),
    ("https://api.github.com/repos/FaisalXL/dso429-hourly-quiz/contents/questions.csv",
     {"Accept": "application/vnd.github.raw"}),
]
LETTERS = "ABCD"
EDIT_WINDOW = dt.timedelta(hours=3)          # the last push inside this window is the graded one
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

NUM = r"-?\$?-?[\d,]*\.?\d+"


def num(s):
    """'$1,234.50' -> 1234.5, '10.4%' -> 10.4, '667 units' -> 667."""
    m = re.search(r"-?\$?-?[\d,]*\.?\d+", s)
    if not m:
        return None
    return float(m.group().replace("$", "").replace(",", ""))


def decimals(s):
    m = re.search(r"\.(\d+)", s)
    return len(m.group(1)) if m else 0


# ---------------------------------------------------------------- rule solvers
# Each returns the exact numeric answer (in the option's units) or a date, or None.

def discount(q):
    m = re.fullmatch(rf"An item listed at ({NUM}) is discounted by ({NUM})%\. What is the sale price\?", q)
    if m:
        return num(m[1]) * (1 - num(m[2]) / 100)


def revenue(q):
    m = re.fullmatch(rf"A retailer sells ({NUM}) units at ({NUM}) each\. What is total revenue\?", q)
    if m:
        return num(m[1]) * num(m[2])


def fx(q):
    m = re.fullmatch(rf"An invoice for ({NUM}) is converted to euros at ({NUM}) euros per US dollar\. "
                     r"How many euros is that\?", q)
    if m:
        return num(m[1]) * num(m[2])


def weighted_avg(q):
    if not q.endswith("What is the overall average order value?"):
        return None
    groups = re.findall(rf"had ({NUM}) orders averaging ({NUM})", q)
    if len(groups) >= 2:
        n = [num(a) for a, _ in groups]
        v = [num(b) for _, b in groups]
        return sum(a * b for a, b in zip(n, v)) / sum(n)


def gross_margin(q):
    m = re.fullmatch(rf"A product line generates ({NUM}) in revenue against ({NUM}) in cost of goods sold\. "
                     r"What is the gross margin\?", q)
    if m:
        rev, cogs = num(m[1]), num(m[2])
        return (rev - cogs) / rev * 100


def compound(q):
    m = re.fullmatch(rf"An investment of ({NUM}) grows at ({NUM})% compounded annually\. "
                     rf"What is its value after ({NUM}) years\?", q)
    if m:
        return num(m[1]) * (1 + num(m[2]) / 100) ** num(m[3])


def break_even(q):
    m = re.fullmatch(rf"Fixed costs are ({NUM})\. Each unit sells for ({NUM}) with a variable cost of ({NUM})\. "
                     r"How many units must be sold to break even, to the nearest unit\?", q)
    if m:
        return num(m[1]) / (num(m[2]) - num(m[3]))


def probability(q):
    m = re.search(r"what is the probability it is an? (\w+) ticket\?$", q)
    if not m or not q.startswith("A support queue holds"):
        return None
    counts = {kind: num(n) for n, kind in re.findall(rf"({NUM}) (\w+) tickets", q)}
    if m[1] in counts and len(counts) >= 2:
        return counts[m[1]] / sum(counts.values()) * 100


def pct_change(q):
    m = re.fullmatch(rf"Quarterly revenue moved from ({NUM}) to ({NUM})\. What is the percentage change\?", q)
    if m:
        old, new = num(m[1]), num(m[2])
        return (new - old) / old * 100


def ratio_split(q):
    m = re.fullmatch(rf"An? ({NUM}) marketing budget is split between two channels in the ratio (\d+):(\d+)\. "
                     r"What does the (first|second) channel receive\?", q)
    if m:
        total, a, b = num(m[1]), int(m[2]), int(m[3])
        return total * (a if m[4] == "first" else b) / (a + b)


def series(q):
    m = re.fullmatch(r"A monthly headcount series runs ([-\d, ]+)\. If the pattern continues, what is the next value\?", q)
    if not m:
        return None
    xs = [float(x) for x in m[1].split(",") if x.strip()]
    if len(xs) < 3:
        return None
    d1 = [b - a for a, b in zip(xs, xs[1:])]
    if len(set(d1)) == 1:                                   # arithmetic
        return xs[-1] + d1[0]
    if 0 not in xs[:-1]:
        r = [b / a for a, b in zip(xs, xs[1:])]
        if max(r) - min(r) < 1e-9:                          # geometric
            return xs[-1] * r[0]
    d2 = [b - a for a, b in zip(d1, d1[1:])]
    if len(xs) >= 4 and len(set(d2)) == 1:                  # constant second difference
        return xs[-1] + d1[-1] + d2[0]


def settlement(q):
    m = re.fullmatch(r"A contract signed on (\d{1,2}) (\w+) (\d{4}) has a (\d+)-day settlement period\. "
                     r"On what date does it settle\?", q)
    if m and m[2] in MONTHS:
        start = dt.date(int(m[3]), MONTHS.index(m[2]) + 1, int(m[1]))
        return start + dt.timedelta(days=int(m[4]))


RULES = [discount, revenue, fx, weighted_avg, gross_margin, compound, break_even,
         probability, pct_change, ratio_split, series, settlement]


def parse_date(s):
    m = re.fullmatch(r"\s*(\d{1,2}) (\w+) (\d{4})\s*", s)
    if m and m[2] in MONTHS:
        return dt.date(int(m[3]), MONTHS.index(m[2]) + 1, int(m[1]))


def pick(value, options):
    """Return the letter of the option that equals the computed value, or None if no
    option matches to its displayed precision (or more than one does)."""
    if isinstance(value, dt.date):
        hits = [L for L, o in zip(LETTERS, options) if parse_date(o) == value]
    else:
        hits = []
        for L, o in zip(LETTERS, options):
            x = num(o)
            if x is not None and abs(x - value) <= 0.5 * 10 ** -decimals(o) + 1e-9:
                hits.append(L)
    return hits[0] if len(hits) == 1 else None


def solve_by_rule(q, options):
    for rule in RULES:
        try:
            value = rule(q)
        except (ArithmeticError, ValueError):
            value = None
        if value is not None:
            letter = pick(value, options)
            if letter:
                return letter, f"rule:{rule.__name__}"
    return None, None


# ---------------------------------------------------------------- rounds

def parse_rounds(text):
    rounds = defaultdict(list)
    for row in csv.DictReader(io.StringIO(text)):
        try:
            rounds[int(row["round_id"])].append(row)
        except (KeyError, ValueError):
            continue
    return rounds


def load_questions(path):
    if path:
        return parse_rounds(pathlib.Path(path).read_text(encoding="utf-8"))
    best, errors = None, []
    for url, headers in QUESTION_SOURCES:
        req = urllib.request.Request(url, headers={**headers, "User-Agent": "dso429-quizbot"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                rounds = parse_rounds(resp.read().decode("utf-8"))
        except Exception as e:                                  # 403 in the sandbox, network blips
            errors.append(f"{url}: {e}")
            continue
        if best is None or sum(map(len, rounds.values())) > sum(map(len, best.values())):
            best = rounds
    if best is None:
        sys.exit("could not download questions.csv from any source:\n  " + "\n  ".join(errors))
    print(f"downloaded questions.csv: {len(best)} rounds, newest is round {max(best)}")
    return best


def qhash(row):
    blob = "\x1f".join([row["question_text"]] + [row[f"option_{c}"] for c in "abcd"])
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def released(rows):
    try:
        return dt.datetime.fromisoformat(rows[0]["released_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def answer_round(rid, rows, now):
    """Returns (answers, state, changed, report) or None when the round should be left alone."""
    out = ANSWERS / f"round-{rid:04d}.json"
    meta = STATE / f"round-{rid:04d}.json"
    rel = released(rows)
    if out.exists() and rel and now - rel > EDIT_WINDOW:
        return None                                             # locked: later pushes are not graded
    old_ans = json.loads(out.read_text())["answers"] if out.exists() else {}
    old_state = json.loads(meta.read_text()) if meta.exists() else {}

    answers, state, report = {}, {}, Counter()
    for row in sorted(rows, key=lambda r: int(r["q_index"])):
        k = f"q{int(row['q_index'])}"
        h = qhash(row)
        prev = old_state.get(k, {})
        if prev.get("hash") == h and prev.get("method", "guess") != "guess" and k in old_ans:
            answers[k], state[k] = old_ans[k], prev             # already solved this exact question
            report[prev["method"].split(":")[0]] += 1
            continue
        opts = [row[f"option_{c}"] for c in "abcd"]
        letter, method = solve_by_rule(row["question_text"].strip(), opts)
        if not letter:
            letter, method = old_ans.get(k, "A"), "guess"
        answers[k], state[k] = letter, {"method": method, "hash": h}
        report[method.split(":")[0]] += 1

    changed = answers != old_ans or not out.exists()
    return answers, state, changed, report


def todo(rounds, now):
    """Questions that still hold a placeholder letter, oldest round first."""
    items = []
    for rid in sorted(rounds):
        meta = STATE / f"round-{rid:04d}.json"
        if not meta.exists():
            continue
        state = json.loads(meta.read_text())
        rel = released(rounds[rid])
        for row in sorted(rounds[rid], key=lambda r: int(r["q_index"])):
            k = f"q{int(row['q_index'])}"
            if state.get(k, {}).get("method") == "guess":
                left = (rel + EDIT_WINDOW - now) if rel else None
                items.append({"round": rid, "q": k, "question": row["question_text"],
                              "options": {L: row[f"option_{c}"] for L, c in zip(LETTERS, "abcd")},
                              "minutes_left_for_full_credit": int(left.total_seconds() // 60) if left else None})
    return items


def set_answer(rid, k, letter):
    out = ANSWERS / f"round-{rid:04d}.json"
    meta = STATE / f"round-{rid:04d}.json"
    letter = letter.strip().upper()
    if letter not in LETTERS or not re.fullmatch(r"q\d+", k):
        sys.exit(f"bad answer {k}={letter}: use e.g. --set 49 q7 C")
    if not out.exists():
        sys.exit(f"{out.name} does not exist yet; run quizbot.py first")
    body = json.loads(out.read_text())
    state = json.loads(meta.read_text()) if meta.exists() else {}
    if k not in body["answers"]:
        sys.exit(f"round {rid} has no {k}")
    body["answers"][k] = letter
    state.setdefault(k, {})["method"] = "claude"
    out.write_text(json.dumps(body, indent=2) + "\n")
    meta.write_text(json.dumps(state, indent=2) + "\n")
    print(f"round {rid} {k} = {letter}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", help="local questions.csv (default: download from GitHub)")
    ap.add_argument("--todo", action="store_true", help="list questions still holding a placeholder, as JSON")
    ap.add_argument("--set", nargs=3, metavar=("ROUND", "Q", "LETTER"), help="record an answer, e.g. 49 q7 C")
    args = ap.parse_args()

    if args.set:
        return set_answer(int(args.set[0]), args.set[1], args.set[2])

    now = dt.datetime.now(dt.timezone.utc)
    rounds = load_questions(args.questions)
    if args.todo:
        return print(json.dumps(todo(rounds, now), indent=2))
    ANSWERS.mkdir(exist_ok=True)
    STATE.mkdir(exist_ok=True)

    written, placeholders = [], 0
    for rid in sorted(rounds):
        res = answer_round(rid, rounds[rid], now)
        if not res:
            continue
        answers, state, changed, report = res
        (STATE / f"round-{rid:04d}.json").write_text(json.dumps(state, indent=2) + "\n")
        if changed:
            body = {"round_id": rid, "answers": answers}
            (ANSWERS / f"round-{rid:04d}.json").write_text(json.dumps(body, indent=2) + "\n")
            written.append(rid)
            print(f"round {rid}: wrote {len(answers)} answers {dict(report)}")
        placeholders += report["guess"]

    print(f"{len(rounds)} rounds seen, {len(written)} written, {placeholders} placeholder answer(s)")
    if placeholders:
        print("Run `python3 quizbot.py --todo` to list them.")


if __name__ == "__main__":
    main()
