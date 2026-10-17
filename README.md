# DSO 429 · Homework 3 — The Hourly Quiz

Answers for the hourly quiz rounds, one file per round:

```
answers/round-0001.json
answers/round-0002.json
...
```

Questions source: https://github.com/FaisalXL/dso429-hourly-quiz

## How the answers get here

A scheduled Claude Code routine runs in Anthropic's cloud once an hour. Each run checks out
this repo, downloads the latest `questions.csv`, answers every round that needs answering, and
pushes the answer files to `main`. No API keys are involved.

`quizbot.py` does the arithmetic. Each question that matches one of the 12 known question
types (compound growth, break-even, settlement dates, and so on) is solved with an exact
formula. A question that matches no formula gets a placeholder letter so the round is never
missing. `--todo` lists those questions, Claude works them out, and `--set` records the answers.

- Every round without a file gets answered, not just the newest.
- For three hours after release a round can still be corrected; after that it is left alone,
  since later pushes are not graded.
- `state/` records how each answer was produced, so placeholders are retried and edited
  questions are re-solved.

`check_practice.py` scores the formulas against the practice keys: 1,200/1,200 on rounds 1–48.

```
python3 check_practice.py questions.csv practice_keys.csv
python3 quizbot.py --questions questions.csv
python3 quizbot.py --questions questions.csv --todo
python3 quizbot.py --set 49 q7 C
```
