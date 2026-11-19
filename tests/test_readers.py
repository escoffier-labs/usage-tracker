"""Parsing for each provider's transcript format, using the files under tests/fixtures."""

import json

import pytest

from helpers import (NOW_MS, antigravity_usage, fixture, generation, make_antigravity_db,
                     make_home, make_opencode_db, step)
from usage_tracker.readers import Env, antigravity, claude, codex, grok, opencode, pi
from usage_tracker.transcripts import parse_transcript, scan_transcript_dir

WINDOW_START = NOW_MS - 90 * 86_400_000


def parse(path, fmt):
    result = parse_transcript(fixture(path), fmt)
    assert result is not None
    return result


def test_claude_one_record_per_message_and_skips_synthetic_and_broken_lines():
    records = parse("claude-project.jsonl", claude.FORMAT).records
    models = [r.model for r in records]
    # msg_1 repeated once (second content block) and a synthetic message are dropped.
    assert models == ["claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5-5", "mystery-model-9", "claude-sonnet-5-5[1m]"]
    first = records[0]
    assert (first.uncached, first.cached, first.cache_write, first.output) == (100, 1000, 200, 50)
    assert first.dedupe_key == "msg_1:req_1"
    assert first.speed == "standard" and first.session == "claude-s1"


def test_claude_fast_speed_and_advisor_iteration_is_its_own_record():
    records = parse("claude-project.jsonl", claude.FORMAT).records
    executor, advisor = records[1], records[2]
    assert executor.speed == "fast" and executor.output == 5
    assert advisor.model == "claude-sonnet-5-5"
    assert (advisor.uncached, advisor.output) == (20, 10)
    assert advisor.dedupe_key == "msg_3:req_3:advisor:0"
    assert advisor.speed == "fast"


def test_claude_reported_cost_is_kept():
    records = parse("claude-project.jsonl", claude.FORMAT).records
    assert records[-1].reported_cost == 0.5


def test_codex_counts_requests_from_cumulative_counters():
    records = parse("codex-rollout.jsonl", codex.FORMAT).records
    assert len(records) == 2  # the re-emitted snapshot and the zero-size context marker add nothing
    first, second = records
    assert first.model == "gpt-6.1-sol" and first.session == "codex-s1"
    # Codex input is inclusive of the cached portion.
    assert (first.uncached, first.cached, first.output, first.reasoning) == (600, 400, 100, 30)
    assert (second.uncached, second.cached, second.output) == (400, 100, 50)
    assert first.speed == "fast"  # thread_settings_applied carried forward


def test_codex_fork_does_not_rebill_copied_parent_history():
    records = parse("codex-fork.jsonl", codex.FORMAT).records
    assert [(r.uncached, r.output) for r in records] == [(200, 20)]
    assert records[0].session == "codex-s2"


def test_codex_rejects_inconsistent_usage():
    assert codex.read_token_usage({"input_tokens": 5, "output_tokens": 1, "cached_input_tokens": 9}) is None
    assert codex.read_token_usage({"input_tokens": 5, "output_tokens": 1, "reasoning_output_tokens": 2}) is None
    ok = codex.read_token_usage({"input_tokens": 5, "output_tokens": 1})
    assert ok["total_tokens"] == 6


def test_grok_splits_per_model_cost_and_dedupes_by_prompt():
    records = parse("grok-updates.jsonl", grok.FORMAT).records
    by_model = {r.model: r for r in records}
    build = by_model["grok-5-build"]
    assert (build.uncached, build.cached, build.output, build.reasoning) == (800, 200, 100, 20)
    assert build.reported_cost == pytest.approx(0.003)
    # The model without its own cost takes the rest of the turn's cost, by token share.
    assert by_model["grok-5-mini"].reported_cost == pytest.approx(0.002)
    # Same prompt id without per-model usage: one "grok" record, and the repeat is dropped.
    assert [r.model for r in records].count("grok") == 2
    assert len({r.dedupe_key for r in records}) == len(records)


def test_grok_only_reads_updates_jsonl(tmp_path):
    home = make_home(str(tmp_path))
    source = grok.scan(Env({}, home), WINDOW_START)[0]
    assert [f.path.rsplit("/", 1)[-1] for f in source.files] == ["updates.jsonl"]


