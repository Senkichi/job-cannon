from job_finder.sources._pii_scrub import DEFAULT_DENYLIST, scrub_text


def test_removes_to_header_lines():
    raw = "From: jobs@x.com\nTo: senki@example.com\nSubject: hi\nBody here"
    out = scrub_text(raw)
    assert "To: senki@example.com" not in out
    assert "Body here" in out


def test_redacts_identifiers_case_insensitively():
    out = scrub_text("Hello Senki and SENKICHI", identifiers=["senki", "senkichi"])
    assert "senki" not in out.lower()
    assert "[redacted]" in out.lower()


def test_redacts_bare_emails():
    out = scrub_text("reach me at jane.doe@gmail.com please")
    assert "jane.doe@gmail.com" not in out
    assert "please" in out


def test_default_denylist_is_iterable_of_str():
    assert all(isinstance(x, str) for x in DEFAULT_DENYLIST)
