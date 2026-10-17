"""Score the rule solvers against practice_keys.csv (rounds 1-48). No LLM calls.

Run:  python3 check_practice.py questions.csv practice_keys.csv
"""
import csv
import sys
from collections import Counter

from quizbot import solve_by_rule

questions, keys = sys.argv[1], sys.argv[2]
key = {(r["round_id"], r["q_index"]): r["answer"] for r in csv.DictReader(open(keys, newline=""))}
right, wrong, unsolved, by_rule = 0, [], [], Counter()
for r in csv.DictReader(open(questions, newline="")):
    k = (r["round_id"], r["q_index"])
    if k not in key:
        continue
    letter, method = solve_by_rule(r["question_text"].strip(), [r[f"option_{c}"] for c in "abcd"])
    if letter is None:
        unsolved.append((k, r["question_text"]))
    elif letter == key[k]:
        right += 1
        by_rule[method] += 1
    else:
        wrong.append((k, method, letter, key[k], r["question_text"]))

total = right + len(wrong) + len(unsolved)
print(f"{right}/{total} correct, {len(wrong)} wrong, {len(unsolved)} not matched by any rule")
for m, n in by_rule.most_common():
    print(f"  {n:4d}  {m}")
for w in wrong[:20]:
    print("WRONG", w)
for u in unsolved[:20]:
    print("UNSOLVED", u)
sys.exit(0 if not wrong and not unsolved else 1)
