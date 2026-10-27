from suite.models import SendError, Verdict, worst


def test_worst_orders_fail_inconclusive_review_pass():
    assert worst([Verdict.PASS, Verdict.NEEDS_REVIEW]) is Verdict.NEEDS_REVIEW
    assert worst([Verdict.NEEDS_REVIEW, Verdict.INCONCLUSIVE, Verdict.PASS]) is Verdict.INCONCLUSIVE
    assert worst([Verdict.INCONCLUSIVE, Verdict.FAIL]) is Verdict.FAIL
    assert worst([]) is Verdict.INCONCLUSIVE


def test_send_error_carries_status():
    e = SendError("http_status", "HTTP 429", status=429, retry_after=2.0)
    assert (e.kind, e.status, e.retry_after, str(e)) == (
        "http_status",
        429,
        2.0,
        "http_status: HTTP 429",
    )
