import argparse
import re
from pathlib import Path


TICK_RE = re.compile(r"^\[[^\]]+\]\s+(?P<symbol>.+?)\s+\|\s+price=")
FINAL_CANDIDATE_RE = re.compile(r"^-\s+(?P<symbol>.+?)\s+\|")


def normalize_symbol(value: str) -> str:
    return value.strip().casefold()


def symbol_from_tick(line: str) -> str | None:
    match = TICK_RE.match(line)
    if not match:
        return None
    return match.group("symbol").strip()


def symbol_from_final_candidate(line: str) -> str | None:
    match = FINAL_CANDIDATE_RE.match(line)
    if not match:
        return None
    return match.group("symbol").strip()


def line_mentions_token(line: str, token: str) -> bool:
    # Keeps token mentions from scanner/Jupiter/final filter lines without
    # matching substrings inside addresses or unrelated words.
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


def cycle_has_token(cycle: list[str], token: str) -> bool:
    wanted = normalize_symbol(token)

    for line in cycle:
        tick_symbol = symbol_from_tick(line)
        if tick_symbol and normalize_symbol(tick_symbol) == wanted:
            return True

        candidate_symbol = symbol_from_final_candidate(line)
        if candidate_symbol and normalize_symbol(candidate_symbol) == wanted:
            return True

        if line_mentions_token(line, token):
            return True

    return False


def is_token_tick(line: str, token: str) -> bool:
    symbol = symbol_from_tick(line)
    return bool(symbol and normalize_symbol(symbol) == normalize_symbol(token))


def is_token_final_candidate(line: str, token: str) -> bool:
    symbol = symbol_from_final_candidate(line)
    return bool(symbol and normalize_symbol(symbol) == normalize_symbol(token))


def is_cycle_boundary_or_date(line: str) -> bool:
    if not line.strip():
        return False

    return (
        line.startswith("===============================")
        or re.match(r"^[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+", line) is not None
    )


def is_scanner_context_line(line: str) -> bool:
    return line.startswith("=== M") and "Token Scanner" in line


def is_monitor_context_line(line: str) -> bool:
    if not line.strip():
        return False

    return (
        (line.startswith("=== M") and ("Token Monitor Buy" in line or "Position Monitor" in line))
        or line.startswith("Candidatos finais:")
        or line.startswith("[INFO] Modo PAPER")
        or line.startswith("[INFO] Monitorando")
        or line.startswith("[INFO] Modo: PAPER")
        or line.startswith("[INFO] Tempo máximo de monitoramento atingido.")
        or line.startswith("[INFO] Tempo m")
        or line.startswith("[INFO] Monitoramento encerrado.")
        or line.startswith("[INFO] Nenhuma posição aberta")
        or line.startswith("[INFO] Nenhuma posi")
        or line.startswith("[INFO] Nenhum token restante")
    )


def cycle_has_token_tick(cycle: list[str], token: str) -> bool:
    return any(is_token_tick(line, token) for line in cycle)


def cycle_has_token_final_candidate(cycle: list[str], token: str) -> bool:
    return any(is_token_final_candidate(line, token) for line in cycle)


def should_keep_line(
    line: str,
    token: str,
    include_rejections: bool,
    include_monitor_context: bool,
    include_final_candidate_header: bool,
) -> bool:
    if is_cycle_boundary_or_date(line):
        return True

    if is_token_tick(line, token):
        return True

    if is_token_final_candidate(line, token):
        return True

    if line.startswith("Candidatos finais:"):
        return include_final_candidate_header

    if line_mentions_token(line, token):
        if not include_rejections and ("REPROVADO" in line or "REPROVADO" in line.upper()):
            return False
        return True

    if is_scanner_context_line(line):
        return True

    if include_monitor_context and is_monitor_context_line(line):
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
    include_rejections: bool,
) -> list[str]:
    selected: list[str] = []

    for cycle in split_cycles(raw_lines):
        if not cycle_has_token(cycle, token):
            continue

        has_ticks = cycle_has_token_tick(cycle, token)
        has_final_candidate = cycle_has_token_final_candidate(cycle, token)
        include_monitor_context = has_ticks or has_final_candidate

        cycle_lines = [
            line
            for line in cycle
            if should_keep_line(
                line,
                token,
                include_rejections=include_rejections,
                include_monitor_context=include_monitor_context,
                include_final_candidate_header=has_final_candidate,
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


def default_output_dir_for(log_file: Path) -> Path:
    resolved = log_file.resolve()

    if resolved.parent.name.lower() == "cloud" and resolved.parent.parent.name.lower() == "logs":
        return resolved.parent.parent / "analysis"

    return resolved.parent / "analysis"


def main() -> None:
    parser = argparse.ArgumentParser(description="Extrai do log bruto todos os trechos relacionados a um token.")
    parser.add_argument("log_file", type=Path, help="Arquivo de log bruto.")
    parser.add_argument("token", help="Símbolo do token, por exemplo CABAL.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Diretório onde o recorte será gerado.",
    )
    parser.add_argument(
        "--only-monitored",
        action="store_true",
        help="Remove linhas de reprovação do scanner/Jupiter e mantém foco nos ciclos monitorados.",
    )
    args = parser.parse_args()

    if not args.log_file.exists():
        raise SystemExit(f"Log não encontrado: {args.log_file}")

    output_dir = args.output_dir or default_output_dir_for(args.log_file)

    raw_text = args.log_file.read_text(encoding="utf-8", errors="replace")
    raw_lines = raw_text.splitlines()
    extracted = extract_token_lines(
        raw_lines,
        token=args.token,
        include_rejections=not args.only_monitored,
    )

    if not extracted:
        raise SystemExit(f"Nenhum trecho encontrado para token: {args.token}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{safe_filename(args.token)}.log"
    output_text = "\n".join(extracted).rstrip() + "\n"
    try:
        output_path.write_text(output_text, encoding="utf-8")
    except PermissionError:
        if output_path.exists():
            output_path.unlink()
            output_path.write_text(output_text, encoding="utf-8")
        else:
            raise

    tick_count = sum(1 for line in extracted if is_token_tick(line, args.token))
    print(f"Arquivo gerado: {output_path}")
    print(f"Linhas extraídas: {len(extracted)}")
    print(f"Ticks do token: {tick_count}")
    if tick_count == 0:
        print("Aviso: o token apareceu no scanner/Jupiter, mas não há ticks dele no Monitor Buy neste log.")


if __name__ == "__main__":
    main()
