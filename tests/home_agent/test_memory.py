from home_agent.memory import Conversation


def test_append_and_load_oldest_first(tmp_path):
    c = Conversation(str(tmp_path / "m.db"))
    c.append(1, "user", "hi")
    c.append(1, "assistant", "hello")
    assert c.load(1) == [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": "hello"}]


def test_persists_across_connections_and_isolates_chats(tmp_path):
    path = str(tmp_path / "m.db")
    Conversation(path).append(1, "user", "remember me")
    Conversation(path).append(2, "user", "other chat")
    assert Conversation(path).load(1) == [{"role": "user", "content": "remember me"}]
    assert Conversation(path).load(2) == [{"role": "user", "content": "other chat"}]


def test_load_limit_keeps_most_recent_in_order(tmp_path):
    c = Conversation(str(tmp_path / "m.db"))
    for i in range(5):
        c.append(1, "user", f"m{i}")
    assert [m["content"] for m in c.load(1, limit=2)] == ["m3", "m4"]


def test_context_manager_closes_and_data_persists(tmp_path):
    path = str(tmp_path / "m.db")
    with Conversation(path) as c:
        c.append(1, "user", "kept")
    # connection closed on exit; data durable in the file
    assert Conversation(path).load(1) == [{"role": "user", "content": "kept"}]


def test_usable_from_a_different_thread(tmp_path):
    # Regression: the Telegram adapter runs handlers in a worker thread (asyncio.to_thread)
    # while the store is built on the main thread. A single shared sqlite3 connection
    # raised ProgrammingError there. A store instance must work across threads.
    import threading

    c = Conversation(str(tmp_path / "m.db"))  # created on the main thread
    errors = []

    def worker():
        try:
            c.append(7, "user", "from-thread")
            assert c.load(7) == [{"role": "user", "content": "from-thread"}]
        except Exception as e:  # sqlite3.ProgrammingError before the fix
            errors.append(repr(e))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == []
