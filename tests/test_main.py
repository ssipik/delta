import pytest

from rag_delta.__main__ import build_parser, main


def test_run_id_is_optional():
    args = build_parser().parse_args([])
    assert args.run_id is None  # None → whole-table mode


def test_mark_ingested_requires_run_id(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--mark-ingested"])
    assert exc_info.value.code == 2
    assert "requires --run-id" in capsys.readouterr().err


def test_parser_accepts_flags():
    args = build_parser().parse_args(["--run-id", "r1", "--mark-ingested"])
    assert args.run_id == "r1"
    assert args.mark_ingested is True


def test_main_missing_config_exits_2(capsys):
    rc = main(["--run-id", "r1", "--config", "/nonexistent/sources.yaml"])
    assert rc == 2
    assert "error:" in capsys.readouterr().err
