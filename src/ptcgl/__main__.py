"""`python -m ptcgl` のエントリポイント。

引数なし（または `gui` サブコマンド）なら GUI を起動し、それ以外は CLI（検証用）として動作する。
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv == ["gui"]:
        from ptcgl.app import main as app_main

        return app_main([sys.argv[0]])
    from ptcgl.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
