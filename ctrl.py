import argparse


def parse_args():
    parser = argparse.ArgumentParser(description="Run an NXBT macro file.")
    parser.add_argument("macro", help="Path to the macro text file.")
    return parser.parse_args()


def main():
    args = parse_args()
    with open(args.macro, "r", encoding="utf-8") as handle:
        cmd = handle.read().splitlines()

    import nxbt
    from tqdm import tqdm

    nx = nxbt.Nxbt()
    procon = nx.create_controller(nxbt.PRO_CONTROLLER)
    print("Waiting for console connection...")
    nx.wait_for_connection(procon)

    print("[+] Connected. Press Enter to start.")
    reconnect = input()
    if reconnect == "y":
        # send "L R 0.0s" to the console
        nx.macro(procon, "L R 0.0s")
        nx.macro(procon, "1s")

    chunk_size = 100
    with tqdm(total=len(cmd), desc="Sending macro", unit="line") as pbar:
        for start in range(0, len(cmd), chunk_size):
            chunk = cmd[start : start + chunk_size]
            nx.macro(procon, "\n".join(chunk))
            pbar.update(len(chunk))


if __name__ == "__main__":
    main()
