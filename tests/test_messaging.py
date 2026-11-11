
"""Issue #5: real-time text messaging (CHAT-F-007, CHAT-F-008, CHAT-F-010, CHAT-NF-003; TC-MSG-01/02/04, TC-REL-01)."""
import queue
import threading
import time

from client.net.connection import ChatConnection


def send(client, to, text):
    return client.request("MSG", {"to": to, "text": text})


def test_message_reaches_recipient_and_sender_gets_ack(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    seq = send(alice, "bob", "hi bob")

    event = bob.recv()
    assert event["type"] == "EVENT" and event["kind"] == "msg"
    assert event["data"]["from"] == "alice"
    assert event["data"]["to"] == "bob"
    assert event["data"]["text"] == "hi bob"
    assert event["data"]["seq"] == seq

    ack = alice.reply_to(seq)
    assert ack["type"] == "ACK" and ack["status"] == "DELIVERED"
    assert ack["data"]["to"] == "bob"


def test_reply_in_both_directions(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    send(alice, "bob", "ping?")
    assert bob.recv()["data"]["text"] == "ping?"
    send(bob, "alice", "pong!")
    received = [alice.recv() for _ in range(2)]
    # ACK for Alice's message and Bob's reply can arrive in either order.
    assert any(
        m.get("kind") == "msg" and m["data"]["text"] == "pong!"
        for m in received
    )


def test_recipient_name_is_case_insensitive(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("Bob")
    seq = send(alice, "BOB", "hello")
    assert bob.recv()["data"]["to"] == "Bob"
    assert alice.reply_to(seq)["status"] == "DELIVERED"


def test_messages_arrive_in_send_order(connect):
    alice, bob = connect().sign_in("alice"), connect().sign_in("bob")
    for i in range(200):
        send(alice, "bob", f"message {i}")

    received = [bob.recv() for _ in range(200)]
    assert [m["data"]["text"] for m in received] == [
        f"message {i}" for i in range(200)
    ]
    seqs = [m["data"]["seq"] for m in received]
    assert seqs == sorted(seqs)


def test_order_is_kept_per_sender_with_concurrent_senders(connect):
    bob = connect().sign_in("bob")
    senders = [connect().sign_in(f"user{i}") for i in range(4)]

    def blast(client):
        for n in range(50):
            send(client, "bob", str(n))

    threads = [
        threading.Thread(target=blast, args=(client,))
        for client in senders
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    by_sender = {}
    for _ in range(200):
        data = bob.recv()["data"]
        by_sender.setdefault(data["from"], []).append(int(data["text"]))

    assert all(numbers == list(range(50)) for numbers in by_sender.values())
    assert len(by_sender) == 4


def test_offline_recipient_gets_stored_not_delivered_status(connect):
    alice = connect().sign_in("alice")
    connect().sign_in("bob").close()
    time.sleep(0.2)

    ack = alice.reply_to(send(alice, "bob", "are you there?"))
    assert ack["type"] == "ACK" and ack["status"] == "STORED_NOT_DELIVERED"
    assert ack["data"]["reason"] == "bob is offline"


def test_unknown_recipient_is_not_found(connect):
    alice = connect().sign_in("alice")
    reply = alice.reply_to(send(alice, "ghost", "boo"))
    assert reply["type"] == "ERROR" and reply["code"] == "NOT_FOUND"


def test_cannot_message_yourself(connect):
    alice = connect().sign_in("alice")
    assert alice.reply_to(send(alice, "Alice", "me"))["code"] == "INVALID_MESSAGE"


def test_room_message_needs_membership(connect):
    alice = connect().sign_in("alice")
    reply = alice.reply_to(
        alice.request("MSG", {"room": "general", "text": "hi"})
    )
    assert reply["code"] == "NOT_FOUND"


def test_must_be_logged_in_to_send(connect):
    connect().sign_in("bob")
    anonymous = connect()
    assert anonymous.reply_to(send(anonymous, "bob", "hi"))["code"] == "SESSION_EXPIRED"


def test_invalid_message_is_rejected_and_others_unaffected(connect):
    alice, bob, carol = (
        connect().sign_in("alice"),
        connect().sign_in("bob"),
        connect().sign_in("carol"),
    )
    bad = alice.reply_to(send(alice, "bob", "x" * 2001))
    assert bad["code"] == "INVALID_MESSAGE"

    carol.sock.sendall(b"\x00\x00\x00\x05{bad}")
    carol.close()

    seq = send(alice, "bob", "still working")
    assert bob.recv()["data"]["text"] == "still working"
    assert alice.reply_to(seq)["status"] == "DELIVERED"


def next_non_notification(inbox):
    """Next item from a client inbox, skipping presence and room notifications."""
    while True:
        item = inbox.get(timeout=5)
        if item[0] != "message" or item[1].get("kind") not in ("presence", "room", "join", "leave"):
            return item


def test_two_client_network_handlers_chat(server_address):
    inbox_a, inbox_b = queue.Queue(), queue.Queue()
    a, b = ChatConnection(inbox_a), ChatConnection(inbox_b)

    try:
        for conn, inbox, name in (
            (a, inbox_a, "alice"),
            (b, inbox_b, "bob"),
        ):
            conn.connect(*server_address)
            conn.request(
                "REGISTER",
                {"username": name, "password": "password-123"},
            )

            register_event = inbox.get(timeout=5)
            assert register_event[0] == "message"
            assert register_event[1]["status"] == "REGISTERED"

            conn.request(
                "LOGIN",
                {"username": name, "password": "password-123"},
            )

            login_event = inbox.get(timeout=15)
            assert login_event[0] == "message"
            assert login_event[1]["status"] == "LOGGED_IN"

        a.request(
            "MSG",
            {"to": "bob", "text": "hello from a real client"},
        )

        _, ack, request_type = next_non_notification(inbox_a)
        assert (request_type, ack["status"]) == ("MSG", "DELIVERED")

        _, event, _ = next_non_notification(inbox_b)
        assert event["data"]["text"] == "hello from a real client"

    finally:
        a.close()
        b.close()

def test_50_authenticated_clients_concurrent_delivery_latency(connect):
    client_count = 50
    clients = []
    sent_at = {}
    latencies = []
    errors = []
    lock = threading.Lock()
    start_senders = threading.Event()

    try:
        for i in range(client_count):
            client = connect().sign_in(f"loaduser{i}")
            clients.append(client)

        def sender(i):
            try:
                start_senders.wait(timeout=10)
                recipient = f"loaduser{(i + 1) % client_count}"

                with lock:
                    sent_at[f"loaduser{i}"] = time.perf_counter()

                send(clients[i], recipient, f"load message {i}")

            except Exception as exc:
                with lock:
                    errors.append(f"Sender {i}: {exc}")

        def receiver(i):
            try:
                expected_sender = f"loaduser{(i - 1) % client_count}"

                while True:
                    event = clients[i].recv()
                    if event.get("type") == "EVENT":
                        assert event.get("kind") == "msg"
                        break

                assert event["data"]["from"] == expected_sender
                assert event["data"]["text"] == (
                    f"load message {(i - 1) % client_count}"
                )

                received_at = time.perf_counter()

                with lock:
                    latency = received_at - sent_at[expected_sender]
                    latencies.append(latency)

            except Exception as exc:
                with lock:
                    errors.append(f"Receiver {i}: {exc}")

        receiver_threads = [
            threading.Thread(target=receiver, args=(i,))
            for i in range(client_count)
        ]

        for thread in receiver_threads:
            thread.start()

        sender_threads = [
            threading.Thread(target=sender, args=(i,))
            for i in range(client_count)
        ]

        for thread in sender_threads:
            thread.start()

        start_senders.set()

        for thread in sender_threads:
            thread.join(timeout=15)

        for thread in receiver_threads:
            thread.join(timeout=15)

        assert all(not thread.is_alive() for thread in sender_threads)
        assert all(not thread.is_alive() for thread in receiver_threads)
        assert not errors, errors
        assert len(latencies) == client_count

        sorted_latencies = sorted(latencies)
        p90 = sorted_latencies[
            (90 * client_count + 99) // 100 - 1
        ]

        print(f"\nClients: {client_count}")
        print(f"Messages delivered: {len(latencies)}")
        print(f"Average latency: {sum(latencies) / len(latencies):.4f} seconds")
        print(f"P90 latency: {p90:.4f} seconds")

        assert p90 <= 1.0, f"P90 latency exceeded 1 second: {p90:.4f}s"

    finally:
        for client in clients:
            client.close()
