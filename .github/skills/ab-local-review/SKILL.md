---
name: ab-local-review
description: >-
  Run AppBuilder's own two-panel PR review (skeptical + state-logic) on a
  local change BEFORE pushing it, so the session catches what AB would deny.
  Use before every `git push` to dev in a fleet repo (lm, ab, cs, netbox,
  opnsense, truenas, pxmx, zabbix, ...) and before pushing a fix onto an open
  promotion branch. The prompts, fleet rules, specialist agents and reviewer
  models all come from AB's code, so this changes when AB's review changes.
---

# AB local review

AB reviews every PR with two panels of reviewers from different providers:

- **Panel 1, skeptical review:** correctness, regressions, intent vs. diff, and the fleet rules.
- **Panel 2, state-logic review:** checks that no status conflates distinct states, and that every new line is reachable.

This skill runs those same prompts locally. Never write your own version
of them. `ab/scripts/local_review.py` builds the prompts by importing AB's
`pr_review` and `fix_engine`.

## Steps

1. Commit the change locally (do not push). Write the commit message as the
   PR intent: the problem, the fix, and the evidence for any contract the
   change relies on in another repo (reviewers cannot see other repos).
2. Build the prompts (the venv is `~/.venvs/ab`; if it is missing, run
   `/usr/bin/python3 -m venv ~/.venvs/ab && ~/.venvs/ab/bin/pip install -r ~/vscode/ab/requirements.txt pytest`):

       rm -rf <dir> && ~/.venvs/ab/bin/python ~/vscode/ab/scripts/local_review.py \
           <repo path> --base origin/dev --out <dir>; echo "exit $?"

   With `<dir>` = `<session folder>/files/ab-review/<repo>`. Removing it
   first means a failed run leaves nothing stale to review, and the command
   is not piped, so its exit status is the script's. Its last line is JSON
   with `prompts` (panel1.txt, panel2.txt), `reviewers` (the models AB's
   panel used on its latest review) and `agents_error`.
   - Non-zero exit: it could not tell which models AB reviews with. Stop and
     fix that; never review with a guessed panel.
   - `agents_error` not null: AB's specialist checklists did not load, so
     panel 1 would be missing them. Stop and fix that.
3. Launch one `code-review` agent per panel per reviewer model, all in
   parallel (2 panels × 3 models = 6), with `model` set to that reviewer
   model. Prompt each one:

   > Read `<panel file>` and do exactly what it says. The change is in the
   > git repo at `<repo path>`; read any file there to check a claim before
   > rejecting on it. Reply with only the JSON object the prompt asks for.

4. Save each reply unedited as `<dir>/votes/panel1-<model>.txt` or
   `<dir>/votes/panel2-<model>.txt` (clear `votes/` first each round). A
   reviewer that failed gets no file; never write a vote for it. Then apply
   AB's own verdict rule (`fix_engine.panel_verdict`):

       ~/.venvs/ab/bin/python ~/vscode/ab/scripts/local_review.py --gate <dir>

   Exit 0 means both panels Approve. As in AB, a seat in `reviewers.json`
   with no reply, or a reply with no verdict, is a failed reviewer: the
   panel then needs every other vote to Approve at an average of 0.80 or
   more, else it prints `Retry` (re-run the failed reviewers). A panel with
   no replies fails.
5. On a failed gate, read every `Reject` critique, including those from a
   panel that passed on majority:
   - Fix the whole class of defect, not just the line cited: search the change
     (and its twins) for every sibling instance.
   - Add a test that fails without the fix.
   - Amend the commit, then go back to step 2.
   - If a reject is wrong, add the evidence to the commit message or a code
     comment rather than ignoring it. AB's reviewers will raise it too.
6. After three rounds still failing: stop, do not push, and report the
   remaining findings to the user.
7. Push only once the gate passes.

## How sessions find this skill

It lives in ab, not in lm's synced fleet skills folder, because AB offers
every fleet skill to its own fix prompts and this one is only for a session
about to push. A local Copilot session loads it from any repo through
`skillDirectories` in `~/.copilot/settings.json`, which lists
`~/vscode/ab/.github/skills`.

## Known differences from AB

AB's panel 1 also gets two hints that read the pushed head on GitHub, so
they cannot run before a push: where getattr'd names are defined
(`attr_definition_lookup`) and the names ruff verified as defined
(`check_undefined_names_verified`). Both only help a reviewer approve, so
the local panel can be stricter than AB, never more lenient.

## Keeping this in step with AB

- Change the review prompts only in `ab/pr_review.py`
  (`skeptical_issue_body`, `state_logic_issue_body`) and `ab/fix_engine.py`
  (`reviewer_prompt`). This skill and AB both pick the change up.
- Change the fleet rules only in `lm/.github/fleet-rules.md` (synced into
  ab, where `fleet_checks.rules_text()` reads them).
- The gate is `fix_engine.panel_verdict`, which AB's `review_fix` also uses, so
  a change to AB's verdict rule reaches this skill automatically.
