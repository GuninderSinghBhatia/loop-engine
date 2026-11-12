from loop_engine.intake import MAX_QUESTIONS, parse_questions


def test_parses_a_numbered_list():
    reply = "1. What is it for?\n2) Web or desktop?\n3. Any constraints?"
    assert parse_questions(reply) == [
        "What is it for?",
        "Web or desktop?",
        "Any constraints?",
    ]


def test_ignores_chatter_around_the_list():
    reply = "Sure! Here are my questions:\n\n1. What is it for?\n\nHope that helps."
    assert parse_questions(reply) == ["What is it for?"]


def test_none_means_no_questions():
    assert parse_questions("NONE") == []
    assert parse_questions("  none \n") == []


def test_caps_the_number_of_questions():
    reply = "\n".join(f"{i}. Question {i}?" for i in range(1, 10))
    assert len(parse_questions(reply)) == MAX_QUESTIONS
