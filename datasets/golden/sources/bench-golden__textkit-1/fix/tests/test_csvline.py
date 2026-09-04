from textkit.csvline import parse_line


def test_splits_plain_fields():
    assert parse_line("a,b,c") == ["a", "b", "c"]


def test_keeps_empty_fields():
    assert parse_line("a,,c") == ["a", "", "c"]


def test_single_field():
    assert parse_line("only") == ["only"]


def test_strips_trailing_newline():
    assert parse_line("a,b\n") == ["a", "b"]


def test_quoted_field_keeps_comma():
    assert parse_line('"a,b",c') == ["a,b", "c"]


def test_quoted_field_in_the_middle():
    assert parse_line('x,"y,z",w') == ["x", "y,z", "w"]


def test_double_quote_inside_quoted_field():
    assert parse_line('"he said ""hi""",done') == ['he said "hi"', "done"]
