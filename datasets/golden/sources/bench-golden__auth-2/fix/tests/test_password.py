from auth.password import Account, hash_password, verify_password


def account(raw_password: str) -> Account:
    return Account(username="alice", password_hash=hash_password(raw_password))


def test_correct_password_is_accepted():
    assert verify_password(account("hunter2"), "hunter2") is True


def test_wrong_password_is_rejected():
    assert verify_password(account("hunter2"), "hunter3") is False


def test_hash_is_stable():
    assert hash_password("hunter2") == hash_password("hunter2")


def test_hash_is_not_the_raw_password():
    assert hash_password("hunter2") != "hunter2"


def test_empty_password_is_rejected():
    assert verify_password(account("hunter2"), "") is False


def test_none_password_is_rejected():
    assert verify_password(account("hunter2"), None) is False


def test_account_without_password_rejects_everything():
    never_set = Account(username="bob", password_hash="")
    assert verify_password(never_set, "") is False
    assert verify_password(never_set, "anything") is False
