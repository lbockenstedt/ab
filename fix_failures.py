"""The shared vocabulary for "the model gave us nothing to evaluate".

`fix_one_pr` (pr_review) PRODUCES these strings; `auto_remediate_pr`
(pr_remediate) CONSUMES them to decide whether a failure deserves the barren
allowance or a real remediation attempt. They live here, in a module neither
imports from the other, for two reasons:

  * pr_review imports pr_remediate lazily *inside* functions to avoid a circular
    import, so a top-level import in the other direction would be fragile.
  * Keeping the literals in one place is the point. When the producer and the
    consumer each spell out their own copy of a contract, they drift silently
    and the mitigation degrades to a no-op that still looks present -- exactly
    how `_full_file_context` came to return "" for every PR review.

A "barren" failure means the model returned an empty reply, no JSON, or
unparseable JSON. Unlike an anchor miss or a no-edits reply -- where the model
did produce a structured fix that merely failed to apply -- a barren reply
carries no signal about the fix, so it must not spend the remediation budget.
"""

BARREN_EMPTY = "The model returned an empty response."

BARREN_NO_JSON = ("No JSON object was found in the response. Return ONLY a JSON "
                  "object with \"confidence\" and \"edits\".")

BARREN_INVALID_JSON = ("The response was not valid JSON. Return ONLY a single "
                       "well-formed JSON object with \"confidence\" and \"edits\".")

BARREN_FAILURES = frozenset({BARREN_EMPTY, BARREN_NO_JSON, BARREN_INVALID_JSON})

# fix_one_pr refuses outright when another task already holds the per-PR fix
# lock. Unlike every other failure it can return, this one happens BEFORE a
# model is chosen or a checkout is touched: nothing was attempted, so there is
# nothing to learn and nobody to blame. Charging it to the remediation budget
# (and, on an escalation turn, to the premium budget) spent real attempts on a
# scan that did no work -- lm#1071 reached exhausted_human_review with this as
# its recorded failure. The contended scan must simply come back next cycle.
CONTENDED_LOCK = "A fix is already in progress for this PR."


def is_contended_failure(message):
    """True when `message` reports that another task already holds the fix lock.

    Substring, for the same reason is_barren_failure is. Deliberately NOT part
    of BARREN_FAILURES: a barren reply is a model that spoke and said nothing,
    and is capped by its own allowance so a persistently mute model cannot loop
    forever. Contention is not a model event at all and needs no such cap --
    the lock is released when the in-flight fix finishes.
    """
    if not isinstance(message, str):
        return False
    return CONTENDED_LOCK in message


def is_barren_failure(message):
    """True when `message` reports a model reply with nothing evaluable in it.

    Substring rather than equality: callers wrap these strings in banners and
    prefixes ("Fix invocation failed: ..."), and a wrapped barren failure is
    still barren.
    """
    if not isinstance(message, str):
        return False
    text = message.strip()
    if not text:
        return False
    return any(failure in text for failure in BARREN_FAILURES)
