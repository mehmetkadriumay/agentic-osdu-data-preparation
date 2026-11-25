import sys
from pathlib import Path


def main():
    shared = Path(__file__).resolve().parents[2] / "agents" / "shared"
    if shared.is_dir():
        sys.path.insert(0, str(shared))
    from agent import create_agent
    from osdu_agent_host.application import serve

    serve(create_agent)


if __name__ == "__main__":
    main()
