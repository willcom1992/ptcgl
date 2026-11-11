"""コマンドラインインターフェース（検証・デバッグ用）。

python -m ptcgl identify --set SVI --number 1        # OCR を使わず照合だけ
python -m ptcgl identify --image shot.png            # 画像からカードを特定
python -m ptcgl windows                              # ウィンドウ一覧
python -m ptcgl capture --window "TCG Live" --out shot.png
python -m ptcgl config-path
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ptcgl.config import AppConfig, config_path, load_config


def build_services(cfg: AppConfig):  # noqa: ANN201 - 戻り値は (Pipeline, CachedHttpClient)
    from ptcgl.data.cache import CachedHttpClient
    from ptcgl.data.cards import CardRepository
    from ptcgl.data.mapping import MappingStore
    from ptcgl.pipeline import Pipeline
    from ptcgl.recognition.detector import ManualRegionDetector

    http = CachedHttpClient(cfg.cache_dir, min_interval=cfg.http_min_interval)
    repo = CardRepository(http, cfg.images_dir, MappingStore(cfg.db_path))
    manual = cfg.capture.manual_box()
    # 既定は OCR アンカー方式（detector=None）。設定で手動領域が指定された場合だけ固定領域を使う
    detector = (
        ManualRegionDetector(manual) if cfg.capture.detection_mode == "manual" and manual else None
    )
    return Pipeline(repo, detector=detector), http


def _parse_region(text: str | None):  # noqa: ANN202
    if not text:
        return None
    from ptcgl.recognition.detector import CardBox

    try:
        parts = [float(v) for v in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--region は rx,ry,rw,rh の 4 値（比率）") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--region は rx,ry,rw,rh の 4 値（比率）")
    return CardBox(*parts)


def cmd_identify(args: argparse.Namespace) -> int:
    from ptcgl.data.limitless import CardRef

    cfg = load_config()
    pipeline, http = build_services(cfg)
    try:
        if args.set and args.number:
            number = args.number.upper().lstrip("0") or "0"
            result = pipeline.identify_ref(CardRef("en", args.set.upper(), number))
        elif args.image:
            from ptcgl.imageio import read_image

            frame = read_image(args.image)
            if frame is None:
                print(f"画像を読み込めません: {args.image}", file=sys.stderr)
                return 2
            box = args.region
            if args.no_detect and box is None:
                from ptcgl.recognition.detector import CardBox

                box = CardBox(0.0, 0.0, 1.0, 1.0)  # 画像全体を 1 枚のカードとして扱う
            result = pipeline.identify_frame(frame, box=box)
        else:
            print("--set/--number か --image を指定してください", file=sys.stderr)
            return 2
    finally:
        http.close()

    _print_result(result, verbose=args.verbose)
    if result.match is not None and not args.no_gui:
        from ptcgl.app import show_match_window

        show_match_window(result.match, cfg)
    return 0 if result.ok else 1


def _print_result(result, verbose: bool) -> None:  # noqa: ANN001
    if verbose and result.ocr_lines:
        print("--- OCR ---")
        for ln in result.ocr_lines:
            print(f"  {ln.confidence:.2f}  {ln.text}")
    if result.card_box:
        b = result.card_box
        print(f"card box: ({b.rx:.3f}, {b.ry:.3f}, {b.rw:.3f}, {b.rh:.3f}) conf={b.confidence:.2f}")
    if result.recognized:
        r = result.recognized
        print(
            f"recognized: name={r.name_en!r} set={r.set_code} number={r.number} "
            f"total={r.total} hp={r.hp} conf={r.confidence:.2f}"
        )
    if result.en_ref:
        print(f"en card: {result.en_ref}")
    if result.match:
        m = result.match
        print(f"en page: {m.en.name} / {m.en.set_name} / illustrator={m.en.illustrator}")
        if m.best:
            print(
                f"jp best: {m.best.label} same_art={m.same_art} conf={m.confidence:.2f} "
                f"({m.reason}) image={m.best.image_path}"
            )
        else:
            print(f"jp best: なし ({m.reason})")
        if len(m.candidates) > 1:
            print("candidates:")
            for c in m.candidates:
                print(f"  - {c.ref}  {c.name_ja}  illustrator={c.illustrator}")
    if result.error:
        print(f"error: {result.error}")
    print("timings(ms): " + ", ".join(f"{k}={v:.0f}" for k, v in result.timings_ms.items()))


def cmd_windows(_: argparse.Namespace) -> int:
    from ptcgl.capture.window import list_windows

    for w in list_windows():
        left, top, width, height = w.rect
        print(
            f"{w.hwnd:>10}  dpi={w.dpi:<4} {left:>6},{top:>6} {width:>5}x{height:<5} "
            f"{w.process_name:<28} {w.title}"
        )
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from ptcgl.capture.screen import ScreenCapture
    from ptcgl.capture.window import ensure_dpi_aware, find_window
    from ptcgl.imageio import write_image

    ensure_dpi_aware()
    cfg = load_config()
    pattern = args.window or cfg.target_window.title_pattern
    info = find_window(title_pattern=pattern, process_name=cfg.target_window.process_name)
    if info is None:
        print(f"ウィンドウが見つかりません: {pattern}", file=sys.stderr)
        return 1
    frame = ScreenCapture().grab(info.client_rect)
    write_image(args.out, frame)
    print(
        f"{info.title} (dpi={info.dpi}, client={info.client_rect}) "
        f"-> {args.out} {frame.shape[1]}x{frame.shape[0]}"
    )
    return 0


def cmd_config_path(_: argparse.Namespace) -> int:
    cfg = load_config()
    print(config_path())
    print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ptcgl", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("identify", help="カードを特定して日本語版を表示する")
    p.add_argument("--set", help="英語セットコード（例 SVI）")
    p.add_argument("--number", help="カード番号（例 1）")
    p.add_argument("--image", type=Path, help="スクリーンショット画像")
    p.add_argument(
        "--region", type=_parse_region, help="カード領域の比率 rx,ry,rw,rh（省略時は自動検出）"
    )
    p.add_argument("--no-detect", action="store_true", help="画像全体をカードとして扱う")
    p.add_argument("--no-gui", action="store_true", help="結果を表示せず終了する")
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("windows", help="可視ウィンドウの一覧")
    p.set_defaults(func=cmd_windows)

    p = sub.add_parser("capture", help="対象ウィンドウをキャプチャして保存する")
    p.add_argument("--window", help="ウィンドウタイトルの正規表現")
    p.add_argument("--out", type=Path, default=Path("capture.png"))
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("config-path", help="設定ファイルの場所と内容を表示する")
    p.set_defaults(func=cmd_config_path)

    p = sub.add_parser("gui", help="GUI を起動する（引数なしと同じ）")
    p.set_defaults(func=cmd_gui)
    return parser


def cmd_gui(_: argparse.Namespace) -> int:
    from ptcgl.app import main as app_main

    return app_main([sys.argv[0]])


def configure_console_encoding() -> None:
    """日本語 Windows（cp932）でリダイレクト時に UnicodeEncodeError で落ちないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.command:
        parser.print_help()
        return 2
    return int(args.func(args))