def test_pi_models_costs_and_dedupe():
    records = parse("pi-session.jsonl", pi.FORMAT).records
    models = [r.model for r in records]
    assert models == ["anthropic/claude-sonnet-5-5", "Tools/summaries", "openai/gpt-6.1-sol"]
    assert records[0].rate_model == "claude-sonnet-5-5"
    assert records[0].reasoning == 5 and records[0].reported_cost is None  # zero cost means unknown
    assert records[2].reported_cost == 0.25
    assert all(r.session == "pi-s1" for r in records)


def test_opencode_legacy_json_and_database(tmp_path):
    home = make_home(str(tmp_path))
    root = f"{home}/.local/share/opencode"
    make_opencode_db(f"{root}/opencode.db", [
        {"id": "msg_a", "session": "ses_a", "created": 1_794_900_000_000,
         "data": {"role": "assistant", "modelID": "deepseek-v4.1-flash", "tokens": {"input": 10, "output": 4, "reasoning": 2, "cache": {"read": 6, "write": 1}}, "cost": 0.01}},
        {"id": "msg_u", "session": "ses_a", "created": 1_794_900_001_000, "data": {"role": "user"}},
        {"id": "msg_z", "session": "ses_a", "created": 1_794_900_002_000,
         "data": {"role": "assistant", "modelID": "x", "tokens": {}}},
    ])
    source = opencode.read_root(root, 0)
    records = [r for f in source.files for r in f.records]
    by_id = {r.dedupe_key: r for r in records}
    assert set(by_id) == {"opencode:msg_a", "opencode:msg_legacy"}
    a = by_id["opencode:msg_a"]
    assert (a.uncached, a.cached, a.cache_write) == (10, 6, 1)
    assert (a.output, a.reasoning) == (6, 2)  # reasoning is added to output, kept as a subset
    assert a.reported_cost == 0.01
    assert by_id["opencode:msg_legacy"].reported_cost is None  # cost 0 means unknown


def test_opencode_missing_directory_is_silently_missing(tmp_path):
    assert opencode.read_root(str(tmp_path / "nope"), 0).files is None


def test_antigravity_reads_protobuf_and_merges_repeated_request_ids(tmp_path):
    ts = 1_794_900_000_000
    usage = antigravity_usage(uncached=100, output=40, cached=60, request_id="req-1")
    bigger = antigravity_usage(uncached=100, output=55, cached=60, request_id="req-1")
    db = tmp_path / "conversations" / "conv-1.db"
    make_antigravity_db(
        str(db),
        generations=[generation(usage, "Gemini 3.1 Pro (High)", ts)],
        steps=[step(bigger, "Gemini 3.1 Pro (High)", ts + 5000),
               step(antigravity_usage(uncached=1, output=1, request_id="req-2"), "Claude 4.5 Sonnet", ts)],
    )
    files, errors = antigravity.read_roots([str(tmp_path / "conversations")], 0)
    assert errors == []
    records = [r for _root, f in files for r in f.records]
    by_model = {r.model: r for r in records}
    assert set(by_model) == {"gemini-3.1-pro", "claude-sonnet-4-5"}
    gemini = by_model["gemini-3.1-pro"]
    # The two rows for req-1 are one request: the larger counters win.
    assert (gemini.uncached, gemini.output, gemini.cached) == (100, 55, 60)
    assert gemini.session == "conv-1"  # the database file name is the session id


def test_antigravity_thinking_level_suffix_prices_at_the_base_model():
    record = antigravity.Record("antigravity", 0, "gemini-3-flash-high", None, "s", 1, 0, 0, 1, 0, None, "standard", "k")
    assert antigravity._with_rate_model(record).rate_model == "gemini-3-flash"


def test_antigravity_unreadable_database_is_reported(tmp_path):
    bad = tmp_path / "conversations" / "bad.db"
    bad.parent.mkdir()
    bad.write_bytes(b"not a database")
    files, errors = antigravity.read_roots([str(bad.parent)], 0)
    assert files == [] and errors == [str(bad)]


def test_protobuf_decoder_rejects_truncated_input():
    with pytest.raises(antigravity.DecodeError):
        antigravity.fields(bytes([0x0A, 0x05, 0x01]))


def test_missing_provider_directories_are_skipped_without_noise(tmp_path):
    env = Env({}, str(tmp_path))
    for module in (claude, codex, grok, pi, opencode, antigravity):
        for source in module.scan(env, 0):
            assert source.files is None


def test_scan_transcript_dir_skips_old_files(tmp_path):
    home = make_home(str(tmp_path))
    source = scan_transcript_dir(f"{home}/.claude/projects", claude.FORMAT, NOW_MS + 10 * 86_400_000)
    assert source.files == []
