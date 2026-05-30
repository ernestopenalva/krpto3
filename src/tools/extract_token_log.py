import argparse
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENTRY_TICK_RE = re.compile(r"^\[[^\]]+\]\s+(?P<symbol>.+?)\s+\|\s+price=")
POSITION_TICK_RE = re.compile(r"^\[[^\]]+\]\s+\[MONITOR\]\s+(?P<symbol>.+?)\s+\|\s+price=")
BUY_SIGNAL_RE = re.compile(r"^\[[^\]]+\]\s+\[SINAL\]\s+COMPRA SIMULADA:\s+(?P<symbol>.+?)\s+@")
PAPER_BUY_RE = re.compile(r"^\[[^\]]+\]\s+\[PAPER BUY\]\s+posi\S+\s+aberta:\s+(?P<symbol>.+?)\s+@")
PROFIT_LOCK_RE = re.compile(r"^\[[^\]]+\]\s+\[PROFIT LOCK\]\s+(?P<symbol>.+?):")
SELL_RE = re.compile(r"^\[[^\]]+\]\s+\[PAPER SELL\]\s+(?P<symbol>.+?)\s+@")


def normalize_symbol(value: str) -> str:
    return value.strip().casefold()


def symbol_from_match(line: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        match = pattern.match(line)
        if match:
            return match.group("symbol").strip()
    return None


def symbol_from_entry_tick(line: str) -> str | None:
    return symbol_from_match(line, (ENTRY_TICK_RE,))


def symbol_from_position_line(line: str) -> str | None:
    return symbol_from_match(
        line,
        (
            POSITION_TICK_RE,
            BUY_SIGNAL_RE,
            PAPER_BUY_RE,
            PROFIT_LOCK_RE,
            SELL_RE,
        ),
    )


def line_mentions_token(line: str, token: str) -> bool:
    escaped = re.escape(token)
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", re.IGNORECASE)
    return bool(pattern.search(line))


def split_cycles(lines: list[str]) -> list[list[str]]:
    cycles: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("===============================") and current:
            cycles.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        cycles.append(current)

    return cycles


def is_target_symbol(symbol: str | None, token: str) -> bool:
    return bool(symbol and normalize_symbol(symbol) == normalize_symbol(token))


def is_token_entry_tick(line: str, token: str) -> bool:
    return is_target_symbol(symbol_from_entry_tick(line), token)


def is_token_position_line(line: str, token: str) -> bool:
    return is_target_symbol(symbol_from_position_line(line), token)


def cycle_has_token(cycle: list[str], token: str) -> bool:
    for line in cycle:
        if is_token_entry_tick(line, token):
            return True

        if is_token_position_line(line, token):
            return True

        if line_mentions_token(line, token):
            return True

    return False


def cycle_has_monitor_activity(cycle: list[str], token: str) -> bool:
    return any(
        is_token_entry_tick(line, token) or is_token_position_line(line, token)
        for line in cycle
    )


def is_cycle_boundary_or_date(line: str) -> bool:
    if not line.strip():
        return False

    return (
        line.startswith("===============================")
        or re.match(r"^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+", line) is not None
    )


def is_monitor_context_line(line: str) -> bool:
    if not line.strip():
        return False

    return (
        (line.startswith("=== M") and ("Token Monitor Buy" in line or "Position Monitor" in line))
        or line.startswith("[INFO] Monitorando")
        or line.startswith("[INFO] Nenhum candidato")
        or line.startswith("[INFO] Nenhum token restante")
        or line.startswith("[INFO] Monitoramento encerrado")
        or line.startswith("[INFO] Nenhuma posi")
        or line.startswith("[INFO] Position Monitor")
        or line.startswith("[WARN]")
        or line.startswith("[ERRO]")
    )


def should_keep_line(
    line: str,
    token: str,
    include_context: bool,
    only_monitored: bool,
) -> bool:
    if is_cycle_boundary_or_date(line):
        return True

    if is_token_entry_tick(line, token):
        return True

    if is_token_position_line(line, token):
        return True

    if line_mentions_token(line, token):
        if only_monitored:
            return (
                "[DESCARTE]" in line
                or "[WARN]" in line
                or "[ERRO]" in line
                or "[INFO]" in line
            )
        return True

    if include_context and is_monitor_context_line(line):
        return True

    return False


def compact_blank_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    previous_blank = False

    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        result.append(line)
        previous_blank = blank

    while result and not result[-1].strip():
        result.pop()

    return result


def extract_token_lines(
    raw_lines: list[str],
    token: str,
    only_monitored: bool,
) -> list[str]:
    selected: list[str] = []

    for cycle in split_cycles(raw_lines):
        if not cycle_has_token(cycle, token):
            continue

        include_context = cycle_has_monitor_activity(cycle, token)
        cycle_lines = [
            line
            for line in cycle
            if should_keep_line(
                line,
                token,
                include_context=include_context,
                only_monitored=only_monitored,
            )
        ]
        cycle_lines = compact_blank_lines(cycle_lines)

        if cycle_lines:
            if selected and selected[-1].strip():
                selected.append("")
            selected.extend(cycle_lines)

    return selected


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._-") or "token"


def find_latest_monitor_log() -> Path:
    candidates = sorted(
        PROJECT_ROOT.glob("logs/**/monitor_*.txt"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("Nenhum log do monitor encontrado em logs/**/monitor_*.txt")
    return candidates[0]


def dates_referenced_by_log(log_file: Path) -> set[str]:
    dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", log_file.name))
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return dates
    dates.update(re.findall(r"\d{4}-\d{2}-\d{2}", text))
    return dates


def find_matching_position_logs(monitor_log: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    for date in sorted(dates_referenced_by_log(monitor_log)):
        candidates = [
            monitor_log.with_name(f"position_{date}.txt"),
            PROJECT_ROOT / "logs" / "cloud" / f"position_{date}.txt",
            PROJECT_ROOT / "logs" / f"position_{date}.txt",
        ]
        for candidate in candidates:
            if candidate.exists():
                resolved = candidate.resolve()
                if resolved not in seen:
                    found.append(candidate)
                    seen.add(resolved)
                break

    return found


def default_output_dir_for(log_file: Path) -> Path:
    resolved = log_file.resolve()

    if resolved.parent.name.lower() == "cloud" and resolved.parent.parent.name.lower() == "logs":
        return resolved.parent.parent / "analysis"

    return resolved.parent / "analysis"


def resolve_input_path(path: Path) -> Path:
    if path.is_absolute():
        return path

    cwd_path = path.resolve()
    if cwd_path.exists():
        return cwd_path

    return PROJECT_ROOT / path


def count_entry_ticks(lines: list[str], token: str) -> int:
    return sum(1 for line in lines if is_token_entry_tick(line, token))


def count_position_ticks(lines: list[str], token: str) -> int:
    return sum(1 for line in lines if is_target_symbol(POSITION_TICK_RE.match(line).group("symbol").strip() if POSITION_TICK_RE.match(line) else None, token))


def extract_from_file(log_file: Path, token: str, only_monitored: bool) -> list[str]:
    raw_text = log_file.read_text(encoding="utf-8", errors="replace")
    return extract_token_lines(
        raw_text.splitlines(),
        token=token,
        only_monitored=only_monitored,
    )


def append_source_section(result: list[str], title: str, lines: list[str]) -> None:
    if not lines:
        return

    if result and result[-1].strip():
        result.append("")
    result.append(f"### Fonte: {title}")
    result.append("")
    result.extend(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extrai dos logs do monitor e do position todos os trechos relacionados a um token.")
    parser.add_argument(
        "first",
        help="Simbolo do token, ou arquivo de log se tambem informar o token depois.",
    )
    parser.add_argument(
        "second",
        nargs="?",
        help="Simbolo do token quando o primeiro argumento for o arquivo de log.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Diretorio onde o recorte sera gerado.",
    )
    parser.add_argument(
        "--only-monitored",
        action="store_true",
        help="Mantem foco nas linhas operacionais do monitor e remove mencoes soltas do token.",
    )
    parser.add_argument(
        "--position-log",
        type=Path,
        action="append",
        default=None,
        help="Log separado do Position Monitor. Pode repetir. Se omitido, tenta achar position_YYYY-MM-DD.txt.",
    )
    args = parser.parse_args()

    if args.second:
        log_file = Path(args.first)
        token = args.second
    else:
        log_file = find_latest_monitor_log()
        token = args.first

    log_file = resolve_input_path(log_file)

    if not log_file.exists():
        raise SystemExit(f"Log nao encontrado: {log_file}")

    position_logs = (
        [resolve_input_path(path) for path in args.position_log]
        if args.position_log
        else find_matching_position_logs(log_file)
    )
    missing_position_logs = [path for path in position_logs if not path.exists()]
    if missing_position_logs:
        raise SystemExit(f"Position log nao encontrado: {missing_position_logs[0]}")

    output_dir = args.output_dir or default_output_dir_for(log_file)

    monitor_lines = extract_from_file(
        log_file,
        token=token,
        only_monitored=args.only_monitored,
    )
    position_lines_by_file = [
        (
            position_log,
            extract_from_file(
                position_log,
                token=token,
                only_monitored=args.only_monitored,
            ),
        )
        for position_log in position_logs
    ]

    extracted: list[str] = []
    append_source_section(extracted, log_file.name, monitor_lines)
    for position_log, position_lines in position_lines_by_file:
        append_source_section(extracted, position_log.name, position_lines)

    if not extracted:
        raise SystemExit(f"Nenhum trecho encontrado para token: {token}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_filename(token)}.log"
    output_text = "\n".join(extracted).rstrip() + "\n"
    output_path.write_text(output_text, encoding="utf-8")

    entry_tick_count = count_entry_ticks(extracted, token)
    position_tick_count = count_position_ticks(extracted, token)

    print(f"Arquivo gerado: {output_path}")
    print(f"Log do monitor: {log_file}")
    if position_logs:
        print("Logs do Position Monitor:")
        for position_log in position_logs:
            print(f"- {position_log}")
    else:
        print("Logs do Position Monitor: nenhum arquivo correspondente encontrado")
    print(f"Linhas extraidas: {len(extracted)}")
    print(f"Ticks do Token Monitor Buy: {entry_tick_count}")
    print(f"Ticks do Position Monitor: {position_tick_count}")
    if entry_tick_count == 0 and position_tick_count == 0:
        print("Aviso: o token foi mencionado, mas nao ha ticks parseaveis dele no monitor.")


if __name__ == "__main__":
    main()
