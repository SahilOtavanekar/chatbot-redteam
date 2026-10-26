"""The ``suite`` command line: validate, run, finalize, compare, import.

Each subcommand is a thin shell over one component; the work lives elsewhere and nothing here
decides a verdict. Exit codes: 0 ok, 2 for a problem the user can fix (a bad input file, a
failed preflight, a bad argument), 1 for anything unexpected, 130 for Ctrl+C. Errors print as one
``error: ...`` line on stderr, with no traceback.

``suite.adapters`` imports PyRIT (slow, and it creates files under the user's profile), so it
is imported only inside ``run``, and ``suite.importer`` (which reaches it) only inside ``import``.
``validate``, ``finalize`` and ``compare`` never load it.
"""

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from suite.compare import Change, compare_runs, endpoint_name, write_compare
from suite.config import EndpointSpec, load_endpoint, scrub_secrets
from suite.engine import RateLimiter
from suite.evidence import read_run_json
from suite.models import ConfigError, Verdict
from suite.planner import PreflightReport, format_preflight, preflight
from suite.prompts import Pack, load_packs
from suite.review import finalize
from suite.run import execute_run

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USER_ERROR = 2
EXIT_INTERRUPTED = 130

DEFAULT_RESULTS_DIR = Path("results")
DEFAULT_BASELINE_DIR = Path("prompts/baseline")
SOURCE_GARAK = "garak"  # the one import source; suite.importer (lazy, loads PyRIT) is the authority

# The order of the run summary line.
_SUMMARY_ORDER = (Verdict.FAIL, Verdict.PASS, Verdict.INCONCLUSIVE, Verdict.NEEDS_REVIEW)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command in ``argv`` (default: ``sys.argv[1:]``) and return the exit code."""
    _make_output_safe()
    args = _build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    except KeyboardInterrupt:
        results_dir = getattr(args, "results_dir", None)
        where = f" — partial results were written under {results_dir}" if results_dir else ""
        print(f"interrupted{where}", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:
        # The type and message only, never a traceback: a traceback could show a header value.
        print(f"error: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED


def _make_output_safe() -> None:
    """Make printing never fail on a character the console's code page cannot show."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suite",
        description="Run a pack of test prompts against a chatbot endpoint and judge the replies.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    validate = commands.add_parser(
        "validate", help="dry run: show the plan, budget and authorisation; sends nothing"
    )
    _add_endpoint_and_pack(validate)
    validate.set_defaults(handler=_cmd_validate)

    run = commands.add_parser(
        "run", help="run the pack in parallel: run folder, draft report and review.csv"
    )
    _add_endpoint_and_pack(run)
    run.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="where run folders are created (default: %(default)s)",
    )
    run.set_defaults(handler=_cmd_run)

    final = commands.add_parser(
        "finalize", help="merge the verdicts typed into review.csv and write the final report"
    )
    final.add_argument("--run", type=Path, required=True, help="the run folder")
    final.set_defaults(handler=_cmd_finalize)

    compare = commands.add_parser(
        "compare", help="what a change fixed or broke: fixed / regressed / unchanged"
    )
    compare.add_argument("--baseline", type=Path, required=True, help="the earlier run folder")
    compare.add_argument("--run", type=Path, required=True, help="the newer run folder")
    compare.set_defaults(handler=_cmd_compare)

    import_ = commands.add_parser(
        "import",
        help="build the OWASP baseline pack (git-ignored) from the garak prompt dataset",
    )
    import_.add_argument(
        "--source",
        required=True,
        choices=[SOURCE_GARAK],
        help="the dataset to import",
    )
    import_.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_BASELINE_DIR,
        help="the pack folder to write (default: %(default)s)",
    )
    import_.add_argument(
        "--canary",
        default=None,
        help="the marker your bot must never reveal; becomes a must_not_contain check "
        "on the prompts that ask for it (default: none)",
    )
    import_.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="send each prompt this many times (default: %(default)s)",
    )
    import_.set_defaults(handler=_cmd_import)
    return parser


