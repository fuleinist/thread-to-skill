"""English locale pack.

This is the default and the fallback. The cue tuples are copied verbatim from
the original offline extractor so English output does not change. English
stop words stay in util.STOPWORDS, so EXTRA_STOPWORDS is empty here.
"""

NAME = "en"

CUE_ACTION = (
    "we should", "you should", "let's", "we need to", "make sure", "the fix",
    "the solution", "the workaround", "step ", "first,", "first ", "then ",
    "next,", "instead", "use ", "run ", "add ", "remove ", "change ", "update ",
    "set ", "check ", "verify ", "configure ", "install ", "deploy ", "restart ",
    "pin ", "revert ", "roll back", "rollback", "try ", "do ", "only retry",
    "freeze ", "post ", "tag ", "watch ", "introduce ", "revoke ",
)
CUE_FIX = ("the fix is", "the solution is", "the workaround is", "to fix", "root cause is")
CUE_DECISION = (
    "we decided", "decided to", "decided:", "the decision", "we agreed",
    "agreed to", "let's go with", "we'll use", "we will use", "the plan is",
    "we're going with",
)
CUE_PITFALL = (
    "don't", "do not", "avoid", "gotcha", "watch out", "careful", "be careful",
    "the problem is", "this breaks", "it breaks", "failed because", "mistake",
    "anti-pattern", "never ", "not supported", "doesn't work", "does not work",
)
CUE_VERIFY = (
    "verify", "verification", "check that", "confirm", "make sure", "test that",
    "validate", "should see", "expected", "assert", "smoke test", "run the test",
    "i added a test", "added a test",
)
GREETING = ("thanks", "thank you", "hi ", "hello", "hey ", "lgtm", "sounds good", "cool", "nice", "perfect")

# English stop words live in util.STOPWORDS. A pack only lists its own extras.
EXTRA_STOPWORDS: tuple[str, ...] = ()
