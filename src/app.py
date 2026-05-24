"""
KRPTO3 — app.py

O scanner roda como processo independente (modules/token_scanner.py via rodar_scanner.sh).
Este app.py orquestra apenas o monitor de entrada e o monitor de posições,
exatamente como no KRPTO2 — sem nenhuma mudança de lógica.
"""

from modules.token_monitor_buy import monitor
from modules.position_monitor import monitor_positions


def main():
    monitor()
    monitor_positions()


if __name__ == "__main__":
    main()