def _add_endpoint_and_pack(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", type=Path, required=True, help="endpoint YAML file")
    parser.add_argument(
        "--pack",
        type=Path,
        action="append",
        required=True,
        help="prompt pack folder or file; repeat to merge several packs (prompt ids must differ)",
    )


def _plan(args: argparse.Namespace) -> tuple[EndpointSpec, Pack, PreflightReport]:
    """Load the endpoint and pack(s), and print the preflight report (stdout)."""
    spec = load_endpoint(args.endpoint)
    pack = load_packs(args.pack)
    report = preflight(spec, pack)
    print(format_preflight(report, spec))
    return spec, pack, report


def _refuse(report: PreflightReport) -> int:
    """Print each preflight problem as an error and return the user-error exit code."""
    for problem in report.problems:
        print(f"error: {problem}", file=sys.stderr)
    return EXIT_USER_ERROR


def _cmd_validate(args: argparse.Namespace) -> int:
    _spec, _pack, report = _plan(args)
    return EXIT_OK if report.ok else _refuse(report)


def _cmd_run(args: argparse.Namespace) -> int:
    spec, pack, report = _plan(args)
    if not report.ok:
        return _refuse(report)

    # run.json records one pack_path; several packs are joined with "+" like the merged pack's name.
    pack_path = args.pack[0] if len(args.pack) == 1 else Path("+".join(map(str, args.pack)))
    try:
        run_dir = asyncio.run(_run(spec, pack, pack_path, args.results_dir))
    except (KeyboardInterrupt, ConfigError):
        raise  # main prints these
    except Exception as exc:
        # PyRIT's and httpx's messages can quote the url or a header value, so the endpoint's
        # ${VAR} secrets are scrubbed first. (main's own handler cannot: it has no spec.)
        detail = scrub_secrets(spec, f"{type(exc).__name__}: {exc}")
        print(f"error: unexpected {detail}", file=sys.stderr)
        return EXIT_UNEXPECTED

    run = read_run_json(run_dir)
    summary = run["summary"]
    print(" · ".join(f"{verdict.value} {summary[verdict.value]}" for verdict in _SUMMARY_ORDER))
    print(f"run folder: {run_dir}")
    if run["status"] != "complete":
        print(
            f"warning: the run ended as {run['status']}; attempts that were not sent show as "
            "Inconclusive in the report",
            file=sys.stderr,
        )
    return EXIT_OK


async def _run(spec: EndpointSpec, pack: Pack, pack_path: Path, results_dir: Path) -> Path:
    # Imported here, not at the top: this is what loads PyRIT.
    from suite.adapters import build_detectors, build_sender, init_pyrit

    await init_pyrit()
    # One limiter for the whole run: the detectors' own requests (Gandalf's password check)
    # count against the same requests-per-minute budget as the prompts.
    limiter = RateLimiter(spec.limits.requests_per_minute)
    detectors, forced = build_detectors(spec, limiter)
    sender = build_sender(spec)
    try:
        return await execute_run(
            spec,
            pack,
            pack_path=pack_path,
            results_dir=results_dir,
            sender=sender,
            detectors=detectors,
            forced=forced,
            limiter=limiter,
        )
    finally:
        await sender.aclose()


def _cmd_import(args: argparse.Namespace) -> int:
    # Imported here, not at the top: suite.importer reaches suite.adapters, which loads PyRIT.
    from suite.importer import import_source

    try:
        result = import_source(args.source, args.out, canary=args.canary, repeat=args.repeat)
    except OSError as err:
        # The importer lets a mkdir/write failure through raw; a path the user can fix is a
        # clean exit 2, not an unexpected exit 1. The class name only: the message repeats paths.
        raise ConfigError(f"cannot write to {args.out}: {type(err).__name__}") from err
    count = len(result.categories)
    noun = "category" if count == 1 else "categories"
    print(f"imported {result.prompt_count} prompts across {count} {noun} → {result.out_dir}")
    return EXIT_OK


def _cmd_finalize(args: argparse.Namespace) -> int:
    result = finalize(args.run)
    print(f"applied {result.applied}, pending {result.pending}")
    return EXIT_OK


def _cmd_compare(args: argparse.Namespace) -> int:
    rows = compare_runs(args.baseline, args.run)
    baseline = read_run_json(args.baseline)
    path = write_compare(
        args.run,
        rows,
        baseline_id=str(baseline.get("run_id") or args.baseline.name),
        baseline_endpoint=endpoint_name(baseline),
    )
    counts = Counter(row.change for row in rows)
    print(" · ".join(f"{change.value} {counts[change]}" for change in Change))
    print(f"compare: {path}")
    return EXIT_OK
