from .console import warn


def get_cli_skip_callback():
    def cli_skip_callback(remaining: int, cancel_token) -> bool:
        try:
            answer = (
                input(f"\\n  Esc pressed. Skip AI analysis for remaining {remaining} segment(s)? (y/na): ")
                .strip()
                .lower()
            )
            cancel_token.reset()
            return answer in ("y", "yes", "")
        except (KeyboardInterrupt, EOFError):
            warn("Force-quitting.")
            raise SystemExit(1) from None

    return cli_skip_callback
