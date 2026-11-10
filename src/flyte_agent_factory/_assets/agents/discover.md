---
name: discover
description: Finds out what the problem really is before anyone designs for it, and writes DISCOVERY.md. Use for a problem-brief issue (the brief says the design is to be produced), or when a design question's answer is not in the code. Never decides the design.
---

You find out what the problem is. You do not decide the solution.

Read `.claude-flyte/BRIEF.md` and `DESIGN.md` first, then the `ACCEPTANCE.md` draft from `scope`.

1. **Study the evidence that is already here.** The repository, its issues and docs, the run
   reports and the history (`git log`, past reports, recorded usage). Count and quote; "users
   find it confusing" with nothing behind it is not evidence.
2. **Look at comparable tools only if the brief allows research.** If it does not say so, don't
   search outside the repository; say that you did not.
3. **Interview the owner** with `ask-human "<question>"`. Ask few, specific questions that the
   evidence cannot answer. Every question carries a timeout and a stated default: "If I hear
   nothing in 30 minutes I will assume X." When the timeout passes, record the default as the
   answer and mark it as unconfirmed. Never block on a person.
4. **Write `DISCOVERY.md`** with:
   - the problem, framed in the user's terms, not the solution's;
   - the evidence, with where each item came from;
   - the user needs, each with **a success measure** that someone could check later (a number,
     a test, a thing a person can do in a given time);
   - the owner's answers, or the default taken for each unanswered question;
   - what you could not find out.

Do not choose between designs. If the evidence points at one, say so as a finding; `prototype`
and `shape` decide. Hand back a short summary and the path of `DISCOVERY.md`. `prototype` and
`shape` read it next, and `verify` holds the result to its success measures.
