from orchestrator import hello


def test_hello_returns_hello_world():
    assert hello() == "hello, world"
