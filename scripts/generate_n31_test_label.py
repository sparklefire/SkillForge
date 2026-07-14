#!/usr/bin/env python3
"""Generate a privacy-safe N31 test label matching the physical media."""

from __future__ import annotations

from pathlib import Path

from reportlab.graphics.barcode import code128
from reportlab.lib.colors import black, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


MEDIA_WIDTH_MM = 72.0
MEDIA_HEIGHT_MM = 130.0
POINTS_PER_MM = 72.0 / 25.4
PAGE_WIDTH_PT = MEDIA_WIDTH_MM * POINTS_PER_MM
PAGE_HEIGHT_PT = MEDIA_HEIGHT_MM * POINTS_PER_MM
OUTPUT = Path("output/pdf/n31_skillforge_test_label.pdf")
CN_FONT = "SkillForgeCN"
CJK_FONT_CANDIDATES = (
    Path.home() / "Library/Fonts/Heiti SC Medium.ttf",
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
)


def register_cjk_font() -> Path:
    for path in CJK_FONT_CANDIDATES:
        if path.is_file():
            pdfmetrics.registerFont(TTFont(CN_FONT, str(path)))
            return path
    raise FileNotFoundError(
        "找不到可嵌入的中文字体；可安装 Noto Sans CJK 或设置脚本中的字体候选路径"
    )


def draw_centered(c: canvas.Canvas, text: str, y: float, font: str, size: float) -> None:
    c.setFont(font, size)
    c.drawCentredString(PAGE_WIDTH_PT / 2, y, text)


def generate(destination: Path = OUTPUT) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    register_cjk_font()

    c = canvas.Canvas(
        str(destination),
        pagesize=(PAGE_WIDTH_PT, PAGE_HEIGHT_PT),
        invariant=1,
    )
    c.setTitle("SkillForge N31 privacy-safe test label")
    c.setAuthor("SkillForge")
    c.setSubject("Synthetic print verification label; no real shipment data")

    margin = 7
    c.setStrokeColor(black)
    c.setLineWidth(1.2)
    c.rect(margin, margin, PAGE_WIDTH_PT - 2 * margin, PAGE_HEIGHT_PT - 2 * margin)

    c.setFillColor(black)
    c.rect(margin, PAGE_HEIGHT_PT - 48, PAGE_WIDTH_PT - 2 * margin, 41, fill=1, stroke=0)
    c.setFillColor(white)
    draw_centered(c, "SKILLFORGE TEST", PAGE_HEIGHT_PT - 27, "Helvetica-Bold", 15)
    draw_centered(c, "非真实运单  禁止物流流转", PAGE_HEIGHT_PT - 42, CN_FONT, 8.5)

    c.setFillColor(black)
    c.setLineWidth(0.8)
    c.line(margin, 283, PAGE_WIDTH_PT - margin, 283)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(18, 292, "DEMO")
    c.drawRightString(PAGE_WIDTH_PT - 18, 292, "TEST")
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(PAGE_WIDTH_PT / 2, 296, ">")

    barcode = code128.Code128(
        "SKILLFORGE-DEMO-01",
        barHeight=34,
        barWidth=0.48,
        humanReadable=True,
    )
    barcode.drawOn(c, (PAGE_WIDTH_PT - barcode.width) / 2, 246)

    c.setLineWidth(0.6)
    c.rect(13, 179, PAGE_WIDTH_PT - 26, 58, fill=0, stroke=1)
    c.setFont(CN_FONT, 9)
    c.drawString(19, 219, "收件人：测试用户")
    c.drawString(19, 202, "电话：000-0000-0000")
    c.drawString(19, 185, "地址：演示地址，仅用于打印验证")

    c.rect(13, 104, PAGE_WIDTH_PT - 26, 62, fill=0, stroke=1)
    c.setFont(CN_FONT, 8.5)
    c.drawString(19, 149, "验收项目")
    c.drawString(23, 132, "□ 内容完整    □ 位置居中")
    c.drawString(23, 116, "□ 无跳纸      □ 无明显偏斜")

    c.setFont(CN_FONT, 8)
    c.drawString(17, 86, "页面尺寸：72.00 × 130.00 mm")
    c.drawString(17, 72, "打印设置：实际大小 / 100%，不要缩放")

    c.setFillColor(black)
    c.rect(13, 34, PAGE_WIDTH_PT - 26, 24, fill=1, stroke=0)
    c.setFillColor(white)
    draw_centered(c, "FOR DEMO ONLY / 仅供演示", 42, CN_FONT, 9)

    c.setFillColor(black)
    c.setLineWidth(0.5)
    for x, y in ((10, 10), (PAGE_WIDTH_PT - 10, 10), (10, PAGE_HEIGHT_PT - 10), (PAGE_WIDTH_PT - 10, PAGE_HEIGHT_PT - 10)):
        c.line(x - 3, y, x + 3, y)
        c.line(x, y - 3, x, y + 3)

    c.showPage()
    c.save()
    return destination


if __name__ == "__main__":
    print(generate())
