from home_agent.finance import _is_card_payment

CARDS = {"1743"}


def test_matches_real_charge():
    assert _is_card_payment("חיוב לכרטיס ויזה 1743", CARDS)


def test_matches_real_credit():
    assert _is_card_payment("זיכוי לכרטיס ויזה 1743", CARDS)


def test_other_card_not_matched():
    assert not _is_card_payment("חיוב לכרטיס ויזה 6146", CARDS)


def test_non_card_line_not_matched():
    assert not _is_card_payment("תחנת דלק יעד כפר קאסם", CARDS)


def test_spacing_robust():
    assert _is_card_payment("חיוב  לכרטיס   ויזה 1743", CARDS)
