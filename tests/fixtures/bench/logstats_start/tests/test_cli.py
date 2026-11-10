import gzip
import json

from samples import LINE  # type: ignore[import-not-found]

from logstats import cli


def test_gzip_files_are_read_as_gzip(run, tmp_path):
    path = tmp_path / "access.log.gz"
    path.write_bytes(gzip.compress((LINE + "\n").encode()))
    assert json.loads(run(str(path), "--json").stdout)["parsed"] == 1


def test_a_plain_file_named_like_text_is_not_gunzipped(run, log):
    assert json.loads(run(log(LINE, name="access.txt"), "--json").stdout)["parsed"] == 1


def test_dash_reads_stdin(run):
    result = run("-", "--json", stdin=(LINE + "\n").encode())
    assert json.loads(result.stdout)["parsed"] == 1


def test_files_are_combined(run, log):
    result = run(log(LINE, name="a.log"), log(LINE, "junk", name="b.log"), "--json")
    report = json.loads(result.stdout)
    assert (report["lines"], report["parsed"], report["malformed"]) == (3, 2, 1)


def test_invalid_utf8_is_replaced(run, tmp_path):
    path = tmp_path / "bad.log"
    head, tail = LINE.replace("/index.html", "/caf@@").split("@@")
    path.write_bytes(head.encode() + b"\xe9\xff" + tail.encode() + b"\n")
    report = json.loads(run(str(path), "--json").stdout)
    assert report["parsed"] == 1 and report["top_paths"][0]["path"] == "/caf\ufffd\ufffd"


def test_crlf_and_a_missing_final_newline(run, tmp_path):
    path = tmp_path / "crlf.log"
    path.write_bytes((LINE + "\r\n" + LINE).encode())
    report = json.loads(run(str(path), "--json").stdout)
    assert (report["lines"], report["parsed"]) == (2, 2)


def test_json_output_is_only_the_object(run, log):
    result = run(log(LINE), "--json")
    assert result.stdout.decode().startswith("{") and result.stdout.endswith(b"}\n")
    assert result.stderr == b""


def test_exit_0_when_something_parsed(run, log):
    assert run(log(LINE, "junk")).returncode == 0


def test_exit_1_when_nothing_parsed_but_the_summary_is_printed(run, log):
    result = run(log("junk"))
    assert result.returncode == 1 and b"lines: 1" in result.stdout


def test_exit_1_for_empty_input(run, log):
    result = run("-", stdin=b"")
    assert result.returncode == 1 and b"lines: 0" in result.stdout


def test_a_missing_file_exits_2_before_any_output(run, log, tmp_path):
    missing = str(tmp_path / "nope.log")
    result = run(log(LINE), missing)
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr.decode().startswith(f"logstats: {missing}: ")


def test_a_truncated_gzip_exits_2_naming_the_file(run, tmp_path):
    path = tmp_path / "cut.log.gz"
    path.write_bytes(gzip.compress(((LINE + "\n") * 2000).encode())[:-20])
    result = run(str(path))
    assert result.returncode == 2 and result.stdout == b""
    assert str(path) in result.stderr.decode() and b"Traceback" not in result.stderr


def test_usage_errors_exit_2(run, log):
    path = log(LINE)
    assert run().returncode == 2
    assert run(path, "--bogus").returncode == 2
    assert run(path, "--top", "0").returncode == 2
    assert run(path, "--top", "x").returncode == 2


def test_main_returns_the_exit_code(log, capsys):
    assert cli.main([log(LINE), "--top", "3"]) == 0
    assert "lines: 1" in capsys.readouterr().out
