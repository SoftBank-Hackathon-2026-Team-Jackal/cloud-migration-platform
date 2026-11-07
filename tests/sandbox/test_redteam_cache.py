"""Ticket 39 draft: the optional S3-backed red-team attack cache.

Same test shape as `tests/sandbox/test_archive.py`'s `AwsCliArchiveStore` tests: a fake `aws`
binary (a shell script), never a mocked `subprocess.run`, so the exact argv the real CLI would
see is what is checked.
"""

import stat

import pytest
from cmp_worker.redteam_cache import RedTeamCacheError, S3RedTeamCache, cache_key, optional_s3_cache

BUCKET = "cmp-test-redteam"


def fake_aws(tmp_path, script):
    path = tmp_path / "aws"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_the_key_is_the_source_hash_never_the_built_images_own_digest():
    # Two findings in any order give the same key; no findings is the first attack on that source.
    assert cache_key("src-abc", []) == "redteam/attack/src-abc/initial.json"
    assert cache_key("src-abc", ["f2", "f1"]) == cache_key("src-abc", ["f1", "f2"])
    assert cache_key("src-abc", ["f1"]) != cache_key("src-def", ["f1"])


def test_a_miss_returns_none_without_raising(tmp_path):
    store = S3RedTeamCache(
        bucket=BUCKET,
        aws_bin=fake_aws(
            tmp_path,
            'echo "An error occurred (NoSuchKey) when calling GetObject: Not Found" >&2\nexit 254\n',
        ),
    )
    assert store.get("redteam/attack/x/initial.json") is None


def test_a_store_error_raises_without_leaking_the_cli_output(tmp_path):
    store = S3RedTeamCache(
        bucket=BUCKET, aws_bin=fake_aws(tmp_path, 'echo "AccessDenied: secret-looking-text" >&2\nexit 254\n')
    )
    with pytest.raises(RedTeamCacheError) as exc:
        store.get("redteam/attack/x/initial.json")
    assert exc.value.code == "CACHE_STORE_ERROR" and "secret-looking-text" not in str(exc.value)


def test_a_hit_returns_the_stored_json(tmp_path):
    store = S3RedTeamCache(
        bucket=BUCKET,
        aws_bin=fake_aws(tmp_path, 'dest="${@: -1}"\necho \'{"status": "DONE", "open": []}\' > "$dest"\n'),
    )
    assert store.get("redteam/attack/x/initial.json") == {"status": "DONE", "open": []}


def test_a_corrupt_object_is_a_miss_not_a_crash(tmp_path):
    store = S3RedTeamCache(
        bucket=BUCKET, aws_bin=fake_aws(tmp_path, 'dest="${@: -1}"\necho "not json" > "$dest"\n')
    )
    assert store.get("redteam/attack/x/initial.json") is None


def test_put_sends_the_value_as_the_body_file(tmp_path):
    log = tmp_path / "calls"
    store = S3RedTeamCache(bucket=BUCKET, aws_bin=fake_aws(tmp_path, f'echo "$@" >> {log}\necho "{{}}"\n'))
    store.put("redteam/attack/x/initial.json", {"status": "DONE", "open": []})
    call = log.read_text()
    assert "put-object --bucket cmp-test-redteam --key redteam/attack/x/initial.json" in call
    assert "--body" in call


def test_put_raises_on_failure(tmp_path):
    store = S3RedTeamCache(bucket=BUCKET, aws_bin=fake_aws(tmp_path, 'echo "boom" >&2\nexit 1\n'))
    with pytest.raises(RedTeamCacheError):
        store.put("redteam/attack/x/initial.json", {})


def test_a_prefix_is_prepended_to_every_key(tmp_path):
    log = tmp_path / "calls"
    store = S3RedTeamCache(
        bucket=BUCKET, prefix="demo/", aws_bin=fake_aws(tmp_path, f'echo "$@" >> {log}\necho "{{}}"\n')
    )
    store.put("redteam/attack/x/initial.json", {})
    assert "--key demo/redteam/attack/x/initial.json" in log.read_text()


def test_the_profile_is_passed_through_not_hardcoded(tmp_path):
    log = tmp_path / "calls"
    store = S3RedTeamCache(
        bucket=BUCKET, profile="cmp-aws-prod", aws_bin=fake_aws(tmp_path, f'echo "$@" >> {log}\necho "{{}}"\n')
    )
    store.put("k", {})
    assert "--profile cmp-aws-prod" in log.read_text()


def test_no_bucket_configured_means_no_cache_at_all():
    assert optional_s3_cache(None) is None
    assert isinstance(optional_s3_cache("cmp-test-redteam"), S3RedTeamCache)
