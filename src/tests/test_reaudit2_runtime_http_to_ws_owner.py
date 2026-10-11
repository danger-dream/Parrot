"""Split P15 cancellation ownership gates; shared fakes live in the support module."""

from src.tests.reaudit2_runtime_cancellation_support import *  # noqa: F401,F403

@pytest.mark.asyncio
async def test_cancel_outer_retry_update_aborts_unstarted_http_to_ws_stream(
    monkeypatch,
):
    logs = _StrictLogFakes()
    record_retry = logs.record_retry_attempt
    update_retry = logs.update_retry_attempt
    finish_error = logs.finish_error
    update_proxy = logs.update_proxy_attempt
    monkeypatch.setattr(failover.log_db, "record_retry_attempt", record_retry)
    monkeypatch.setattr(failover.log_db, "update_retry_attempt", update_retry)
    monkeypatch.setattr(failover.log_db, "finish_error", finish_error)
    monkeypatch.setattr(failover.log_db, "update_proxy_attempt", update_proxy)

    channel = SimpleNamespace(
        key="oauth:ws-one",
        type="oauth",
        protocol="openai-responses",
        provider="openai",
        cc_mimicry=False,
    )
    upstream_ws = _FakeUpstreamWebSocket()
    tracker = _Tracker()
    proxy_bytes = failover._WsProxyBytes(up=5, down=7)
    timing = failover.WsAttemptTiming(route_type="direct", round_id="ws-round-1")
    timing.mark_handshake_complete()
    timing.mark_ws_frame(b"first")
    round_timeouts = RoundTimeouts.from_config({
        "connect": 1, "firstByte": 1, "idle": 1, "total": 5,
    })

    async def recv_until_visible(
        upstream_ws_arg,
        tracker_arg,
        *,
        ch,
        deadline_ts,
        first_wait,
        idle_timeout,
        proxy_bytes,
        start_time,
        start_monotonic,
        translator_ctx,
        timing,
        round_timeouts,
    ):
        del (
            deadline_ts, first_wait, idle_timeout, start_time, start_monotonic,
            translator_ctx, timing, round_timeouts,
        )
        assert upstream_ws_arg is upstream_ws
        assert tracker_arg is tracker
        assert ch is channel
        assert proxy_bytes is not None
        return ['{"type":"response.output_text.delta","delta":"first"}'], None, 1

    monkeypatch.setattr(
        failover, "_recv_oauth_ws_until_visible", recv_until_visible,
    )
    started_at = time.time()
    started_monotonic = time.monotonic()
    stream_result = await failover._consume_oauth_responses_ws_stream(
        upstream_ws,
        tracker=tracker,
        ch=channel,
        resolved_model="m",
        deadline_ts=started_at + 5,
        start_time=started_at,
        start_monotonic=started_monotonic,
        connect_ms=1,
        first_byte_timeout=1,
        idle_timeout=1,
        request_id="request-ws-1",
        messages=[],
        api_key_name="key-1",
        client_ip="127.0.0.1",
        fp_query=None,
        retry_count_so_far=0,
        affinity_hit=0,
        translator_ctx=None,
        body={"model": "m", "stream": True},
        identity_state=failover.ProtocolIdentityMap(),
        client_key=None,
        proxy_name="proxy-a",
        proxy_bytes=proxy_bytes,
        timing=timing,
        round_timeouts=round_timeouts,
        proxy_attempt_id="proxy-ws-1",
        retry_attempt_id="retry-1",
        attempt_start_monotonic=started_monotonic,
    )
    assert isinstance(stream_result.response, StreamingResponse)

    monkeypatch.setattr(
        failover.config,
        "get",
        lambda: {
            "timeouts": {"connect": 1, "firstByte": 1, "idle": 1, "total": 5},
            "concurrency": {"queueWaitSeconds": 1},
        },
    )
    monkeypatch.setattr(
        failover,
        "_pick_non_direct_proxy_name",
        lambda ch, resolved_model: None,
    )
    monkeypatch.setattr(
        failover,
        "_should_use_responses_upstream_ws",
        lambda ch, *, ingress_protocol, cfg=None: True,
    )
    slot_releases: list[str] = []

    async def try_acquire(ch_key: str) -> bool:
        assert ch_key == channel.key
        return True

    def release(ch_key: str) -> None:
        slot_releases.append(ch_key)

    monkeypatch.setattr(failover.concurrency, "try_acquire", try_acquire)
    monkeypatch.setattr(failover.concurrency, "release", release)

    async def try_responses_ws_channel(
        ch,
        resolved_model,
        body,
        is_stream,
        deadline_ts,
        start_time,
        fp_query,
        messages,
        api_key_name,
        client_ip,
        request_id,
        retry_count_so_far,
        affinity_hit,
        *,
        client_key=None,
        retry_attempt_id=None,
        start_monotonic=None,
        attempt_start_monotonic=None,
        terminal_release=None,
    ):
        del (
            resolved_model, body, deadline_ts, start_time, fp_query, messages,
            api_key_name, client_ip, request_id, retry_count_so_far,
            affinity_hit, client_key, retry_attempt_id, start_monotonic,
            attempt_start_monotonic, terminal_release,
        )
        assert ch is channel
        assert is_stream is True
        return stream_result

    monkeypatch.setattr(
        failover,
        "_try_openai_oauth_responses_ws_channel",
        try_responses_ws_channel,
    )

    retry_worker_entered = asyncio.Event()
    allow_retry_worker = asyncio.Event()

    async def controlled_to_thread(func, /, *args, **kwargs):
        if func is update_retry and kwargs.get("outcome") == "open":
            retry_worker_entered.set()
            await allow_retry_worker.wait()
        return func(*args, **kwargs)

    monkeypatch.setattr(failover.asyncio, "to_thread", controlled_to_thread)
    task = asyncio.create_task(failover.run_failover(
        _schedule_result(channel, queued=False),
        {"model": "m", "stream": True, "messages": []},
        "request-ws-1",
        "key-1",
        "127.0.0.1",
        True,
        started_at,
        ingress_protocol="responses",
        start_monotonic=started_monotonic,
    ))
    await retry_worker_entered.wait()
    task.cancel()
    allow_retry_worker.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert upstream_ws.close_calls == 1
    assert slot_releases == [channel.key]
    assert [row["outcome"] for row in logs.retry_updates] == [
        "open", "client_disconnected",
    ]
    assert all(
        row["attempt_id"] == logs.retry_records[0]["handle"]
        for row in logs.retry_updates
    )
    assert [
        (row["status"], row["http_status"]) for row in logs.request_terminals
    ] == [("cancelled", 499)]
    assert [row["outcome"] for row in logs.proxy_updates] == [
        "client_disconnected",
    ]
    assert logs.proxy_updates[0]["proxy_attempt_id"] == "proxy-ws-1"
    assert logs.proxy_updates[0]["ended_at"] is not None
