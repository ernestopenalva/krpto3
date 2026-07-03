from modules.token_monitor_buy import monitor


def main():
    try:
        monitor()
    except KeyboardInterrupt:
        print("[INFO] Interrupcao recebida. Monitor encerrado com status salvo.")


if __name__ == "__main__":
    main()
