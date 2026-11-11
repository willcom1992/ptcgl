"""アプリアイコンを生成する（packaging/ptcgl.ico と src/ptcgl/data/icon.png）。

使い方:
    python scripts/build_icon.py [--preview out.png]

図柄: 青いタイルの上に、英語カード（"A"）の手前へ日本語カード（"あ"）が重なる。
既存のゲームやカードの意匠は使わず、Pillow だけで描く。文字のフォントは Windows 同梱の
游ゴシック／Segoe UI を使う（開発時にしか実行しないので Windows 前提でよい）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ICO_PATH = ROOT / "packaging" / "ptcgl.ico"
PNG_PATH = ROOT / "src" / "ptcgl" / "data" / "icon.png"
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
S = 1024  # 作画サイズ（縮小して各サイズを作る）

FONT_JA = [r"C:\Windows\Fonts\YuGothB.ttc", r"C:\Windows\Fonts\meiryob.ttc"]
FONT_EN = [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"]


def load_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise FileNotFoundError(f"フォントが見つかりません: {candidates}")


def gradient_tile(size: int, radius: int) -> Image.Image:
    """角丸の青いタイル（左上が明るく右下が濃い）。"""
    top, bottom = (56, 132, 255), (24, 58, 170)
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        grad.putpixel(
            (0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True))
        )
    tile = grad.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)
    return out


def card(
    w: int, h: int, text: str, font: ImageFont.FreeTypeFont, color: tuple[int, int, int]
) -> Image.Image:
    """白いカード。中央に 1 文字、上端に細い帯を描く。"""
    pad = 24
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box = (pad, pad, pad + w, pad + h)
    d.rounded_rectangle(box, radius=w // 10, fill=(255, 255, 255), outline=(215, 222, 235), width=6)
    # 上端の色帯（カードらしさ）
    band = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(band).rounded_rectangle(
        (pad, pad, pad + w, pad + h // 5), radius=w // 10, fill=color
    )
    clip = Image.new("L", img.size, 0)
    ImageDraw.Draw(clip).rectangle((pad, pad, pad + w, pad + h // 7), fill=255)
    img.paste(band, (0, 0), ImageChops.multiply(band.split()[3], clip))
    # 文字
    bbox = d.textbbox((0, 0), text, font=font, anchor="ls")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = pad + (w - tw) / 2 - bbox[0]
    y = pad + h // 7 + (h - h // 7 - th) / 2 - bbox[1]
    d.text((x, y), text, font=font, fill=color, anchor="ls")
    return img


def with_shadow(layer: Image.Image, offset: int, blur: int, alpha: int) -> Image.Image:
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, alpha), (0, 0), layer.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (offset, offset))
    out.alpha_composite(layer)
    return out


def draw_icon() -> Image.Image:
    img = gradient_tile(S, radius=S * 22 // 100)
    cw, ch = int(S * 0.46), int(S * 0.64)
    font_en = load_font(FONT_EN, int(ch * 0.52))
    font_ja = load_font(FONT_JA, int(ch * 0.50))

    back = card(cw, ch, "A", font_en, (90, 100, 120)).rotate(
        12, resample=Image.BICUBIC, expand=True
    )
    front = card(cw, ch, "あ", font_ja, (225, 60, 70)).rotate(
        -6, resample=Image.BICUBIC, expand=True
    )
    back = with_shadow(back, offset=10, blur=18, alpha=90)
    front = with_shadow(front, offset=14, blur=22, alpha=110)

    cx, cy = S // 2, S // 2
    img.alpha_composite(
        back, (cx - back.width // 2 + int(S * 0.13), cy - back.height // 2 - int(S * 0.08))
    )
    img.alpha_composite(
        front, (cx - front.width // 2 - int(S * 0.10), cy - front.height // 2 + int(S * 0.06))
    )
    return img


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, help="確認用に 1024px の PNG も書き出す")
    args = parser.parse_args(argv)

    icon = draw_icon()
    if args.preview:
        icon.save(args.preview)
    PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    icon.resize((256, 256), Image.LANCZOS).save(PNG_PATH, optimize=True)
    ICO_PATH.parent.mkdir(parents=True, exist_ok=True)
    icon.save(ICO_PATH, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"{ICO_PATH}\n{PNG_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
